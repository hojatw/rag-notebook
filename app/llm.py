import asyncio
import json
import logging
import random
import time
from typing import Any

import httpx


SYSTEM_PROMPT = """You are a source-grounded RAG assistant.
Answer only from the provided source excerpts.
Reply in the same language as the user's question (Traditional Chinese question -> Traditional Chinese answer).
If the excerpts do not contain enough information, say: "I cannot determine that from the selected sources."
Keep the answer concise and include bracket citations like [1], [2] for the excerpts you used."""

QUERY_REWRITE_PROMPT = """You create retrieval queries for a source-grounded RAG system.
Do not answer the user. Rewrite the user's question into 1 to 4 concise search queries.
Resolve pronouns from the conversation context when possible.
Prefer exact terms, product names, field names, versions, and likely document wording.
Return only a JSON array of strings."""

RERANK_PROMPT = """You are a retrieval reranker for a source-grounded RAG system.
Score each candidate by whether it contains evidence needed to answer the question.
Use 0 for irrelevant and 1 for directly useful evidence.
Return only a JSON array of objects like [{"id": 1, "score": 0.92}]."""

STARTER_QUESTIONS_PROMPT = """You suggest starter questions for a notebook of source documents.
Read the provided excerpts. Propose 4 short, varied questions a curious reader might ask.
Each question must be answerable from the excerpts and stand alone (no pronouns).
Match the dominant language of the excerpts (Traditional Chinese excerpts -> Traditional Chinese questions).
Return only a JSON array of strings, each under 80 characters."""

SOURCE_SUMMARY_PROMPT = """You write tight summaries of single source documents.
Read the provided excerpts (which are the first chunks of one document).
Write 2 to 4 sentences capturing what the document is about and its key claims or findings.
No filler ("This document discusses..."), no bullets, no headings.

LANGUAGE RULE — strictly match the dominant language of the source excerpts:
- Traditional Chinese excerpts -> Traditional Chinese summary (繁體中文).
- Simplified Chinese excerpts -> Simplified Chinese summary.
- Japanese excerpts -> Japanese summary.
- English excerpts -> English summary.
Do NOT translate. If excerpts are mixed-language, follow whichever language carries the majority of the content."""

NOTEBOOK_BRIEFING_PROMPT = """You write a one-paragraph briefing across multiple source summaries in a notebook.

LANGUAGE RULE — strictly match the dominant language of the source summaries:
- Traditional Chinese summaries -> Traditional Chinese briefing (繁體中文).
- Simplified Chinese summaries -> Simplified Chinese briefing.
- Japanese summaries -> Japanese briefing.
- English summaries -> English briefing.
Do NOT translate. If summaries are mixed-language, follow whichever language carries the majority.

Read each source's summary and produce a single paragraph of 80 to 110 words covering:
- What this collection of sources is about as a whole.
- Recurring themes or shared subject matter.
- Any notable contrasts or differences in perspective.
Do not list sources mechanically; weave them into prose. No headings, no bullets.
Keep it tight — the briefing is shown in a small sidebar."""

SOURCE_COMPARE_PROMPT = """You compare two or more source documents grounded in their summaries.

LANGUAGE RULE — read this FIRST, it overrides the structure example below.
Strictly match the dominant language of the source summaries:
- Traditional Chinese summaries -> Traditional Chinese comparison (繁體中文).
  Use headings: ## 共同點 / ## 各自獨特之處 / ## 矛盾之處
- Simplified Chinese summaries -> Simplified Chinese comparison.
  Use headings: ## 共同点 / ## 各自独特之处 / ## 矛盾之处
- Japanese summaries -> Japanese comparison.
  Use headings: ## 共通点 / ## それぞれの特徴 / ## 矛盾点
- English summaries -> English comparison.
  Use headings: ## Shared / ## Distinct / ## Contradictions
Do NOT translate. The headings below are FORMAT examples in English — translate
them to match the source language before writing.

Use this Markdown structure, OMITTING any section that would be empty:

## Shared
- Common ground across the sources.

## Distinct
- **{filename}** — what is unique to this source.
- (one bullet per source that has distinctive points)

## Contradictions
- Direct disagreements between sources, citing the sources by filename.

Stay grounded in the provided summaries. If a focus question is given, prioritise points relevant to it."""

