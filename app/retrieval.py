"""Retrieval engine: hybrid (vector + keyword) search, scoring, diversification,
and the runtime-safe retrieval-parameter machinery shared with the eval workbench.

Extracted from ``app/main.py`` to keep the HTTP layer thin. This module owns the
pure retrieval logic and the in-process "active retrieval params" state; routes
import what they need from here. See ``docs/RETRIEVAL.md`` before changing
retrieval, chunking, ranking, reranking, or scoring behaviour.
"""

import asyncio
import logging
import re
import time
from typing import Any

from fastapi import HTTPException

from .config import config
from .db import connect, dumps, loads
from .llm import (
    cosine,
    embed_texts,
    rerank_chunks,
    rewrite_search_queries,
)
from .vector_store import query as query_vectors

logger = logging.getLogger(__name__)


# Per-question minimum top-score before we let the answer LLM run. Below this
# the model is asked to abstain. Lives on the ask() side, not retrieve(), so
# the eval harness can still observe raw retrieval scores.
LOW_CONFIDENCE_THRESHOLD = config.retrieval.low_confidence_threshold

# Upper bound on rows pulled into the degraded "Chroma is down" fallback in
# retrieve(). Without it, a transient Chroma failure would decode every chunk's
# embedding from SQLite and run Python-side cosine over the whole corpus —
# O(all_chunks) memory + CPU that melts the box at scale. The fallback has no
# real vector index, so it is a best-effort safety net, not the primary path.
FALLBACK_MAX_CHUNKS = config.retrieval.fallback_max_chunks

# Hybrid blend weights + candidate-pool / final-chunk sizes (config-driven).
VECTOR_WEIGHT = config.retrieval.vector_weight
KEYWORD_WEIGHT = config.retrieval.keyword_weight
CANDIDATE_POOL_SIZE = config.retrieval.candidate_pool_size
FINAL_CHUNK_COUNT = config.retrieval.final_chunk_count


async def retrieve(
    question: str,
    rows,
    settings: dict,
    history: list[dict[str, str]] | None = None,
    user_id: int | None = None,
    source_ids: list[int] | None = None,
    params: dict | None = None,
    usage_context: dict[str, Any] | None = None,
) -> list[dict]:
    """Retrieve chunks with query rewriting, hybrid search, and optional LLM reranking.

    ``params`` overrides the runtime-safe retrieval knobs for this call only
    (used by the eval workbench for isolated per-run experiments); None falls
    back to the active applied profile.
    """
    started = time.perf_counter()
    p = resolve_retrieval_params(params)
    pool_size = int(p["candidate_pool_size"])
    final_count = int(p["final_chunk_count"])
    vector_weight = float(p["vector_weight"])
    keyword_weight = float(p["keyword_weight"])
    rerank_weight = float(p["rerank_weight"])
    rerank_base_weight = float(p["rerank_base_weight"])
    queries = await rewrite_search_queries(question, history or [], settings, usage_context=usage_context)
    query_embeddings = await embed_texts(queries, settings, role="query", usage_context=usage_context)
    if user_id is not None:
        try:
            # Vector (Chroma) and keyword (SQLite) search are independent — run
            # them concurrently in threads so their I/O overlaps instead of
            # adding up (P2-2). Both are sync; to_thread releases the event loop.
            vector_candidates, keyword_candidates = await asyncio.gather(
                asyncio.to_thread(query_vectors, query_embeddings, user_id, source_ids, n_results=pool_size),
                asyncio.to_thread(keyword_candidates_from_sqlite, user_id, source_ids or [], queries, limit=pool_size),
            )
            candidates = merge_candidates(vector_candidates, keyword_candidates, queries, params=p)
            ranked = diversify_candidates(
                sorted(candidates.values(), key=lambda item: item["score"], reverse=True),
                limit=pool_size,
            )
            retrieved = await rerank_chunks(
                question, ranked, settings, limit=final_count,
                rerank_weight=rerank_weight, rerank_base_weight=rerank_base_weight,
                usage_context=usage_context,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "retrieve_completed mode=chroma rewritten_queries=%s vector_candidates=%s keyword_candidates=%s candidates=%s reranked=%s elapsed_ms=%.1f",
                len(queries),
                len(vector_candidates),
                len(keyword_candidates),
                len(ranked),
                len(retrieved),
                elapsed_ms,
            )
            return retrieved
        except Exception:
            logger.warning(
                "retrieve_vector_failed user_id=%s — falling back to capped SQLite scan (max=%s chunks); "
                "results are degraded until Chroma recovers (try /admin/index Rebuild)",
                user_id, FALLBACK_MAX_CHUNKS, exc_info=True,
            )
            rows = fetch_candidate_rows(user_id, source_ids or [])
    if not rows:
        logger.info("retrieve_skipped reason=no_candidate_rows")
        return []
    candidates = {}
    for row in rows:
        embedding = loads(row["embedding_json"])
        vector_score = max(cosine(query_embedding, embedding) for query_embedding in query_embeddings)
        keyword = keyword_score(queries, row["text"])
        score = (vector_weight * max(0.0, vector_score)) + (keyword_weight * keyword)
        if score <= 0:
            continue
        candidates[row["id"]] = {
            "id": row["id"],
            "source_id": row["source_id"],
            "filename": row["filename"],
            "location": row["location"],
            "text": row["text"],
            "score": score,
            "vector_score": vector_score,
            "keyword_score": keyword,
        }
    ranked = diversify_candidates(
        sorted(candidates.values(), key=lambda item: item["score"], reverse=True),
        limit=pool_size,
    )
    retrieved = await rerank_chunks(
        question, ranked, settings, limit=final_count,
        rerank_weight=rerank_weight, rerank_base_weight=rerank_base_weight,
        usage_context=usage_context,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "retrieve_completed source_rows=%s rewritten_queries=%s candidates=%s reranked=%s elapsed_ms=%.1f",
        len(rows),
        len(queries),
        len(ranked),
        len(retrieved),
        elapsed_ms,
    )
    return retrieved


