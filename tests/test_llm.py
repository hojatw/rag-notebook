import asyncio
import importlib

import httpx
import pytest

import app.llm as llm
from app.llm import build_chat_request, build_embedding_request, chat_settings, close_http_client, compare_sources, embedding_settings, generate_briefing, get_http_client, parse_eval_candidates, parse_json_strings, parse_rerank_scores, summarize_source


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


def test_empty_api_key_omits_auth_header():
    """Local services (e5 / Ollama) need no key — send no auth header at all."""
    openai_settings = {
        "provider": "openai_compatible",
        "base_url": "http://localhost:8001/v1",
        "api_key": "",
        "chat_model": "chat-model",
        "embedding_model": "embedding-model",
        "embedding_api_key": "",
    }
    chat = build_chat_request(openai_settings, "Question")
    embedding = build_embedding_request(openai_settings, ["Text"])
    assert chat["headers"] == {}
    assert embedding["headers"] == {}

    azure_settings = {
        "provider": "azure_openai",
        "base_url": "https://r.openai.azure.com",
        "api_key": "",
        "chat_model": "chat-deployment",
        "api_version": "2024-02-15-preview",
    }
    azure_chat = build_chat_request(azure_settings, "Question")
    assert azure_chat["headers"] == {}


def test_chat_and_embedding_use_independent_connections():
    """A split settings row routes chat and embedding to their own endpoints/keys."""
    settings = {
        "provider": "azure_openai",
        "base_url": "https://chat.openai.azure.com",
        "api_key": "chat-key",
        "api_version": "2024-02-15-preview",
        "chat_model": "gpt",
        "embedding_provider": "openai_compatible",
        "embedding_base_url": "http://10.0.0.1:8001/v1",
        "embedding_api_key": "embed-key",
        "embedding_model": "intfloat/multilingual-e5-large",
    }

    chat = build_chat_request(settings, "Q")
    embedding = build_embedding_request(settings, ["T"])

    # Chat → Azure deployment + api-key header.
    assert chat["url"].startswith("https://chat.openai.azure.com/openai/deployments/gpt/")
    assert chat["headers"] == {"api-key": "chat-key"}
    # Embedding → independent OpenAI-compatible host + bearer with its own key.
    assert embedding["url"] == "http://10.0.0.1:8001/v1/embeddings"
    assert embedding["headers"] == {"Authorization": "Bearer embed-key"}


def test_embedding_settings_honours_empty_split_key():
    """An explicitly-empty embedding key is kept (not inherited from chat)."""
    resolved = embedding_settings({
        "provider": "openai_compatible",
        "api_key": "chat-key",
        "embedding_provider": "openai_compatible",
        "embedding_base_url": "http://e5/v1",
        "embedding_api_key": "",
        "embedding_model": "e5",
    })
    assert resolved["api_key"] == ""
    assert resolved["base_url"] == "http://e5/v1"


def test_embedding_settings_falls_back_to_chat_for_legacy_dict():
    """A combined dict without embedding_* columns reuses the chat connection."""
    resolved = embedding_settings({
        "provider": "azure_openai",
        "base_url": "https://legacy",
        "api_key": "shared-key",
        "api_version": "2099-01-01",
        "embedding_model": "ada",
    })
    assert resolved["provider"] == "azure_openai"
    assert resolved["api_key"] == "shared-key"
    assert resolved["api_version"] == "2099-01-01"