logger = logging.getLogger(__name__)
EMBEDDING_BATCH_SIZE = 64
# How many embedding batches may be in flight at once. Bounded so a large
# ingest doesn't hammer a shared/borrowed embedding endpoint. Override per
# deployment via the `embedding_max_concurrency` setting.
EMBEDDING_MAX_CONCURRENCY = 4

# Retry policy for the LLM/embedding HTTP calls. The target endpoint is often a
# shared, occasionally-throttled service; one transient 429/5xx or timeout
# should not fail the whole question (which makes several calls).
LLM_RETRY_MAX_ATTEMPTS = 3
LLM_RETRY_BACKOFF_BASE_S = 0.5
LLM_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

_http_client: httpx.AsyncClient | None = None


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """Set the shared HTTP client used by LLM API calls."""
    global _http_client
    _http_client = client


def get_http_client() -> httpx.AsyncClient:
    """Return a shared HTTP client, creating a lazy fallback if needed."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=None)
    return _http_client


async def _post_json_with_retry(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
    *,
    max_attempts: int = LLM_RETRY_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """POST JSON and return the decoded body, retrying transient failures.

    Retries network/timeout errors and 429/5xx with exponential backoff + jitter
    — the failure modes of a shared, throttled endpoint. Non-retryable 4xx (e.g.
    a malformed request) and the final attempt's error propagate to the caller.
    """
    client = get_http_client()
    attempt = 0
    while True:
        attempt += 1
        try:
            response = await client.post(url, headers=headers, json=payload, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in LLM_RETRYABLE_STATUS or attempt >= max_attempts:
                raise
        except httpx.RequestError:
            # Covers connect/read/write/pool timeouts and transport/network errors.
            if attempt >= max_attempts:
                raise
        delay = LLM_RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1)) + random.uniform(0.0, 0.25)
        logger.warning(
            "llm_http_retry attempt=%s/%s delay_ms=%.0f url=%s",
            attempt, max_attempts, delay * 1000, url,
        )
        await asyncio.sleep(delay)


async def close_http_client() -> None:
    """Close the shared HTTP client when the app shuts down."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


def cosine(a: list[float], b: list[float]) -> float:
    """Return cosine similarity for normalized embedding vectors."""
    if not a or not b:
        return 0.0
    limit = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(limit))


def _embedding_prefix(settings: dict[str, Any], role: str | None) -> str:
    """Return the configured embedding prefix for this path, or empty.

    Some models (the e5 family) need ``"query: "`` on search queries and
    ``"passage: "`` on indexed text. The prefixes are configured in settings
    and default to empty, so models that don't want them (OpenAI, etc.) are
    unaffected and the app stays embedding-model-agnostic.
    """
    if role == "query":
        return settings.get("embedding_query_prefix") or ""
    if role == "passage":
        return settings.get("embedding_passage_prefix") or ""
    return ""


async def embed_texts(
    texts: list[str],
    settings: dict[str, Any],
    *,
    role: str | None = None,
) -> list[list[float]]:
    """Embed texts using the configured embedding API.

    ``role`` selects an optional, settings-driven prefix: ``"passage"`` for
    indexed chunks, ``"query"`` for search queries (e5-style). It only changes
    the text sent to the embedding endpoint, never the stored chunk text.

    Raises RuntimeError when the embedding model or API key is missing — we
    no longer fall back to a local hash embedder because the resulting
    vectors are incompatible with whatever real model the index was built
    against, and silent-fallback masks misconfiguration as poor retrieval.
    """
    if not settings.get("api_key") or not settings.get("embedding_model"):
        raise RuntimeError(
            "Embedding model is not configured. An admin must set the embedding "
            "model and API key at /settings before embeddings can be generated."
        )

    prefix = _embedding_prefix(settings, role)
    if prefix:
        texts = [prefix + text for text in texts]

    batch_size = int(settings.get("embedding_batch_size") or EMBEDDING_BATCH_SIZE)
    batches = [texts[start : start + batch_size] for start in range(0, len(texts), batch_size)]
    if not batches:
        return []
    if len(batches) == 1:
        return await embed_text_batch(batches[0], settings)

    # Run batches with bounded concurrency instead of one-at-a-time, so a large
    # ingest isn't dominated by serial round-trips to the embedding endpoint.
    # asyncio.gather preserves order, so results still line up with `texts`.
    max_concurrency = max(1, int(settings.get("embedding_max_concurrency") or EMBEDDING_MAX_CONCURRENCY))
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _run(batch: list[str]) -> list[list[float]]:
        async with semaphore:
            return await embed_text_batch(batch, settings)

    results = await asyncio.gather(*(_run(batch) for batch in batches))
    embeddings: list[list[float]] = []
    for result in results:
        embeddings.extend(result)
    return embeddings