def fetch_candidate_rows(user_id: int, source_ids: list[int], limit: int = FALLBACK_MAX_CHUNKS) -> list:
    """Fetch SQLite chunks for the degraded fallback used when Chroma is down.

    Capped at ``limit`` rows (most recent first) so a Chroma outage degrades
    gracefully instead of decoding every chunk's embedding and melting the box
    at corpus scale. This path has no real vector index — best-effort only.
    """
    with connect() as conn:
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            return conn.execute(
                f"""
                SELECT chunks.*, sources.filename
                FROM chunks JOIN sources ON sources.id = chunks.source_id
                WHERE chunks.user_id = ? AND sources.status = 'indexed' AND chunks.source_id IN ({placeholders})
                ORDER BY chunks.id DESC
                LIMIT ?
                """,
                (user_id, *source_ids, limit),
            ).fetchall()
        return conn.execute(
            """
            SELECT chunks.*, sources.filename
            FROM chunks JOIN sources ON sources.id = chunks.source_id
            WHERE chunks.user_id = ? AND sources.status = 'indexed'
            ORDER BY chunks.id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()


def keyword_candidates_from_sqlite(user_id: int, source_ids: list[int], queries: list[str], limit: int = 20) -> list[dict]:
    """Find keyword candidate chunks from SQLite without decoding all embeddings."""
    tokens = []
    for query in queries:
        tokens.extend(search_tokens(query))
    unique_tokens = list(dict.fromkeys(tokens))[:12]
    if not unique_tokens:
        return []
    like_clause = " OR ".join("chunks.text LIKE ?" for _ in unique_tokens)
    params: list = [user_id]
    source_clause = ""
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        source_clause = f"AND chunks.source_id IN ({placeholders})"
        params.extend(source_ids)
    params.extend(f"%{token}%" for token in unique_tokens)
    params.append(limit * 4)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT chunks.*, sources.filename
            FROM chunks JOIN sources ON sources.id = chunks.source_id
            WHERE chunks.user_id = ? AND sources.status = 'indexed'
              {source_clause}
              AND ({like_clause})
            ORDER BY chunks.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    candidates = [
        {
            "id": row["id"],
            "source_id": row["source_id"],
            "filename": row["filename"],
            "location": row["location"],
            "text": row["text"],
            "vector_score": 0.0,
        }
        for row in rows
    ]
    return sorted(candidates, key=lambda item: keyword_score(queries, item["text"]), reverse=True)[:limit]


def merge_candidates(
    vector_candidates: list[dict],
    keyword_candidates: list[dict],
    queries: list[str],
    params: dict | None = None,
) -> dict[int, dict]:
    """Merge vector and keyword candidates into one hybrid-scored map."""
    p = resolve_retrieval_params(params)
    vector_weight = float(p["vector_weight"])
    keyword_weight = float(p["keyword_weight"])
    candidates: dict[int, dict] = {}
    for item in [*vector_candidates, *keyword_candidates]:
        chunk_id = int(item["id"])
        keyword = keyword_score(queries, item["text"])
        vector_score = max(0.0, float(item.get("vector_score") or 0.0))
        score = (vector_weight * vector_score) + (keyword_weight * keyword)
        existing = candidates.get(chunk_id)
        if existing and existing["score"] >= score:
            continue
        candidates[chunk_id] = {
            "id": chunk_id,
            "source_id": item["source_id"],
            "filename": item["filename"],
            "location": item["location"],
            "text": item["text"],
            "score": score,
            "vector_score": vector_score,
            "keyword_score": keyword,
        }
    return {chunk_id: item for chunk_id, item in candidates.items() if item["score"] > 0}


def diversify_candidates(
    candidates: list[dict],
    *,
    limit: int,
    overlap_threshold: float = 0.88,
    min_tokens: int = 8,
) -> list[dict]:
    """Keep high-scoring candidates while dropping near-duplicate text.

    Sentence overlap is useful at ingest time, but adjacent chunks can
    otherwise occupy several pre-rerank slots with nearly the same evidence.
    This runs after hybrid scoring, so the best-scoring representative wins.
    """
    selected: list[dict] = []
    selected_tokens: list[set[str]] = []
    for item in candidates:
        tokens = _candidate_overlap_tokens(item.get("text") or "")
        if len(tokens) >= min_tokens and any(
            _jaccard(tokens, existing) >= overlap_threshold
            for existing in selected_tokens
            if existing
        ):
            continue
        selected.append(item)
        selected_tokens.append(tokens)
        if len(selected) >= limit:
            break
    return selected


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _candidate_overlap_tokens(text: str) -> set[str]:
    """Token set for candidate diversity, normalized more than keyword search."""
    stripped = ".,;:!?()[]{}\"'`“”‘’"
    return {token.strip(stripped) for token in search_tokens(text) if token.strip(stripped)}


def keyword_score(queries: list[str], text: str) -> float:
    """Score lexical overlap between retrieval queries and a candidate chunk."""
    text_tokens = set(search_tokens(text))
    if not text_tokens:
        return 0.0
    best = 0.0
    lowered_text = text.lower()
    for query in queries:
        tokens = search_tokens(query)
        if not tokens:
            continue
        overlap = sum(1 for token in tokens if token in text_tokens) / len(tokens)
        phrase_boost = 0.15 if query.lower() in lowered_text else 0.0
        best = max(best, min(1.0, overlap + phrase_boost))
    return best


def search_tokens(text: str) -> list[str]:
    """Tokenize query text for lightweight keyword retrieval."""
    stopwords = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "要",
        "的",
        "是",
        "有",
        "和",
        "或",
        "在",
        "嗎",
        "呢",
    }
    latin_tokens = [
        token
        for token in re.findall(r"[\w.-]+", text.lower(), flags=re.UNICODE)
        if len(token) > 1 and token not in stopwords
    ]
    cjk_text = "".join(re.findall(r"[一-鿿]", text))
    cjk_tokens = [token for token in cjk_ngrams(cjk_text) if token not in stopwords]
    return latin_tokens + cjk_tokens


def cjk_ngrams(text: str) -> list[str]:
    """Create short CJK character n-grams for keyword matching."""
    if len(text) < 2:
        return []
    grams = [text[index : index + 2] for index in range(len(text) - 1)]
    if len(text) > 2:
        grams.extend(text[index : index + 3] for index in range(len(text) - 2))
    return grams


def citation_payload(chunks: list[dict]) -> list[dict]:
    """Convert retrieved chunks into serializable citation metadata.

    Includes the hybrid / vector / keyword / rerank scores so the chat
    "Why these citations?" debug pane can show why each chunk was picked.
    Scores default to 0.0 — older messages stored before this field existed
    will simply render no debug numbers, which the template handles.
    """
    return [
        {
            "index": index,
            "source_id": chunk.get("source_id"),
            # U3: chunk row id → lets the citation chip open the source preview
            # scrolled to + highlighting this exact chunk (#preview-chunk-{id}).
            "chunk_id": chunk.get("id"),
            "filename": chunk["filename"],
            "location": chunk["location"],
            "snippet": chunk["text"][:260],
            "score": round(float(chunk.get("score", 0.0)), 3),
            "vector_score": round(float(chunk.get("vector_score", 0.0)), 3),
            "keyword_score": round(float(chunk.get("keyword_score", 0.0)), 3),
            "rerank_score": round(float(chunk["rerank_score"]), 3) if chunk.get("rerank_score") is not None else None,
        }
        for index, chunk in enumerate(chunks, start=1)
    ]


def message_with_citations(row) -> dict:
    """Attach decoded citation + per-message metadata to a row dictionary."""
    message = dict(row)
    message["citations"] = loads(message["citations_json"])
    raw_meta = message.get("metadata_json") or "{}"
    try:
        message["metadata"] = loads(raw_meta) if isinstance(raw_meta, str) else (raw_meta or {})
    except Exception:
        message["metadata"] = {}
    return message


def current_retrieval_profile_params() -> dict[str, Any]:
    """Snapshot runtime-safe retrieval knobs used by the E1 eval workbench."""
    return {
        "low_confidence_threshold": LOW_CONFIDENCE_THRESHOLD,
        "vector_weight": VECTOR_WEIGHT,
        "keyword_weight": KEYWORD_WEIGHT,
        "candidate_pool_size": CANDIDATE_POOL_SIZE,
        "final_chunk_count": FINAL_CHUNK_COUNT,
        "rerank_weight": config.retrieval.rerank_weight,
        "rerank_base_weight": config.retrieval.rerank_base_weight,
    }


PROFILE_PARAM_LABELS = {
    "low_confidence_threshold": "低信心閾值",
    "vector_weight": "Vector 權重",
    "keyword_weight": "Keyword 權重",
    "candidate_pool_size": "候選池大小",
    "final_chunk_count": "最終 chunk 數",
    "rerank_weight": "Rerank 權重",
    "rerank_base_weight": "Rerank base 權重",
}

# Type + range rules for the 7 runtime-safe profile params (E1c authoring form).
# Pool/chunk counts are positive ints; the rest are floats >= 0.
PROFILE_PARAM_INT_KEYS = {"candidate_pool_size", "final_chunk_count"}


def coerce_profile_params(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate/coerce admin-entered profile params; raise HTTP 400 on bad input."""
    params: dict[str, Any] = {}
    for key in PROFILE_PARAM_LABELS:
        if key not in raw or raw[key] in (None, ""):
            raise HTTPException(status_code=400, detail=f"缺少參數：{PROFILE_PARAM_LABELS[key]}")
        try:
            if key in PROFILE_PARAM_INT_KEYS:
                value: Any = int(raw[key])
                if value < 1:
                    raise ValueError
            else:
                value = float(raw[key])
                if value < 0:
                    raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"參數值無效：{PROFILE_PARAM_LABELS[key]}")
        params[key] = value
    return params


