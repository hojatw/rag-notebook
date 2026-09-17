import asyncio
import importlib

import httpx
import pytest

import app.llm as llm
from app.llm import build_chat_request, build_embedding_request, chat_settings, close_http_client, compare_sources, embedding_settings, generate_briefing, get_http_client, parse_answer_judge, parse_eval_candidates, parse_json_strings, parse_rerank_scores, summarize_source


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


def test_runtime_llm_settings_loads_persisted_diagnostics(monkeypatch, tmp_path):
    """The request builder must see the capability probe stored in diagnostics_json.

    Display settings already decoded the JSON blob, but runtime settings did not.
    That made the settings page report an adapted request shape while every real
    LLM call silently fell back to the unprobed defaults.
    """
    monkeypatch.setenv("NOTEBOOKLM_DATA_DIR", str(tmp_path / "data"))
    import app.db as db

    importlib.reload(db)
    db.init_db()
    diagnostics = {
        "chat": {
            "capabilities": {
                "sampling_params": {"status": "failed", "temperature_accepted": False},
            }
        }
    }
    with db.connect() as conn:
        conn.execute(
            "UPDATE llm_settings SET diagnostics_json = ? WHERE id = 1",
            (db.dumps(diagnostics),),
        )
        loaded = db.load_llm_settings(conn)

    assert loaded is not None
    assert loaded["diagnostics"] == diagnostics
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


def test_parse_answer_judge_normalizes_valid_output():
    """E1e-2: judge parser accepts fenced JSON and clamps/normalizes each dimension."""
    result = parse_answer_judge(
        """```json
        {
          "answer_quality": {"label": "PARTIAL", "score": 1.7, "rationale": "  mostly right  "},
          "groundedness": {"score": -0.2, "unsupported_claims": [" claim one ", "", "claim one"], "rationale": "one gap"},
          "citation_correctness": {"score": 0.5, "wrong_citations": ["2", 3, "x"], "rationale": "marker 2 wrong"}
        }
        ```"""
    )

    assert result["judge_ok"] is True
    assert result["answer_quality"]["label"] == "partial"
    assert result["answer_quality"]["score"] == 1.0  # clamped into [0, 1]
    assert result["answer_quality"]["rationale"] == "mostly right"
    assert result["groundedness"]["score"] == 0.0  # negative clamped
    # Whitespace collapsed; duplicates preserved (dedup is not this layer's job) but empties dropped.
    assert result["groundedness"]["unsupported_claims"] == ["claim one", "claim one"]
    assert result["citation_correctness"]["wrong_citations"] == [2, 3]  # non-numeric skipped


def test_parse_answer_judge_flags_bad_json():
    """Malformed model output must yield judge_ok=False with a neutral, stable shape."""
    result = parse_answer_judge("sorry, I cannot output JSON")

    assert result["judge_ok"] is False
    assert result["answer_quality"] == {"label": "", "score": 0.0, "rationale": ""}
    assert result["groundedness"]["unsupported_claims"] == []
    assert result["citation_correctness"]["wrong_citations"] == []


def test_refusal_markers_stay_pinned_to_system_prompt():
    """E1e-2 guard: eval refusal detection matches wording that SYSTEM_PROMPT pins.

    The coupling is deliberate — the eval must detect the *same* refusal the production
    answer path produces. This test makes changing the prompt's refusal wording fail
    loudly instead of silently breaking abstain measurement.
    """
    assert llm.REFUSAL_MARKERS, "at least one refusal marker must be defined"
    for marker in llm.REFUSAL_MARKERS:
        assert marker.casefold() in llm.SYSTEM_PROMPT.casefold(), (
            f"refusal marker {marker!r} no longer appears in SYSTEM_PROMPT — update "
            "REFUSAL_MARKERS together with the prompt, or eval abstain metrics will under-report"
        )


def test_eval_generation_and_judge_use_distinct_call_types(monkeypatch):
    """E1e-2 telemetry (G1a): eval answer generation records call_type=eval_answer and
    the judge records eval_judge, so their LLM usage is separable from live chat."""
    seen = []

    async def fake_chat_completion(
        settings,
        user_prompt,
        system_prompt,
        temperature=None,
        *,
        intent=None,
        call_type="chat_completion",
        usage_context=None,
    ):
        seen.append(call_type)
        # Return valid judge JSON so parse_answer_judge succeeds for the judge path.
        return (
            '{"answer_quality": {"label": "correct", "score": 1.0, "rationale": "ok"},'
            ' "groundedness": {"score": 1.0, "unsupported_claims": [], "rationale": "ok"},'
            ' "citation_correctness": {"score": 1.0, "wrong_citations": [], "rationale": "ok"}}'
        )

    monkeypatch.setattr(llm, "chat_completion", fake_chat_completion)
    settings = {"chat_model": "m"}
    chunks = [{"filename": "f", "location": "l", "text": "t"}]

    asyncio.run(llm.generate_answer("q", chunks, settings, call_type="eval_answer"))
    asyncio.run(llm.judge_answer(
        question="q", generated_answer="a [1]", expected_answer="a",
        item_type="answerable", retrieved_chunks=chunks, settings=settings,
    ))

    assert seen == ["eval_answer", "eval_judge"]


def test_parse_answer_judge_rejects_missing_dimension_or_bad_label():
    """A dropped dimension or an out-of-vocabulary label counts as a parse failure."""
    missing = parse_answer_judge(
        '{"answer_quality": {"label": "correct", "score": 1.0}, "groundedness": {"score": 1.0}}'
    )
    bad_label = parse_answer_judge(
        '{"answer_quality": {"label": "great"}, "groundedness": {"score": 1.0}, '
        '"citation_correctness": {"score": 1.0}}'
    )

    assert missing["judge_ok"] is False  # citation_correctness absent
    assert bad_label["judge_ok"] is False  # "great" is not a valid label


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


def test_compare_sources_treats_missing_topic_evidence_as_metadata(monkeypatch):
    """U17: the real compare prompt must not turn an empty retrieval into facts."""
    captured = {}

    async def fake_chat(settings, user_prompt, system_prompt, **kwargs):
        captured["user_prompt"] = user_prompt
        captured["system_prompt"] = system_prompt
        captured["call_type"] = kwargs.get("call_type")
        return "## 共同點\n- 有依據的比較"

    monkeypatch.setattr(llm, "chat_completion", fake_chat)
    evidence = [
        {"filename": "a.pdf", "summary": "[page 2]\n風險包含供應鏈中斷。"},
        {"filename": "b.pdf", "summary": "[NO_RELEVANT_TOPIC_EVIDENCE]"},
    ]

    result = asyncio.run(
        compare_sources(evidence, "風險控管", {"api_key": "x", "chat_model": "m"})
    )

    assert result.startswith("## 共同點")
    assert "Focus: 風險控管" in captured["user_prompt"]
    assert "[NO_RELEVANT_TOPIC_EVIDENCE]" in captured["user_prompt"]
    assert "do not infer or fill in its position" in captured["system_prompt"]
    assert captured["call_type"] == "compare"