async def probe_embedding_dimension(settings: dict[str, Any]) -> int:
    """Embed a tiny probe string and return the dimensionality of the result.

    Used by ``/settings`` to validate connectivity and lock-in dimension
    BEFORE persisting the new configuration, so dim mismatches surface at
    save time instead of at first ingest.
    """
    vectors = await embed_texts(["dim probe"], settings)
    return len(vectors[0]) if vectors else 0


async def embed_text_batch(texts: list[str], settings: dict[str, Any]) -> list[list[float]]:
    """Embed one bounded batch through the configured embedding API."""
    request = build_embedding_request(settings, texts)
    timeout = float(settings.get("timeout_seconds") or 60)
    started = time.perf_counter()
    try:
        data = await _post_json_with_retry(request["url"], request["headers"], request["json"], timeout)
    except Exception:
        logger.exception(
            "embedding_api_failed provider=%s model=%s text_count=%s",
            settings.get("provider") or "openai_compatible",
            settings.get("embedding_model"),
            len(texts),
        )
        raise
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "embedding_api_completed provider=%s model=%s batch_text_count=%s elapsed_ms=%.1f",
        settings.get("provider") or "openai_compatible",
        settings.get("embedding_model"),
        len(texts),
        elapsed_ms,
    )
    return [item["embedding"] for item in data["data"]]


async def generate_answer(question: str, chunks: list[dict[str, Any]], settings: dict[str, Any]) -> str:
    """Ask the configured chat model to answer from retrieved chunks only."""
    if not settings.get("api_key") or not settings.get("chat_model"):
        raise RuntimeError("LLM settings are not configured. Ask an admin to set base URL, API key, and chat model.")

    logger.info("answer_generation_started chunks=%s question_chars=%s", len(chunks), len(question))
    context = "\n\n".join(
        f"[{index}] {chunk['filename']} - {chunk['location']}\n{chunk['text']}"
        for index, chunk in enumerate(chunks, start=1)
    )
    user_prompt = f"Source excerpts:\n{context}\n\nQuestion: {question}"
    return await chat_completion(settings, user_prompt, SYSTEM_PROMPT)


async def rewrite_search_queries(question: str, history: list[dict[str, str]], settings: dict[str, Any]) -> list[str]:
    """Ask the chat model for retrieval-focused query rewrites."""
    if not settings.get("api_key") or not settings.get("chat_model"):
        logger.info("query_rewrite_skipped reason=no_chat_settings")
        return [question]

    context = "\n".join(f"{item['role']}: {item['content']}" for item in history[-6:])
    user_prompt = (
        f"Conversation context:\n{context or '(none)'}\n\n"
        f"User question:\n{question}\n\n"
        "Return retrieval queries as JSON."
    )
    try:
        content = await chat_completion(settings, user_prompt, QUERY_REWRITE_PROMPT, temperature=0.0)
        queries = parse_json_strings(content)
    except Exception:
        logger.exception("query_rewrite_failed question_chars=%s history_messages=%s", len(question), len(history))
        queries = []
    rewritten = unique_nonempty([question, *queries])[:5]
    logger.info("query_rewrite_completed input_chars=%s output_queries=%s", len(question), len(rewritten))
    return rewritten


