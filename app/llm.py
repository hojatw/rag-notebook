import asyncio
import json
import logging
import random
import re
import time
from typing import Any

import httpx

from .config import config
from .governance import normalize_usage, record_llm_usage_event


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

LANGUAGE RULE — strictly match the dominant language of the source excerpts:
- Traditional Chinese excerpts -> Traditional Chinese questions (繁體中文).
- Simplified Chinese excerpts -> Simplified Chinese questions.
- Japanese excerpts -> Japanese questions.
- English excerpts -> English questions.
Do NOT translate. If excerpts are mixed-language, follow whichever language carries the majority of the content.

Return only a JSON array of strings, each under 80 characters."""

EVAL_AUTHORING_PROMPT = """You create draft eval questions for an in-deployment RAG eval workbench.
Use ONLY the provided source excerpts. The output is reviewed by an admin before becoming ground truth.

Return only a JSON array of objects. Each object must use this shape:
{
  "question": "standalone eval question",
  "type": "answerable | cross_lingual | unanswerable",
  "source_id": 123,
  "chunk_id": 456,
  "expected_answer": "short reference answer, blank for unanswerable",
  "expected_substrings": ["short exact evidence substring"],
  "rationale": "why this is useful as an eval item"
}

Rules:
- "answerable" questions must be answerable from one provided excerpt.
- "cross_lingual" questions must ask in a different language than the supporting excerpt while still being answerable from it.
- "unanswerable" questions must be plausible for this notebook but NOT answerable from the provided excerpts; use null source_id/chunk_id and an empty expected_substrings list.
- For answerable/cross_lingual items, copy 1 to 3 short exact substrings from the supporting excerpt. Prefer distinctive names, numbers, dates, or terminology.
- Do not invent source ids or chunk ids; use the ids shown in the excerpt headers.
- Keep each question under 140 characters.
- Use Traditional Chinese questions unless the requested item type is cross_lingual or the excerpts are clearly non-Chinese and the instruction asks otherwise."""

FOLLOWUP_QUESTIONS_PROMPT = """You suggest follow-up questions after an assistant answered a user inside a source-grounded RAG app.
Read the source excerpts, the user's question, and the assistant's answer. Propose 3 short, distinct follow-up questions the user would plausibly ask next.
Each question must stand alone (no pronouns) and be answerable from the same source documents.

LANGUAGE RULE — the user prompt includes TARGET LANGUAGE. Write every follow-up question in that target language.
The TARGET LANGUAGE overrides the user's question language and the assistant answer language.
If TARGET LANGUAGE is English, every follow-up must be English.
If TARGET LANGUAGE is Traditional Chinese, every follow-up must be Traditional Chinese (繁體中文).
If TARGET LANGUAGE is Japanese, every follow-up must be Japanese.
Do NOT translate the source content; ask natural follow-up questions in the target language.

Return only a JSON array of strings, each under 60 characters."""

MEETING_MINUTES_PROMPT = """You turn a meeting transcript (or meeting-notes document) into structured minutes.

LANGUAGE RULE — strictly match the dominant language of the transcript; do NOT translate:
- Traditional Chinese transcript -> 繁體中文, use headings: ## 會議主題 / ## 重要決議 / ## 行動項目 / ## 待辦與追蹤 / ## 未決事項
- Simplified Chinese transcript -> Simplified Chinese with equivalent headings.
- Japanese transcript -> Japanese with equivalent headings.
- English transcript -> English, use headings: ## Topic / ## Decisions / ## Action items / ## Follow-ups / ## Open questions