@pytest.mark.parametrize("focus", ["", "保固期間"])
def test_compare_sources_requests_neutral_evidence_based_sections(monkeypatch, focus):
    """Pin the prompt sent by both comparison modes, not a model's reasoning."""
    captured = {}
    response = "## 差異\n- 保固期間：a-v1.pdf 一年；b-v2.pdf 三年。"

    async def fake_chat(settings, user_prompt, system_prompt, **kwargs):
        captured["user"] = user_prompt
        captured["system"] = system_prompt
        return response

    monkeypatch.setattr(llm, "chat_completion", fake_chat)
    sources = [
        {"filename": "a-v1.pdf", "summary": "[page 2]\n保固期間為一年。"},
        {"filename": "b-v2.pdf", "summary": "[page 3]\n保固期間為三年。"},
    ]
    result = asyncio.run(compare_sources(sources, focus, {"chat_model": "m"}))

    assert result == response
    if focus:
        assert f"Focus: {focus}" in captured["user"]
    else:
        assert "Focus:" not in captured["user"]
    for source in sources:
        assert source["filename"] in captured["user"]
        assert source["summary"] in captured["user"]
    prompt = captured["system"]
    for headings in (
        "## 共同點 / ## 差異 / ## 待釐清",
        "## 共同点 / ## 差异 / ## 待澄清",
        "## 共通点 / ## 相違点 / ## 要確認",
        "## Shared / ## Differences / ## To clarify",
    ):
        assert headings in prompt
    for old_heading in (
        "各自獨特之處", "矛盾之處", "各自独特之处", "矛盾之处",
        "それぞれの特徴", "矛盾点", "## Distinct", "## Contradictions",
    ):
        assert old_heading not in prompt
    for rule in (
        "one bullet per comparison dimension",
        "cite filenames and excerpt locations when provided",
        "Do not assume that a shared topic means the same project or subject",
        "Different projects or versions can legitimately have different terms",
        "same subject, scope, conditions, and effective period",
        "Do not infer authority or supersession from filenames or version numbers alone",
        "Missing evidence does not prove that the source lacks a provision",
        "not an exhaustive full-document comparison",
        "source-level identifiers, not chunk numbers or references inside a document",
        "Every comparison bullet must name the supporting source IDs and filenames",
        "If a point is shared only by a subset, explicitly label it as a partial commonality",
        "Do not write only 'both reports', 'two sources', or 'all sources'",
        "Account for every source with usable evidence",
        "Shared requires support from at least two selected source documents",
        "not similarities between groups, products, or treatments within one document",
        "Do not claim three sources agree when only two are cited",
    ):
        assert rule in prompt


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


#: A real vLLM refusal, shortened. This is the string the customer had to read
#: out of the embedding container's own log because the app dropped it.
VLLM_TOKEN_LIMIT_ERROR = (
    "This model's maximum context length is 512 tokens. However, you requested "
    "0 output tokens and your prompt contains at least 513 input tokens, for a "
    "total of at least 513 tokens. Please reduce the length of the input prompt."
)


def test_http_error_keeps_the_provider_explanation(monkeypatch):
    """The response body is the only place that says *why* a call was refused.

    httpx's message stops at the status line, so before this the ingest failure
    that reached `sources.error` and `logs/app.log` was
    "Client error '400 Bad Request'" and nothing else — the token-limit message
    it was raised for survived only in the provider's container log.
    """
    client, _ = _client_returning(
        monkeypatch, [(400, {"object": "error", "message": VLLM_TOKEN_LIMIT_ERROR})]
    )
    try:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            asyncio.run(llm._post_json_with_retry("http://e5.test/v1/embeddings", {}, {}, 5.0))
    finally:
        asyncio.run(client.aclose())
        llm.set_http_client(None)

    message = str(caught.value)
    assert "maximum context length is 512 tokens" in message
    assert "513 input tokens" in message
    assert "http://e5.test/v1/embeddings" in message
    # The exception TYPE is telemetry vocabulary: `_record_usage_event` stores
    # `exc.__class__.__name__` as `error_class`, and every caller catches
    # httpx.HTTPStatusError. Enriching the message must not smuggle in a subclass.
    assert caught.value.__class__ is httpx.HTTPStatusError
    assert caught.value.response.status_code == 400


def test_http_error_detail_is_bounded(monkeypatch):
    """A provider that echoes the rejected request must not paste it into the log.

    `sources.error` is capped at 500 chars and the app log is on disk, so the
    body goes in trimmed rather than whole.
    """
    client, _ = _client_returning(monkeypatch, [(400, {"message": "x" * 5000})])
    try:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            asyncio.run(llm._post_json_with_retry("http://e5.test/v1/embeddings", {}, {}, 5.0))
    finally:
        asyncio.run(client.aclose())
        llm.set_http_client(None)

    message = str(caught.value)
    assert len(message) <= llm.config.llm_retry.error_body_chars + 120
    assert message.endswith("\u2026")


def test_retryable_status_still_carries_detail_when_it_finally_gives_up(monkeypatch):
    """A 5xx that exhausts retries is the other case an operator has to debug."""
    client, calls = _client_returning(monkeypatch, [(503, {"message": "no free GPU slots"})])
    try:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            asyncio.run(llm._post_json_with_retry("http://e5.test/v1/embeddings", {}, {}, 5.0))
    finally:
        asyncio.run(client.aclose())
        llm.set_http_client(None)

    assert calls["n"] == llm.LLM_RETRY_MAX_ATTEMPTS
    assert "no free GPU slots" in str(caught.value)


def test_streaming_http_error_keeps_the_provider_explanation():
    """A streamed 4xx must carry its reason too, and that is timing-sensitive.

    A streamed response holds no body when the status is checked, and httpx
    closes it as the `client.stream(...)` block exits — so the body has to be
    pulled *inside* that block. Read it from the outer `except` and this comes
    back empty, which is exactly the regression this pins.

    The endpoint answers 400 twice on purpose: the first one is consumed by the
    benign `stream_options` fallback (a provider that rejects usage reporting),
    so this also proves that retry still happens before the give-up path.
    """
    settings = {
        "provider": "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "chat_model": "chat-model",
    }
    reason = "This model's maximum context length is 8192 tokens. However, your messages resulted in 9001 tokens."
    calls = {"n": 0, "with_usage": 0}

    def handler(request):
        calls["n"] += 1
        if b'"stream_options"' in request.read():
            calls["with_usage"] += 1
        return httpx.Response(400, json={"object": "error", "message": reason})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm.set_http_client(client)

    async def collect():
        async for _ in llm.chat_completion_stream(settings, "Question", llm.SYSTEM_PROMPT):
            pass

    try:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            asyncio.run(collect())
    finally:
        asyncio.run(client.aclose())
        llm.set_http_client(None)

    assert "maximum context length is 8192 tokens" in str(caught.value)
    assert caught.value.__class__ is httpx.HTTPStatusError
    # The first attempt asked for usage, the retry dropped it: the 400 was not
    # mistaken for a hard failure before the fallback got its turn.
    assert calls["n"] == 2 and calls["with_usage"] == 1


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


# --- LLM-2 / LLM-3: sampling-parameter capability and output caps -------------


def _settings(**overrides):
    base = {
        "provider": "openai_compatible",
        "base_url": "http://llm.test/v1",
        "chat_model": "chat",
        "api_key": "",
        "temperature": 0.2,
        "timeout_seconds": 30,
    }
    base.update(overrides)
    return base


def _settings_with_capabilities(capabilities, **overrides):
    settings = _settings(**overrides)
    settings["diagnostics"] = {
        "chat": {
            "settings_fingerprint": llm.chat_settings_fingerprint(settings),
            "capabilities": capabilities,
        }
    }
    return settings