def profile_param_rows(params: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"key": key, "label": PROFILE_PARAM_LABELS.get(key, key), "value": params[key]}
        for key in PROFILE_PARAM_LABELS
        if key in params
    ]


def profile_params_for_display(profile: dict) -> list[dict[str, Any]]:
    return profile_param_rows(loads(profile.get("params_json") or "{}"))


# Runtime-safe retrieval params resolved at import as the literal defaults (equal
# to app/config.py), plus a mutable in-process "active" copy the live retrieval
# path reads. The eval workbench (E1c) can apply a saved profile to mutate the
# active copy (and persist it via retrieval_profiles.is_active), or run an eval
# with an isolated per-run override without touching the active copy. Defaults
# stay equal to today's behaviour, so an un-applied deployment is unchanged.
RETRIEVAL_PARAM_DEFAULTS: dict[str, Any] = current_retrieval_profile_params()
ACTIVE_RETRIEVAL_PARAMS: dict[str, Any] = dict(RETRIEVAL_PARAM_DEFAULTS)


def resolve_retrieval_params(params: dict | None) -> dict[str, Any]:
    """Merge a (possibly partial) override over defaults; None → active params."""
    if params is None:
        return dict(ACTIVE_RETRIEVAL_PARAMS)
    merged = dict(RETRIEVAL_PARAM_DEFAULTS)
    merged.update({k: v for k, v in params.items() if k in RETRIEVAL_PARAM_DEFAULTS})
    return merged