Rules:
- OMIT any section that would be empty. No filler, no preamble before the first heading.
- Under 行動項目 / Action items, one bullet per item as: **負責人/owner (if stated)** — task — 期限/deadline (if stated).
- Stay strictly grounded in the transcript; never invent attendees, decisions, or dates.
- If the document is clearly NOT a meeting transcript or meeting notes, reply with exactly one line saying it does not look like a meeting record (in the document's language) and nothing else."""

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

# --- A4 artifact prompts (study guide / FAQ / timeline) -------------------
# Siblings of briefing/compare: each takes the notebook's source summaries and
# produces one Markdown artifact saved to Notes. Same strong per-language rule
# so an English notebook never gets a Chinese study guide (cf. Q0-6).

STUDY_GUIDE_PROMPT = """You write a study guide from a notebook of source summaries.

LANGUAGE RULE — strictly match the dominant language of the source summaries. Do NOT translate:
- Traditional Chinese summaries -> 繁體中文, headings: ## 核心概念 / ## 重點整理 / ## 自我測驗問題 / ## 名詞解釋
- Simplified Chinese summaries -> Simplified Chinese with equivalent headings.
- Japanese summaries -> Japanese with equivalent headings.
- English summaries -> English, headings: ## Key concepts / ## Summary points / ## Self-test questions / ## Glossary
If summaries are mixed-language, follow whichever language carries the majority.

Produce a study guide using the Markdown headings above, OMITTING any section that would be empty:
- 核心概念 / Key concepts: the main ideas a reader must understand, one bullet each.
- 重點整理 / Summary points: concise takeaways grounded in the summaries.
- 自我測驗問題 / Self-test questions: 4-6 open questions answerable from the sources (no answers).
- 名詞解釋 / Glossary: key terms with a one-line definition, only if the sources define them.
Stay strictly grounded in the provided summaries; never invent facts. No preamble before the first heading."""

FAQ_PROMPT = """You write a FAQ from a notebook of source summaries.

LANGUAGE RULE — strictly match the dominant language of the source summaries. Do NOT translate:
- Traditional Chinese summaries -> 繁體中文 questions and answers.
- Simplified Chinese summaries -> Simplified Chinese.
- Japanese summaries -> Japanese.
- English summaries -> English.
If summaries are mixed-language, follow whichever language carries the majority.

Produce 5-8 frequently-asked questions a reader would have, each with a 1-3 sentence grounded answer.
Format each as Markdown: a bold question line, then the answer on the next line.
Example shape (translate to the source language):
**Q: ...?**
A: ...

Stay strictly grounded in the provided summaries; never invent facts. If the sources do not support an answer, omit that question. No preamble before the first question."""

TIMELINE_PROMPT = """You build a chronological timeline from a notebook of source summaries.

LANGUAGE RULE — strictly match the dominant language of the source summaries. Do NOT translate:
- Traditional Chinese summaries -> 繁體中文.
- Simplified Chinese summaries -> Simplified Chinese.
- Japanese summaries -> Japanese.
- English summaries -> English.
If summaries are mixed-language, follow whichever language carries the majority.

List the events, milestones, or stages mentioned in the sources in chronological order, one Markdown bullet each:
- Prefix each bullet with its date/period in bold if the sources state one (**2023** — ...); otherwise order by the logical/described sequence.
- Keep each entry to one line, grounded strictly in the summaries.
Stay strictly grounded; never invent dates or events. If the sources contain no temporal or sequential information at all, reply with exactly one line saying there is no timeline information in these sources (in the sources' language) and nothing else. No preamble before the first bullet."""

TRANSLATE_SUMMARY_PROMPT = """You translate a source document's summary into a target language (A5).
The user prompt includes TARGET LANGUAGE. Translate the provided summary into exactly that language.

Rules:
- Output ONLY the translation — no preamble, no notes, no the original text, no quotes around it.
- Preserve meaning, terminology, numbers, and names; do not add, omit, or summarise further.
- If the summary is already in the target language, return it unchanged."""

# kind -> (prompt, temperature, log label). The single dispatch point for A4.
ARTIFACT_PROMPTS: dict[str, tuple[str, float, str]] = {
    "study_guide": (STUDY_GUIDE_PROMPT, 0.4, "study_guide"),
    "faq": (FAQ_PROMPT, 0.4, "faq"),
    "timeline": (TIMELINE_PROMPT, 0.3, "timeline"),
}

logger = logging.getLogger(__name__)
# Tunables sourced from app.config (defaults <- config.toml <- env); see config.py.
EMBEDDING_BATCH_SIZE = config.embedding.batch_size
# How many embedding batches may be in flight at once. Bounded so a large
# ingest doesn't hammer a shared/borrowed embedding endpoint. Override per
# deployment via the `embedding_max_concurrency` setting or config.
EMBEDDING_MAX_CONCURRENCY = config.embedding.max_concurrency

# Retry policy for the LLM/embedding HTTP calls. The target endpoint is often a
# shared, occasionally-throttled service; one transient 429/5xx or timeout
# should not fail the whole question (which makes several calls).
LLM_RETRY_MAX_ATTEMPTS = config.llm_retry.max_attempts
LLM_RETRY_BACKOFF_BASE_S = config.llm_retry.backoff_base_s
LLM_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Rerank blend: final score = RERANK_WEIGHT * llm_rerank + RERANK_BASE_WEIGHT * hybrid.
RERANK_WEIGHT = config.retrieval.rerank_weight
RERANK_BASE_WEIGHT = config.retrieval.rerank_base_weight
# Cap on candidates sent to the LLM reranker — same pool size as retrieve().
RERANK_INPUT_SIZE = config.retrieval.candidate_pool_size
FOLLOWUPS_CACHE_VERSION = 2

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
    retry_stats: dict[str, Any] | None = None,
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
        if retry_stats is not None:
            retry_stats["attempts"] = attempt
        try:
            response = await client.post(url, headers=headers, json=payload, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if retry_stats is not None:
                retry_stats["last_error_class"] = exc.__class__.__name__
                retry_stats["last_status_code"] = exc.response.status_code
            if exc.response.status_code not in LLM_RETRYABLE_STATUS or attempt >= max_attempts:
                raise
        except httpx.RequestError as exc:
            # Covers connect/read/write/pool timeouts and transport/network errors.
            if retry_stats is not None:
                retry_stats["last_error_class"] = exc.__class__.__name__
            if attempt >= max_attempts:
                raise
        delay = LLM_RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1)) + random.uniform(0.0, 0.25)
        if retry_stats is not None:
            retry_stats["retry_count"] = int(retry_stats.get("retry_count") or 0) + 1
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

    Defensive: the convention needs a separator after the prefix, but people
    typically type ``"query:"`` without the trailing space. If a non-empty
    prefix doesn't already end in whitespace, append one space so the prefix
    is never glued onto the text (e.g. ``"query:weather"``). The stored value
    is left as-typed; only the composed string is normalised.
    """
    if role == "query":
        prefix = settings.get("embedding_query_prefix") or ""
    elif role == "passage":
        prefix = settings.get("embedding_passage_prefix") or ""
    else:
        return ""
    if prefix and not prefix[-1].isspace():
        prefix += " "
    return prefix


async def embed_texts(
    texts: list[str],
    settings: dict[str, Any],
    *,
    role: str | None = None,
    usage_context: dict[str, Any] | None = None,
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
    if not settings.get("embedding_model"):
        raise RuntimeError(
            "Embedding model is not configured. An admin must set the embedding "
            "model at /settings before embeddings can be generated."
        )

    prefix = _embedding_prefix(settings, role)
    if prefix:
        texts = [prefix + text for text in texts]

    batch_size = int(settings.get("embedding_batch_size") or EMBEDDING_BATCH_SIZE)
    batches = [texts[start : start + batch_size] for start in range(0, len(texts), batch_size)]
    if not batches:
        return []
    if len(batches) == 1:
        return await embed_text_batch(batches[0], settings, role=role, usage_context=usage_context)

    # Run batches with bounded concurrency instead of one-at-a-time, so a large
    # ingest isn't dominated by serial round-trips to the embedding endpoint.
    # asyncio.gather preserves order, so results still line up with `texts`.
    max_concurrency = max(1, int(settings.get("embedding_max_concurrency") or EMBEDDING_MAX_CONCURRENCY))
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _run(batch: list[str]) -> list[list[float]]:
        async with semaphore:
            return await embed_text_batch(batch, settings, role=role, usage_context=usage_context)

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


DIAGNOSTIC_SYSTEM_PROMPT = (
    "You are a deployment diagnostics endpoint check. Follow the user's output "
    "format exactly. Do not include explanations."
)
DIAGNOSTIC_IMAGE_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP4z8AARAwQCgAf7gP9i18U1AAAAABJRU5ErkJggg=="
)


async def probe_embedding_diagnostics(
    settings: dict[str, Any],
    *,
    usage_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return compact embedding diagnostics without storing probe text/output."""
    provider = settings.get("provider") or "openai_compatible"
    model = settings.get("embedding_model") or ""
    result: dict[str, Any] = {
        "status": "failed",
        "provider": provider,
        "model": model,
        "latency_ms": 0.0,
        "embedding_dimension": None,
        "error_class": "",
    }
    if not model:
        result["error_class"] = "MissingSettings"
        return result

    texts = ["diagnostics embedding probe"]
    request = build_embedding_request(settings, texts)
    timeout = float(settings.get("timeout_seconds") or 60)
    started = time.perf_counter()
    input_chars = sum(len(text) for text in texts)
    retry_stats: dict[str, Any] = {}
    try:
        data = await _post_json_with_retry(
            request["url"],
            request["headers"],
            request["json"],
            timeout,
            retry_stats=retry_stats,
        )
        vectors = [item["embedding"] for item in data["data"]]
        dimension = len(vectors[0]) if vectors else 0
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        _record_usage_event(
            settings=settings,
            call_type="settings_embedding_probe",
            status="failed",
            latency_ms=elapsed_ms,
            input_chars=input_chars,
            output_chars=0,
            usage=None,
            usage_context=usage_context,
            error_class=exc.__class__.__name__,
            metadata=_usage_metadata({"probe": "embedding", "text_count": len(texts)}, retry_stats),
            model_key="embedding_model",
        )
        result["latency_ms"] = round(elapsed_ms, 1)
        result["error_class"] = exc.__class__.__name__
        logger.warning("settings_embedding_probe_failed provider=%s model=%s", provider, model, exc_info=True)
        return result

    elapsed_ms = (time.perf_counter() - started) * 1000
    _record_usage_event(
        settings=settings,
        call_type="settings_embedding_probe",
        status="succeeded",
        latency_ms=elapsed_ms,
        input_chars=input_chars,
        output_chars=0,
        usage=data.get("usage"),
        usage_context=usage_context,
        metadata=_usage_metadata(
            {
                "probe": "embedding",
                "text_count": len(texts),
                "embedding_dimension": dimension,
                "usage_available": isinstance(data.get("usage"), dict),
            },
            retry_stats,
        ),
        model_key="embedding_model",
    )
    result.update({
        "status": "succeeded",
        "latency_ms": round(elapsed_ms, 1),
        "embedding_dimension": dimension,
    })
    return result