@pytest.mark.parametrize(
    ("intent_name", "expected_temperature"),
    [
        ("DETERMINISTIC", 0.0),
        ("PRECISE", 0.2),
        ("BALANCED", 0.3),
        ("EXPLORATORY", 0.4),
        ("CREATIVE", 0.6),
    ],
)
def test_semantic_intents_preserve_existing_temperature_behavior(intent_name, expected_temperature):
    """Gemma-compatible providers keep the exact pre-LLM-5 sampling values."""
    intent = getattr(llm.ChatIntent, intent_name)
    request = build_chat_request(
        _settings(),
        "hi",
        "sys",
        intent=intent,
        call_type="chat_completion",
    )

    assert request["json"]["temperature"] == expected_temperature
    assert "reasoning_effort" not in request["json"]


def test_verified_reasoning_effort_replaces_temperature_for_semantic_intent():
    """A positive probe maps task semantics to effort without model-name guessing."""
    probed = _settings_with_capabilities({
        llm.SAMPLING_PARAMS_CAPABILITY: {
            "status": "failed",
            "temperature_accepted": False,
        },
        llm.MAX_TOKENS_FIELD_CAPABILITY: {
            "status": "succeeded",
            "field": "max_completion_tokens",
        },
        llm.REASONING_EFFORT_CAPABILITY: {
            "status": "succeeded",
            "supported_values": ["low", "medium"],
        },
    })

    deterministic = build_chat_request(
        probed,
        "hi",
        "sys",
        intent=llm.ChatIntent.DETERMINISTIC,
        call_type="chat_completion",
    )
    creative = build_chat_request(
        probed,
        "hi",
        "sys",
        intent=llm.ChatIntent.CREATIVE,
        call_type="chat_completion",
    )

    assert deterministic["json"]["reasoning_effort"] == "low"
    assert creative["json"]["reasoning_effort"] == "medium"
    assert "temperature" not in deterministic["json"]
    assert "temperature" not in creative["json"]


def test_provider_default_policy_omits_verified_reasoning_effort():
    """An admin can explicitly leave effort selection to the provider."""
    probed = _settings_with_capabilities(
        {
            llm.SAMPLING_PARAMS_CAPABILITY: {
                "status": "failed",
                "temperature_accepted": False,
            },
            llm.MAX_TOKENS_FIELD_CAPABILITY: {
                "status": "succeeded",
                "field": "max_completion_tokens",
            },
            llm.REASONING_EFFORT_CAPABILITY: {
                "status": "succeeded",
                "supported_values": ["low", "medium"],
            },
        },
        reasoning_effort_mode="provider_default",
        reasoning_effort="medium",
    )

    request = build_chat_request(
        probed,
        "hi",
        "sys",
        intent=llm.ChatIntent.CREATIVE,
        call_type="chat_completion",
    )

    assert "temperature" not in request["json"]
    assert "reasoning_effort" not in request["json"]


def test_fixed_policy_applies_verified_effort_to_generic_chat_calls():
    """Fixed mode covers call sites without a task-level semantic intent."""
    probed = _settings_with_capabilities(
        {
            llm.SAMPLING_PARAMS_CAPABILITY: {
                "status": "failed",
                "temperature_accepted": False,
            },
            llm.MAX_TOKENS_FIELD_CAPABILITY: {
                "status": "succeeded",
                "field": "max_completion_tokens",
            },
            llm.REASONING_EFFORT_CAPABILITY: {
                "status": "succeeded",
                "supported_values": ["low", "medium", "high"],
            },
        },
        reasoning_effort_mode="fixed",
        reasoning_effort="high",
    )

    request = build_chat_request(probed, "hi", "sys", call_type="chat_completion")

    assert request["json"]["reasoning_effort"] == "high"
    assert "temperature" not in request["json"]


def test_fixed_policy_adds_only_the_selected_effort_to_the_probe(monkeypatch):
    """Fixed high is measured, without probing every provider-specific value."""
    seen: list[dict] = []

    async def fake_post(url, headers, payload, timeout, retry_stats=None):
        seen.append(payload)
        if "temperature" in payload or "max_tokens" in payload:
            raise httpx.HTTPStatusError(
                "Unsupported parameter",
                request=httpx.Request("POST", url),
                response=httpx.Response(400, text="Unsupported parameter: temperature"),
            )
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(llm, "_post_json_with_retry", fake_post)
    capabilities = asyncio.run(
        llm._probe_sampling_params(
            _settings(reasoning_effort_mode="fixed", reasoning_effort="high"),
            usage_context=None,
        )
    )

    effort = capabilities[llm.REASONING_EFFORT_CAPABILITY]
    assert effort["supported_values"] == ["low", "medium", "high"]
    assert [payload["reasoning_effort"] for payload in seen if "reasoning_effort" in payload] == [
        "low",
        "medium",
        "high",
    ]


@pytest.mark.parametrize("status", ["failed", "not_tested"])
def test_unverified_reasoning_effort_is_never_sent(status):
    """Unsupported and inconclusive probes both fail closed for effort."""
    probed = _settings_with_capabilities({
        llm.SAMPLING_PARAMS_CAPABILITY: {
            "status": "failed",
            "temperature_accepted": False,
        },
        llm.MAX_TOKENS_FIELD_CAPABILITY: {
            "status": "succeeded",
            "field": "max_completion_tokens",
        },
        llm.REASONING_EFFORT_CAPABILITY: {
            "status": status,
            "supported_values": [],
        },
    })

    request = build_chat_request(
        probed,
        "hi",
        "sys",
        intent=llm.ChatIntent.CREATIVE,
        call_type="chat_completion",
    )

    assert "temperature" not in request["json"]
    assert "reasoning_effort" not in request["json"]


def test_image_probe_reports_accepted_semantic_mismatch_as_inconclusive(monkeypatch):
    """A valid multimodal response is not an endpoint failure just because its enum is wrong."""

    async def fake_probe(*args, **kwargs):
        return {
            "status": "succeeded",
            "latency_ms": 8.0,
            "error_class": "",
            "json_valid": True,
            "json_object": {"dominant_color": "#ff0000"},
            "usage_available": True,
        }

    monkeypatch.setattr(llm, "_probe_chat_once", fake_probe)
    result = asyncio.run(llm._probe_image_understanding(_settings(), usage_context=None))

    assert result["status"] == "inconclusive"
    assert result["semantic_match"] is False
    assert result["json_valid"] is True


def test_image_probe_keeps_transport_rejection_failed(monkeypatch):
    """Only a rejected or failed image request is shown as a real failure."""

    async def fake_probe(*args, **kwargs):
        return {
            "status": "failed",
            "latency_ms": 8.0,
            "error_class": "HTTPStatusError",
            "json_valid": False,
            "usage_available": False,
        }

    monkeypatch.setattr(llm, "_probe_chat_once", fake_probe)
    result = asyncio.run(llm._probe_image_understanding(_settings(), usage_context=None))

    assert result["status"] == "failed"
    assert result["semantic_match"] is False