async def rerank_chunks(question: str, candidates: list[dict[str, Any]], settings: dict[str, Any], limit: int = 6) -> list[dict[str, Any]]:
    """Ask the chat model to rerank candidate chunks, falling back to hybrid scores."""
    if not candidates:
        return []
    fallback = sorted(candidates, key=lambda item: item["score"], reverse=True)[:limit]
    if not settings.get("api_key") or not settings.get("chat_model"):
        logger.info("rerank_skipped reason=no_chat_settings candidates=%s returned=%s", len(candidates), len(fallback))
        return fallback

    # Pass full chunk text — chunks are bounded by chunk_text() (~400 chars
    # CJK / ~800 Latin) and earlier truncation at 900 chars sometimes lopped
    # off answer evidence in the tail of a chunk. The extra prompt budget is
    # cheap relative to the rerank call itself.
    excerpts = "\n\n".join(
        f'Candidate {index}\nFile: {chunk["filename"]}\nLocation: {chunk["location"]}\nText: {chunk["text"]}'
        for index, chunk in enumerate(candidates[:20], start=1)
    )
    user_prompt = f"Question:\n{question}\n\nCandidates:\n{excerpts}"
    try:
        content = await chat_completion(settings, user_prompt, RERANK_PROMPT, temperature=0.0)
        scores = parse_rerank_scores(content)
    except Exception:
        logger.exception("rerank_failed candidates=%s", len(candidates))
        return fallback

    ranked = []
    for index, chunk in enumerate(candidates[:20], start=1):
        rerank_score = scores.get(index)
        if rerank_score is None:
            continue
        combined = (0.8 * rerank_score) + (0.2 * chunk["score"])
        ranked.append((combined, {**chunk, "rerank_score": rerank_score, "score": combined}))
    if not ranked:
        logger.warning("rerank_empty_scores candidates=%s", len(candidates))
        return fallback
    ranked.sort(key=lambda item: item[0], reverse=True)
    reranked = [chunk for score, chunk in ranked[:limit] if score > 0]
    logger.info("rerank_completed candidates=%s scored=%s returned=%s", len(candidates), len(ranked), len(reranked))
    return reranked


async def generate_starter_questions(excerpts: list[dict[str, Any]], settings: dict[str, Any]) -> list[str]:
    """Ask the chat model for 4 short starter questions grounded in sample excerpts."""
    if not excerpts:
        return []
    if not settings.get("api_key") or not settings.get("chat_model"):
        logger.info("starter_questions_skipped reason=no_chat_settings")
        return []
    samples = excerpts[:8]
    context = "\n\n".join(
        f"[{index}] {chunk['filename']} - {chunk['location']}\n{chunk['text'][:400]}"
        for index, chunk in enumerate(samples, start=1)
    )
    user_prompt = f"Source excerpts:\n{context}\n\nReturn the JSON array now."
    try:
        content = await chat_completion(settings, user_prompt, STARTER_QUESTIONS_PROMPT, temperature=0.6)
        questions = parse_json_strings(content)
    except Exception:
        logger.exception("starter_questions_failed excerpts=%s", len(samples))
        return []
    cleaned = [q for q in (q.strip() for q in questions) if q]
    logger.info("starter_questions_generated excerpts=%s returned=%s", len(samples), len(cleaned[:4]))
    return cleaned[:4]


async def summarize_source(chunks: list[dict[str, Any]], settings: dict[str, Any]) -> str:
    """Generate a 2-4 sentence summary of one source from its first chunks.

    Returns an empty string on missing settings or upstream failure — the
    caller (ingest) treats summary generation as best-effort and must not
    fail the source on a summary error.
    """
    if not chunks:
        return ""
    if not settings.get("api_key") or not settings.get("chat_model"):
        logger.info("source_summary_skipped reason=no_chat_settings")
        return ""
    samples = chunks[:12]
    context = "\n\n".join(
        f"[{index}] {chunk.get('location', '')}\n{chunk['text']}"
        for index, chunk in enumerate(samples, start=1)
    )
    user_prompt = f"Source excerpts:\n{context}\n\nWrite the summary now."
    try:
        content = await chat_completion(settings, user_prompt, SOURCE_SUMMARY_PROMPT, temperature=0.3)
    except Exception:
        logger.exception("source_summary_failed chunks=%s", len(samples))
        return ""
    summary = (content or "").strip()
    logger.info("source_summary_generated excerpts=%s chars=%s", len(samples), len(summary))
    return summary


