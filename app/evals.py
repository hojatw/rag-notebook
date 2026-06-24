"""Admin Eval Workbench (E1): retrieval-profile management and eval set / item /
run / compare endpoints, plus their supporting helpers.

Extracted from ``app/main.py`` to keep the HTTP layer thin. These routes live on
an APIRouter that ``app/main.py`` mounts; route paths and behaviour are
unchanged. Read ``docs/RETRIEVAL.md`` and ``docs/ROADMAP.md`` (Eval Workbench)
before changing eval scope or scoring.
"""

import logging
import time
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import i18n
from .db import connect, dumps, load_llm_settings, loads
from .governance import record_ai_safety_events
from .llm import generate_eval_candidates
from .main import _json_download, record_audit_event, render, require_admin
from .retrieval import (
    ACTIVE_RETRIEVAL_PARAMS,
    FINAL_CHUNK_COUNT,
    PROFILE_PARAM_LABELS,
    active_low_confidence_threshold,
    active_retrieval_params,
    coerce_profile_params,
    current_retrieval_profile_params,
    profile_param_rows,
    profile_params_for_display,
    resolve_retrieval_params,
    retrieve,
    set_active_retrieval_params,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def ensure_default_retrieval_profile(conn, admin_user_id: int | None = None) -> dict:
    """Ensure there is a baseline profile reflecting current app config."""
    row = conn.execute(
        "SELECT * FROM retrieval_profiles WHERE is_active = 1 ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM retrieval_profiles ORDER BY id ASC LIMIT 1"
        ).fetchone()
    if row is None:
        cursor = conn.execute(
            """
            INSERT INTO retrieval_profiles (name, description, params_json, is_active, is_default, created_by)
            VALUES (?, ?, ?, 1, 1, ?)
            """,
            (
                "目前系統預設",
                "從 app/config.py 目前 retrieval 設定建立的 baseline profile。",
                dumps(current_retrieval_profile_params()),
                admin_user_id,
            ),
        )
        row = conn.execute("SELECT * FROM retrieval_profiles WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def load_eval_set(conn, eval_set_id: int) -> dict:
    row = conn.execute(
        """
        SELECT es.*, n.title AS notebook_title, n.emoji AS notebook_emoji, u.username AS target_username
        FROM eval_sets es
        JOIN notebooks n ON n.id = es.notebook_id
        JOIN users u ON u.id = es.target_user_id
        WHERE es.id = ?
        """,
        (eval_set_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="找不到 eval set")
    return dict(row)


def split_expected_substrings(value: str) -> list[str]:
    """Normalize newline-separated expected evidence snippets from the admin form."""
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


def generated_eval_snippet(text: str, limit: int = 80) -> str:
    snippet = " ".join((text or "").split())
    if len(snippet) <= limit:
        return snippet
    return snippet[:limit].rsplit(" ", 1)[0] or snippet[:limit]


def generated_eval_question(filename: str, snippet: str) -> str:
    anchor = snippet[:72]
    suffix = "..." if len(snippet) > 72 else ""
    return f"「{anchor}{suffix}」這段內容的重點是什麼？來源：{filename}"


EVAL_ITEM_TYPE_LABELS = {
    "answerable": "可回答",
    "cross_lingual": "跨語言",
    "unanswerable": "不可回答",
}


def normalize_eval_item_type(value: str | None) -> str:
    item_type = (value or "answerable").strip().lower()
    return item_type if item_type in EVAL_ITEM_TYPE_LABELS else "answerable"


def eval_item_type_options() -> list[dict[str, str]]:
    return [
        {"value": "answerable", "label": "一般可回答"},
        {"value": "cross_lingual", "label": "跨語言"},
        {"value": "unanswerable", "label": "不可回答"},
    ]


def eval_authoring_chunks(
    conn,
    eval_set: dict,
    source_ids: list[int] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch bounded chunks for LLM eval authoring, spread across sources."""
    source_ids = [int(value) for value in (source_ids or []) if int(value) > 0]
    params: list[Any] = [eval_set["notebook_id"], eval_set["target_user_id"]]
    source_filter = ""
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        source_filter = f"AND s.id IN ({placeholders})"
        params.extend(source_ids)
    params.append(max(1, min(limit, 24)))
    rows = conn.execute(
        f"""
        WITH eligible AS (
            SELECT
                c.id AS chunk_id,
                c.text,
                c.location,
                c.chunk_index,
                s.id AS source_id,
                s.filename,
                ROW_NUMBER() OVER (
                    PARTITION BY s.id
                    ORDER BY c.chunk_index ASC, c.id ASC
                ) AS chunk_rank
            FROM chunks c
            JOIN sources s ON s.id = c.source_id
            WHERE s.notebook_id = ? AND s.user_id = ? AND s.status = 'indexed'
              AND TRIM(c.text) != ''
              {source_filter}
        )
        SELECT chunk_id, source_id, filename, location, text
        FROM eligible
        ORDER BY chunk_rank ASC, source_id DESC, chunk_id ASC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def eval_item_hit_rank(item: dict, retrieved: list[dict]) -> int | None:
    expected_source_id = item.get("expected_source_id")
    expected_chunk_id = item.get("expected_chunk_id")
    substrings = item.get("expected_substrings") or []
    has_criteria = bool(expected_source_id or expected_chunk_id or substrings)
    if not has_criteria:
        return None
    for rank, chunk in enumerate(retrieved, start=1):
        if expected_source_id and int(chunk.get("source_id") or 0) != int(expected_source_id):
            continue
        if expected_chunk_id and int(chunk.get("id") or 0) != int(expected_chunk_id):
            continue
        if substrings and not any(snippet in chunk.get("text", "") for snippet in substrings):
            continue
        return rank
    return 0


def eval_result_diagnosis(result: dict) -> str:
    if result.get("status") == "hit":
        rank = result.get("hit_rank")
        return f"命中預期依據，rank {rank}。" if rank else "命中預期依據。"
    if result.get("status") == "unscored":
        return "此題沒有設定 expected source/chunk/substrings，因此不納入 Recall/MRR。"
    if result.get("status") == "error":
        return result.get("error") or "執行此題時發生錯誤。"

    retrieved = result.get("retrieved") or []
    if not retrieved:
        return "沒有找回任何 chunk。"
    expected_source_id = result.get("expected_source_id")
    expected_chunk_id = result.get("expected_chunk_id")
    expected_substrings = result.get("expected_substrings") or []
    retrieved_source_ids = {int(chunk.get("source_id") or 0) for chunk in retrieved}
    retrieved_chunk_ids = {int(chunk.get("chunk_id") or 0) for chunk in retrieved}

    reasons = []
    if expected_source_id and int(expected_source_id) not in retrieved_source_ids:
        reasons.append("未找回預期來源。")
    elif expected_source_id:
        reasons.append("有找回同一來源，但不是預期 chunk/片段。")
    if expected_chunk_id and int(expected_chunk_id) not in retrieved_chunk_ids:
        reasons.append("預期 chunk 不在目前 top-k 結果中。")
    if expected_substrings:
        retrieved_text = " ".join(str(chunk.get("snippet") or "") for chunk in retrieved)
        missing = [snippet for snippet in expected_substrings if snippet not in retrieved_text]
        if missing:
            reasons.append("預期 substring 沒出現在 retrieved snippets。")
    return " ".join(reasons) or "找回結果未符合此題的 expected criteria。"


def compact_retrieved_chunks(retrieved: list[dict]) -> list[dict[str, Any]]:
    compact = []
    for rank, chunk in enumerate(retrieved, start=1):
        compact.append({
            "rank": rank,
            "chunk_id": chunk.get("id"),
            "source_id": chunk.get("source_id"),
            "filename": chunk.get("filename", ""),
            "location": chunk.get("location", ""),
            "score": round(float(chunk.get("score") or 0.0), 4),
            "vector_score": round(float(chunk.get("vector_score") or 0.0), 4),
            "keyword_score": round(float(chunk.get("keyword_score") or 0.0), 4),
            "snippet": " ".join((chunk.get("text") or "").split())[:220],
        })
    return compact


def run_metrics_from_results(
    results: list[dict],
    threshold: float | None = None,
    final_chunk_count: int | None = None,
) -> dict[str, Any]:
    if threshold is None:
        threshold = active_low_confidence_threshold()
    if final_chunk_count is None:
        final_chunk_count = int(ACTIVE_RETRIEVAL_PARAMS.get("final_chunk_count", FINAL_CHUNK_COUNT))
    scored = [r for r in results if r["status"] in {"hit", "miss"}]
    hits = [r for r in scored if r["status"] == "hit"]
    total = len(results)
    scored_count = len(scored)
    latencies = [float(r.get("latency_ms") or 0.0) for r in results]
    top_scores = [float(r.get("top_score") or 0.0) for r in results]
    mrr = sum(1 / int(r["hit_rank"]) for r in hits if r.get("hit_rank")) / scored_count if scored_count else 0.0
    recall = len(hits) / scored_count if scored_count else 0.0
    low_confidence = sum(1 for score in top_scores if score < threshold)
    return {
        "total": total,
        "scored": scored_count,
        "hits": len(hits),
        "misses": sum(1 for r in scored if r["status"] == "miss"),
        "unscored": sum(1 for r in results if r["status"] == "unscored"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "recall_at_k": round(recall, 4),
        "mrr": round(mrr, 4),
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        "avg_top_score": round(sum(top_scores) / len(top_scores), 4) if top_scores else 0.0,
        "low_confidence_rate": round(low_confidence / total, 4) if total else 0.0,
        "threshold": threshold,
        "final_chunk_count": final_chunk_count,
    }


async def run_eval_job(run_id: int) -> None:
    """Background E1b retrieval-only eval runner."""
    try:
        with connect() as conn:
            run = conn.execute("SELECT * FROM eval_runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                return
            # Isolated per-run override: the run uses its frozen profile snapshot,
            # not the live applied profile, so candidate vs baseline comparisons
            # are meaningful without mutating real chat retrieval.
            run_params = resolve_retrieval_params(loads(run["profile_snapshot_json"] or "{}"))
            eval_set = load_eval_set(conn, run["eval_set_id"])
            items = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM eval_items
                    WHERE eval_set_id = ? AND approved = 1
                    ORDER BY id ASC
                    """,
                    (eval_set["id"],),
                ).fetchall()
            ]
            settings = load_llm_settings(conn) or {}
            source_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM sources WHERE notebook_id = ? AND user_id = ? AND status = 'indexed'",
                    (eval_set["notebook_id"], eval_set["target_user_id"]),
                ).fetchall()
            ]
            conn.execute(
                """
                UPDATE eval_runs
                SET status = 'running', progress_total = ?, progress_current = 0,
                    current_step = ?, started_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (len(items), "準備 retrieval eval", run_id),
            )
        if not items:
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE eval_runs
                    SET status = 'failed', error = ?, current_step = '',
                        finished_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    ("沒有 approved eval 題目可執行。", run_id),
                )
            return
        if not source_ids:
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE eval_runs
                    SET status = 'failed', error = ?, current_step = '',
                        finished_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    ("這個 eval set 的 notebook 沒有已索引來源。", run_id),
                )
            return

        results: list[dict] = []
        for index, item in enumerate(items, start=1):
            question = item["question"]
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE eval_runs
                    SET progress_current = ?, current_step = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (index - 1, f"檢索第 {index} / {len(items)} 題", run_id),
                )
            started = time.perf_counter()
            try:
                item["expected_substrings"] = loads(item["expected_substrings_json"] or "[]")
                retrieved = await retrieve(
                    question,
                    None,
                    settings,
                    [],
                    eval_set["target_user_id"],
                    source_ids,
                    params=run_params,
                    usage_context={
                        "user_id": eval_set["target_user_id"],
                        "notebook_id": eval_set["notebook_id"],
                        "eval_run_id": run_id,
                        "eval_set_id": eval_set["id"],
                    },
                )
                latency_ms = round((time.perf_counter() - started) * 1000, 1)
                top_score = float(retrieved[0].get("score", 0.0)) if retrieved else 0.0
                hit_rank = eval_item_hit_rank(item, retrieved)
                if hit_rank is None:
                    status = "unscored"
                    stored_rank = None
                elif hit_rank > 0:
                    status = "hit"
                    stored_rank = hit_rank
                else:
                    status = "miss"
                    stored_rank = None
                result = {
                    "eval_item_id": item["id"],
                    "status": status,
                    "hit_rank": stored_rank,
                    "top_score": top_score,
                    "latency_ms": latency_ms,
                    "retrieved_json": dumps(compact_retrieved_chunks(retrieved)),
                    "error": "",
                }
            except Exception as exc:
                logger.exception("eval_item_failed run_id=%s item_id=%s", run_id, item["id"])
                result = {
                    "eval_item_id": item["id"],
                    "status": "error",
                    "hit_rank": None,
                    "top_score": 0.0,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "retrieved_json": "[]",
                    "error": str(exc)[:300],
                }
            results.append(result)
            with connect() as conn:
                conn.execute(
                    """
                    INSERT INTO eval_results
                    (run_id, eval_item_id, status, hit_rank, top_score, latency_ms, retrieved_json, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        result["eval_item_id"],
                        result["status"],
                        result["hit_rank"],
                        result["top_score"],
                        result["latency_ms"],
                        result["retrieved_json"],
                        result["error"],
                    ),
                )
                conn.execute(
                    """
                    UPDATE eval_runs
                    SET progress_current = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (index, run_id),
                )
        metrics = run_metrics_from_results(
            results,
            threshold=float(run_params["low_confidence_threshold"]),
            final_chunk_count=int(run_params["final_chunk_count"]),
        )
        with connect() as conn:
            conn.execute(
                """
                UPDATE eval_runs
                SET status = 'succeeded', progress_current = progress_total,
                    current_step = '', metrics_json = ?, finished_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (dumps(metrics), run_id),
            )
        logger.info("eval_run_completed run_id=%s total=%s hits=%s", run_id, metrics["total"], metrics["hits"])
    except Exception as exc:
        logger.exception("eval_run_failed run_id=%s", run_id)
        with connect() as conn:
            conn.execute(
                """
                UPDATE eval_runs
                SET status = 'failed', error = ?, current_step = '',
                    finished_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (str(exc)[:500], run_id),
            )


def eval_run_context(run_id: int) -> dict[str, Any]:
    with connect() as conn:
        run = conn.execute(
            """
            SELECT er.*, es.name AS eval_set_name, es.notebook_id, n.title AS notebook_title
            FROM eval_runs er
            JOIN eval_sets es ON es.id = er.eval_set_id
            JOIN notebooks n ON n.id = es.notebook_id
            WHERE er.id = ?
            """,
            (run_id,),
        ).fetchone()
        if run is None:
            raise HTTPException(status_code=404, detail="找不到 eval run")
        results = [
            dict(row)
            for row in conn.execute(
                """
                SELECT r.*, i.question, i.expected_source_id, i.expected_chunk_id,
                       i.expected_substrings_json, s.filename AS expected_filename,
                       c.location AS expected_chunk_location,
                       c.text AS expected_chunk_text
                FROM eval_results r
                JOIN eval_items i ON i.id = r.eval_item_id
                LEFT JOIN sources s ON s.id = i.expected_source_id
                LEFT JOIN chunks c ON c.id = i.expected_chunk_id
                WHERE r.run_id = ?
                ORDER BY r.id ASC
                """,
                (run_id,),
            ).fetchall()
        ]
    run_dict = dict(run)
    run_dict["metrics"] = loads(run_dict.get("metrics_json") or "{}")
    run_dict["profile_snapshot"] = loads(run_dict.get("profile_snapshot_json") or "{}")
    run_dict["profile_params"] = profile_param_rows(run_dict["profile_snapshot"])
    for result in results:
        result["retrieved"] = loads(result.get("retrieved_json") or "[]")
        result["expected_substrings"] = loads(result.get("expected_substrings_json") or "[]")
        result["expected_chunk_snippet"] = " ".join((result.get("expected_chunk_text") or "").split())[:220]
        result["diagnosis"] = eval_result_diagnosis(result)
    return {"run": run_dict, "results": results}


def profile_export_payload(profile: dict) -> dict[str, Any]:
    params = loads(profile.get("params_json") or "{}")
    return {
        "export_schema": "retrieval_profile.v1",
        "export_type": "sanitized_profile",
        "profile": {
            "id": profile["id"],
            "name": profile["name"],
            "description": profile["description"],
            "params": params,
            "requires_reindex": bool(profile["requires_reindex"]),
            "is_active": bool(profile["is_active"]),
            "is_default": bool(profile["is_default"]),
            "source_run_id": profile["source_run_id"],
            "created_at": profile["created_at"],
            "updated_at": profile["updated_at"],
        },
    }


def sanitized_run_export_payload(context: dict[str, Any]) -> dict[str, Any]:
    run = context["run"]
    results = context["results"]
    return {
        "export_schema": "eval_run.v1",
        "export_type": "sanitized_run_report",
        "run": {
            "id": run["id"],
            "eval_set_id": run["eval_set_id"],
            "eval_set_name": run["eval_set_name"],
            "status": run["status"],
            "profile_id": run["profile_id"],
            "profile_snapshot": run["profile_snapshot"],
            "metrics": run["metrics"],
            "progress_current": run["progress_current"],
            "progress_total": run["progress_total"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "created_at": run["created_at"],
        },
        "result_summary": {
            "total_results": len(results),
            "hits": sum(1 for item in results if item["status"] == "hit"),
            "misses": sum(1 for item in results if item["status"] == "miss"),
            "unscored": sum(1 for item in results if item["status"] == "unscored"),
            "errors": sum(1 for item in results if item["status"] == "error"),
        },
        "results": [
            {
                "eval_item_id": item["eval_item_id"],
                "status": item["status"],
                "hit_rank": item["hit_rank"],
                "top_score": item["top_score"],
                "latency_ms": item["latency_ms"],
                "retrieved_count": len(item.get("retrieved") or []),
                "has_error": bool(item.get("error")),
            }
            for item in results
        ],
    }


def full_run_export_payload(context: dict[str, Any]) -> dict[str, Any]:
    payload = sanitized_run_export_payload(context)
    payload["export_type"] = "full_internal_run_report"
    payload["warning"] = "Contains eval questions, expected evidence, and retrieved snippets. Keep inside the deployment unless explicitly approved."
    payload["results"] = [
        {
            "eval_item_id": item["eval_item_id"],
            "question": item["question"],
            "status": item["status"],
            "hit_rank": item["hit_rank"],
            "top_score": item["top_score"],
            "latency_ms": item["latency_ms"],
            "diagnosis": item["diagnosis"],
            "error": item.get("error") or "",
            "expected": {
                "source_id": item["expected_source_id"],
                "filename": item["expected_filename"],
                "chunk_id": item["expected_chunk_id"],
                "chunk_location": item["expected_chunk_location"],
                "substrings": item["expected_substrings"],
                "snippet": item["expected_chunk_snippet"],
            },
            "retrieved": item.get("retrieved") or [],
        }
        for item in context["results"]
    ]
    return payload


def eval_set_items_redirect(eval_set_id: int) -> RedirectResponse:
    return RedirectResponse(f"/admin/evals/sets/{eval_set_id}#eval-items", status_code=303)


def eval_set_detail_context(eval_set_id: int) -> dict[str, Any]:
    with connect() as conn:
        eval_set = load_eval_set(conn, eval_set_id)
        sources = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, filename, status
                FROM sources
                WHERE notebook_id = ? AND user_id = ?
                ORDER BY filename
                """,
                (eval_set["notebook_id"], eval_set["target_user_id"]),
            ).fetchall()
        ]
        items = [
            dict(row)
            for row in conn.execute(
                """
                SELECT i.*, s.filename AS expected_filename
                FROM eval_items i LEFT JOIN sources s ON s.id = i.expected_source_id
                WHERE i.eval_set_id = ?
                ORDER BY i.id ASC
                """,
                (eval_set_id,),
            ).fetchall()
        ]
        runs = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM eval_runs WHERE eval_set_id = ? ORDER BY created_at DESC, id DESC LIMIT 21",
                (eval_set_id,),
            ).fetchall()
        ]
        profiles = [
            dict(row)
            for row in conn.execute(
                "SELECT id, name, is_active FROM retrieval_profiles ORDER BY is_active DESC, id DESC LIMIT 50"
            ).fetchall()
        ]
    for item in items:
        item["expected_substrings"] = loads(item.get("expected_substrings_json") or "[]")
        item["metadata"] = loads(item.get("metadata_json") or "{}")
        item["item_type"] = normalize_eval_item_type(item.get("item_type"))
        item["item_type_label"] = EVAL_ITEM_TYPE_LABELS[item["item_type"]]
        origin = item["metadata"].get("origin") or ("manual" if item["approved"] else "draft")
        item["origin_label"] = {
            "llm_generated": "LLM",
            "deterministic": "自動",
            "manual": "手動",
        }.get(origin, origin)
    runs_truncated = len(runs) > 20  # LIMIT+1 → surface the cap (UX review M4)
    runs = runs[:20]
    for run in runs:
        run["metrics"] = loads(run.get("metrics_json") or "{}")
    return {
        "eval_set": eval_set,
        "sources": sources,
        "items": items,
        "approved_count": sum(1 for item in items if item["approved"]),
        "runs": runs,
        "runs_truncated": runs_truncated,
        "profiles": profiles,
        "item_type_options": eval_item_type_options(),
    }


def eval_set_items_response(
    request: Request,
    user: dict,
    eval_set_id: int,
    notice: str = "",
    error: str = "",
):
    context = eval_set_detail_context(eval_set_id)
    context["eval_notice"] = notice
    context["eval_error"] = error
    if request.headers.get("HX-Request") == "true":
        return render(request, "_eval_items_section.html", {"user": user, **context})
    return eval_set_items_redirect(eval_set_id)


@router.get("/admin/evals", response_class=HTMLResponse)
def admin_evals(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    notebook_q: str = "",
):
    """Admin E1 landing page: eval sets, run history, active profile."""
    notebook_q = notebook_q.strip()[:80]
    notebook_like = f"%{notebook_q}%"
    with connect() as conn:
        profile = ensure_default_retrieval_profile(conn, user["id"])
        notebooks = [
            dict(row)
            for row in conn.execute(
                """
                SELECT n.id, n.title, n.emoji, u.username,
                       COUNT(s.id) AS indexed_count
                FROM notebooks n
                JOIN users u ON u.id = n.user_id
                JOIN sources s ON s.notebook_id = n.id AND s.user_id = n.user_id AND s.status = 'indexed'
                WHERE (? = '' OR n.title LIKE ? OR u.username LIKE ?)
                GROUP BY n.id
                ORDER BY n.updated_at DESC, n.id DESC
                LIMIT 100
                """,
                (notebook_q, notebook_like, notebook_like),
            ).fetchall()
        ]
        eval_sets = [
            dict(row)
            for row in conn.execute(
                """
                SELECT es.*, n.title AS notebook_title, n.emoji AS notebook_emoji,
                       u.username AS target_username,
                       (SELECT COUNT(*) FROM eval_items WHERE eval_set_id = es.id) AS item_count,
                       (SELECT COUNT(*) FROM eval_items WHERE eval_set_id = es.id AND approved = 1) AS approved_count
                FROM eval_sets es
                JOIN notebooks n ON n.id = es.notebook_id
                JOIN users u ON u.id = es.target_user_id
                ORDER BY es.updated_at DESC, es.id DESC
                LIMIT 51
                """
            ).fetchall()
        ]
        runs = [
            dict(row)
            for row in conn.execute(
                """
                SELECT er.*, es.name AS eval_set_name, n.title AS notebook_title
                FROM eval_runs er
                JOIN eval_sets es ON es.id = er.eval_set_id
                JOIN notebooks n ON n.id = es.notebook_id
                ORDER BY er.created_at DESC, er.id DESC
                LIMIT 21
                """
            ).fetchall()
        ]
    # LIMIT+1 → tell the user the list was capped (no pagination; UX review M4).
    eval_sets_truncated = len(eval_sets) > 50
    eval_sets = eval_sets[:50]
    runs_truncated = len(runs) > 20
    runs = runs[:20]
    for run in runs:
        run["metrics"] = loads(run.get("metrics_json") or "{}")
    return render(
        request,
        "admin_evals.html",
        {
            "user": user,
            "active_tab": "sets",
            "profile": profile,
            "notebooks": notebooks,
            "notebook_q": notebook_q,
            "eval_sets": eval_sets,
            "eval_sets_truncated": eval_sets_truncated,
            "runs": runs,
            "runs_truncated": runs_truncated,
        },
    )


@router.get("/admin/evals/profiles", response_class=HTMLResponse)
def admin_profiles_page(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
):
    """E1c: dedicated Retrieval Profiles management page (separate from eval sets)."""
    with connect() as conn:
        ensure_default_retrieval_profile(conn, user["id"])
        profiles = [
            dict(row)
            for row in conn.execute(
                """
                SELECT p.*, u.username AS created_by_username
                FROM retrieval_profiles p
                LEFT JOIN users u ON u.id = p.created_by
                ORDER BY p.is_active DESC, p.id DESC
                LIMIT 50
                """
            ).fetchall()
        ]
        for prof in profiles:
            prof["params"] = profile_params_for_display(prof)
    return render(
        request,
        "admin_profiles.html",
        {
            "user": user,
            "active_tab": "profiles",
            "profiles": profiles,
            "new_profile_fields": profile_param_rows(active_retrieval_params()),
        },
    )


@router.get("/admin/evals/help", response_class=HTMLResponse)
def admin_eval_help(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
):
    """E1f: in-product tuning guide for retrieval profiles."""
    return render(
        request,
        "admin_eval_help.html",
        {
            "user": user,
            "active_tab": "help",
            "current_profile_fields": profile_param_rows(active_retrieval_params()),
        },
    )


@router.post("/admin/evals/sets")
def admin_create_eval_set(
    user: Annotated[dict, Depends(require_admin)],
    notebook_id: int = Form(...),
    name: str = Form(...),
    description: str = Form(""),
):
    clean_name = " ".join(name.split())[:120] or "未命名 Eval Set"
    description = description.strip()[:500]
    with connect() as conn:
        notebook = conn.execute("SELECT id, user_id FROM notebooks WHERE id = ?", (notebook_id,)).fetchone()
        if notebook is None:
            raise HTTPException(status_code=404, detail="找不到筆記本")
        cursor = conn.execute(
            """
            INSERT INTO eval_sets (name, description, target_user_id, notebook_id, created_by)
            VALUES (?, ?, ?, ?, ?)
            """,
            (clean_name, description, notebook["user_id"], notebook_id, user["id"]),
        )
    logger.info("eval_set_created admin_user_id=%s eval_set_id=%s", user["id"], cursor.lastrowid)
    return RedirectResponse(f"/admin/evals/sets/{cursor.lastrowid}", status_code=303)


@router.post("/admin/evals/sets/{eval_set_id}/delete")
def admin_delete_eval_set(
    eval_set_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    with connect() as conn:
        load_eval_set(conn, eval_set_id)
        active_run = conn.execute(
            """
            SELECT id FROM eval_runs
            WHERE eval_set_id = ? AND status IN ('queued', 'running')
            LIMIT 1
            """,
            (eval_set_id,),
        ).fetchone()
        if active_run is not None:
            raise HTTPException(status_code=400, detail="Eval run 仍在執行，不能刪除此 eval set。")
        conn.execute("DELETE FROM eval_sets WHERE id = ?", (eval_set_id,))
    logger.info("eval_set_deleted admin_user_id=%s eval_set_id=%s", user["id"], eval_set_id)
    return RedirectResponse("/admin/evals", status_code=303)


@router.get("/admin/evals/sets/{eval_set_id}", response_class=HTMLResponse)
def admin_eval_set_detail(
    request: Request,
    eval_set_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    context = eval_set_detail_context(eval_set_id)
    eval_set = context["eval_set"]
    return render(
        request,
        "admin_eval_set.html",
        {
            "user": user,
            "breadcrumb_items": [
                {"label": i18n.t("eval.title"), "href": "/admin/evals"},
                {"label": eval_set["name"], "href": None},
            ],
            **context,
        },
    )


@router.post("/admin/evals/sets/{eval_set_id}/generate")
def admin_generate_eval_items(
    request: Request,
    eval_set_id: int,
    user: Annotated[dict, Depends(require_admin)],
    count: int = Form(5),
):
    count = max(1, min(int(count), 20))
    created = 0
    with connect() as conn:
        eval_set = load_eval_set(conn, eval_set_id)
        rows = conn.execute(
            """
            WITH eligible AS (
                SELECT
                    c.id AS chunk_id,
                    c.text,
                    s.id AS source_id,
                    s.filename,
                    s.updated_at AS source_updated_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY s.id
                        ORDER BY c.chunk_index ASC, c.id ASC
                    ) AS chunk_rank
                FROM chunks c
                JOIN sources s ON s.id = c.source_id
                WHERE s.notebook_id = ? AND s.user_id = ? AND s.status = 'indexed'
                  AND TRIM(c.text) != ''
                  AND NOT EXISTS (
                      SELECT 1 FROM eval_items i
                      WHERE i.eval_set_id = ? AND i.expected_chunk_id = c.id
                  )
            )
            SELECT chunk_id, text, source_id, filename
            FROM eligible
            ORDER BY chunk_rank ASC, source_updated_at DESC, source_id DESC, chunk_id ASC
            LIMIT ?
            """,
            (eval_set["notebook_id"], eval_set["target_user_id"], eval_set_id, count),
        ).fetchall()
        for row in rows:
            snippet = generated_eval_snippet(row["text"])
            if not snippet:
                continue
            conn.execute(
                """
                INSERT INTO eval_items
                (eval_set_id, question, expected_source_id, expected_chunk_id,
                 expected_substrings_json, item_type, expected_answer, metadata_json, notes, approved)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    eval_set_id,
                    generated_eval_question(row["filename"], snippet),
                    row["source_id"],
                    row["chunk_id"],
                    dumps([snippet]),
                    "answerable",
                    "",
                    dumps({
                        "origin": "deterministic",
                        "prompt_version": "eval_authoring.deterministic.v1",
                        "source_id": row["source_id"],
                        "chunk_id": row["chunk_id"],
                    }),
                    "自動生成候選題；請人工確認後 approve。",
                ),
            )
            created += 1
        if created:
            conn.execute("UPDATE eval_sets SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (eval_set_id,))
    logger.info(
        "eval_items_generated admin_user_id=%s eval_set_id=%s created=%s",
        user["id"],
        eval_set_id,
        created,
    )
    return eval_set_items_response(request, user, eval_set_id)


@router.post("/admin/evals/sets/{eval_set_id}/generate/llm")
async def admin_generate_eval_items_llm(
    request: Request,
    eval_set_id: int,
    user: Annotated[dict, Depends(require_admin)],
    count: int = Form(5),
    item_types: list[str] | None = Form(None),
    source_ids: list[int] | None = Form(None),
    target_language: str = Form("Traditional Chinese"),
):
    count = max(1, min(int(count), 20))
    requested_types = [normalize_eval_item_type(value) for value in (item_types or [])]
    requested_types = list(dict.fromkeys(requested_types)) or ["answerable"]
    target_language = target_language.strip()[:60] or "Traditional Chinese"
    selected_source_ids = [int(value) for value in (source_ids or []) if int(value) > 0]
    with connect() as conn:
        eval_set = load_eval_set(conn, eval_set_id)
        settings = load_llm_settings(conn) or {}
        chunks = eval_authoring_chunks(conn, eval_set, selected_source_ids, limit=max(count * 3, 8))
    record_ai_safety_events(
        text=target_language,
        event_type="input_scan",
        surface="eval_authoring.target_language",
        context={"user_id": user["id"], "notebook_id": eval_set["notebook_id"], "eval_set_id": eval_set_id},
        metadata={"requested_type_count": len(requested_types), "selected_source_count": len(selected_source_ids)},
    )
    if not settings.get("chat_model"):
        return eval_set_items_response(
            request,
            user,
            eval_set_id,
            error="尚未完成 LLM 設定，無法使用 LLM 生成候選題；可先使用 deterministic 自動生成或手動新增。",
        )
    if not chunks:
        return eval_set_items_response(
            request,
            user,
            eval_set_id,
            error="找不到可用的 indexed chunks；請確認來源已完成索引，或調整來源選擇。",
        )
    candidates = await generate_eval_candidates(
        chunks,
        settings,
        count=count,
        item_types=requested_types,
        target_language=target_language,
        usage_context={
            "user_id": user["id"],
            "notebook_id": eval_set["notebook_id"],
            "eval_set_id": eval_set_id,
        },
    )
    if not candidates:
        return eval_set_items_response(
            request,
            user,
            eval_set_id,
            error="LLM 沒有產生可用候選題；請減少來源範圍、調整題型，或稍後再試。",
        )

    chunk_by_id = {int(chunk["chunk_id"]): chunk for chunk in chunks}
    source_ids_available = {int(chunk["source_id"]) for chunk in chunks}
    created = 0
    skipped = 0
    with connect() as conn:
        load_eval_set(conn, eval_set_id)
        for candidate in candidates:
            item_type = normalize_eval_item_type(candidate.get("item_type"))
            question = " ".join(str(candidate.get("question") or "").split())[:500]
            if not question:
                skipped += 1
                continue

            chunk = chunk_by_id.get(int(candidate.get("chunk_id") or 0))
            expected_source_id = candidate.get("source_id")
            expected_chunk_id = None
            expected_substrings: list[str] = []
            if item_type != "unanswerable":
                if chunk is None and expected_source_id not in source_ids_available:
                    skipped += 1
                    continue
                if chunk is not None:
                    expected_chunk_id = chunk["chunk_id"]
                    expected_source_id = chunk["source_id"]
                    chunk_text = chunk.get("text") or ""
                    expected_substrings = [
                        value
                        for value in (candidate.get("expected_substrings") or [])
                        if value and value in chunk_text
                    ][:3]
                    if not expected_substrings:
                        snippet = generated_eval_snippet(chunk_text)
                        expected_substrings = [snippet] if snippet else []
                elif expected_source_id not in source_ids_available:
                    skipped += 1
                    continue
            else:
                expected_source_id = None
                expected_chunk_id = None

            duplicate = conn.execute(
                """
                SELECT id FROM eval_items
                WHERE eval_set_id = ? AND question = ?
                LIMIT 1
                """,
                (eval_set_id, question),
            ).fetchone()
            if duplicate is not None:
                skipped += 1
                continue

            metadata = {
                "origin": "llm_generated",
                "prompt_version": "eval_authoring.llm.v1",
                "model": settings.get("chat_model") or "",
                "target_language": target_language,
                "requested_item_types": requested_types,
                "selected_source_ids": selected_source_ids,
                "generated_source_id": expected_source_id,
                "generated_chunk_id": expected_chunk_id,
            }
            conn.execute(
                """
                INSERT INTO eval_items
                (eval_set_id, question, expected_source_id, expected_chunk_id,
                 expected_substrings_json, item_type, expected_answer, metadata_json, notes, approved)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    eval_set_id,
                    question,
                    expected_source_id,
                    expected_chunk_id,
                    dumps(expected_substrings),
                    item_type,
                    str(candidate.get("expected_answer") or "")[:800],
                    dumps(metadata),
                    (str(candidate.get("rationale") or "LLM 生成候選題；請人工確認後 approve。"))[:500],
                ),
            )
            created += 1
        if created:
            conn.execute("UPDATE eval_sets SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (eval_set_id,))
    logger.info(
        "eval_items_generated_llm admin_user_id=%s eval_set_id=%s created=%s skipped=%s",
        user["id"],
        eval_set_id,
        created,
        skipped,
    )
    return eval_set_items_response(
        request,
        user,
        eval_set_id,
        notice=f"LLM 已建立 {created} 題 draft 候選題；略過 {skipped} 題無效或重複輸出。",
    )


@router.post("/admin/evals/sets/{eval_set_id}/items")
def admin_add_eval_item(
    request: Request,
    eval_set_id: int,
    user: Annotated[dict, Depends(require_admin)],
    question: str = Form(...),
    expected_source_id: str = Form(""),
    expected_substrings: str = Form(""),
    notes: str = Form(""),
):
    question = " ".join(question.split())[:500]
    if not question:
        raise HTTPException(status_code=400, detail="問題不可為空。")
    try:
        source_id = int(expected_source_id) if str(expected_source_id).strip() else None
    except ValueError:
        raise HTTPException(status_code=400, detail="預期來源格式不正確。") from None
    snippets = split_expected_substrings(expected_substrings)
    with connect() as conn:
        eval_set_context = load_eval_set(conn, eval_set_id)
    record_ai_safety_events(
        text=question,
        event_type="input_scan",
        surface="eval_authoring.manual_question",
        context={"user_id": user["id"], "notebook_id": eval_set_context["notebook_id"], "eval_set_id": eval_set_id},
        metadata={"has_expected_source": bool(source_id), "substring_count": len(snippets)},
    )
    with connect() as conn:
        eval_set = load_eval_set(conn, eval_set_id)
        if source_id is not None:
            row = conn.execute(
                "SELECT id FROM sources WHERE id = ? AND notebook_id = ? AND user_id = ?",
                (source_id, eval_set["notebook_id"], eval_set["target_user_id"]),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=400, detail="預期來源不屬於此 eval set。")
        conn.execute(
            """
            INSERT INTO eval_items
            (eval_set_id, question, expected_source_id, expected_substrings_json, notes, approved)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (eval_set_id, question, source_id, dumps(snippets), notes.strip()[:500]),
        )
        conn.execute("UPDATE eval_sets SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (eval_set_id,))
    logger.info("eval_item_created admin_user_id=%s eval_set_id=%s", user["id"], eval_set_id)
    return eval_set_items_response(request, user, eval_set_id)


@router.post("/admin/evals/sets/{eval_set_id}/items/{item_id}/approve")
def admin_approve_eval_item(
    request: Request,
    eval_set_id: int,
    item_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    with connect() as conn:
        load_eval_set(conn, eval_set_id)
        updated = conn.execute(
            """
            UPDATE eval_items
            SET approved = 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND eval_set_id = ?
            RETURNING id
            """,
            (item_id, eval_set_id),
        ).fetchone()
        if updated is None:
            raise HTTPException(status_code=404, detail="找不到 eval item")
        conn.execute("UPDATE eval_sets SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (eval_set_id,))
    logger.info("eval_item_approved admin_user_id=%s eval_set_id=%s item_id=%s", user["id"], eval_set_id, item_id)
    return eval_set_items_response(request, user, eval_set_id)


@router.post("/admin/evals/sets/{eval_set_id}/items/{item_id}/delete")
def admin_delete_eval_item(
    request: Request,
    eval_set_id: int,
    item_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    with connect() as conn:
        load_eval_set(conn, eval_set_id)
        used_in_run = conn.execute(
            "SELECT id FROM eval_results WHERE eval_item_id = ? LIMIT 1",
            (item_id,),
        ).fetchone()
        if used_in_run is not None:
            raise HTTPException(status_code=400, detail="此題目已有 run result，不能刪除以免破壞歷史紀錄。")
        deleted = conn.execute(
            "DELETE FROM eval_items WHERE id = ? AND eval_set_id = ? RETURNING id",
            (item_id, eval_set_id),
        ).fetchone()
        if deleted is None:
            raise HTTPException(status_code=404, detail="找不到 eval item")
        conn.execute("UPDATE eval_sets SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (eval_set_id,))
    logger.info("eval_item_deleted admin_user_id=%s eval_set_id=%s item_id=%s", user["id"], eval_set_id, item_id)
    return eval_set_items_response(request, user, eval_set_id)


@router.post("/admin/evals/sets/{eval_set_id}/run")
def admin_start_eval_run(
    eval_set_id: int,
    background_tasks: BackgroundTasks,
    user: Annotated[dict, Depends(require_admin)],
    profile_id: int | None = Form(None),
):
    with connect() as conn:
        load_eval_set(conn, eval_set_id)
        profile = ensure_default_retrieval_profile(conn, user["id"])
        if profile_id is not None:
            chosen = conn.execute(
                "SELECT * FROM retrieval_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            if chosen is None:
                raise HTTPException(status_code=404, detail="找不到 retrieval profile")
            profile = dict(chosen)
        approved_count = conn.execute(
            "SELECT COUNT(*) AS n FROM eval_items WHERE eval_set_id = ? AND approved = 1",
            (eval_set_id,),
        ).fetchone()["n"]
        if approved_count == 0:
            raise HTTPException(status_code=400, detail="至少需要一題 approved eval item。")
        cursor = conn.execute(
            """
            INSERT INTO eval_runs
            (eval_set_id, profile_id, created_by, status, progress_total, profile_snapshot_json, current_step)
            VALUES (?, ?, ?, 'queued', ?, ?, '等待背景執行')
            """,
            (eval_set_id, profile["id"], user["id"], approved_count, profile["params_json"]),
        )
        run_id = cursor.lastrowid
    background_tasks.add_task(run_eval_job, run_id)
    logger.info("eval_run_queued admin_user_id=%s eval_set_id=%s run_id=%s", user["id"], eval_set_id, run_id)
    return RedirectResponse(f"/admin/evals/runs/{run_id}", status_code=303)


@router.get("/admin/evals/runs/{run_id}", response_class=HTMLResponse)
def admin_eval_run_detail(
    request: Request,
    run_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    context = eval_run_context(run_id)
    run = context["run"]
    return render(
        request,
        "admin_eval_run.html",
        {
            "user": user,
            "breadcrumb_items": [
                {"label": i18n.t("eval.title"), "href": "/admin/evals"},
                {"label": run["eval_set_name"], "href": f"/admin/evals/sets/{run['eval_set_id']}"},
                {"label": f"Run #{run['id']}", "href": None},
            ],
            **context,
        },
    )


@router.get("/admin/evals/runs/{run_id}/_status", response_class=HTMLResponse)
def admin_eval_run_status(
    request: Request,
    run_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    return render(request, "_eval_run_status.html", {"user": user, **eval_run_context(run_id)})


@router.get("/admin/evals/runs/{run_id}/_results", response_class=HTMLResponse)
def admin_eval_run_results(
    request: Request,
    run_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    return render(request, "_eval_run_results.html", {"user": user, **eval_run_context(run_id)})


@router.get("/admin/evals/runs/{run_id}/export/sanitized")
def admin_export_eval_run_sanitized(
    request: Request,
    run_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    context = eval_run_context(run_id)
    payload = sanitized_run_export_payload(context)
    record_audit_event(
        request,
        user,
        "eval_run_export_sanitized",
        "eval_run",
        run_id,
        {
            "eval_set_id": context["run"]["eval_set_id"],
            "status": context["run"]["status"],
            "result_count": len(context["results"]),
        },
        "normal",
    )
    logger.info("eval_run_exported_sanitized admin_user_id=%s run_id=%s", user["id"], run_id)
    return _json_download(payload, f"eval-run-{run_id}-sanitized.json")


@router.get("/admin/evals/runs/{run_id}/export/full")
def admin_export_eval_run_full(
    request: Request,
    run_id: int,
    user: Annotated[dict, Depends(require_admin)],
    confirm: int = 0,
):
    if confirm != 1:
        raise HTTPException(status_code=400, detail="Full internal report export requires explicit confirmation.")
    context = eval_run_context(run_id)
    payload = full_run_export_payload(context)
    record_audit_event(
        request,
        user,
        "eval_run_export_full",
        "eval_run",
        run_id,
        {
            "eval_set_id": context["run"]["eval_set_id"],
            "status": context["run"]["status"],
            "result_count": len(context["results"]),
            "contains_questions": True,
            "contains_expected_evidence": True,
            "contains_retrieved_snippets": True,
        },
        "high",
    )
    logger.info("eval_run_exported_full admin_user_id=%s run_id=%s", user["id"], run_id)
    return _json_download(payload, f"eval-run-{run_id}-full-internal.json")


# --- E1c: retrieval profile authoring + apply/rollback + run comparison ---

@router.get("/admin/evals/profiles/{profile_id}/export")
def admin_export_profile(
    request: Request,
    profile_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    with connect() as conn:
        profile = conn.execute("SELECT * FROM retrieval_profiles WHERE id = ?", (profile_id,)).fetchone()
    if profile is None:
        raise HTTPException(status_code=404, detail="找不到 retrieval profile")
    payload = profile_export_payload(dict(profile))
    record_audit_event(
        request,
        user,
        "retrieval_profile_export_sanitized",
        "retrieval_profile",
        profile_id,
        {
            "name": profile["name"],
            "requires_reindex": bool(profile["requires_reindex"]),
            "is_active": bool(profile["is_active"]),
        },
        "normal",
    )
    logger.info("eval_profile_exported admin_user_id=%s profile_id=%s", user["id"], profile_id)
    return _json_download(payload, f"retrieval-profile-{profile_id}.json")

@router.post("/admin/evals/profiles")
def admin_create_profile(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    name: str = Form(...),
    description: str = Form(""),
    low_confidence_threshold: str = Form(...),
    vector_weight: str = Form(...),
    keyword_weight: str = Form(...),
    candidate_pool_size: str = Form(...),
    final_chunk_count: str = Form(...),
    rerank_weight: str = Form(...),
    rerank_base_weight: str = Form(...),
):
    """Create a candidate retrieval profile (runtime-safe params only, inactive)."""
    name = name.strip()[:120]
    if not name:
        raise HTTPException(status_code=400, detail="Profile 名稱不可為空。")
    params = coerce_profile_params({
        "low_confidence_threshold": low_confidence_threshold,
        "vector_weight": vector_weight,
        "keyword_weight": keyword_weight,
        "candidate_pool_size": candidate_pool_size,
        "final_chunk_count": final_chunk_count,
        "rerank_weight": rerank_weight,
        "rerank_base_weight": rerank_base_weight,
    })
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO retrieval_profiles
            (name, description, params_json, requires_reindex, is_active, created_by)
            VALUES (?, ?, ?, 0, 0, ?)
            """,
            (name, description.strip()[:500], dumps(params), user["id"]),
        )
    profile_id = cursor.lastrowid
    record_audit_event(
        request,
        user,
        "retrieval_profile_created",
        "retrieval_profile",
        profile_id,
        {"name": name, "params": params, "requires_reindex": False},
        "normal",
    )
    logger.info("eval_profile_created admin_user_id=%s profile_id=%s", user["id"], profile_id)
    return RedirectResponse("/admin/evals/profiles", status_code=303)


@router.post("/admin/evals/profiles/{profile_id}/delete")
def admin_delete_profile(
    request: Request,
    profile_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM retrieval_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="找不到 retrieval profile")
        if row["is_default"]:
            raise HTTPException(status_code=400, detail="系統預設 profile 不能刪除，它是回復預設值的保底。")
        if row["is_active"]:
            raise HTTPException(status_code=400, detail="作用中的 profile 不能刪除，請先套用其他 profile。")
        conn.execute("DELETE FROM retrieval_profiles WHERE id = ?", (profile_id,))
    record_audit_event(
        request,
        user,
        "retrieval_profile_deleted",
        "retrieval_profile",
        profile_id,
        {"name": row["name"], "requires_reindex": bool(row["requires_reindex"])},
        "normal",
    )
    logger.info("eval_profile_deleted admin_user_id=%s profile_id=%s", user["id"], profile_id)
    return RedirectResponse("/admin/evals/profiles", status_code=303)


@router.post("/admin/evals/profiles/{profile_id}/apply")
def admin_apply_profile(
    request: Request,
    profile_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    """Apply a runtime-safe profile to live retrieval; persisted via is_active."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM retrieval_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="找不到 retrieval profile")
        if row["requires_reindex"]:
            raise HTTPException(
                status_code=400,
                detail="此 profile 含需重建索引的參數，不能直接套用；請改用 Clear/Rebuild 流程。",
            )
        previous = conn.execute(
            "SELECT id, name, params_json FROM retrieval_profiles WHERE is_active = 1 ORDER BY id ASC LIMIT 1"
        ).fetchone()
        conn.execute("UPDATE retrieval_profiles SET is_active = 0 WHERE is_active = 1")
        conn.execute(
            "UPDATE retrieval_profiles SET is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (profile_id,),
        )
    set_active_retrieval_params(loads(row["params_json"] or "{}"))
    record_audit_event(
        request,
        user,
        "retrieval_profile_applied",
        "retrieval_profile",
        profile_id,
        {
            "name": row["name"],
            "previous_profile_id": previous["id"] if previous else None,
            "previous_profile_name": previous["name"] if previous else "",
            "params": loads(row["params_json"] or "{}"),
        },
        "high",
    )
    logger.info(
        "eval_profile_applied admin_user_id=%s profile_id=%s params=%s",
        user["id"], profile_id, dumps(ACTIVE_RETRIEVAL_PARAMS),
    )
    return RedirectResponse("/admin/evals/profiles", status_code=303)


def compare_runs_context(base_id: int, candidate_id: int) -> dict[str, Any]:
    """Build a side-by-side comparison of two succeeded runs in the same eval set."""
    base = eval_run_context(base_id)
    candidate = eval_run_context(candidate_id)
    base_run, candidate_run = base["run"], candidate["run"]
    if base_run["eval_set_id"] != candidate_run["eval_set_id"]:
        raise HTTPException(status_code=400, detail="只能比較同一個 eval set 的 run。")
    for run in (base_run, candidate_run):
        if run["status"] != "succeeded":
            raise HTTPException(status_code=400, detail="只能比較已成功完成的 run。")

    # Param diff: union of both snapshots, flag changed rows.
    base_params = base_run["profile_snapshot"]
    cand_params = candidate_run["profile_snapshot"]
    param_diff = []
    for key in PROFILE_PARAM_LABELS:
        if key not in base_params and key not in cand_params:
            continue
        bval, cval = base_params.get(key), cand_params.get(key)
        param_diff.append({
            "label": PROFILE_PARAM_LABELS.get(key, key),
            "base": bval,
            "candidate": cval,
            "changed": bval != cval,
        })

    # Metric diff: lower-is-better for latency + low-confidence rate.
    base_metrics = base_run["metrics"]
    cand_metrics = candidate_run["metrics"]
    metric_specs = [
        ("recall_at_k", "Recall@k", True),
        ("mrr", "MRR", True),
        ("avg_top_score", "平均 top score", True),
        ("avg_latency_ms", "平均延遲 (ms)", False),
        ("low_confidence_rate", "低信心率", False),
        ("hits", "命中數", True),
    ]
    metric_diff = []
    for key, label, higher_better in metric_specs:
        bval = float(base_metrics.get(key) or 0.0)
        cval = float(cand_metrics.get(key) or 0.0)
        delta = round(cval - bval, 4)
        if delta == 0:
            direction = "same"
        elif (delta > 0) == higher_better:
            direction = "better"
        else:
            direction = "worse"
        metric_diff.append({
            "label": label, "base": bval, "candidate": cval,
            "delta": delta, "direction": direction,
        })

    # Per-question delta keyed by eval_item_id.
    base_by_item = {r["eval_item_id"]: r for r in base["results"]}
    cand_by_item = {r["eval_item_id"]: r for r in candidate["results"]}
    rank_order = {"hit": 0, "miss": 1, "unscored": 2, "error": 3, "pending": 4}
    question_diff = []
    for item_id in sorted(set(base_by_item) | set(cand_by_item)):
        b = base_by_item.get(item_id)
        c = cand_by_item.get(item_id)
        bstatus = b["status"] if b else "—"
        cstatus = c["status"] if c else "—"
        brank = b.get("hit_rank") if b else None
        crank = c.get("hit_rank") if c else None
        if b and c:
            bscore, cscore = rank_order.get(bstatus, 9), rank_order.get(cstatus, 9)
            if cscore < bscore or (bstatus == cstatus == "hit" and crank and brank and crank < brank):
                trend = "improved"
            elif cscore > bscore or (bstatus == cstatus == "hit" and crank and brank and crank > brank):
                trend = "regressed"
            else:
                trend = "unchanged"
        else:
            trend = "unchanged"
        question_diff.append({
            "question": (b or c)["question"],
            "base_status": bstatus, "candidate_status": cstatus,
            "base_rank": brank, "candidate_rank": crank,
            "trend": trend,
        })

    return {
        "base_run": base_run,
        "candidate_run": candidate_run,
        "param_diff": param_diff,
        "metric_diff": metric_diff,
        "question_diff": question_diff,
        "improved": sum(1 for q in question_diff if q["trend"] == "improved"),
        "regressed": sum(1 for q in question_diff if q["trend"] == "regressed"),
    }


@router.get("/admin/evals/compare", response_class=HTMLResponse)
def admin_eval_compare(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    base: int,
    candidate: int,
):
    context = compare_runs_context(base, candidate)
    base_run = context["base_run"]
    return render(
        request,
        "admin_eval_compare.html",
        {
            "user": user,
            "breadcrumb_items": [
                {"label": i18n.t("eval.title"), "href": "/admin/evals"},
                {"label": base_run["eval_set_name"], "href": f"/admin/evals/sets/{base_run['eval_set_id']}"},
                {"label": f"比較 #{context['base_run']['id']} ↔ #{context['candidate_run']['id']}", "href": None},
            ],
            **context,
        },
    )