def active_retrieval_params() -> dict[str, Any]:
    return dict(ACTIVE_RETRIEVAL_PARAMS)


def active_low_confidence_threshold() -> float:
    return float(ACTIVE_RETRIEVAL_PARAMS.get("low_confidence_threshold", LOW_CONFIDENCE_THRESHOLD))


def set_active_retrieval_params(params: dict | None) -> None:
    """Replace the in-process active retrieval params (apply / startup load)."""
    resolved = resolve_retrieval_params(params)
    ACTIVE_RETRIEVAL_PARAMS.clear()
    ACTIVE_RETRIEVAL_PARAMS.update(resolved)


def load_active_retrieval_profile() -> None:
    """Seed the active retrieval params from the persisted active profile (if any).

    Called at startup so a previously applied profile survives a restart. Falls
    back to the import-time defaults when no profile has been applied yet.
    """
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT params_json FROM retrieval_profiles WHERE is_active = 1 ORDER BY id ASC LIMIT 1"
            ).fetchone()
    except Exception:
        logger.exception("active_profile_load_failed")
        return
    if row is None:
        set_active_retrieval_params(None)
        return
    set_active_retrieval_params(loads(row["params_json"] or "{}"))
    logger.info("active_profile_loaded params=%s", dumps(ACTIVE_RETRIEVAL_PARAMS))
