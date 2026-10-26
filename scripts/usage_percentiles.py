"""P3-3：依 call_type 彙總 `llm_usage_events` 的 token 與延遲分位數。

設計給「在客戶環境內執行、只帶出彙總數字」：

- 以 SQLite 唯讀 URI（``mode=ro``）開啟資料庫，不寫入任何東西。
- 只讀 ``call_type``／``provider``／``model``／``status``／數值欄位；
  不讀 ``metadata_json``，也不碰 messages／chunks 等含內容的表，
  所以輸出不可能含題目、答案或來源文字。
- 依 ``model`` 分組：本部署兩台 chat 主機各跑不同模型（Gemma 4 31B／
  gpt-oss-120b），``llm_usage_events`` 沒有記錄 base URL，模型名就是主機的
  識別（PERFORMANCE.md P3-2 provenance note）。若日後兩台跑同一模型，
  這個分組就分不出主機，需另加欄位。
- ``prompt_tokens``／``completion_tokens`` 分位數只取 ``is_estimated = 0``
  （provider 回報的真實 usage）；延遲取全部成功呼叫。
- 預設排除 Eval Workbench 流量（``eval_run_id IS NOT NULL``），因為那是批次
  重跑、不代表使用者提問；``--include-evals`` 可納入。

用法::

    .venv/bin/python -m scripts.usage_percentiles --db data/app.sqlite3 [--since 2026-10-01] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

# 一題問答依序經過的呼叫；用來算「各段 p50 相加」的估計值。
QUESTION_PIPELINE = ("query_rewrite", "embedding_query", "rerank", "answer_stream")


def percentile(values: list[float], pct: float) -> float | None:
    """線性內插分位數（與 numpy 預設相同），不需第三方套件。"""
    if not values:
        return None
    data = sorted(values)
    if len(data) == 1:
        return float(data[0])
    rank = (len(data) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(data) - 1)
    return float(data[low] + (data[high] - data[low]) * (rank - low))


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def collect(db_path: Path, since: str | None = None, include_evals: bool = False) -> dict[str, Any]:
    where = []
    params: list[Any] = []
    if since:
        where.append("created_at >= ?")
        params.append(since)
    if not include_evals:
        where.append("eval_run_id IS NULL")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _open_readonly(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT call_type, provider, model, status, latency_ms,
                   prompt_tokens, completion_tokens, is_estimated, created_at
            FROM llm_usage_events {clause}
            """,
            params,
        ).fetchall()

    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["call_type"], row["provider"] or "", row["model"] or "")
        g = groups.setdefault(key, {
            "calls": 0, "failed": 0, "measured": 0,
            "latency": [], "prompt": [], "completion": [],
            "first": row["created_at"], "last": row["created_at"],
        })
        g["calls"] += 1
        g["first"] = min(g["first"], row["created_at"])
        g["last"] = max(g["last"], row["created_at"])
        if row["status"] != "succeeded":
            g["failed"] += 1
            continue
        g["latency"].append(float(row["latency_ms"] or 0))
        if row["is_estimated"] == 0 and row["prompt_tokens"] is not None:
            g["measured"] += 1
            g["prompt"].append(row["prompt_tokens"])
            if row["completion_tokens"] is not None:
                g["completion"].append(row["completion_tokens"])

    stats = []
    for (call_type, provider, model), g in sorted(groups.items()):
        stats.append({
            "call_type": call_type,
            "provider": provider,
            "model": model,
            "calls": g["calls"],
            "failed": g["failed"],
            "measured_usage": g["measured"],
            "prompt_tokens_p50": percentile(g["prompt"], 0.50),
            "prompt_tokens_p95": percentile(g["prompt"], 0.95),
            "completion_tokens_p50": percentile(g["completion"], 0.50),
            "completion_tokens_p95": percentile(g["completion"], 0.95),
            "latency_ms_p50": percentile(g["latency"], 0.50),
            "latency_ms_p95": percentile(g["latency"], 0.95),
            "first_at": g["first"],
            "last_at": g["last"],
        })

    return {
        "db_rows": len(rows),
        "filters": {"since": since, "include_evals": include_evals},
        "stats": stats,
        "per_model": _per_model_summary(stats),
    }


