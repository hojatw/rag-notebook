"""P3-3 量測工具：usage 分位數腳本與 retrieve 的分段計時 log。"""

import asyncio
import hashlib
import importlib
import time

from tests.test_governance import _fresh_governance_stack


def _seed(governance, call_type, model, prompt, *, latency=100.0, provider_usage=True,
          status="succeeded", context=None, metadata=None):
    # 走真正的 writer（normalize_usage → record_llm_usage_event），讓腳本讀到的
    # is_estimated／欄位名稱與 app 實際寫入的一致（AGENTS.md：producer→reader）。
    usage = governance.normalize_usage(
        {"prompt_tokens": prompt, "completion_tokens": 10, "total_tokens": prompt + 10} if provider_usage else None,
        input_chars=prompt * 2,
        output_chars=20,
    )
    governance.record_llm_usage_event(
        call_type=call_type, provider="openai_compatible", model=model, status=status,
        latency_ms=latency, usage=usage, context=context, metadata=metadata,
    )


def test_usage_percentiles_groups_by_host_model_and_uses_only_provider_counts(monkeypatch, tmp_path):
    db, governance, _llm = _fresh_governance_stack(monkeypatch, tmp_path)
    from scripts import usage_percentiles as up

    for prompt in (4000, 5000, 6000):
        _seed(governance, "rerank", "gemma-4-31b", prompt, latency=900)
    for prompt in (2000, 2500, 3000):
        _seed(governance, "answer_stream", "gemma-4-31b", prompt, latency=3000)
    _seed(governance, "query_rewrite", "gemma-4-31b", 600, latency=500)
    _seed(governance, "embedding_query", "e5-large", 30, latency=50)
    # 估算值不得混進 token 分位數；失敗與 eval 流量不得進延遲。
    _seed(governance, "rerank", "gemma-4-31b", 99999, provider_usage=False, latency=900)
    _seed(governance, "rerank", "gemma-4-31b", 1, status="failed", latency=99999)
    _seed(governance, "rerank", "gpt-oss-120b", 7000, latency=400)

    report = up.collect(db.DB_PATH)
    rows = {(r["call_type"], r["model"]): r for r in report["stats"]}
    gemma_rerank = rows[("rerank", "gemma-4-31b")]
    assert gemma_rerank["calls"] == 5
    assert gemma_rerank["failed"] == 1
    assert gemma_rerank["measured_usage"] == 3
    assert gemma_rerank["prompt_tokens_p50"] == 5000  # 估算值 99999 與失敗呼叫都不算
    assert rows[("rerank", "gpt-oss-120b")]["prompt_tokens_p50"] == 7000

    per_model = {r["model"]: r for r in report["per_model"]}
    assert per_model["gemma-4-31b"]["rerank_to_answer_prompt_ratio_p50"] == 5000 / 2500
    assert per_model["gemma-4-31b"]["sum_of_stage_p50_latency_ms"] == 500 + 50 + 900 + 3000
    assert per_model["gpt-oss-120b"]["missing_stages"] == ["query_rewrite", "answer_stream"]


def test_usage_percentiles_excludes_eval_runs_by_default(monkeypatch, tmp_path):
    db, governance, _llm = _fresh_governance_stack(monkeypatch, tmp_path)
    from scripts import usage_percentiles as up

    _seed(governance, "rerank", "m", 100)
    _seed(governance, "rerank", "m", 900)
    with db.connect() as conn:
        # 只需要 eval_run_id 非空；不建整套 eval 資料，關掉 FK 直接標記。
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("UPDATE llm_usage_events SET eval_run_id = 1 WHERE prompt_tokens = 900")

    assert up.collect(db.DB_PATH)["stats"][0]["calls"] == 1
    assert up.collect(db.DB_PATH, include_evals=True)["stats"][0]["calls"] == 2


def test_usage_percentiles_is_read_only_and_emits_no_content(monkeypatch, tmp_path, capsys):
    db, governance, _llm = _fresh_governance_stack(monkeypatch, tmp_path)
    from scripts import usage_percentiles as up

    marker = "機密題目內容XYZ"
    _seed(governance, "answer_stream", "m", 100, metadata={"note": marker})
    with db.connect() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = hashlib.sha256(db.DB_PATH.read_bytes()).hexdigest()

    assert up.main(["--db", str(db.DB_PATH)]) == 0
    assert up.main(["--db", str(db.DB_PATH), "--json"]) == 0
    out = capsys.readouterr().out
    assert marker not in out
    assert "answer_stream" in out
    assert hashlib.sha256(db.DB_PATH.read_bytes()).hexdigest() == before

    conn = up._open_readonly(db.DB_PATH)
    try:
        import sqlite3
        import pytest
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM llm_usage_events")
    finally:
        conn.close()


def test_percentile_interpolates():
    from scripts.usage_percentiles import percentile

    assert percentile([], 0.5) is None
    assert percentile([10, 20, 30, 40], 0.95) == 38.5


def test_retrieve_logs_per_stage_timings_without_content(local_embed, monkeypatch, caplog):
    import app.retrieval as retrieval

    async def fake_rewrite(question, history, settings, **kwargs):
        return [question]

    async def fake_rerank(question, candidates, settings, limit=6, **kwargs):
        return candidates[:limit]

    def slow_vectors(*args, **kwargs):
        time.sleep(0.05)
        return []

    monkeypatch.setattr(retrieval, "rewrite_search_queries", fake_rewrite)
    monkeypatch.setattr(retrieval, "rerank_chunks", fake_rerank)
    monkeypatch.setattr(retrieval, "query_vectors", slow_vectors)
    monkeypatch.setattr(retrieval, "keyword_candidates_from_sqlite", lambda *a, **k: [])
    retrieval.reset_vector_health()

    question = "機密問題ABC"
    with caplog.at_level("INFO", logger="app.retrieval"):
        asyncio.run(retrieval.retrieve(question, [], {}, user_id=1, source_ids=[1]))

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("retrieve_stage_timings")]
    assert len(lines) == 1
    line = lines[0]
    assert "mode=chroma" in line
    fields = dict(part.split("=", 1) for part in line.split()[1:])
    assert set(fields) == {"mode", *(f"{name}_ms" for name in retrieval.STAGE_TIMING_FIELDS)}
    assert float(fields["vector_ms"]) >= 40
    assert float(fields["search_ms"]) >= float(fields["vector_ms"])
    assert question not in line