async def generate_briefing(summaries: list[dict[str, Any]], settings: dict[str, Any]) -> str:
    """Synthesize a one-paragraph briefing across multiple source summaries.

    Each item should look like ``{"filename": str, "summary": str}``. Sources
    with empty summaries are skipped — caller can pass a "(no summary)"
    placeholder if it wants the source mentioned anyway.
    """
    items = [item for item in summaries if (item.get("summary") or "").strip()]
    if not items:
        return ""
    if not settings.get("api_key") or not settings.get("chat_model"):
        logger.info("briefing_skipped reason=no_chat_settings")
        return ""
    context = "\n\n".join(
        f"[{index}] {item['filename']}\n{item['summary'].strip()}"
        for index, item in enumerate(items, start=1)
    )
    user_prompt = f"Source summaries:\n{context}\n\nWrite the briefing now."
    try:
        content = await chat_completion(settings, user_prompt, NOTEBOOK_BRIEFING_PROMPT, temperature=0.4)
    except Exception:
        logger.exception("briefing_failed summaries=%s", len(items))
        return ""
    briefing = (content or "").strip()
    logger.info("briefing_generated summaries=%s chars=%s", len(items), len(briefing))
    return briefing


async def compare_sources(
    summaries: list[dict[str, Any]],
    focus: str,
    settings: dict[str, Any],
) -> str:
    """Compare 2+ sources by their summaries.

    Returns an empty string if fewer than 2 usable summaries are provided or
    settings are missing. ``focus`` is an optional free-text hint from the
    user about what to compare on.
    """
    items = [item for item in summaries if (item.get("summary") or "").strip()]
    if len(items) < 2:
        logger.info("compare_skipped reason=fewer_than_two_summaries provided=%s", len(items))
        return ""
    if not settings.get("api_key") or not settings.get("chat_model"):
        logger.info("compare_skipped reason=no_chat_settings")
        return ""
    context = "\n\n".join(
        f"[{index}] {item['filename']}\n{item['summary'].strip()}"
        for index, item in enumerate(items, start=1)
    )
    focus_line = f"Focus: {focus.strip()}\n\n" if (focus or "").strip() else ""
    user_prompt = f"{focus_line}Sources to compare:\n{context}\n\nWrite the comparison now."
    try:
        content = await chat_completion(settings, user_prompt, SOURCE_COMPARE_PROMPT, temperature=0.3)
    except Exception:
        logger.exception("compare_failed summaries=%s focus_chars=%s", len(items), len(focus or ""))
        return ""
    comparison = (content or "").strip()
    logger.info("compare_generated summaries=%s chars=%s focus_chars=%s", len(items), len(comparison), len(focus or ""))
    return comparison


async def chat_completion(
    settings: dict[str, Any],
    user_prompt: str,
    system_prompt: str,
    temperature: float | None = None,
) -> str:
    """Call the configured chat completion endpoint and return message text."""
    request = build_chat_request(settings, user_prompt, system_prompt, temperature)
    timeout = float(settings.get("timeout_seconds") or 60)
    started = time.perf_counter()
    try:
        data = await _post_json_with_retry(request["url"], request["headers"], request["json"], timeout)
    except Exception:
        logger.exception(
            "chat_completion_failed provider=%s model=%s prompt_chars=%s",
            settings.get("provider") or "openai_compatible",
            settings.get("chat_model"),
            len(user_prompt),
        )
        raise
    content = data["choices"][0]["message"]["content"].strip()
    elapsed_ms = (time.perf_counter() - started) * 1000
    # Token estimates are chars/4 — accurate enough for cost monitoring
    # without pulling in a tokenizer dependency. Used by the per-message
    # cost badge in the chat UI.
    logger.info(
        "chat_completion_completed provider=%s model=%s prompt_chars=%s prompt_tokens_est=%s response_chars=%s response_tokens_est=%s elapsed_ms=%.1f",
        settings.get("provider") or "openai_compatible",
        settings.get("chat_model"),
        len(user_prompt),
        len(user_prompt) // 4,
        len(content),
        len(content) // 4,
        elapsed_ms,
    )
    return content