def test_embedding_connection_backfilled_from_legacy_shared_fields(monkeypatch, tmp_path):
    """Upgrading a pre-split DB copies the shared chat connection into embedding."""
    monkeypatch.setenv("NOTEBOOKLM_DATA_DIR", str(tmp_path / "data"))
    import app.db as db
    importlib.reload(db)
    # Pre-create the OLD (pre-split) llm_settings schema, then run migrations.
    with db.connect() as conn:
        conn.execute(
            """
            CREATE TABLE llm_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                provider TEXT NOT NULL DEFAULT 'openai_compatible',
                base_url TEXT NOT NULL DEFAULT '',
                embedding_base_url TEXT NOT NULL DEFAULT '',
                api_key TEXT NOT NULL DEFAULT '',
                chat_model TEXT NOT NULL DEFAULT '',
                embedding_model TEXT NOT NULL DEFAULT '',
                api_version TEXT NOT NULL DEFAULT '2024-02-15-preview',
                temperature REAL NOT NULL DEFAULT 0.2,
                timeout_seconds REAL NOT NULL DEFAULT 60
            )
            """
        )
        conn.execute(
            "INSERT INTO llm_settings (id, provider, base_url, api_key, chat_model, embedding_model, api_version) "
            "VALUES (1, 'azure_openai', 'https://legacy', ?, 'gpt', 'ada', '2099-01-01')",
            (db.encrypt_for_storage("legacy-key"),),
        )
        conn.commit()

    db.init_db()

    with db.connect() as conn:
        row = dict(conn.execute("SELECT * FROM llm_settings WHERE id = 1").fetchone())
        decrypted = db.load_llm_settings(conn)
    # Existing chat connection untouched; embedding backfilled from it.
    assert row["embedding_provider"] == "azure_openai"
    assert row["embedding_api_version"] == "2099-01-01"
    assert row["embedding_base_url"] == "https://legacy"
    assert decrypted["embedding_api_key"] == "legacy-key"
    importlib.reload(db)