async def probe_chat_diagnostics(
    settings: dict[str, Any],
    *,
    include_image: bool = False,
    usage_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return compact chat/capability diagnostics for the active provider."""
    provider = settings.get("provider") or "openai_compatible"
    model = settings.get("chat_model") or ""
    result: dict[str, Any] = {
        "status": "failed",
        "provider": provider,
        "model": model,
        "latency_ms": 0.0,
        "error_class": "",
        "capabilities": {
            "streaming": {"status": "not_tested"},
            "usage_reporting": {"status": "not_tested"},
            "json_following": {"status": "not_tested"},
            "image_understanding": {"status": "not_tested" if include_image else "skipped"},
        },
    }
    if not model:
        result["error_class"] = "MissingSettings"
        return result

    json_probe = await _probe_chat_once(
        settings,
        user_prompt='Return exactly this JSON object: {"ok": true, "label": "pong"}',
        system_prompt=DIAGNOSTIC_SYSTEM_PROMPT,
        call_type="settings_chat_probe",
        probe_name="json",
        usage_context=usage_context,
        expect_json=True,
    )
    result["status"] = json_probe["status"]
    result["latency_ms"] = json_probe["latency_ms"]
    result["error_class"] = json_probe.get("error_class", "")
    result["capabilities"]["json_following"] = {
        "status": "succeeded" if json_probe.get("json_valid") else "failed",
        "latency_ms": json_probe["latency_ms"],
        "error_class": json_probe.get("error_class", ""),
    }

    stream_probe = await _probe_chat_stream(settings, usage_context=usage_context)
    result["capabilities"]["streaming"] = {
        "status": stream_probe["status"],
        "latency_ms": stream_probe["latency_ms"],
        "error_class": stream_probe.get("error_class", ""),
        "usage_available": stream_probe.get("usage_available", False),
        "stream_usage_fallback": stream_probe.get("stream_usage_fallback", False),
    }

    usage_available = bool(json_probe.get("usage_available") or stream_probe.get("usage_available"))
    result["capabilities"]["usage_reporting"] = {
        "status": "succeeded" if usage_available else "failed",
        "usage_available": usage_available,
    }

    if include_image:
        image_probe = await _probe_image_understanding(settings, usage_context=usage_context)
        result["capabilities"]["image_understanding"] = image_probe

    return result


async def _probe_chat_once(
    settings: dict[str, Any],
    *,
    user_prompt: str,
    system_prompt: str,
    call_type: str,
    probe_name: str,
    usage_context: dict[str, Any] | None,
    expect_json: bool = False,
    messages_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    request = build_chat_request(settings, user_prompt, system_prompt, temperature=0.0)
    if messages_override is not None:
        request["json"]["messages"] = messages_override
    timeout = float(settings.get("timeout_seconds") or 60)
    started = time.perf_counter()
    input_chars = _message_chars(request["json"].get("messages") or [])
    retry_stats: dict[str, Any] = {}
    json_valid = False
    json_object: dict[str, Any] = {}
    try:
        data = await _post_json_with_retry(
            request["url"],
            request["headers"],
            request["json"],
            timeout,
            retry_stats=retry_stats,
        )
        content = str(data["choices"][0]["message"]["content"]).strip()
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        _record_usage_event(
            settings=settings,
            call_type=call_type,
            status="failed",
            latency_ms=elapsed_ms,
            input_chars=input_chars,
            output_chars=0,
            usage=None,
            usage_context=usage_context,
            error_class=exc.__class__.__name__,
            metadata=_usage_metadata({"probe": probe_name, "json_valid": False}, retry_stats),
            model_key="chat_model",
        )
        logger.warning("%s_failed provider=%s model=%s", call_type, settings.get("provider"), settings.get("chat_model"), exc_info=True)
        return {
            "status": "failed",
            "latency_ms": round(elapsed_ms, 1),
            "error_class": exc.__class__.__name__,
            "usage_available": False,
            "json_valid": False,
        }

    if expect_json:
        try:
            parsed = json.loads(extract_json(content))
            json_valid = isinstance(parsed, dict)
            json_object = parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError, TypeError):
            json_valid = False

    elapsed_ms = (time.perf_counter() - started) * 1000
    usage_available = isinstance(data.get("usage"), dict)
    _record_usage_event(
        settings=settings,
        call_type=call_type,
        status="succeeded",
        latency_ms=elapsed_ms,
        input_chars=input_chars,
        output_chars=len(content),
        usage=data.get("usage"),
        usage_context=usage_context,
        metadata=_usage_metadata(
            {"probe": probe_name, "json_valid": json_valid, "usage_available": usage_available},
            retry_stats,
        ),
        model_key="chat_model",
    )
    return {
        "status": "succeeded",
        "latency_ms": round(elapsed_ms, 1),
        "error_class": "",
        "usage_available": usage_available,
        "json_valid": json_valid,
        "json_object": json_object,
    }


async def _probe_chat_stream(
    settings: dict[str, Any],
    *,
    usage_context: dict[str, Any] | None,
) -> dict[str, Any]:
    request = build_chat_request(settings, "Reply with exactly: ok", DIAGNOSTIC_SYSTEM_PROMPT, temperature=0.0)
    request["json"]["stream"] = True
    request["json"]["stream_options"] = {"include_usage": True}
    timeout = float(settings.get("timeout_seconds") or 60)
    started = time.perf_counter()
    input_chars = _message_chars(request["json"].get("messages") or [])
    chars = 0
    usage: dict[str, Any] | None = None
    retry_stats: dict[str, Any] = {}
    stream_usage_requested = True
    stream_usage_fallback = False
    client = get_http_client()

    while True:
        retry_stats["attempts"] = int(retry_stats.get("attempts") or 0) + 1
        chars_at_attempt_start = chars
        try:
            async with client.stream(
                "POST",
                request["url"],
                headers=request["headers"],
                json=request["json"],
                timeout=timeout,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line.removeprefix("data:").strip()
                    if not payload or payload == "[DONE]":
                        continue
                    data = json.loads(payload)
                    if isinstance(data.get("usage"), dict):
                        usage = data["usage"]
                    choices = data.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta", {}).get("content") or ""
                        chars += len(delta)
            break
        except httpx.HTTPStatusError as exc:
            retry_stats["last_error_class"] = exc.__class__.__name__
            retry_stats["last_status_code"] = exc.response.status_code
            if stream_usage_requested and chars == chars_at_attempt_start and exc.response.status_code in {400, 422}:
                request["json"].pop("stream_options", None)
                stream_usage_requested = False
                stream_usage_fallback = True
                retry_stats["retry_count"] = int(retry_stats.get("retry_count") or 0) + 1
                continue
            return _finish_stream_probe_failure(
                settings,
                started,
                input_chars,
                chars,
                usage_context,
                exc.__class__.__name__,
                request,
                retry_stats,
                stream_usage_requested,
                stream_usage_fallback,
            )
        except Exception as exc:
            retry_stats["last_error_class"] = exc.__class__.__name__
            return _finish_stream_probe_failure(
                settings,
                started,
                input_chars,
                chars,
                usage_context,
                exc.__class__.__name__,
                request,
                retry_stats,
                stream_usage_requested,
                stream_usage_fallback,
            )

    elapsed_ms = (time.perf_counter() - started) * 1000
    usage_available = isinstance(usage, dict)
    _record_usage_event(
        settings=settings,
        call_type="settings_stream_probe",
        status="succeeded",
        latency_ms=elapsed_ms,
        input_chars=input_chars,
        output_chars=chars,
        usage=usage,
        usage_context=usage_context,
        metadata=_stream_usage_metadata(
            request=request,
            retry_stats=retry_stats,
            stream_usage_requested=stream_usage_requested,
            stream_usage_fallback=stream_usage_fallback,
            usage=usage,
        ),
        model_key="chat_model",
    )
    return {
        "status": "succeeded",
        "latency_ms": round(elapsed_ms, 1),
        "error_class": "",
        "usage_available": usage_available,
        "stream_usage_fallback": stream_usage_fallback,
    }


def _finish_stream_probe_failure(
    settings: dict[str, Any],
    started: float,
    input_chars: int,
    chars: int,
    usage_context: dict[str, Any] | None,
    error_class: str,
    request: dict[str, Any],
    retry_stats: dict[str, Any],
    stream_usage_requested: bool,
    stream_usage_fallback: bool,
) -> dict[str, Any]:
    elapsed_ms = (time.perf_counter() - started) * 1000
    _record_usage_event(
        settings=settings,
        call_type="settings_stream_probe",
        status="failed",
        latency_ms=elapsed_ms,
        input_chars=input_chars,
        output_chars=chars,
        usage=None,
        usage_context=usage_context,
        error_class=error_class,
        metadata=_stream_usage_metadata(
            request=request,
            retry_stats=retry_stats,
            stream_usage_requested=stream_usage_requested,
            stream_usage_fallback=stream_usage_fallback,
            usage=None,
        ),
        model_key="chat_model",
    )
    return {
        "status": "failed",
        "latency_ms": round(elapsed_ms, 1),
        "error_class": error_class,
        "usage_available": False,
        "stream_usage_fallback": stream_usage_fallback,
    }


async def _probe_image_understanding(
    settings: dict[str, Any],
    *,
    usage_context: dict[str, Any] | None,
) -> dict[str, Any]:
    messages = [
        {"role": "system", "content": DIAGNOSTIC_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": 'What is the dominant color in this image? Return JSON only: {"dominant_color":"color"}'},
                {"type": "image_url", "image_url": {"url": DIAGNOSTIC_IMAGE_DATA_URL}},
            ],
        },
    ]
    probe = await _probe_chat_once(
        settings,
        user_prompt="image diagnostics",
        system_prompt=DIAGNOSTIC_SYSTEM_PROMPT,
        call_type="settings_image_probe",
        probe_name="image",
        usage_context=usage_context,
        expect_json=True,
        messages_override=messages,
    )
    color = str((probe.get("json_object") or {}).get("dominant_color") or "").lower()
    understood = probe["status"] == "succeeded" and probe.get("json_valid") and "red" in color
    return {
        "status": "succeeded" if understood else "failed",
        "latency_ms": probe["latency_ms"],
        "error_class": probe.get("error_class", ""),
        "json_valid": probe.get("json_valid", False),
        "usage_available": probe.get("usage_available", False),
    }


def _message_chars(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    total += len(item["text"])
    return total


async def embed_text_batch(
    texts: list[str],
    settings: dict[str, Any],
    *,
    role: str | None = None,
    usage_context: dict[str, Any] | None = None,
) -> list[list[float]]:
    """Embed one bounded batch through the configured embedding API."""
    request = build_embedding_request(settings, texts)
    timeout = float(settings.get("timeout_seconds") or 60)
    started = time.perf_counter()
    input_chars = sum(len(text) for text in texts)
    call_type = f"embedding_{role}" if role in {"query", "passage"} else "embedding"
    retry_stats: dict[str, Any] = {}
    try:
        data = await _post_json_with_retry(
            request["url"],
            request["headers"],
            request["json"],
            timeout,
            retry_stats=retry_stats,
        )
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        _record_usage_event(
            settings=settings,
            call_type=call_type,
            status="failed",
            latency_ms=elapsed_ms,
            input_chars=input_chars,
            output_chars=0,
            usage=None,
            usage_context=usage_context,
            error_class=exc.__class__.__name__,
            metadata=_usage_metadata({"text_count": len(texts), "role": role or ""}, retry_stats),
            model_key="embedding_model",
        )
        logger.exception(
            "embedding_api_failed provider=%s model=%s text_count=%s",
            settings.get("embedding_provider") or settings.get("provider") or "openai_compatible",
            settings.get("embedding_model"),
            len(texts),
        )
        raise
    elapsed_ms = (time.perf_counter() - started) * 1000
    _record_usage_event(
        settings=settings,
        call_type=call_type,
        status="succeeded",
        latency_ms=elapsed_ms,
        input_chars=input_chars,
        output_chars=0,
        usage=data.get("usage"),
        usage_context=usage_context,
        metadata=_usage_metadata({"text_count": len(texts), "role": role or ""}, retry_stats),
        model_key="embedding_model",
    )
    logger.info(
        "embedding_api_completed provider=%s model=%s batch_text_count=%s elapsed_ms=%.1f",
        settings.get("embedding_provider") or settings.get("provider") or "openai_compatible",
        settings.get("embedding_model"),
        len(texts),
        elapsed_ms,
    )
    return [item["embedding"] for item in data["data"]]


async def generate_answer(
    question: str,
    chunks: list[dict[str, Any]],
    settings: dict[str, Any],
    *,
    usage_context: dict[str, Any] | None = None,
) -> str:
    """Ask the configured chat model to answer from retrieved chunks only."""
    if not settings.get("chat_model"):
        raise RuntimeError("LLM settings are not configured. Ask an admin to set base URL, API key, and chat model.")

    logger.info("answer_generation_started chunks=%s question_chars=%s", len(chunks), len(question))
    return await chat_completion(
        settings,
        answer_prompt(question, chunks),
        SYSTEM_PROMPT,
        call_type="answer",
        usage_context=usage_context,
    )


async def generate_answer_stream(
    question: str,
    chunks: list[dict[str, Any]],
    settings: dict[str, Any],
    *,
    usage_context: dict[str, Any] | None = None,
):
    """Stream answer text from the configured chat model."""
    if not settings.get("chat_model"):
        raise RuntimeError("LLM settings are not configured. Ask an admin to set base URL, API key, and chat model.")

    logger.info("answer_stream_started chunks=%s question_chars=%s", len(chunks), len(question))
    async for chunk in chat_completion_stream(
        settings,
        answer_prompt(question, chunks),
        SYSTEM_PROMPT,
        call_type="answer_stream",
        usage_context=usage_context,
    ):
        yield chunk


def answer_prompt(question: str, chunks: list[dict[str, Any]]) -> str:
    """Build the grounded answer prompt shared by normal and streaming chat."""
    context = "\n\n".join(
        f"[{index}] {chunk['filename']} - {chunk['location']}\n{chunk['text']}"
        for index, chunk in enumerate(chunks, start=1)
    )
    return f"Source excerpts:\n{context}\n\nQuestion: {question}"


async def rewrite_search_queries(
    question: str,
    history: list[dict[str, str]],
    settings: dict[str, Any],
    *,
    usage_context: dict[str, Any] | None = None,
) -> list[str]:
    """Ask the chat model for retrieval-focused query rewrites."""
    if not settings.get("chat_model"):
        logger.info("query_rewrite_skipped reason=no_chat_settings")
        return [question]

    context = "\n".join(f"{item['role']}: {item['content']}" for item in history[-6:])
    user_prompt = (
        f"Conversation context:\n{context or '(none)'}\n\n"
        f"User question:\n{question}\n\n"
        "Return retrieval queries as JSON."
    )
    try:
        content = await chat_completion(
            settings,
            user_prompt,
            QUERY_REWRITE_PROMPT,
            temperature=0.0,
            call_type="query_rewrite",
            usage_context=usage_context,
        )
        queries = parse_json_strings(content)
    except Exception:
        logger.exception("query_rewrite_failed question_chars=%s history_messages=%s", len(question), len(history))
        queries = []
    rewritten = unique_nonempty([question, *queries])[:5]
    logger.info("query_rewrite_completed input_chars=%s output_queries=%s", len(question), len(rewritten))
    return rewritten


async def rerank_chunks(
    question: str,
    candidates: list[dict[str, Any]],
    settings: dict[str, Any],
    limit: int = 6,
    rerank_weight: float | None = None,
    rerank_base_weight: float | None = None,
    usage_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Ask the chat model to rerank candidate chunks, falling back to hybrid scores.

    ``rerank_weight``/``rerank_base_weight`` override the module defaults for a
    single call (eval workbench per-run params); None keeps current behaviour.
    """
    weight = RERANK_WEIGHT if rerank_weight is None else float(rerank_weight)
    base_weight = RERANK_BASE_WEIGHT if rerank_base_weight is None else float(rerank_base_weight)
    if not candidates:
        return []
    fallback = sorted(candidates, key=lambda item: item["score"], reverse=True)[:limit]
    if not settings.get("chat_model"):
        logger.info("rerank_skipped reason=no_chat_settings candidates=%s returned=%s", len(candidates), len(fallback))
        return fallback

    # Pass full chunk text — chunks are bounded by chunk_text() (~400 chars
    # CJK / ~800 Latin) and earlier truncation at 900 chars sometimes lopped
    # off answer evidence in the tail of a chunk. The extra prompt budget is
    # cheap relative to the rerank call itself.
    excerpts = "\n\n".join(
        f'Candidate {index}\nFile: {chunk["filename"]}\nLocation: {chunk["location"]}\nText: {chunk["text"]}'
        for index, chunk in enumerate(candidates[:RERANK_INPUT_SIZE], start=1)
    )
    user_prompt = f"Question:\n{question}\n\nCandidates:\n{excerpts}"
    try:
        content = await chat_completion(
            settings,
            user_prompt,
            RERANK_PROMPT,
            temperature=0.0,
            call_type="rerank",
            usage_context=usage_context,
        )
        scores = parse_rerank_scores(content)
    except Exception:
        logger.exception("rerank_failed candidates=%s", len(candidates))
        return fallback

    ranked = []
    for index, chunk in enumerate(candidates[:RERANK_INPUT_SIZE], start=1):
        rerank_score = scores.get(index)
        if rerank_score is None:
            continue
        combined = (weight * rerank_score) + (base_weight * chunk["score"])
        ranked.append((combined, {**chunk, "rerank_score": rerank_score, "score": combined}))
    if not ranked:
        logger.warning("rerank_empty_scores candidates=%s", len(candidates))
        return fallback
    ranked.sort(key=lambda item: item[0], reverse=True)
    reranked = [chunk for score, chunk in ranked[:limit] if score > 0]
    logger.info("rerank_completed candidates=%s scored=%s returned=%s", len(candidates), len(ranked), len(reranked))
    return reranked


async def generate_starter_questions(
    excerpts: list[dict[str, Any]],
    settings: dict[str, Any],
    *,
    usage_context: dict[str, Any] | None = None,
) -> list[str]:
    """Ask the chat model for 4 short starter questions grounded in sample excerpts."""
    if not excerpts:
        return []
    if not settings.get("chat_model"):
        logger.info("starter_questions_skipped reason=no_chat_settings")
        return []
    samples = excerpts[:8]
    context = "\n\n".join(
        f"[{index}] {chunk['filename']} - {chunk['location']}\n{chunk['text'][:400]}"
        for index, chunk in enumerate(samples, start=1)
    )
    user_prompt = f"Source excerpts:\n{context}\n\nReturn the JSON array now."
    try:
        content = await chat_completion(
            settings,
            user_prompt,
            STARTER_QUESTIONS_PROMPT,
            temperature=0.6,
            call_type="starter_questions",
            usage_context=usage_context,
        )
        questions = parse_json_strings(content)
    except Exception:
        logger.exception("starter_questions_failed excerpts=%s", len(samples))
        return []
    cleaned = [q for q in (q.strip() for q in questions) if q]
    logger.info("starter_questions_generated excerpts=%s returned=%s", len(samples), len(cleaned[:4]))
    return cleaned[:4]


async def generate_eval_candidates(
    excerpts: list[dict[str, Any]],
    settings: dict[str, Any],
    count: int = 5,
    item_types: list[str] | None = None,
    target_language: str = "Traditional Chinese",
    usage_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Ask the chat model for draft eval items from selected source excerpts."""
    if not excerpts:
        return []
    if not settings.get("chat_model"):
        logger.info("eval_candidates_skipped reason=no_chat_settings")
        return []
    allowed_types = {"answerable", "cross_lingual", "unanswerable"}
    requested_types = [item for item in (item_types or ["answerable"]) if item in allowed_types]
    if not requested_types:
        requested_types = ["answerable"]
    count = max(1, min(int(count), 20))
    samples = excerpts[:12]
    context = "\n\n".join(
        (
            f"Excerpt {index}\n"
            f"source_id: {chunk['source_id']}\n"
            f"chunk_id: {chunk['chunk_id']}\n"
            f"file: {chunk['filename']}\n"
            f"location: {chunk['location']}\n"
            f"text: {chunk['text'][:900]}"
        )
        for index, chunk in enumerate(samples, start=1)
    )
    user_prompt = (
        f"Target question language: {target_language}\n"
        f"Requested item types: {', '.join(requested_types)}\n"
        f"Requested count: {count}\n\n"
        f"Source excerpts:\n{context}\n\n"
        "Return the JSON array now."
    )
    try:
        content = await chat_completion(
            settings,
            user_prompt,
            EVAL_AUTHORING_PROMPT,
            temperature=0.4,
            call_type="eval_authoring",
            usage_context=usage_context,
        )
        candidates = parse_eval_candidates(content)
    except Exception:
        logger.exception("eval_candidates_failed excerpts=%s count=%s", len(samples), count)
        return []
    cleaned = candidates[:count]
    logger.info(
        "eval_candidates_generated excerpts=%s requested=%s returned=%s",
        len(samples),
        count,
        len(cleaned),
    )
    return cleaned


async def suggest_followup_questions(
    question: str,
    answer: str,
    settings: dict[str, Any],
    source_context: list[str] | None = None,
    usage_context: dict[str, Any] | None = None,
) -> list[str]:
    """Suggest up to 3 follow-up questions for the latest answered QA pair (A2)."""
    if not question.strip() or not answer.strip():
        return []
    if not settings.get("chat_model"):
        logger.info("followups_skipped reason=no_chat_settings")
        return []
    target_language = followup_target_language(source_context or [], answer, question)
    excerpts = "\n\n".join(
        f"[{index}] {text.strip()[:500]}"
        for index, text in enumerate(source_context or [], start=1)
        if text and text.strip()
    )
    user_prompt = (
        f"TARGET LANGUAGE: {target_language}\n\n"
        f"Source excerpts:\n{excerpts or '(unavailable)'}\n\n"
        f"User question:\n{question.strip()[:1000]}\n\n"
        f"Assistant answer:\n{answer.strip()[:4000]}\n\n"
        "Return the JSON array now."
    )
    try:
        content = await chat_completion(
            settings,
            user_prompt,
            FOLLOWUP_QUESTIONS_PROMPT,
            temperature=0.6,
            call_type="followups",
            usage_context=usage_context,
        )
        questions = parse_json_strings(content)
    except Exception:
        logger.exception("followups_failed")
        return []
    cleaned = [q for q in (q.strip() for q in questions) if q]
    logger.info("followups_generated returned=%s", len(cleaned[:3]))
    return cleaned[:3]


def followup_target_language(source_context: list[str], answer: str, question: str) -> str:
    """Pick a concrete output language for follow-ups.

    Source excerpts win over Q/A language so English documents still produce
    English follow-ups even when the user asks in Traditional Chinese.
    """
    for text in ("\n".join(source_context), answer, question):
        detected = detect_dominant_language(text)
        if detected:
            return detected
    return "Traditional Chinese"


def detect_dominant_language(text: str) -> str:
    sample = (text or "")[:6000]
    latin = len(re.findall(r"[A-Za-z]", sample))
    han = len(re.findall(r"[\u4e00-\u9fff]", sample))
    kana = len(re.findall(r"[\u3040-\u30ff]", sample))
    simplified_markers = set("这为与时会们问题临床试验药")
    simplified = sum(1 for char in sample if char in simplified_markers)

    if latin >= max(40, (han + kana) * 2):
        return "English"
    if kana >= 8:
        return "Japanese"
    if han >= 12:
        return "Simplified Chinese" if simplified >= 3 else "Traditional Chinese"
    if latin >= 12:
        return "English"
    return ""


MEETING_MINUTES_CONTEXT_CHARS = 16000


async def generate_meeting_minutes(
    chunks: list[dict[str, Any]],
    settings: dict[str, Any],
    *,
    usage_context: dict[str, Any] | None = None,
) -> str:
    """Turn one source's chunks (a transcript) into structured minutes (A1).

    Returns an empty string on missing settings or upstream failure — the
    caller surfaces a friendly error instead.
    """
    if not chunks:
        return ""
    if not settings.get("chat_model"):
        logger.info("meeting_minutes_skipped reason=no_chat_settings")
        return ""
    parts: list[str] = []
    used = 0
    for chunk in chunks:
        text = chunk["text"]
        if used + len(text) > MEETING_MINUTES_CONTEXT_CHARS:
            break
        parts.append(text)
        used += len(text)
    user_prompt = f"Transcript:\n{'\n\n'.join(parts)}\n\nWrite the minutes now."
    try:
        minutes = await chat_completion(
            settings,
            user_prompt,
            MEETING_MINUTES_PROMPT,
            temperature=0.3,
            call_type="meeting_minutes",
            usage_context=usage_context,
        )
    except Exception:
        logger.exception("meeting_minutes_failed chunks=%s", len(chunks))
        return ""
    logger.info("meeting_minutes_generated chunks_used=%s chars=%s", len(parts), len(minutes))
    return minutes.strip()


async def summarize_source(
    chunks: list[dict[str, Any]],
    settings: dict[str, Any],
    *,
    usage_context: dict[str, Any] | None = None,
) -> str:
    """Generate a 2-4 sentence summary of one source from its first chunks.

    Returns an empty string on missing settings or upstream failure — the
    caller (ingest) treats summary generation as best-effort and must not
    fail the source on a summary error.
    """
    if not chunks:
        return ""
    if not settings.get("chat_model"):
        logger.info("source_summary_skipped reason=no_chat_settings")
        return ""
    samples = chunks[:12]
    context = "\n\n".join(
        f"[{index}] {chunk.get('location', '')}\n{chunk['text']}"
        for index, chunk in enumerate(samples, start=1)
    )
    user_prompt = f"Source excerpts:\n{context}\n\nWrite the summary now."
    try:
        content = await chat_completion(
            settings,
            user_prompt,
            SOURCE_SUMMARY_PROMPT,
            temperature=0.3,
            call_type="source_summary",
            usage_context=usage_context,
        )
    except Exception:
        logger.exception("source_summary_failed chunks=%s", len(samples))
        return ""
    summary = (content or "").strip()
    logger.info("source_summary_generated excerpts=%s chars=%s", len(samples), len(summary))
    return summary


async def generate_briefing(
    summaries: list[dict[str, Any]],
    settings: dict[str, Any],
    *,
    usage_context: dict[str, Any] | None = None,
) -> str:
    """Synthesize a one-paragraph briefing across multiple source summaries.

    Each item should look like ``{"filename": str, "summary": str}``. Sources
    with empty summaries are skipped — caller can pass a "(no summary)"
    placeholder if it wants the source mentioned anyway.
    """
    items = [item for item in summaries if (item.get("summary") or "").strip()]
    if not items:
        return ""
    if not settings.get("chat_model"):
        logger.info("briefing_skipped reason=no_chat_settings")
        return ""
    context = "\n\n".join(
        f"[{index}] {item['filename']}\n{item['summary'].strip()}"
        for index, item in enumerate(items, start=1)
    )
    user_prompt = f"Source summaries:\n{context}\n\nWrite the briefing now."
    try:
        content = await chat_completion(
            settings,
            user_prompt,
            NOTEBOOK_BRIEFING_PROMPT,
            temperature=0.4,
            call_type="briefing",
            usage_context=usage_context,
        )
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
    *,
    usage_context: dict[str, Any] | None = None,
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
    if not settings.get("chat_model"):
        logger.info("compare_skipped reason=no_chat_settings")
        return ""
    context = "\n\n".join(
        f"[{index}] {item['filename']}\n{item['summary'].strip()}"
        for index, item in enumerate(items, start=1)
    )
    focus_line = f"Focus: {focus.strip()}\n\n" if (focus or "").strip() else ""
    user_prompt = f"{focus_line}Sources to compare:\n{context}\n\nWrite the comparison now."
    try:
        content = await chat_completion(
            settings,
            user_prompt,
            SOURCE_COMPARE_PROMPT,
            temperature=0.3,
            call_type="compare",
            usage_context=usage_context,
        )
    except Exception:
        logger.exception("compare_failed summaries=%s focus_chars=%s", len(items), len(focus or ""))
        return ""
    comparison = (content or "").strip()
    logger.info("compare_generated summaries=%s chars=%s focus_chars=%s", len(items), len(comparison), len(focus or ""))
    return comparison


async def generate_artifact(
    kind: str,
    summaries: list[dict[str, Any]],
    settings: dict[str, Any],
    *,
    usage_context: dict[str, Any] | None = None,
) -> str:
    """Generate an A4 artifact (study guide / FAQ / timeline) from source summaries.

    ``kind`` must be a key in ``ARTIFACT_PROMPTS``. Mirrors ``generate_briefing``:
    operates on ``[{"filename", "summary"}, ...]`` (empty summaries skipped) and
    returns an empty string on missing settings or upstream failure — the caller
    surfaces a friendly error instead.
    """
    spec = ARTIFACT_PROMPTS.get(kind)
    if spec is None:
        raise ValueError(f"unknown artifact kind: {kind}")
    prompt, temperature, label = spec
    items = [item for item in summaries if (item.get("summary") or "").strip()]
    if not items:
        return ""
    if not settings.get("chat_model"):
        logger.info("artifact_skipped kind=%s reason=no_chat_settings", label)
        return ""
    context = "\n\n".join(
        f"[{index}] {item['filename']}\n{item['summary'].strip()}"
        for index, item in enumerate(items, start=1)
    )
    user_prompt = f"Source summaries:\n{context}\n\nWrite the {label.replace('_', ' ')} now."
    try:
        content = await chat_completion(
            settings,
            user_prompt,
            prompt,
            temperature=temperature,
            call_type=f"artifact_{label}",
            usage_context=usage_context,
        )
    except Exception:
        logger.exception("artifact_failed kind=%s summaries=%s", label, len(items))
        return ""
    artifact = (content or "").strip()
    logger.info("artifact_generated kind=%s summaries=%s chars=%s", label, len(items), len(artifact))
    return artifact


async def translate_summary(
    text: str,
    target_language: str,
    settings: dict[str, Any],
    *,
    usage_context: dict[str, Any] | None = None,
) -> str:
    """Translate a source summary into ``target_language`` (A5).

    Returns an empty string on empty input, missing settings, or upstream
    failure — the caller surfaces a friendly error instead.
    """
    text = (text or "").strip()
    if not text:
        return ""
    if not settings.get("chat_model"):
        logger.info("translate_skipped reason=no_chat_settings")
        return ""
    user_prompt = f"TARGET LANGUAGE: {target_language}\n\nSummary to translate:\n{text}\n\nWrite the translation now."
    try:
        content = await chat_completion(
            settings,
            user_prompt,
            TRANSLATE_SUMMARY_PROMPT,
            temperature=0.2,
            call_type="translate_summary",
            usage_context=usage_context,
        )
    except Exception:
        logger.exception("translate_failed target_language=%s chars=%s", target_language, len(text))
        return ""
    translated = (content or "").strip()
    logger.info("translate_completed target_language=%s in_chars=%s out_chars=%s", target_language, len(text), len(translated))
    return translated


async def chat_completion(
    settings: dict[str, Any],
    user_prompt: str,
    system_prompt: str,
    temperature: float | None = None,
    *,
    call_type: str = "chat_completion",
    usage_context: dict[str, Any] | None = None,
) -> str:
    """Call the configured chat completion endpoint and return message text."""
    request = build_chat_request(settings, user_prompt, system_prompt, temperature)
    timeout = float(settings.get("timeout_seconds") or 60)
    started = time.perf_counter()
    input_chars = len(system_prompt) + len(user_prompt)
    retry_stats: dict[str, Any] = {}
    try:
        data = await _post_json_with_retry(
            request["url"],
            request["headers"],
            request["json"],
            timeout,
            retry_stats=retry_stats,
        )
        content = data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        _record_usage_event(
            settings=settings,
            call_type=call_type,
            status="failed",
            latency_ms=elapsed_ms,
            input_chars=input_chars,
            output_chars=0,
            usage=None,
            usage_context=usage_context,
            error_class=exc.__class__.__name__,
            metadata=_usage_metadata({"temperature": request["json"].get("temperature")}, retry_stats),
            model_key="chat_model",
        )
        logger.exception(
            "chat_completion_failed provider=%s model=%s prompt_chars=%s",
            settings.get("provider") or "openai_compatible",
            settings.get("chat_model"),
            len(user_prompt),
        )
        raise
    elapsed_ms = (time.perf_counter() - started) * 1000
    _record_usage_event(
        settings=settings,
        call_type=call_type,
        status="succeeded",
        latency_ms=elapsed_ms,
        input_chars=input_chars,
        output_chars=len(content),
        usage=data.get("usage"),
        usage_context=usage_context,
        metadata=_usage_metadata({"temperature": request["json"].get("temperature")}, retry_stats),
        model_key="chat_model",
    )
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


async def chat_completion_stream(
    settings: dict[str, Any],
    user_prompt: str,
    system_prompt: str,
    temperature: float | None = None,
    *,
    call_type: str = "chat_stream",
    usage_context: dict[str, Any] | None = None,
):
    """Stream message text from an OpenAI-compatible chat completion endpoint."""
    request = build_chat_request(settings, user_prompt, system_prompt, temperature)
    request["json"]["stream"] = True
    request["json"]["stream_options"] = {"include_usage": True}
    timeout = float(settings.get("timeout_seconds") or 60)
    started = time.perf_counter()
    input_chars = len(system_prompt) + len(user_prompt)
    chars = 0
    usage: dict[str, Any] | None = None
    retry_stats: dict[str, Any] = {}
    stream_usage_requested = True
    stream_usage_fallback = False
    client = get_http_client()
    attempt = 0
    while True:
        attempt += 1
        retry_stats["attempts"] = attempt
        chars_at_attempt_start = chars
        try:
            async with client.stream(
                "POST",
                request["url"],
                headers=request["headers"],
                json=request["json"],
                timeout=timeout,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line.removeprefix("data:").strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        logger.warning("chat_stream_bad_json payload_chars=%s", len(payload))
                        continue
                    if isinstance(data.get("usage"), dict):
                        usage = data["usage"]
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {}).get("content") or ""
                    if delta:
                        chars += len(delta)
                        yield delta
            break
        except httpx.HTTPStatusError as exc:
            retry_stats["last_error_class"] = exc.__class__.__name__
            retry_stats["last_status_code"] = exc.response.status_code
            can_retry_without_duplicate_output = chars == chars_at_attempt_start
            if (
                stream_usage_requested
                and can_retry_without_duplicate_output
                and exc.response.status_code in {400, 422}
            ):
                request["json"].pop("stream_options", None)
                stream_usage_requested = False
                stream_usage_fallback = True
                retry_stats["retry_count"] = int(retry_stats.get("retry_count") or 0) + 1
                logger.warning("chat_stream_usage_option_rejected status=%s retrying_without_usage", exc.response.status_code)
                continue
            if not can_retry_without_duplicate_output or exc.response.status_code not in LLM_RETRYABLE_STATUS or attempt >= LLM_RETRY_MAX_ATTEMPTS:
                _record_stream_usage_failure(
                    settings=settings,
                    call_type=call_type,
                    started=started,
                    input_chars=input_chars,
                    chars=chars,
                    usage_context=usage_context,
                    error_class=exc.__class__.__name__,
                    request=request,
                    retry_stats=retry_stats,
                    stream_usage_requested=stream_usage_requested,
                    stream_usage_fallback=stream_usage_fallback,
                )
                raise
        except httpx.RequestError as exc:
            retry_stats["last_error_class"] = exc.__class__.__name__
            can_retry_without_duplicate_output = chars == chars_at_attempt_start
            if not can_retry_without_duplicate_output or attempt >= LLM_RETRY_MAX_ATTEMPTS:
                _record_stream_usage_failure(
                    settings=settings,
                    call_type=call_type,
                    started=started,
                    input_chars=input_chars,
                    chars=chars,
                    usage_context=usage_context,
                    error_class=exc.__class__.__name__,
                    request=request,
                    retry_stats=retry_stats,
                    stream_usage_requested=stream_usage_requested,
                    stream_usage_fallback=stream_usage_fallback,
                )
                raise
        except Exception as exc:
            retry_stats["last_error_class"] = exc.__class__.__name__
            _record_stream_usage_failure(
                settings=settings,
                call_type=call_type,
                started=started,
                input_chars=input_chars,
                chars=chars,
                usage_context=usage_context,
                error_class=exc.__class__.__name__,
                request=request,
                retry_stats=retry_stats,
                stream_usage_requested=stream_usage_requested,
                stream_usage_fallback=stream_usage_fallback,
            )
            raise
        retry_stats["retry_count"] = int(retry_stats.get("retry_count") or 0) + 1
        delay = LLM_RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1)) + random.uniform(0.0, 0.25)
        logger.warning(
            "chat_stream_retry attempt=%s/%s delay_ms=%.0f url=%s",
            attempt, LLM_RETRY_MAX_ATTEMPTS, delay * 1000, request["url"],
        )
        await asyncio.sleep(delay)
    elapsed_ms = (time.perf_counter() - started) * 1000
    _record_usage_event(
        settings=settings,
        call_type=call_type,
        status="succeeded",
        latency_ms=elapsed_ms,
        input_chars=input_chars,
        output_chars=chars,
        usage=usage,
        usage_context=usage_context,
        metadata=_stream_usage_metadata(
            request=request,
            retry_stats=retry_stats,
            stream_usage_requested=stream_usage_requested,
            stream_usage_fallback=stream_usage_fallback,
            usage=usage,
        ),
        model_key="chat_model",
    )
    logger.info(
        "chat_stream_completed provider=%s model=%s prompt_chars=%s response_chars=%s elapsed_ms=%.1f",
        settings.get("provider") or "openai_compatible",
        settings.get("chat_model"),
        len(user_prompt),
        chars,
        elapsed_ms,
    )


def _record_stream_usage_failure(
    *,
    settings: dict[str, Any],
    call_type: str,
    started: float,
    input_chars: int,
    chars: int,
    usage_context: dict[str, Any] | None,
    error_class: str,
    request: dict[str, Any],
    retry_stats: dict[str, Any],
    stream_usage_requested: bool,
    stream_usage_fallback: bool,
) -> None:
    elapsed_ms = (time.perf_counter() - started) * 1000
    _record_usage_event(
        settings=settings,
        call_type=call_type,
        status="failed",
        latency_ms=elapsed_ms,
        input_chars=input_chars,
        output_chars=chars,
        usage=None,
        usage_context=usage_context,
        error_class=error_class,
        metadata=_stream_usage_metadata(
            request=request,
            retry_stats=retry_stats,
            stream_usage_requested=stream_usage_requested,
            stream_usage_fallback=stream_usage_fallback,
            usage=None,
        ),
        model_key="chat_model",
    )
    logger.exception(
        "chat_stream_failed provider=%s model=%s prompt_chars=%s",
        settings.get("provider") or "openai_compatible",
        settings.get("chat_model"),
        input_chars,
    )


def _usage_metadata(base: dict[str, Any], retry_stats: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = dict(base)
    attempts = int((retry_stats or {}).get("attempts") or 1)
    retry_count = int((retry_stats or {}).get("retry_count") or max(0, attempts - 1))
    metadata["attempts"] = attempts
    metadata["retry_count"] = retry_count
    status_code = (retry_stats or {}).get("last_status_code")
    if status_code is not None:
        metadata["last_status_code"] = status_code
    last_error_class = (retry_stats or {}).get("last_error_class")
    if last_error_class:
        metadata["last_error_class"] = str(last_error_class)
    return metadata


def _stream_usage_metadata(
    *,
    request: dict[str, Any],
    retry_stats: dict[str, Any],
    stream_usage_requested: bool,
    stream_usage_fallback: bool,
    usage: dict[str, Any] | None,
) -> dict[str, Any]:
    return _usage_metadata(
        {
            "stream": True,
            "temperature": request["json"].get("temperature"),
            "stream_usage_requested": stream_usage_requested,
            "stream_usage_fallback": stream_usage_fallback,
            "stream_usage_available": isinstance(usage, dict),
        },
        retry_stats,
    )


def _record_usage_event(
    *,
    settings: dict[str, Any],
    call_type: str,
    status: str,
    latency_ms: float,
    input_chars: int,
    output_chars: int,
    usage: dict[str, Any] | None,
    usage_context: dict[str, Any] | None,
    error_class: str = "",
    metadata: dict[str, Any] | None = None,
    model_key: str = "chat_model",
) -> None:
    # Chat and embedding are independent connections, so the provider recorded
    # must match the model: embedding usage belongs to the embedding provider,
    # not the (possibly different) chat provider.
    if model_key == "embedding_model":
        provider = settings.get("embedding_provider") or settings.get("provider") or "openai_compatible"
    else:
        provider = settings.get("provider") or "openai_compatible"
    model = settings.get(model_key) or ""
    normalized = normalize_usage(usage, input_chars=input_chars, output_chars=output_chars)
    record_llm_usage_event(
        call_type=call_type,
        provider=provider,
        model=model,
        status=status,
        latency_ms=latency_ms,
        usage=normalized,
        context=usage_context,
        error_class=error_class,
        metadata=metadata or {},
    )


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


def parse_eval_candidates(content: str) -> list[dict[str, Any]]:
    """Parse bounded eval candidate objects from model JSON output."""
    parsed = json.loads(extract_json(content))
    if not isinstance(parsed, list):
        return []
    allowed_types = {"answerable", "cross_lingual", "unanswerable"}
    candidates: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        question = " ".join(str(item.get("question") or "").split())[:500]
        if not question:
            continue
        item_type = str(item.get("type") or "answerable").strip().lower()
        if item_type not in allowed_types:
            item_type = "answerable"
        expected_substrings = []
        raw_substrings = item.get("expected_substrings")
        if isinstance(raw_substrings, list):
            for value in raw_substrings:
                text = " ".join(str(value).split())
                if text and text not in expected_substrings:
                    expected_substrings.append(text[:160])
        candidates.append({
            "question": question,
            "item_type": item_type,
            "source_id": optional_int(item.get("source_id")),
            "chunk_id": optional_int(item.get("chunk_id")),
            "expected_answer": " ".join(str(item.get("expected_answer") or "").split())[:800],
            "expected_substrings": expected_substrings[:3],
            "rationale": " ".join(str(item.get("rationale") or "").split())[:300],
        })
    return candidates


def optional_int(value: Any) -> int | None:
    """Best-effort positive integer parser for model-emitted ids."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


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


def chat_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Project the global settings row onto the chat connection.

    Chat keeps using the original top-level columns (``provider`` / ``base_url``
    / ``api_key`` / ``api_version``), so this is mostly a pass-through. It exists
    so chat and embedding resolve symmetrically and request builders never have
    to know which columns belong to which connection.
    """
    return {
        "provider": settings.get("provider") or "openai_compatible",
        "base_url": settings.get("base_url") or "",
        "api_key": settings.get("api_key") or "",
        "api_version": settings.get("api_version") or "",
        "chat_model": settings.get("chat_model") or "",
        "temperature": settings.get("temperature"),
        "timeout_seconds": settings.get("timeout_seconds"),
    }


def embedding_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Project the global settings row onto the embedding connection.

    Embedding has its own ``embedding_provider`` / ``embedding_api_key`` /
    ``embedding_api_version`` columns so it can point at a different service from
    chat (e.g. Gemma chat on one host, e5 embedding on another). When those
    columns are absent — a legacy combined settings dict, e.g. in tests — we fall
    back to the shared chat fields, preserving the previous single-connection
    behaviour. An explicitly-empty embedding API key is honoured (local services
    such as e5 need no key), so the key fallback only triggers when the column is
    missing entirely.
    """
    has_split_key = "embedding_api_key" in settings
    api_key = (settings.get("embedding_api_key") or "") if has_split_key else (settings.get("api_key") or "")
    has_split_version = "embedding_api_version" in settings
    api_version = settings.get("embedding_api_version") if has_split_version else settings.get("api_version")
    base_url = settings.get("embedding_base_url") or settings.get("base_url") or ""
    return {
        "provider": settings.get("embedding_provider") or settings.get("provider") or "openai_compatible",
        "base_url": base_url,
        "embedding_base_url": base_url,
        "api_key": api_key,
        "api_version": api_version or "",
        "embedding_model": settings.get("embedding_model") or "",
        "embedding_query_prefix": settings.get("embedding_query_prefix") or "",
        "embedding_passage_prefix": settings.get("embedding_passage_prefix") or "",
        "timeout_seconds": settings.get("timeout_seconds"),
    }


def _bearer_headers(api_key: str) -> dict[str, str]:
    """OpenAI-style auth header, omitted when no key is set.

    Local services (e5, Ollama, vLLM, TEI) accept requests without a key, so an
    empty key must mean "send no Authorization header", not "send an empty one".
    """
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def build_embedding_request(settings: dict[str, Any], texts: list[str]) -> dict[str, Any]:
    """Build the provider-specific HTTP request for embeddings.

    Resolves the embedding connection first (see ``embedding_settings``), so the
    embedding endpoint, key and provider are independent of chat.
    """
    resolved = embedding_settings(settings)
    provider = resolved.get("provider") or "openai_compatible"
    if provider == "azure_openai":
        return _azure_request(resolved, resolved["embedding_model"], "embeddings", {"input": texts})

    base_url = resolved.get("base_url") or "https://api.openai.com/v1"
    return {
        "url": base_url.rstrip("/") + "/embeddings",
        "headers": _bearer_headers(resolved.get("api_key") or ""),
        "json": {"model": resolved["embedding_model"], "input": texts},
    }


def build_chat_request(
    settings: dict[str, Any],
    user_prompt: str,
    system_prompt: str = SYSTEM_PROMPT,
    temperature: float | None = None,
) -> dict[str, Any]:
    """Build the provider-specific HTTP request for chat completion.

    Resolves the chat connection first (see ``chat_settings``), so the chat
    endpoint, key and provider are independent of embedding.
    """
    resolved = chat_settings(settings)
    effective_temperature = resolved.get("temperature") if temperature is None else temperature
    payload = {
        "temperature": float(0.2 if effective_temperature is None else effective_temperature),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    provider = resolved.get("provider") or "openai_compatible"
    if provider == "azure_openai":
        return _azure_request(resolved, resolved["chat_model"], "chat/completions", payload)

    base_url = resolved.get("base_url") or "https://api.openai.com/v1"
    payload["model"] = resolved["chat_model"]
    return {
        "url": base_url.rstrip("/") + "/chat/completions",
        "headers": _bearer_headers(resolved.get("api_key") or ""),
        "json": payload,
    }


def _azure_request(settings: dict[str, Any], deployment: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Build an Azure OpenAI deployment URL and api-key request payload.

    Expects an already-resolved per-connection settings dict (see
    ``chat_settings`` / ``embedding_settings``). The api-key header is omitted
    when no key is set, matching the OpenAI-compatible path.
    """
    if not settings.get("base_url"):
        raise RuntimeError("Azure OpenAI endpoint is required.")
    if not settings.get("api_version"):
        raise RuntimeError("Azure OpenAI API version is required.")
    base_url = settings["base_url"].rstrip("/")
    url = f"{base_url}/openai/deployments/{deployment}/{operation}?api-version={settings['api_version']}"
    headers = {"api-key": settings["api_key"]} if settings.get("api_key") else {}
    return {
        "url": url,
        "headers": headers,
        "json": payload,
    }