def _per_model_summary(stats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每台 chat 主機（model）一列：P3-5 門檻比值與一題的估計延遲。

    embedding 走另一條連線、另一個 model，所以 ``embedding_query`` 的 p50
    取全部 embedding model 中呼叫數最多的那一組，附在每台 chat 主機上。
    """
    by_type: dict[str, list[dict[str, Any]]] = {}
    for item in stats:
        by_type.setdefault(item["call_type"], []).append(item)
    embed_rows = by_type.get("embedding_query", [])
    embed = max(embed_rows, key=lambda r: r["calls"]) if embed_rows else None

    chat_models = sorted({
        (item["provider"], item["model"]) for item in stats
        if item["call_type"] in ("query_rewrite", "rerank", "answer_stream")
    })
    out = []
    for provider, model in chat_models:
        def pick(call_type: str) -> dict[str, Any] | None:
            for item in by_type.get(call_type, []):
                if item["provider"] == provider and item["model"] == model:
                    return item
            return None

        rerank, answer = pick("rerank"), pick("answer_stream")
        ratio = None
        if rerank and answer and rerank["prompt_tokens_p50"] and answer["prompt_tokens_p50"]:
            ratio = rerank["prompt_tokens_p50"] / answer["prompt_tokens_p50"]
        parts = {
            "query_rewrite": pick("query_rewrite"),
            "embedding_query": embed,
            "rerank": rerank,
            "answer_stream": answer,
        }
        missing = [name for name in QUESTION_PIPELINE if not parts[name] or parts[name]["latency_ms_p50"] is None]
        est = None if missing else sum(parts[name]["latency_ms_p50"] for name in QUESTION_PIPELINE)
        out.append({
            "provider": provider,
            "model": model,
            "rerank_to_answer_prompt_ratio_p50": ratio,
            "sum_of_stage_p50_latency_ms": est,
            "missing_stages": missing,
        })
    return out


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}" if value < 10 else f"{value:.0f}"
    return str(value)


def render_text(report: dict[str, Any]) -> str:
    cols = ("call_type", "model", "calls", "failed", "measured_usage",
            "prompt_tokens_p50", "prompt_tokens_p95", "completion_tokens_p50",
            "completion_tokens_p95", "latency_ms_p50", "latency_ms_p95")
    lines = [
        f"# llm_usage_events 彙總（rows={report['db_rows']}, since={report['filters']['since'] or '-'}, "
        f"include_evals={report['filters']['include_evals']}）",
        "# tokens 只取 is_estimated=0；latency 只取 status=succeeded；model = chat 主機識別",
        "\t".join(cols),
    ]
    for item in report["stats"]:
        lines.append("\t".join(_fmt(item[c]) for c in cols))
    lines.append("")
    lines.append("# 每台 chat 主機：rerank/answer prompt_tokens p50 比值（P3-5 門檻）與各段 p50 相加（估計，非端到端實測）")
    lines.append("model\trerank_to_answer_ratio\tsum_stage_p50_ms\tmissing_stages")
    for item in report["per_model"]:
        lines.append("\t".join([
            item["model"] or "-",
            _fmt(item["rerank_to_answer_prompt_ratio_p50"]),
            _fmt(item["sum_of_stage_p50_latency_ms"]),
            ",".join(item["missing_stages"]) or "-",
        ]))
    return "\n".join(lines)


def _default_db() -> Path:
    data_dir = os.environ.get("NOTEBOOKLM_DATA_DIR") or "data"
    return Path(data_dir) / "app.sqlite3"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=None, help="SQLite path (default: $NOTEBOOKLM_DATA_DIR/app.sqlite3)")
    parser.add_argument("--since", default=None, help="only events with created_at >= this (UTC, e.g. 2026-10-01)")
    parser.add_argument("--include-evals", action="store_true", help="include Eval Workbench runs")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a TSV table")
    args = parser.parse_args(argv)
    report = collect(args.db or _default_db(), since=args.since, include_evals=args.include_evals)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
