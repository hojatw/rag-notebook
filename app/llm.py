import hashlib
import json
import logging
import math
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

logger = logging.getLogger(__name__)
EMBEDDING_BATCH_SIZE = 64
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


async def close_http_client() -> None:
    """Close the shared HTTP client when the app shuts down."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


def local_embedding(text: str, dimensions: int = 384) -> list[float]:
    """Create a deterministic local embedding for offline demonstrations."""
    vector = [0.0] * dimensions
    tokens = [t for t in "".join(c.lower() if c.isalnum() else " " for c in text).split() if t]
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 else -1.0
        vector[bucket] += sign
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def cosine(a: list[float], b: list[float]) -> float:
    """Return cosine similarity for normalized embedding vectors."""
    if not a or not b:
        return 0.0
    limit = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(limit))


async def embed_texts(texts: list[str], settings: dict[str, Any]) -> list[list[float]]:
    """Embed texts using configured API settings, or a local fallback."""
    if not settings.get("api_key") or not settings.get("embedding_model"):
        logger.info("embedding_local_fallback text_count=%s", len(texts))
        return [local_embedding(text) for text in texts]

    batch_size = int(settings.get("embedding_batch_size") or EMBEDDING_BATCH_SIZE)
    embeddings: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        embeddings.extend(await embed_text_batch(texts[start : start + batch_size], settings))
    return embeddings


async def embed_text_batch(texts: list[str], settings: dict[str, Any]) -> list[list[float]]:
    """Embed one bounded batch through the configured embedding API."""
    request = build_embedding_request(settings, texts)
    timeout = float(settings.get("timeout_seconds") or 60)
    started = time.perf_counter()
    try:
        client = get_http_client()
        response = await client.post(request["url"], headers=request["headers"], json=request["json"], timeout=timeout)
        response.raise_for_status()
        data = response.json()
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

    # Pass full chunk text — chunks are bounded at ~1200 chars by chunk_text(),
    # and earlier truncation at 900 chars sometimes lopped off the answer
    # evidence in the tail of a chunk. The extra ~6 KB / call is cheap.
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
        client = get_http_client()
        response = await client.post(request["url"], headers=request["headers"], json=request["json"], timeout=timeout)
        response.raise_for_status()
        data = response.json()
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
    """Build the provider-specific HTTP request for embeddings."""
    provider = settings.get("provider") or "openai_compatible"
    if provider == "azure_openai":
        return _azure_request(settings, settings["embedding_model"], "embeddings", {"input": texts})

    base_url = settings.get("base_url") or "https://api.openai.com/v1"
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
