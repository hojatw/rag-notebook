import json

from test_ui import TestClient, _fresh_app, _login, _seed_notebook


def test_streaming_runtime_applies_domain_context_and_never_persists_marker(monkeypatch, tmp_path):
    main, db = _fresh_app(monkeypatch, tmp_path)
    observed = {}

    async def fake_retrieve(question, rows, settings, history, user_id, source_ids=None, **kwargs):
        observed["domain_hints"] = kwargs.get("domain_hints")
        return [{
            "id": 1,
            "source_id": 10,
            "filename": "evidence.xlsx",
            "location": "Sheet1 row 2",
            "text": "ACME evidence",
            "score": 0.9,
        }]

    async def fake_stream(question, chunks, settings, **kwargs):
        observed.update(kwargs)
        kwargs["result_state"]["abstained"] = True
        yield kwargs["abstain_text"]

    monkeypatch.setattr(main, "retrieve", fake_retrieve)
    monkeypatch.setattr(main, "generate_answer_stream", fake_stream)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO notebook_domain_config "
                "(notebook_id, hints_enabled, answer_policy_enabled, answer_policy, revision, version_token, updated_by) "
                "VALUES (?, 1, 1, ?, 2, 'opaque-token', ?)",
                (notebook_id, "請使用秘密內部格式。", user["id"]),
            )
            conn.execute(
                "INSERT INTO notebook_domain_hints "
                "(notebook_id, term, synonyms_json, definition, query_expansions_json, answer_note, enabled) "
                "VALUES (?, 'ACME', '[]', '', '[\"ACME deployment\"]', ?, 1)",
                (notebook_id, "請沿用 ACME 標題。"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO llm_settings "
                "(id, provider, base_url, api_key, chat_model, embedding_model) "
                "VALUES (1, 'openai_compatible', 'https://x/v1', ?, 'chat', 'embed')",
                (db.encrypt_for_storage("sk-test"),),
            )

        response = client.post(
            f"/notebooks/{notebook_id}/chat/ask-stream",
            data={"question": "ACME 的總數是多少？", "conversation_id": ""},
        )

        assert response.status_code == 200
        assert "[[RAG_ABSTAIN]]" not in response.text
        assert main.i18n.t("chat.abstain") in response.text
        assert observed["domain_hints"][0]["term"] == "ACME"
        assert observed["answer_policy"] == "請使用秘密內部格式。"
        assert observed["answer_notes"] == ["請沿用 ACME 標題。"]
        assert observed["spreadsheet_guard"] is True

        with db.connect() as conn:
            saved = dict(conn.execute(
                "SELECT content, citations_json, metadata_json FROM messages "
                "WHERE role = 'assistant' ORDER BY id DESC LIMIT 1"
            ).fetchone())
        metadata = json.loads(saved["metadata_json"])
        assert saved["content"] == main.i18n.t("chat.abstain")
        assert json.loads(saved["citations_json"]) == []
        assert metadata["outcome"] == "abstained"
        assert metadata["matched_hint_count"] == 1
        assert "秘密內部格式" not in saved["metadata_json"]
        assert "ACME" not in saved["metadata_json"]


def test_non_owner_cannot_reach_notebook_domain_runtime(monkeypatch, tmp_path):
    main, db = _fresh_app(monkeypatch, tmp_path)
    calls = {"retrieve": 0}

    async def fake_retrieve(*args, **kwargs):
        calls["retrieve"] += 1
        return []

    monkeypatch.setattr(main, "retrieve", fake_retrieve)
    with TestClient(main.app) as client:
        _login(client)
        _owner, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 0)",
                ("runtime-outsider", main.hash_password("other-password")),
            )
        client.post("/logout", follow_redirects=False)
        assert client.post(
            "/login",
            data={"username": "runtime-outsider", "password": "other-password"},
            follow_redirects=False,
        ).status_code == 303

        response = client.post(
            f"/notebooks/{notebook_id}/chat/ask-stream",
            data={"question": "ACME?", "conversation_id": ""},
        )

    # Streaming responses commit HTTP 200 before the generator runs; the owner
    # check still fails inside the stream before retrieval or domain loading.
    assert response.status_code == 200
    assert calls["retrieve"] == 0


def test_stream_discards_shown_text_when_it_abstains_after_the_gate(monkeypatch, tmp_path):
    """A late abstention must clear the browser, not just append the refusal.

    The generator streams a preamble past the gate and only then abstains. The
    SSE response has to carry a `discard` event before the refusal chunk, and
    the persisted message must still be the canned refusal alone.
    """
    main, db = _fresh_app(monkeypatch, tmp_path)
    preamble = "這份文件確實提到了相關的營收數字。" * 8

    async def fake_retrieve(question, rows, settings, history, user_id, source_ids=None, **kwargs):
        return [{
            "id": 1,
            "source_id": 10,
            "filename": "evidence.pdf",
            "location": "p.1",
            "text": "evidence",
            "score": 0.9,
        }]

    async def fake_stream(question, chunks, settings, **kwargs):
        yield preamble
        kwargs["result_state"]["discard_stream"] = True
        kwargs["result_state"]["abstained"] = True
        yield kwargs["abstain_text"]

    monkeypatch.setattr(main, "retrieve", fake_retrieve)
    monkeypatch.setattr(main, "generate_answer_stream", fake_stream)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_settings "
                "(id, provider, base_url, api_key, chat_model, embedding_model) "
                "VALUES (1, 'openai_compatible', 'https://x/v1', ?, 'chat', 'embed')",
                (db.encrypt_for_storage("sk-test"),),
            )

        response = client.post(
            f"/notebooks/{notebook_id}/chat/ask-stream",
            data={"question": "營收成長多少？", "conversation_id": ""},
        )

        assert response.status_code == 200
        body = response.text
        assert "event: discard" in body
        # Order matters: the client must be told to clear before the refusal.
        assert body.index("event: discard") < body.rindex(main.i18n.t("chat.abstain"))

        with db.connect() as conn:
            saved = dict(conn.execute(
                "SELECT content, metadata_json FROM messages "
                "WHERE role = 'assistant' ORDER BY id DESC LIMIT 1"
            ).fetchone())
        assert saved["content"] == main.i18n.t("chat.abstain")
        assert preamble not in saved["content"]
        assert json.loads(saved["metadata_json"])["outcome"] == "abstained"