def test_stale_probe_fingerprint_cannot_enable_effort_for_a_different_model():
    """A capability belongs to the exact settings tested, not the next model."""
    old_settings = _settings(chat_model="old-model")
    current = _settings(
        chat_model="new-model",
        diagnostics={
            "chat": {
                "settings_fingerprint": llm.chat_settings_fingerprint(old_settings),
                "capabilities": {
                    llm.SAMPLING_PARAMS_CAPABILITY: {
                        "status": "failed",
                        "temperature_accepted": False,
                    },
                    llm.MAX_TOKENS_FIELD_CAPABILITY: {
                        "status": "succeeded",
                        "field": "max_completion_tokens",
                    },
                    llm.REASONING_EFFORT_CAPABILITY: {
                        "status": "succeeded",
                        "supported_values": ["low", "medium"],
                    },
                },
            }
        },
    )

    request = build_chat_request(
        current,
        "hi",
        "sys",
        intent=llm.ChatIntent.CREATIVE,
        call_type="chat_completion",
    )

    assert request["json"]["temperature"] == 0.6
    assert "reasoning_effort" not in request["json"]


def test_feature_chat_calls_do_not_hardcode_numeric_temperatures():
    """New feature calls must name intent instead of reintroducing magic numbers."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(llm.__file__).read_text(encoding="utf-8"))
    offenders = []
    for function in (node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)):
        if function.name.startswith("_probe_"):
            continue  # diagnostics intentionally retains a low-level override
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            name = call.func.id if isinstance(call.func, ast.Name) else ""
            if name not in {"chat_completion", "chat_completion_stream"}:
                continue
            for keyword in call.keywords:
                if keyword.arg == "temperature" and isinstance(keyword.value, ast.Constant):
                    offenders.append((function.name, keyword.value.value))

    assert offenders == []
    assert all(
        isinstance(intent, llm.ChatIntent)
        for _prompt, intent, _label in llm.ARTIFACT_PROMPTS.values()
    )


def test_request_carries_temperature_and_max_tokens_by_default():
    """Unprobed, behave exactly as before plus the new output cap.

    The permissive default matters: every OpenAI-compatible server this project
    actually targets accepts both fields, so a fresh install must not start
    silently dropping `temperature` just because nobody has clicked "test" yet.
    """
    from app.llm import build_chat_request

    request = build_chat_request(_settings(), "hi", "sys", call_type="rerank")
    assert request["json"]["temperature"] == 0.2
    assert request["json"]["max_tokens"] == 768  # [max_tokens].rerank
    assert "max_completion_tokens" not in request["json"]


def test_temperature_is_omitted_when_the_probe_found_it_unsupported():
    """The GPT-5-class case that motivated this (a colleague hit it on 5.4-mini).

    Every LLM call in the app funnels through build_chat_request, so sending an
    unsupported parameter fails chat, rewrite, rerank, briefing and evals at once
    — a total outage, not a degradation.
    """
    from app.llm import build_chat_request

    probed = _settings_with_capabilities({
        "sampling_params": {"status": "failed", "temperature_accepted": False},
        "max_tokens_field": {"status": "succeeded", "field": "max_completion_tokens"},
    })
    request = build_chat_request(probed, "hi", "sys", temperature=0.9, call_type="chat_completion")
    assert "temperature" not in request["json"]
    assert request["json"]["max_completion_tokens"] == 2048
    assert "max_tokens" not in request["json"]


def test_inconclusive_probe_leaves_temperature_alone():
    """A timeout or 500 says nothing about capabilities — do not infer from it.

    Recording "unsupported" from an unrelated failure would strip temperature
    from every later request on a model that supports it perfectly well.
    """
    from app.llm import build_chat_request

    probed = _settings_with_capabilities({"sampling_params": {"status": "not_tested"}})
    assert "temperature" in build_chat_request(probed, "hi", "sys")["json"]


def test_every_call_type_has_its_own_output_cap():
    """A new call_type must not silently inherit the default.

    `resolve_max_tokens` looks the field up by name with a fallback, so a typo or
    a new call site would quietly get `default` instead of a size chosen for its
    output shape. This pins the two lists together.
    """
    import dataclasses
    import pathlib
    import re

    from app.config import MaxTokensConfig

    known = {f.name for f in dataclasses.fields(MaxTokensConfig)}
    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    used = set()
    for path in app_dir.glob("*.py"):
        used.update(re.findall(r'call_type="([a-z_]+)"', path.read_text(encoding="utf-8")))
    # Artifact call types are built as f"artifact_{label}" from ARTIFACT_PROMPTS.
    from app.llm import ARTIFACT_PROMPTS

    used.update(f"artifact_{label}" for _, _, label in ARTIFACT_PROMPTS.values())
    # Embedding calls never reach build_chat_request, so an output cap would be
    # meaningless for them — excluded rather than given a field that implies
    # embeddings have a response length.
    used -= {"settings_embedding_probe"}
    missing = sorted(used - known)
    assert not missing, f"call_type(s) with no [max_tokens] entry, silently using default: {missing}"


def test_unsupported_parameter_400_gets_an_actionable_message():
    """LLM-1: the one 4xx an admin can act on must not read as "check settings"."""
    import httpx

    import app.main as main

    response = httpx.Response(
        400,
        request=httpx.Request("POST", "http://llm.test/v1/chat/completions"),
        json={"error": {"message": "Unsupported parameter: 'temperature' is not supported with this model."}},
    )
    exc = httpx.HTTPStatusError("400", request=response.request, response=response)
    message = main.friendly_error_message(exc)
    assert message == main.i18n.t("error.unsupported_parameter")
    assert message != main.i18n.t("error.generic_check", action=main.i18n.t("error.action_default"))

    # An unrelated 400 still gets the generic message.
    plain = httpx.Response(
        400, request=response.request, json={"error": {"message": "bad request"}}
    )
    plain_exc = httpx.HTTPStatusError("400", request=plain.request, response=plain)
    assert main.friendly_error_message(plain_exc) != main.i18n.t("error.unsupported_parameter")


def test_capability_probe_output_actually_reaches_build_chat_request(monkeypatch):
    """Pin the probe → diagnostics → request-shape contract end to end.

    `chat_sampling_support` had no test at all, yet every LLM call reads it: it
    decides whether `temperature` is sent and whether the cap is `max_tokens` or
    `max_completion_tokens`. Its inputs are capability statuses written by the
    probe into `llm_settings.diagnostics_json` — the same untyped-blob contract
    that let `/admin/index` compare against a `"ok"` no writer produced.

    Nothing here spells a status string: the probe decides it, and the assertion
    is on the request shape a caller would actually get.
    """
    import asyncio

    from app import llm as llm_module

    calls: list[dict] = []

    async def fake_post(url, headers, payload, timeout, retry_stats=None):
        calls.append(payload)
        # A GPT-5-class model: rejects `temperature`, wants `max_completion_tokens`.
        if "temperature" in payload:
            raise RuntimeError("Unsupported parameter: 'temperature' is not supported")
        if "max_tokens" in payload:
            raise RuntimeError("Unsupported parameter: use 'max_completion_tokens'")
        return {"choices": [{"message": {"content": "pong"}}]}

    async def fake_post_status(url, headers, payload, timeout, retry_stats=None):
        try:
            return await fake_post(url, headers, payload, timeout, retry_stats)
        except RuntimeError as exc:
            raise httpx.HTTPStatusError(
                str(exc),
                request=httpx.Request("POST", url),
                response=httpx.Response(400, text=str(exc)),
            ) from None

    monkeypatch.setattr(llm_module, "_post_json_with_retry", fake_post_status)

    settings = {
        "provider": "openai_compatible",
        "base_url": "https://example.invalid/v1",
        "chat_model": "reasoning-model",
    }
    capabilities = asyncio.run(llm_module._probe_sampling_params(settings, usage_context=None))

    # Feed the probe's own output back the way the app stores it.
    probed = {
        **settings,
        "diagnostics": {
            "chat": {
                "settings_fingerprint": llm_module.chat_settings_fingerprint(settings),
                "capabilities": capabilities,
            }
        },
    }
    support = llm_module.chat_sampling_support(probed)
    assert support["temperature"] is False
    assert support["max_tokens_field"] == "max_completion_tokens"

    request = llm_module.build_chat_request(probed, "hi", call_type="answer_stream")
    assert "temperature" not in request["json"], "a model that rejects it must not be sent it"
    assert "max_completion_tokens" in request["json"]
    assert "max_tokens" not in request["json"]


# -------------------- P1: over-long embedding input --------------------

def _embedding_settings(**extra):
    settings = {
        "provider": "openai_compatible",
        "embedding_base_url": "http://e5.test/v1",
        "embedding_model": "intfloat/multilingual-e5-large",
    }
    settings.update(extra)
    return settings


def test_a_long_query_is_trimmed_instead_of_failing_the_whole_request(monkeypatch):
    """The 2026-09-16 failure: a long question got no answer at all.

    e5 refuses an over-window input with HTTP 400 rather than truncating, and
    `embed_texts` batched only by count, so one long query took the request --
    and with it the user's whole question -- down. Asserting on what reaches the
    endpoint, not on the helper: the helper could be perfect and still not be
    wired into the path that broke.
    """
    from app.ingest import estimate_embedding_tokens

    sent: list[list[str]] = []

    async def fake_post(url, headers, payload, timeout, retry_stats=None):
        sent.append(payload["input"])
        return {"data": [{"embedding": [0.1, 0.2]} for _ in payload["input"]]}

    monkeypatch.setattr(llm, "_post_json_with_retry", fake_post)

    question = "這份契約的驗收條件與罰則是什麼？" * 120
    budget = llm.config.diagnostics.embedding_token_budget
    assert estimate_embedding_tokens(question) > budget   # the test is testing something

    asyncio.run(llm.embed_texts([question], _embedding_settings(), role="query"))

    assert len(sent) == 1
    assert estimate_embedding_tokens(sent[0][0]) <= budget
    assert sent[0][0] and question.startswith(sent[0][0])  # a prefix, not a rewrite


def test_trimming_keeps_the_query_prefix_inside_the_window(monkeypatch):
    """e5 needs its `query: ` prefix, so the prefix has to be charged too."""
    from app.ingest import estimate_embedding_tokens

    sent: list[list[str]] = []

    async def fake_post(url, headers, payload, timeout, retry_stats=None):
        sent.append(payload["input"])
        return {"data": [{"embedding": [0.1]} for _ in payload["input"]]}

    monkeypatch.setattr(llm, "_post_json_with_retry", fake_post)

    budget = llm.config.diagnostics.embedding_token_budget
    asyncio.run(llm.embed_texts(
        ["每股盈餘與毛利率的趨勢如何？" * 120],
        _embedding_settings(embedding_query_prefix="query: "),
        role="query",
    ))

    assert sent[0][0].startswith("query: ")
    assert estimate_embedding_tokens(sent[0][0]) <= budget


def test_short_texts_are_passed_through_untouched(monkeypatch):
    """Trimming must not rewrite anything that already fits."""
    sent: list[list[str]] = []

    async def fake_post(url, headers, payload, timeout, retry_stats=None):
        sent.append(payload["input"])
        return {"data": [{"embedding": [0.1]} for _ in payload["input"]]}

    monkeypatch.setattr(llm, "_post_json_with_retry", fake_post)
    asyncio.run(llm.embed_texts(["短問題", "another short one"], _embedding_settings(), role="query"))

    assert sent[0] == ["短問題", "another short one"]


def test_an_over_budget_passage_is_trimmed_but_logged(monkeypatch, caplog):
    """The net must not hide a chunker bug it is covering for.

    Ingest already packs chunks to this budget, so a passage arriving over it
    means the chunker (or the token estimate feeding it) is wrong -- the exact
    failure of TROUBLESHOOTING §2. Trim so the source still indexes, but say so.
    """
    async def fake_post(url, headers, payload, timeout, retry_stats=None):
        return {"data": [{"embedding": [0.1]} for _ in payload["input"]]}

    monkeypatch.setattr(llm, "_post_json_with_retry", fake_post)

    with caplog.at_level("WARNING", logger="app.llm"):
        asyncio.run(llm.embed_texts(["表格內容 " * 600], _embedding_settings(), role="passage"))

    warnings = [r.getMessage() for r in caplog.records if "embedding_input_truncated" in r.getMessage()]
    assert warnings and "role=passage" in warnings[0]


# -------------------- P1: a chat response with no content --------------------

def _chat_settings():
    return {
        "provider": "openai_compatible",
        "base_url": "https://example.invalid/v1",
        "chat_model": "openai/gpt-oss-120b",
    }


def test_a_null_content_response_raises_a_named_error(monkeypatch, caplog):
    """Reasoning models return `"content": null` when reasoning ate the budget.

    `.strip()` on that raised AttributeError from inside the provider layer, so
    every degrade path logged a traceback naming the symptom and not the cause.
    The diagnosis has to survive to the log line, because the fix -- raising the
    cap in [max_tokens] -- is only visible from finish_reason.
    """
    async def fake_post(url, headers, payload, timeout, retry_stats=None):
        return {
            "choices": [{"message": {"content": None}, "finish_reason": "length"}],
            "usage": {"completion_tokens": 768,
                      "completion_tokens_details": {"reasoning_tokens": 768}},
        }

    monkeypatch.setattr(llm, "_post_json_with_retry", fake_post)

    with caplog.at_level("WARNING", logger="app.llm"):
        with pytest.raises(llm.EmptyChatContentError):
            asyncio.run(llm.chat_completion(_chat_settings(), "q", "sys", call_type="rerank"))

    logged = [r.getMessage() for r in caplog.records if "chat_completion_empty_content" in r.getMessage()]
    assert logged
    assert "finish_reason='length'" in logged[0] or "finish_reason=length" in logged[0]
    assert "completion_tokens=768" in logged[0]
    assert "reasoning_tokens=768" in logged[0]
    assert "call_type=rerank" in logged[0]


def test_rerank_degrades_to_hybrid_order_when_content_is_null(monkeypatch):
    """The caller-visible behaviour must not change: degrade, never crash."""
    async def fake_post(url, headers, payload, timeout, retry_stats=None):
        return {"choices": [{"message": {"content": None}, "finish_reason": "stop"}]}

    monkeypatch.setattr(llm, "_post_json_with_retry", fake_post)

    candidates = [
        {"id": 1, "source_id": 1, "filename": "a.md", "location": "doc", "text": "alpha", "score": 0.9},
        {"id": 2, "source_id": 2, "filename": "b.md", "location": "doc", "text": "beta", "score": 0.5},
    ]
    result = asyncio.run(llm.rerank_chunks("q", candidates, _chat_settings(), limit=2))

    assert [c["id"] for c in result] == [1, 2]


def test_a_null_content_probe_is_not_recorded_as_a_working_model(monkeypatch):
    """`str(None)` made an empty probe look like the model answered "None".

    The probe's verdict is stored in llm_settings.diagnostics_json and shapes
    every later request (temperature, max_tokens vs max_completion_tokens), so a
    false "it works" here is worse than a failed probe: it is wrong in a way the
    app then acts on.
    """
    async def fake_post(url, headers, payload, timeout, retry_stats=None):
        return {"choices": [{"message": {"content": None}, "finish_reason": "length"}]}

    monkeypatch.setattr(llm, "_post_json_with_retry", fake_post)

    result = asyncio.run(llm._probe_chat_once(
        _chat_settings(),
        user_prompt="probe",
        system_prompt="sys",
        call_type="settings_chat_probe",
        probe_name="sampling",
        usage_context=None,
    ))

    assert result["status"] == "failed"
    assert result["error_class"] == "EmptyChatContentError"
    assert "None" not in str(result.get("content", ""))


# -------------------- P2: malformed JSON from the model --------------------
#
# Three occurrences in one production week (2026-09-10 x2, 2026-09-16 x1), all
# `Expecting ',' delimiter`. That error class matters: a response cut off at
# max_tokens raises `Unterminated string` or `Expecting property name` instead,
# so these arrays were complete and only their punctuation was wrong. Raising
# the output cap would not have helped; discarding the whole array did the harm.


def test_truncation_and_malformed_output_are_different_failures():
    """Pin the premise the salvage design rests on.

    If this ever stops holding, salvaging is the wrong response -- a truncated
    array is genuinely incomplete and recovering its prefix could silently drop
    the best-scoring candidates, which a larger `[max_tokens]` would fix
    properly.
    """
    import json as _json

    def message(text):
        with pytest.raises(_json.JSONDecodeError) as caught:
            _json.loads(text)
        return caught.value.msg

    assert message('[{"id": 1, "score": 0.9}, {"id": 2, "sco') == "Unterminated string starting at"
    assert message('["查詢一", "查詢二') == "Unterminated string starting at"
    assert message('[{"id":1,"score":0.9}\n{"id":2,"score":0.8}]') == "Expecting ',' delimiter"
    assert message('["合約中的 "驗收" 條件"]') == "Expecting ',' delimiter"


def test_rerank_scores_survive_a_missing_comma_between_objects(caplog):
    """One badly separated object must cost one score, not the whole reranking."""
    malformed = '[{"id": 1, "score": 0.9}\n{"id": 2, "score": 0.8}\n{"id": 3, "score": 0.7}]'

    with caplog.at_level("WARNING", logger="app.llm"):
        scores = parse_rerank_scores(malformed)

    assert scores == {1: 0.9, 2: 0.8, 3: 0.7}
    assert any("rerank_scores_salvaged" in r.getMessage() for r in caplog.records)


def test_rerank_salvage_keeps_the_good_objects_and_drops_only_the_broken_one():
    """Partial recovery is the point: a wrecked object must not take the rest."""
    scores = parse_rerank_scores(
        '[{"id": 1, "score": 0.9}, {"id": 2, "score": zzz}, {"id": 3, "score": 0.7}]'
    )

    assert scores == {1: 0.9, 3: 0.7}


def test_query_rewrite_survives_an_unescaped_quote_inside_a_string(caplog):
    """The production shape: a quoted term inside a query the model forgot to escape."""
    malformed = '[\n  "合約的驗收條件",\n  "所謂 "罰則" 的範圍",\n  "付款期限"\n]'

    with caplog.at_level("WARNING", logger="app.llm"):
        queries = parse_json_strings(malformed)

    assert queries == ["合約的驗收條件", '所謂 "罰則" 的範圍', "付款期限"]
    assert any("json_strings_salvaged" in r.getMessage() for r in caplog.records)


def test_valid_json_is_parsed_strictly_and_never_salvaged(caplog):
    """Salvage must be unreachable for well-formed output, or it masks real drift."""
    with caplog.at_level("WARNING", logger="app.llm"):
        assert parse_rerank_scores('[{"id": 1, "score": 0.92}, {"id": 2, "score": 0.5}]') == {
            1: 0.92, 2: 0.5,
        }
        assert parse_json_strings('["alpha", "beta"]') == ["alpha", "beta"]
        assert parse_json_strings('```json\n["fenced"]\n```') == ["fenced"]

    assert not [r for r in caplog.records if "salvaged" in r.getMessage()]


def test_a_brace_inside_a_string_does_not_split_an_object():
    """The object scanner tracks string state, so JSON-ish prose stays intact."""
    scores = parse_rerank_scores(
        '[{"id": 1, "score": 0.9, "why": "matches {\\"a\\": 1} in the doc"}\n'
        '{"id": 2, "score": 0.4}]'
    )

    assert scores == {1: 0.9, 2: 0.4}


def test_unsalvageable_output_still_degrades_quietly():
    """Prose instead of JSON has nothing to recover -- return empty, never raise."""
    assert parse_rerank_scores("I cannot score these candidates.") == {}
    assert parse_json_strings("Sorry, no queries.") == []


# -------------------- O5a: measuring the embedding input window --------------------

def test_the_window_limit_is_read_out_of_the_endpoints_own_rejection():
    """Both phrasings seen in one production deployment carry the same figure.

    Parsing beats binary-searching for it: one request instead of a dozen, an
    exact number instead of a bracket, and no dependence on
    `estimate_embedding_tokens` -- which rounds *up* by design, so a searched
    bound would sit above the real limit, the wrong direction for a cap.
    """
    token_form = (
        "This model's maximum context length is 512 tokens. However, you requested "
        "0 output tokens and your prompt contains at least 513 input tokens, for a "
        "total of at least 513 tokens. (parameter=input_tokens, value=513)"
    )
    char_form = (
        "This model's maximum context length is 512 tokens. However, your prompt "
        "contains 11011 characters (more than 8192 characters, which is the upper "
        "bound for 512 input tokens). (parameter=input_text, value=11011)"
    )

    assert llm.parse_embedding_window_limit(token_form) == 512
    assert llm.parse_embedding_window_limit(char_form) == 512
    assert llm.parse_embedding_window_limit("Bad Request") is None
    assert llm.parse_embedding_window_limit("") is None


def _window_settings(**extra):
    settings = {
        "provider": "openai_compatible",
        "embedding_base_url": "http://e5.test/v1",
        "embedding_model": "intfloat/multilingual-e5-large",
    }
    settings.update(extra)
    return settings


def _reject_over(limit_tokens):
    """A fake endpoint that refuses over-window input the way vLLM does."""
    from app.ingest import estimate_embedding_tokens

    async def fake_post(url, headers, payload, timeout, retry_stats=None):
        longest = max((estimate_embedding_tokens(t) for t in payload["input"]), default=0)
        if longest > limit_tokens:
            message = (
                f"This model's maximum context length is {limit_tokens} tokens. However, "
                f"you requested 0 output tokens and your prompt contains at least "
                f"{longest} input tokens."
            )
            raise httpx.HTTPStatusError(
                message,
                request=httpx.Request("POST", url),
                response=httpx.Response(400, text=message),
            )
        return {"data": [{"embedding": [0.1, 0.2]} for _ in payload["input"]]}

    return fake_post


def test_probing_a_narrow_window_reports_the_exact_limit(monkeypatch):
    monkeypatch.setattr(llm, "_post_json_with_retry", _reject_over(512))

    result = asyncio.run(llm.probe_embedding_window(_window_settings()))

    assert result["status"] == "succeeded"
    assert result["max_input_tokens"] == 512
    assert result["bound"] == "exact"


def test_an_endpoint_that_accepts_the_oversized_probe_reports_a_floor(monkeypatch):
    """A wide model (text-embedding-3 is 8191) takes the probe whole.

    The floor is all a query path needs: nothing it sends will exceed it, so
    narrowing it with a search would buy nothing.
    """
    monkeypatch.setattr(llm, "_post_json_with_retry", _reject_over(1_000_000))

    result = asyncio.run(llm.probe_embedding_window(_window_settings()))

    assert result["status"] == "succeeded"
    assert result["bound"] == "at_least"
    assert result["max_input_tokens"] > llm.config.diagnostics.embedding_token_budget


def test_an_unparseable_rejection_is_inconclusive_not_a_limit(monkeypatch):
    """Guessing a number from a message we did not understand would be worse
    than not having one: it silently caps every query at the wrong length."""
    async def fake_post(url, headers, payload, timeout, retry_stats=None):
        raise httpx.HTTPStatusError(
            "Bad Request",
            request=httpx.Request("POST", url),
            response=httpx.Response(400, text="Bad Request"),
        )

    monkeypatch.setattr(llm, "_post_json_with_retry", fake_post)

    result = asyncio.run(llm.probe_embedding_window(_window_settings()))

    assert result["status"] == "inconclusive"
    assert result["max_input_tokens"] is None


def test_a_wider_probed_window_actually_widens_what_gets_sent(monkeypatch):
    """The point of the probe: a query that 512 would have trimmed goes whole.

    Asserted on what reaches the endpoint, not on the resolver's return value --
    a correct resolver wired to nothing would still pass that.
    """
    from app.ingest import estimate_embedding_tokens

    sent: list[list[str]] = []

    async def fake_post(url, headers, payload, timeout, retry_stats=None):
        sent.append(payload["input"])
        return {"data": [{"embedding": [0.1]} for _ in payload["input"]]}

    monkeypatch.setattr(llm, "_post_json_with_retry", fake_post)

    question = "這份契約的驗收條件與罰則是什麼？" * 120
    settings = _window_settings()
    fingerprint = llm.embedding_settings_fingerprint(settings)
    settings["diagnostics"] = {
        "embedding": {"settings_fingerprint": fingerprint, "max_input_tokens": 8191},
    }

    asyncio.run(llm.embed_texts([question], settings, role="query"))

    assert sent[0][0] == question       # nothing trimmed at the wider window
    assert estimate_embedding_tokens(question) > llm.config.diagnostics.embedding_token_budget


def test_a_probe_from_different_settings_is_never_applied(monkeypatch):
    """A window measured against another model must not size this one's input.

    The fingerprint is the guard; without it, switching e5 -> a wider model and
    back would leave the wide figure in place and every query would 400.
    """
    sent: list[list[str]] = []

    async def fake_post(url, headers, payload, timeout, retry_stats=None):
        sent.append(payload["input"])
        return {"data": [{"embedding": [0.1]} for _ in payload["input"]]}

    monkeypatch.setattr(llm, "_post_json_with_retry", fake_post)

    settings = _window_settings()
    settings["diagnostics"] = {
        "embedding": {"settings_fingerprint": "measured-against-another-model",
                      "max_input_tokens": 8191},
    }
    question = "這份契約的驗收條件與罰則是什麼？" * 120

    assert llm.embedding_window_budget(settings) == llm.config.diagnostics.embedding_token_budget
    asyncio.run(llm.embed_texts([question], settings, role="query"))
    assert sent[0][0] != question       # fell back to the configured budget, so it trimmed


def test_an_unprobed_deployment_keeps_the_configured_budget():
    """No probe, a failed probe, and a junk value all mean "use the config"."""
    configured = llm.config.diagnostics.embedding_token_budget
    settings = _window_settings()
    # The EMBEDDING fingerprint, so the last two cases reach the value check at
    # all. With a chat fingerprint here they would pass on the mismatch instead,
    # and "a junk value falls back" would never actually be exercised.
    fingerprint = llm.embedding_settings_fingerprint(settings)

    assert llm.embedding_window_budget(settings) == configured
    settings["diagnostics"] = {}
    assert llm.embedding_window_budget(settings) == configured
    settings["diagnostics"] = {"embedding": {"settings_fingerprint": fingerprint,
                                             "max_input_tokens": None}}
    assert llm.embedding_window_budget(settings) == configured
    settings["diagnostics"] = {"embedding": {"settings_fingerprint": fingerprint,
                                             "max_input_tokens": 0}}
    assert llm.embedding_window_budget(settings) == configured


# -------------------- O5b: schema-constrained chat output --------------------

def _structured_settings(shape="response_format", enabled=True, fingerprint_ok=True, status="succeeded"):
    settings = {
        "provider": "openai_compatible",
        "base_url": "https://example.invalid/v1",
        "chat_model": "openai/gpt-oss-120b",
        "structured_output_enabled": enabled,
    }
    fingerprint = llm.chat_settings_fingerprint(settings) if fingerprint_ok else "measured-elsewhere"
    settings["diagnostics"] = {
        "chat": {
            "settings_fingerprint": fingerprint,
            "capabilities": {"structured_output": {"status": status, "shape": shape}},
        }
    }
    return settings


def test_a_json_call_type_carries_the_schema_once_probed_and_enabled():
    """rerank's reply is parsed as JSON, so it is one of the call types that
    may be constrained — and the schema has to match what the parser accepts."""
    request = build_chat_request(_structured_settings(), "q", "sys", call_type="rerank")

    schema = request["json"]["response_format"]["json_schema"]["schema"]
    assert schema["items"]["required"] == ["id", "score"]
    assert llm.parse_rerank_scores('[{"id": 1, "score": 0.9}]') == {1: 0.9}


def test_the_older_guided_json_shape_is_sent_when_that_is_what_was_probed():
    """A vLLM pinned to an older server has the capability under the old name."""
    request = build_chat_request(_structured_settings(shape="guided_json"), "q", "sys",
                                 call_type="query_rewrite")

    assert "response_format" not in request["json"]
    assert request["json"]["guided_json"] == {"type": "array", "items": {"type": "string"}}


def test_a_prose_call_type_is_never_constrained():
    """Constraining an answer or a briefing to a JSON schema would wreck it.
    Only call types whose replies are parsed as JSON may carry one."""
    for call_type in ("answer_stream", "briefing", "source_summary", "meeting_minutes"):
        request = build_chat_request(_structured_settings(), "q", "sys", call_type=call_type)
        assert "response_format" not in request["json"], call_type
        assert "guided_json" not in request["json"], call_type


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"enabled": False}, "admin has not turned it on"),
        ({"status": "failed"}, "the endpoint refused it"),
        ({"status": "not_tested"}, "the probe could not reach it"),
        ({"fingerprint_ok": False}, "the probe belongs to different settings"),
        ({"shape": "something_else"}, "the recorded shape is not one we send"),
    ],
)
def test_structured_output_fails_closed(kwargs, why):
    """Every uncertain case must send an unconstrained request, i.e. behave
    exactly as today. This changes how every JSON call is sampled, so the
    default has to be the one that cannot make things worse."""
    settings = _structured_settings(**kwargs)

    assert llm.structured_output_shape(settings) == "", why
    request = build_chat_request(settings, "q", "sys", call_type="rerank")
    assert "response_format" not in request["json"], why
    assert "guided_json" not in request["json"], why


def test_an_endpoint_that_ignores_the_constraint_is_not_recorded_as_supporting_it(monkeypatch):
    """Accepting the request is not honouring the schema.

    A server that takes `response_format` and answers prose anyway would pass an
    acceptance-only check, and the runtime would then rely on a guarantee it does
    not have -- worse than knowing it is unsupported.
    """
    async def fake_post(url, headers, payload, timeout, retry_stats=None):
        return {"choices": [{"message": {"content": "Sure! Here you go: ok"}}]}

    monkeypatch.setattr(llm, "_post_json_with_retry", fake_post)

    result = asyncio.run(llm._probe_structured_output(
        {"provider": "openai_compatible", "base_url": "https://example.invalid/v1",
         "chat_model": "m"},
        max_tokens_field="max_tokens",
    ))

    assert result["status"] == "failed"
    assert result["shape"] == ""


def test_the_probe_falls_back_to_guided_json_when_response_format_is_refused(monkeypatch):
    seen: list[str] = []

    async def fake_post(url, headers, payload, timeout, retry_stats=None):
        if "response_format" in payload:
            seen.append("response_format")
            raise httpx.HTTPStatusError(
                "unknown field response_format",
                request=httpx.Request("POST", url),
                response=httpx.Response(400, text="unknown field response_format"),
            )
        seen.append("guided_json")
        return {"choices": [{"message": {"content": '["ok"]'}}]}

    monkeypatch.setattr(llm, "_post_json_with_retry", fake_post)

    result = asyncio.run(llm._probe_structured_output(
        {"provider": "openai_compatible", "base_url": "https://example.invalid/v1",
         "chat_model": "m"},
        max_tokens_field="max_tokens",
    ))

    assert seen == ["response_format", "guided_json"]
    assert result == {"status": "succeeded", "shape": "guided_json"}


def test_an_unreachable_endpoint_is_not_tested_rather_than_unsupported(monkeypatch):
    """"We could not reach it" is not "it does not support it", and the
    difference decides whether an admin should retry or stop."""
    async def fake_post(url, headers, payload, timeout, retry_stats=None):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(llm, "_post_json_with_retry", fake_post)

    result = asyncio.run(llm._probe_structured_output(
        {"provider": "openai_compatible", "base_url": "https://example.invalid/v1",
         "chat_model": "m"},
        max_tokens_field="max_tokens",
    ))

    assert result["status"] == "not_tested"


def test_the_capability_snapshot_carries_what_an_eval_comparison_needs():
    """O5c: the flags that move an eval's numbers, and nothing secret."""
    snapshot = llm.capability_snapshot(_structured_settings())

    assert snapshot["chat_model"] == "openai/gpt-oss-120b"
    assert snapshot["structured_output"] == "response_format"
    assert snapshot["max_tokens_field"] in ("max_tokens", "max_completion_tokens")
    assert "api_key" not in snapshot
    assert "base_url" not in snapshot      # internal hostnames stay out of the record
    assert all("prompt" not in key for key in snapshot)