def parse_json_strings(content: str) -> list[str]:
    """Parse a JSON string array from model output, accepting fenced JSON."""
    parsed = json.loads(extract_json(content))
    if not isinstance(parsed, list):
        return []
    return [item.strip() for item in parsed if isinstance(item, str) and item.strip()]


def parse_rerank_scores(content: str) -> dict[int, float]:
    """Parse reranker JSON output into candidate id to bounded score."""
    parsed = json.loads(extract_json(content))
    if not isinstance(parsed, list):
        return {}
    scores: dict[int, float] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            candidate_id = int(item["id"])
            score = float(item["score"])
        except (KeyError, TypeError, ValueError):
            continue
        scores[candidate_id] = max(0.0, min(1.0, score))
    return scores


def extract_json(content: str) -> str:
    """Extract the JSON payload from plain or fenced model output."""
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start_candidates = [index for index in (stripped.find("["), stripped.find("{")) if index >= 0]
    if not start_candidates:
        return stripped
    start = min(start_candidates)
    end = max(stripped.rfind("]"), stripped.rfind("}"))
    return stripped[start : end + 1] if end >= start else stripped[start:]


def unique_nonempty(values: list[str]) -> list[str]:
    """Return unique stripped strings while preserving order."""
    seen = set()
    output = []
    for value in values:
        normalized = " ".join(value.split())
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            output.append(normalized)
    return output


def build_embedding_request(settings: dict[str, Any], texts: list[str]) -> dict[str, Any]:
    """Build the provider-specific HTTP request for embeddings.

    When ``embedding_base_url`` is set it overrides ``base_url`` for this
    call only — needed when chat and embeddings live on different services
    (typical with vLLM for chat + Ollama / TEI for embeddings, since vLLM's
    /v1/embeddings only supports encoder-style models).
    """
    provider = settings.get("provider") or "openai_compatible"
    if provider == "azure_openai":
        return _azure_request(settings, settings["embedding_model"], "embeddings", {"input": texts})

    base_url = settings.get("embedding_base_url") or settings.get("base_url") or "https://api.openai.com/v1"
    return {
        "url": base_url.rstrip("/") + "/embeddings",
        "headers": {"Authorization": f"Bearer {settings['api_key']}"},
        "json": {"model": settings["embedding_model"], "input": texts},
    }


def build_chat_request(
    settings: dict[str, Any],
    user_prompt: str,
    system_prompt: str = SYSTEM_PROMPT,
    temperature: float | None = None,
) -> dict[str, Any]:
    """Build the provider-specific HTTP request for chat completion."""
    effective_temperature = settings.get("temperature") if temperature is None else temperature
    payload = {
        "temperature": float(0.2 if effective_temperature is None else effective_temperature),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    provider = settings.get("provider") or "openai_compatible"
    if provider == "azure_openai":
        return _azure_request(settings, settings["chat_model"], "chat/completions", payload)

    base_url = settings.get("base_url") or "https://api.openai.com/v1"
    payload["model"] = settings["chat_model"]
    return {
        "url": base_url.rstrip("/") + "/chat/completions",
        "headers": {"Authorization": f"Bearer {settings['api_key']}"},
        "json": payload,
    }


def _azure_request(settings: dict[str, Any], deployment: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Build an Azure OpenAI deployment URL and api-key request payload."""
    if not settings.get("base_url"):
        raise RuntimeError("Azure OpenAI endpoint is required.")
    if not settings.get("api_version"):
        raise RuntimeError("Azure OpenAI API version is required.")
    base_url = settings["base_url"].rstrip("/")
    url = f"{base_url}/openai/deployments/{deployment}/{operation}?api-version={settings['api_version']}"
    return {
        "url": url,
        "headers": {"api-key": settings["api_key"]},
        "json": payload,
    }
