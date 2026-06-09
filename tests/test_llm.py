import asyncio

import httpx
import pytest

import app.llm as llm
from app.llm import build_chat_request, build_embedding_request, close_http_client, compare_sources, generate_briefing, get_http_client, parse_json_strings, parse_rerank_scores, summarize_source


def test_openai_compatible_request_shapes():
    """OpenAI-compatible settings should produce bearer-auth /v1 requests."""
    settings = {
        "provider": "openai_compatible",
        "base_url": "https://api.example.com/v1/",
        "api_key": "secret",
        "chat_model": "chat-model",
        "embedding_model": "embedding-model",
        "temperature": 0.3,
    }

    chat = build_chat_request(settings, "Question")
    embedding = build_embedding_request(settings, ["Text"])

    assert chat["url"] == "https://api.example.com/v1/chat/completions"
    assert chat["headers"] == {"Authorization": "Bearer secret"}
    assert chat["json"]["model"] == "chat-model"
    assert embedding["url"] == "https://api.example.com/v1/embeddings"
    assert embedding["json"]["model"] == "embedding-model"


def test_azure_openai_request_shapes():
    """Azure OpenAI settings should produce deployment URLs and api-key auth."""
    settings = {
        "provider": "azure_openai",
        "base_url": "https://my-resource.openai.azure.com/",
        "api_key": "secret",
        "chat_model": "chat-deployment",
        "embedding_model": "embedding-deployment",
        "api_version": "2024-02-15-preview",
        "temperature": 0.3,
    }

    chat = build_chat_request(settings, "Question")
    embedding = build_embedding_request(settings, ["Text"])

    assert chat["url"] == (
        "https://my-resource.openai.azure.com/openai/deployments/"
        "chat-deployment/chat/completions?api-version=2024-02-15-preview"
    )
    assert chat["headers"] == {"api-key": "secret"}
    assert "model" not in chat["json"]
    assert embedding["url"] == (
        "https://my-resource.openai.azure.com/openai/deployments/"
        "embedding-deployment/embeddings?api-version=2024-02-15-preview"
    )
    assert embedding["headers"] == {"api-key": "secret"}


def test_model_json_helpers_accept_fenced_output():
    """Retrieval helpers should parse common fenced JSON model responses."""
    queries = parse_json_strings('```json\n["api version", "deployment name"]\n```')
    scores = parse_rerank_scores('```json\n[{"id": 2, "score": 0.8}, {"id": 1, "score": 1.5}]\n```')

    assert queries == ["api version", "deployment name"]
    assert scores == {2: 0.8, 1: 1.0}


def test_shared_http_client_is_reused():
    """LLM HTTP helper should reuse one AsyncClient until it is closed."""
    first = get_http_client()
    second = get_http_client()

    assert first is second

    asyncio.run(close_http_client())


def test_summarize_source_returns_empty_without_settings():
    """summarize_source must not call any API when LLM settings are missing."""
    chunks = [{"location": "page 1", "text": "Some text from a source document."}]
    result = asyncio.run(summarize_source(chunks, {}))
    assert result == ""

    # Empty chunks shortcut returns empty without touching settings.
    assert asyncio.run(summarize_source([], {"api_key": "x", "chat_model": "m"})) == ""


def test_generate_briefing_returns_empty_without_summaries_or_settings():
    """Briefing helper short-circuits on empty summaries or missing settings."""
    assert asyncio.run(generate_briefing([], {"api_key": "x", "chat_model": "m"})) == ""

    summaries = [
        {"filename": "a.pdf", "summary": "Summary A"},
        {"filename": "b.pdf", "summary": "Summary B"},
    ]
    assert asyncio.run(generate_briefing(summaries, {})) == ""

    # Whitespace-only summaries are filtered out.
    assert asyncio.run(
        generate_briefing(
            [{"filename": "x.pdf", "summary": "   "}],
            {"api_key": "x", "chat_model": "m"},
        )
    ) == ""


def test_compare_sources_requires_two_summaries_and_settings():
    """compare_sources short-circuits if fewer than 2 usable summaries or no settings."""
    summaries = [
        {"filename": "a.pdf", "summary": "Summary A"},
        {"filename": "b.pdf", "summary": "Summary B"},
    ]
    # Missing settings -> empty without raising.
    assert asyncio.run(compare_sources(summaries, "", {})) == ""

    # Only one usable summary -> empty.
    assert asyncio.run(
        compare_sources(
            [{"filename": "a.pdf", "summary": "Only one"}],
            "",
            {"api_key": "x", "chat_model": "m"},
        )
    ) == ""


# -------------------- P0-1: concurrent embedding batches --------------------


def test_embed_texts_runs_batches_concurrently_and_in_order(monkeypatch):
    inflight = {"current": 0, "max": 0}

    async def fake_batch(texts, settings):
        inflight["current"] += 1
        inflight["max"] = max(inflight["max"], inflight["current"])
        await asyncio.sleep(0.01)
        inflight["current"] -= 1
        return [[float(ord(text))] for text in texts]

    monkeypatch.setattr(llm, "embed_text_batch", fake_batch)
    settings = {
        "api_key": "x",
        "embedding_model": "e5",
        "embedding_batch_size": 1,        # 1 text per batch -> 5 batches
        "embedding_max_concurrency": 3,
    }
    texts = ["a", "b", "c", "d", "e"]
    out = asyncio.run(llm.embed_texts(texts, settings))

    assert out == [[float(ord(t))] for t in texts]  # order preserved
    assert inflight["max"] >= 2                       # actually ran concurrently
    assert inflight["max"] <= 3                       # but bounded by the cap


# -------------------- P0-3: LLM/embedding HTTP retry + backoff --------------------


def _client_returning(monkeypatch, responses):
    """Inject a mock HTTP client that yields the given responses in sequence."""
    calls = {"n": 0}

    def handler(request):
        index = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        status, body = responses[index]
        return httpx.Response(status, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm.set_http_client(client)

    async def _no_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(llm.asyncio, "sleep", _no_sleep)  # keep the test fast
    return client, calls


def test_post_json_retries_transient_then_succeeds(monkeypatch):
    client, calls = _client_returning(monkeypatch, [(503, {}), (429, {}), (200, {"ok": True})])
    try:
        data = asyncio.run(llm._post_json_with_retry("http://x/v1/embeddings", {}, {"input": ["a"]}, 5.0))
    finally:
        asyncio.run(client.aclose())
        llm.set_http_client(None)
    assert data == {"ok": True}
    assert calls["n"] == 3


def test_post_json_gives_up_after_max_attempts(monkeypatch):
    client, calls = _client_returning(monkeypatch, [(500, {"error": "boom"})])
    try:
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(llm._post_json_with_retry("http://x", {}, {}, 5.0))
    finally:
        asyncio.run(client.aclose())
        llm.set_http_client(None)
    assert calls["n"] == llm.LLM_RETRY_MAX_ATTEMPTS


def test_post_json_does_not_retry_on_4xx_request_error(monkeypatch):
    client, calls = _client_returning(monkeypatch, [(400, {"error": "bad request"})])
    try:
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(llm._post_json_with_retry("http://x", {}, {}, 5.0))
    finally:
        asyncio.run(client.aclose())
        llm.set_http_client(None)
    assert calls["n"] == 1  # 400 is not retryable
