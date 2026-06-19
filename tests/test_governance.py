import asyncio
import importlib

import httpx


def _fresh_governance_stack(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_DATA_DIR", str(tmp_path / "data"))
    import app.db as db
    import app.governance as governance
    import app.llm as llm

    for module in (db, governance, llm):
        importlib.reload(module)
    db.init_db()
    return db, governance, llm


def test_normalize_usage_prefers_provider_counts(monkeypatch, tmp_path):
    _db, governance, _llm = _fresh_governance_stack(monkeypatch, tmp_path)

    usage = governance.normalize_usage(
        {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        input_chars=100,
        output_chars=20,
    )

    assert usage == {
        "prompt_tokens": 12,
        "completion_tokens": 3,
        "total_tokens": 15,
        "input_chars": 100,
        "output_chars": 20,
        "is_estimated": 0,
    }


def test_normalize_usage_estimates_when_provider_omits_usage(monkeypatch, tmp_path):
    _db, governance, _llm = _fresh_governance_stack(monkeypatch, tmp_path)

    usage = governance.normalize_usage(None, input_chars=17, output_chars=5)

    assert usage["prompt_tokens"] == 5
    assert usage["completion_tokens"] == 2
    assert usage["total_tokens"] == 7
    assert usage["is_estimated"] == 1


def test_record_llm_usage_event_persists_compact_metadata(monkeypatch, tmp_path):
    db, governance, _llm = _fresh_governance_stack(monkeypatch, tmp_path)
    with db.connect() as conn:
        notebook_id = conn.execute(
            "INSERT INTO notebooks (user_id, title) VALUES (1, 'Gov')"
        ).lastrowid

    governance.record_llm_usage_event(
        call_type="answer",
        provider="openai_compatible",
        model="chat",
        status="succeeded",
        latency_ms=12.5,
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
            "input_chars": 40,
            "output_chars": 12,
            "is_estimated": 0,
        },
        context={"user_id": 1, "notebook_id": notebook_id, "message_id": "bad"},
        metadata={"temperature": 0.2, "prompt": "x" * 500, "ignored": {"nested": True}},
    )

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM llm_usage_events").fetchone()

    assert row["call_type"] == "answer"
    assert row["user_id"] == 1
    assert row["notebook_id"] == notebook_id
    assert row["message_id"] is None
    assert row["prompt_tokens"] == 10
    assert row["is_estimated"] == 0
    assert '"temperature": 0.2' in row["metadata_json"]
    assert "nested" not in row["metadata_json"]
    assert "x" * 300 in row["metadata_json"]
    assert "x" * 301 not in row["metadata_json"]


def test_chat_completion_records_provider_usage(monkeypatch, tmp_path):
    db, _governance, llm = _fresh_governance_stack(monkeypatch, tmp_path)
    with db.connect() as conn:
        notebook_id = conn.execute(
            "INSERT INTO notebooks (user_id, title) VALUES (1, 'Gov')"
        ).lastrowid

    def handler(_request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Hello"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm.set_http_client(client)
    try:
        result = asyncio.run(
            llm.chat_completion(
                {"api_key": "sk-test", "chat_model": "chat", "base_url": "http://model/v1"},
                "Question",
                "System",
                call_type="answer",
                usage_context={"user_id": 1, "notebook_id": notebook_id},
            )
        )
    finally:
        asyncio.run(client.aclose())
        llm.set_http_client(None)

    assert result == "Hello"
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM llm_usage_events").fetchone()

    assert row["call_type"] == "answer"
    assert row["provider"] == "openai_compatible"
    assert row["model"] == "chat"
    assert row["status"] == "succeeded"
    assert row["user_id"] == 1
    assert row["notebook_id"] == notebook_id
    assert row["prompt_tokens"] == 12
    assert row["completion_tokens"] == 3
    assert row["total_tokens"] == 15
    assert row["input_chars"] == len("Question") + len("System")
    assert row["output_chars"] == len("Hello")
    assert row["is_estimated"] == 0