def test_usage_event_records_embedding_provider_not_chat(monkeypatch):
    """Embedding usage events must log the embedding connection's provider."""
    captured = {}

    def fake_record(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(llm, "record_llm_usage_event", fake_record)
    settings = {
        "provider": "azure_openai",          # chat connection
        "embedding_provider": "openai_compatible",  # embedding connection
        "chat_model": "gpt",
        "embedding_model": "e5",
    }

    llm._record_usage_event(
        settings=settings, call_type="embedding", status="succeeded",
        latency_ms=1.0, input_chars=1, output_chars=0, usage=None,
        usage_context=None, model_key="embedding_model",
    )
    assert captured["provider"] == "openai_compatible"
    assert captured["model"] == "e5"

    captured.clear()
    llm._record_usage_event(
        settings=settings, call_type="chat", status="succeeded",
        latency_ms=1.0, input_chars=1, output_chars=0, usage=None,
        usage_context=None, model_key="chat_model",
    )
    assert captured["provider"] == "azure_openai"
    assert captured["model"] == "gpt"


def test_model_json_helpers_accept_fenced_output():
    """Retrieval helpers should parse common fenced JSON model responses."""
    queries = parse_json_strings('```json\n["api version", "deployment name"]\n```')
    scores = parse_rerank_scores('```json\n[{"id": 2, "score": 0.8}, {"id": 1, "score": 1.5}]\n```')

    assert queries == ["api version", "deployment name"]
    assert scores == {2: 0.8, 1: 1.0}


def test_parse_eval_candidates_bounds_model_output():
    """E1e-1: eval authoring parser accepts fenced JSON and normalizes unsafe fields."""
    candidates = parse_eval_candidates(
        """```json
        [
          {
            "question": "  請問 alpha 的重點？  ",
            "type": "cross_lingual",
            "source_id": "3",
            "chunk_id": "4",
            "expected_answer": "alpha answer",
            "expected_substrings": [" alpha evidence ", "alpha evidence"],
            "rationale": "good coverage"
          },
          {"question": "unanswerable?", "type": "made_up", "source_id": null, "chunk_id": null}
        ]
        ```"""
    )

    assert candidates[0]["question"] == "請問 alpha 的重點？"
    assert candidates[0]["item_type"] == "cross_lingual"
    assert candidates[0]["source_id"] == 3
    assert candidates[0]["chunk_id"] == 4
    assert candidates[0]["expected_substrings"] == ["alpha evidence"]
    assert candidates[1]["item_type"] == "answerable"


def test_shared_http_client_is_reused():
    """LLM HTTP helper should reuse one AsyncClient until it is closed."""
    first = get_http_client()
    second = get_http_client()

    assert first is second

    asyncio.run(close_http_client())


def test_generation_prompts_carry_strong_language_rule():
    """Starter questions must follow the source language like summary/briefing do.

    Regression guard: a weak one-line rule (only a CJK example) made the model
    emit Chinese questions for English sources. All three generation prompts
    should pin every supported language explicitly and forbid translation.
    """
    prompts = [llm.STARTER_QUESTIONS_PROMPT, llm.SOURCE_SUMMARY_PROMPT, llm.NOTEBOOK_BRIEFING_PROMPT]
    # A4 artifact prompts must follow the same rule so an English notebook never
    # gets a Chinese study guide / FAQ / timeline.
    prompts += [prompt for prompt, _temp, _label in llm.ARTIFACT_PROMPTS.values()]
    for prompt in prompts:
        assert "Do NOT translate" in prompt
        assert "English" in prompt
        assert "Traditional Chinese" in prompt


def test_followup_prompt_uses_source_language_context(monkeypatch):
    """Follow-up questions should follow source language, not just the user's question."""
    captured = {}

    async def fake_chat(settings, user_prompt, system_prompt, temperature=None, **kwargs):
        captured["user_prompt"] = user_prompt
        captured["system_prompt"] = system_prompt
        return '["What evidence supports the conclusion?"]'

    monkeypatch.setattr(llm, "chat_completion", fake_chat)
    result = asyncio.run(
        llm.suggest_followup_questions(
            "請摘要這份文件",
            "這份文件主要討論臨床研究。",
            {"api_key": "sk-test", "chat_model": "chat"},
            ["This clinical study report discusses safety and efficacy."],
        )
    )

    assert result == ["What evidence supports the conclusion?"]
    assert "Source excerpts" in captured["user_prompt"]
    assert "TARGET LANGUAGE: English" in captured["user_prompt"]
    assert "This clinical study report" in captured["user_prompt"]
    assert "TARGET LANGUAGE overrides" in captured["system_prompt"]


def test_followup_target_language_prefers_source_context():
    assert llm.followup_target_language(
        ["This clinical study report discusses safety and efficacy."],
        "這份文件主要討論臨床研究。",
        "請摘要這份文件",
    ) == "English"


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


def test_generate_artifact_dispatches_and_short_circuits(monkeypatch):
    """A4: generate_artifact picks the right prompt, skips on no summaries/settings,
    and rejects unknown kinds."""
    captured = {}

    async def fake_chat(settings, user_prompt, system_prompt, temperature=None, **kwargs):
        captured["system_prompt"] = system_prompt
        captured["temperature"] = temperature
        return "## Key concepts\n- alpha"

    monkeypatch.setattr(llm, "chat_completion", fake_chat)
    summaries = [{"filename": "a.pdf", "summary": "Summary A"}]
    settings = {"api_key": "x", "chat_model": "m"}

    out = asyncio.run(llm.generate_artifact("study_guide", summaries, settings))
    assert out == "## Key concepts\n- alpha"
    assert captured["system_prompt"] is llm.STUDY_GUIDE_PROMPT

    # No usable summaries -> empty, no LLM call.
    assert asyncio.run(llm.generate_artifact("faq", [{"filename": "x", "summary": " "}], settings)) == ""
    # Missing settings -> empty.
    assert asyncio.run(llm.generate_artifact("timeline", summaries, {})) == ""
    # Unknown kind -> ValueError.
    with pytest.raises(ValueError):
        asyncio.run(llm.generate_artifact("nope", summaries, settings))


def test_translate_summary_dispatches_and_short_circuits(monkeypatch):
    """A5: translate_summary passes the target language and short-circuits cleanly."""
    captured = {}

    async def fake_chat(settings, user_prompt, system_prompt, temperature=None, **kwargs):
        captured["user_prompt"] = user_prompt
        captured["system_prompt"] = system_prompt
        return "Translated."

    monkeypatch.setattr(llm, "chat_completion", fake_chat)
    settings = {"api_key": "x", "chat_model": "m"}

    out = asyncio.run(llm.translate_summary("一段摘要", "English", settings))
    assert out == "Translated."
    assert captured["system_prompt"] is llm.TRANSLATE_SUMMARY_PROMPT
    assert "TARGET LANGUAGE: English" in captured["user_prompt"]

    # Empty text or missing settings -> empty, no LLM call.
    assert asyncio.run(llm.translate_summary("  ", "English", settings)) == ""
    assert asyncio.run(llm.translate_summary("text", "English", {})) == ""


# -------------------- P0-1: concurrent embedding batches --------------------


def test_embed_texts_runs_batches_concurrently_and_in_order(monkeypatch):
    inflight = {"current": 0, "max": 0}

    async def fake_batch(texts, settings, **kwargs):
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


# -------------------- Q0-1: e5 query/passage prefix --------------------


def _capture_batch(monkeypatch):
    captured = {}

    async def fake_batch(texts, settings, **kwargs):
        captured["texts"] = list(texts)
        return [[0.0] for _ in texts]

    monkeypatch.setattr(llm, "embed_text_batch", fake_batch)
    return captured


def test_embed_texts_applies_role_prefix_when_configured(monkeypatch):
    captured = _capture_batch(monkeypatch)
    settings = {
        "api_key": "x",
        "embedding_model": "e5",
        "embedding_query_prefix": "query: ",
        "embedding_passage_prefix": "passage: ",
    }
    asyncio.run(llm.embed_texts(["a", "b"], settings, role="passage"))
    assert captured["texts"] == ["passage: a", "passage: b"]

    asyncio.run(llm.embed_texts(["weather"], settings, role="query"))
    assert captured["texts"] == ["query: weather"]


def test_embed_texts_is_model_agnostic_without_prefix(monkeypatch):
    captured = _capture_batch(monkeypatch)
    # No prefix configured (e.g. OpenAI) -> text is sent unchanged.
    asyncio.run(llm.embed_texts(["a"], {"api_key": "x", "embedding_model": "oai"}, role="passage"))
    assert captured["texts"] == ["a"]

    # role=None never prefixes, even if a prefix is configured (e.g. the dim probe).
    settings = {"api_key": "x", "embedding_model": "e5", "embedding_passage_prefix": "passage: "}
    asyncio.run(llm.embed_texts(["a"], settings))
    assert captured["texts"] == ["a"]


def test_embed_texts_adds_missing_separator_space(monkeypatch):
    captured = _capture_batch(monkeypatch)
    # Users typically type the prefix without the trailing space — it's added.
    typed_without_space = {
        "api_key": "x",
        "embedding_model": "e5",
        "embedding_query_prefix": "query:",
        "embedding_passage_prefix": "passage:",
    }
    asyncio.run(llm.embed_texts(["weather"], typed_without_space, role="query"))
    assert captured["texts"] == ["query: weather"]
    asyncio.run(llm.embed_texts(["chunk"], typed_without_space, role="passage"))
    assert captured["texts"] == ["passage: chunk"]

    # An existing trailing space is respected, not doubled.
    with_space = {"api_key": "x", "embedding_model": "e5", "embedding_query_prefix": "query: "}
    asyncio.run(llm.embed_texts(["weather"], with_space, role="query"))
    assert captured["texts"] == ["query: weather"]


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


def test_chat_completion_stream_yields_delta_content():
    settings = {
        "provider": "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "api_key": "secret",
        "chat_model": "chat-model",
    }

    def handler(request):
        assert request.url.path == "/v1/chat/completions"
        body = request.read().decode()
        assert '"stream":true' in body.replace(" ", "")
        return httpx.Response(
            200,
            text=(
                'data: {"prompt_filter_results":[],"choices":[]}\n\n'
                'data: {"choices":[{"delta":{"content":"你"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm.set_http_client(client)

    async def collect():
        chunks = []
        async for chunk in llm.chat_completion_stream(settings, "Question", llm.SYSTEM_PROMPT):
            chunks.append(chunk)
        return chunks

    try:
        assert asyncio.run(collect()) == ["你", "好"]
    finally:
        asyncio.run(client.aclose())
        llm.set_http_client(None)