# -------------------- one fingerprint per connection --------------------

def _both_connections():
    return {
        "provider": "openai_compatible",
        "base_url": "http://192.168.11.138:8000/v1",
        "chat_model": "openai/gpt-oss-120b",
        "embedding_base_url": "http://192.168.11.147:8001/v1",
        "embedding_provider": "openai_compatible",
        "embedding_model": "intfloat/multilingual-e5-large",
        "embedding_api_key": "",
    }


def test_swapping_the_chat_model_keeps_the_embedding_window_measurement():
    """The deployment this was found in fails over between two chat hosts.

    One fingerprint covering both connections meant an afternoon on the standby
    chat model silently discarded the embedding window measured minutes earlier,
    and the query trim went back to the configured default with no error and no
    log line. The embedding endpoint was never touched.
    """
    settings = _both_connections()
    settings["diagnostics"] = {
        "embedding": {
            "settings_fingerprint": llm.embedding_settings_fingerprint(settings),
            "max_input_tokens": 8191,
        }
    }
    assert llm.embedding_window_budget(settings) == 8191

    failed_over = {**settings, "chat_model": "google/gemma-4-31b",
                   "base_url": "http://192.168.11.200:8000/v1"}

    assert llm.embedding_window_budget(failed_over) == 8191


def test_swapping_the_embedding_model_does_discard_its_window():
    """The guard still has to fire for its own connection, or it guards nothing."""
    settings = _both_connections()
    settings["diagnostics"] = {
        "embedding": {
            "settings_fingerprint": llm.embedding_settings_fingerprint(settings),
            "max_input_tokens": 8191,
        }
    }

    for change in ({"embedding_model": "BAAI/bge-m3"},
                   {"embedding_base_url": "http://192.168.11.9:8001/v1"},
                   {"embedding_query_prefix": "query: "}):
        moved = {**settings, **change}
        assert llm.embedding_window_budget(moved) == llm.config.diagnostics.embedding_token_budget, change


def test_swapping_the_embedding_model_keeps_the_chat_capabilities():
    """The symmetric case: an embedding-side edit must not re-open the chat probe.

    Pre-existing before the split, and just as invisible — it only ever cost an
    operator a second click, which is why nobody noticed.
    """
    settings = _both_connections()
    settings["diagnostics"] = {
        "chat": {
            "settings_fingerprint": llm.chat_settings_fingerprint(settings),
            "capabilities": {
                "sampling_params": {"status": "failed", "temperature_accepted": False},
                "max_tokens_field": {"status": "succeeded", "field": "max_completion_tokens"},
            },
        }
    }
    assert llm.chat_sampling_support(settings)["max_tokens_field"] == "max_completion_tokens"

    moved = {**settings, "embedding_model": "BAAI/bge-m3"}

    assert llm.chat_sampling_support(moved)["max_tokens_field"] == "max_completion_tokens"
    assert llm.chat_sampling_support(moved)["temperature"] is False


def test_the_embedding_fingerprint_follows_the_resolved_connection():
    """`embedding_settings` falls back to the shared chat columns when the split
    ones are absent, so a chat-side edit really does move the embedding endpoint
    there. Fingerprinting raw columns would miss exactly that case."""
    shared = {
        "provider": "openai_compatible",
        "base_url": "http://one.invalid/v1",
        "chat_model": "chat",
        "embedding_model": "embed",
    }
    before = llm.embedding_settings_fingerprint(shared)

    # No embedding_base_url, so embeddings go to base_url — moving it moves them.
    assert llm.embedding_settings_fingerprint({**shared, "base_url": "http://two.invalid/v1"}) != before
    # With a split URL set, the chat URL no longer decides where embeddings go.
    split = {**shared, "embedding_base_url": "http://e.invalid/v1"}
    assert llm.embedding_settings_fingerprint({**split, "base_url": "http://two.invalid/v1"}) == \
        llm.embedding_settings_fingerprint(split)
