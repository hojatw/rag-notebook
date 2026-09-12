"""問答與對話的 UI 測試。

涵蓋 /ask 的 HTMX 片段與串流路徑、對話改名與中繼資料、延伸問題的產生與快取、
起始問題、匯出，以及 async 路由不得在事件迴圈上做 SQLite I/O。
契約文件：docs/RETRIEVAL.md、docs/UI.md。

由 tests/test_ui.py 拆出；共用固件見 tests/ui_helpers.py。
"""

import asyncio
import time

from starlette.requests import Request

from tests.ui_helpers import TestClient, _fresh_app, _login, _seed_indexed_source, _seed_notebook


def test_ask_returns_messages_partial_for_htmx(monkeypatch, tmp_path):
    """U1: HTMX asks swap only the messages pane; plain posts keep the redirect."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)

        # HTMX request -> 200 partial with the question echoed, URL pushed,
        # and the OOB conversation-field update present.
        resp = client.post(
            f"/notebooks/{notebook_id}/chat/ask",
            data={"question": "第一個問題", "conversation_id": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert 'id="chat-messages"' in resp.text
        assert "第一個問題" in resp.text
        assert "conversation_id=" in resp.headers.get("HX-Push-Url", "")
        assert 'hx-swap-oob="true"' in resp.text

        # Plain form post (no-JS fallback) -> 303 redirect, unchanged behavior.
        resp2 = client.post(
            f"/notebooks/{notebook_id}/chat/ask",
            data={"question": "第二個問題", "conversation_id": ""},
            follow_redirects=False,
        )
        assert resp2.status_code == 303


def test_streaming_ask_saves_answer_and_returns_final_messages(monkeypatch, tmp_path):
    """U2: streaming ask emits chunks, saves the final assistant message, and returns refreshed HTML."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    async def fake_retrieve(question, conversation_id, settings, history, user_id, source_ids=None, **kwargs):
        return [{
            "id": 1,
            "source_id": 10,
            "filename": "source.md",
            "location": "section",
            "text": "答案依據",
            "score": 0.9,
        }]

    async def fake_stream(question, chunks, settings, **kwargs):
        yield "串流"
        yield "回答 [1]"

    monkeypatch.setattr(main, "retrieve", fake_retrieve)
    monkeypatch.setattr(main, "generate_answer_stream", fake_stream)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_settings (id, provider, base_url, api_key, chat_model, embedding_model) "
                "VALUES (1, 'openai_compatible', 'https://x/v1', ?, 'chat', 'embed')",
                (db.encrypt_for_storage("sk-test"),),
            )

        resp = client.post(
            f"/notebooks/{notebook_id}/chat/ask-stream",
            data={"question": "請回答", "conversation_id": ""},
        )
        assert resp.status_code == 200
        assert "event: init" in resp.text
        assert "event: chunk" in resp.text
        assert "串流" in resp.text
        assert "event: done" in resp.text

        with db.connect() as conn:
            messages = conn.execute(
                "SELECT role, content, citations_json, metadata_json FROM messages ORDER BY id"
            ).fetchall()
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[1]["content"] == "串流回答 [1]"
        assert "source.md" in messages[1]["citations_json"]
        assert '"outcome": "answered"' in messages[1]["metadata_json"]


def test_chat_errors_are_friendly_in_ui(monkeypatch, tmp_path):
    """U14: raw exception text is logged/metadata only, not shown in chat UI."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    async def failing_answer(question, settings, history, user_id, source_ids):
        raise RuntimeError("SECRET_PROVIDER_STACKTRACE")

    monkeypatch.setattr(main, "_answer_question", failing_answer)

    with TestClient(main.app) as client:
        _login(client)
        _user, notebook_id = _seed_notebook(db)
        resp = client.post(
            f"/notebooks/{notebook_id}/chat/ask",
            data={"question": "會失敗", "conversation_id": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "回答生成失敗" in resp.text
        assert "SECRET_PROVIDER_STACKTRACE" not in resp.text
        assert "技術細節已記錄" in resp.text


def test_conversation_rename_and_menu_metadata(monkeypatch, tmp_path):
    """U5: conversations can be renamed and the menu shows message count/time metadata."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            convo_id = conn.execute(
                "INSERT INTO conversations (user_id, notebook_id, title) VALUES (?, ?, '舊名稱')",
                (user["id"], notebook_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO messages (conversation_id, user_id, role, content) VALUES (?, ?, 'user', '問題')",
                (convo_id, user["id"]),
            )
            conn.execute(
                "INSERT INTO messages (conversation_id, user_id, role, content) VALUES (?, ?, 'assistant', '回答')",
                (convo_id, user["id"]),
            )

        renamed = client.post(
            f"/notebooks/{notebook_id}/chat/{convo_id}/rename",
            data={"title": " 新名稱  "},
            follow_redirects=False,
        )
        assert renamed.status_code == 303
        assert f"conversation_id={convo_id}" in renamed.headers["location"]

        page = client.get(f"/notebooks/{notebook_id}?conversation_id={convo_id}")
        assert "新名稱" in page.text
        assert "2 則訊息" in page.text
        assert "重新命名對話" in page.text


def test_followups_generate_once_and_cache(monkeypatch, tmp_path):
    """A2: follow-up chips are generated once, cached into message metadata."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    calls = {"n": 0}

    async def fake_followups(question, answer, settings, source_context=None, **kwargs):
        calls["n"] += 1
        assert source_context == ["English source excerpt"]
        return ["追問一？", "追問二？"]

    monkeypatch.setattr(main, "suggest_followup_questions", fake_followups)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_settings (id, provider, base_url, api_key, chat_model, embedding_model) "
                "VALUES (1, 'openai_compatible', 'https://x/v1', ?, 'chat', 'embed')",
                (db.encrypt_for_storage("sk-test"),),
            )
            convo_id = conn.execute(
                "INSERT INTO conversations (user_id, notebook_id, title) VALUES (?, ?, 'T')",
                (user["id"], notebook_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO messages (conversation_id, user_id, role, content) VALUES (?, ?, 'user', '原始問題')",
                (convo_id, user["id"]),
            )
            msg_id = conn.execute(
                "INSERT INTO messages (conversation_id, user_id, role, content, citations_json, metadata_json) "
                "VALUES (?, ?, 'assistant', '答案', '[{\"index\": 1, \"snippet\": \"English source excerpt\"}]', "
                "'{\"outcome\": \"answered\", \"followups\": [\"舊追問\"]}')",
                (convo_id, user["id"]),
            ).lastrowid

        page = client.get(f"/notebooks/{notebook_id}?conversation_id={convo_id}")
        assert "舊追問" not in page.text
        assert "追問生成中" in page.text

        url = f"/notebooks/{notebook_id}/chat/{convo_id}/_followups?message_id={msg_id}"
        first = client.get(url)
        assert first.status_code == 200
        assert "追問一？" in first.text
        assert "舊追問" not in first.text
        assert "data-fill-question" in first.text
        assert calls["n"] == 1

        # Cached in metadata_json -> the second request must not regenerate.
        second = client.get(url)
        assert "追問一？" in second.text
        assert calls["n"] == 1
        with db.connect() as conn:
            meta = conn.execute("SELECT metadata_json FROM messages WHERE id = ?", (msg_id,)).fetchone()
        assert "追問一" in meta["metadata_json"]


def test_notebook_can_disable_followups(monkeypatch, tmp_path):
    """Notebook-level setting prevents lazy follow-up generation."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    calls = {"n": 0}

    async def fake_followups(question, answer, settings, source_context=None, **kwargs):
        calls["n"] += 1
        return ["不應產生"]

    monkeypatch.setattr(main, "suggest_followup_questions", fake_followups)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db, title="追問設定")
        with db.connect() as conn:
            notebook = conn.execute(
                "SELECT followups_enabled FROM notebooks WHERE id = ?", (notebook_id,)
            ).fetchone()
        assert notebook["followups_enabled"] == 1

        legacy_rename = client.post(
            f"/notebooks/{notebook_id}/rename",
            data={"title": "追問設定", "emoji": "📓", "description": ""},
            follow_redirects=False,
        )
        assert legacy_rename.status_code == 303
        with db.connect() as conn:
            assert conn.execute(
                "SELECT followups_enabled FROM notebooks WHERE id = ?", (notebook_id,)
            ).fetchone()["followups_enabled"] == 1

        renamed = client.post(
            f"/notebooks/{notebook_id}/rename",
            data={"title": "追問設定", "emoji": "📓", "description": "", "followups_setting_present": "1"},
            follow_redirects=False,
        )
        assert renamed.status_code == 303
        with db.connect() as conn:
            assert conn.execute(
                "SELECT followups_enabled FROM notebooks WHERE id = ?", (notebook_id,)
            ).fetchone()["followups_enabled"] == 0
            convo_id = conn.execute(
                "INSERT INTO conversations (user_id, notebook_id, title) VALUES (?, ?, 'T')",
                (user["id"], notebook_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO messages (conversation_id, user_id, role, content) VALUES (?, ?, 'user', '原始問題')",
                (convo_id, user["id"]),
            )
            msg_id = conn.execute(
                "INSERT INTO messages (conversation_id, user_id, role, content, metadata_json) "
                "VALUES (?, ?, 'assistant', '答案', '{\"outcome\": \"answered\"}')",
                (convo_id, user["id"]),
            ).lastrowid

        resp = client.get(f"/notebooks/{notebook_id}/chat/{convo_id}/_followups?message_id={msg_id}")
        assert resp.status_code == 200
        assert resp.text == ""
        assert calls["n"] == 0


def test_export_conversation_and_notes_markdown(monkeypatch, tmp_path):
    """A3: conversation and notes export as downloadable Markdown."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db, title="匯出測試")
        with db.connect() as conn:
            convo_id = conn.execute(
                "INSERT INTO conversations (user_id, notebook_id, title) VALUES (?, ?, '對話A')",
                (user["id"], notebook_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO messages (conversation_id, user_id, role, content) VALUES (?, ?, 'user', '問題X')",
                (convo_id, user["id"]),
            )
            conn.execute(
                "INSERT INTO messages (conversation_id, user_id, role, content, citations_json) "
                "VALUES (?, ?, 'assistant', '回答Y [1]', '[{\"index\": 1, \"filename\": \"a.pdf\", \"location\": \"page 1\"}]')",
                (convo_id, user["id"]),
            )
            conn.execute(
                "INSERT INTO notes (notebook_id, user_id, title, content) VALUES (?, ?, '筆記1', '筆記內容')",
                (notebook_id, user["id"]),
            )
            note_id = conn.execute(
                "INSERT INTO notes (notebook_id, user_id, title, content) VALUES (?, ?, '筆記2', '單筆內容')",
                (notebook_id, user["id"]),
            ).lastrowid

        convo = client.get(f"/notebooks/{notebook_id}/chat/{convo_id}/export")
        assert convo.status_code == 200
        assert "text/markdown" in convo.headers["content-type"]
        assert "attachment" in convo.headers["content-disposition"]
        assert "問題X" in convo.text and "回答Y" in convo.text and "a.pdf" in convo.text

        notes = client.get(f"/notebooks/{notebook_id}/notes/export")
        assert notes.status_code == 200
        assert "筆記內容" in notes.text
        assert "attachment" in notes.headers["content-disposition"]

        note = client.get(f"/notebooks/{notebook_id}/notes/{note_id}/export")
        assert note.status_code == 200
        assert "單筆內容" in note.text
        assert "筆記內容" not in note.text
        assert "attachment" in note.headers["content-disposition"]

        with db.connect() as conn:
            events = [
                dict(row)
                for row in conn.execute(
                    "SELECT action, sensitivity, metadata_json FROM audit_events ORDER BY id"
                ).fetchall()
            ]
        actions = [event["action"] for event in events]
        assert "conversation_exported" in actions
        assert "notes_exported" in actions
        assert "note_exported" in actions
        assert all(event["sensitivity"] == "high" for event in events)
        metadata_blob = "\n".join(event["metadata_json"] for event in events)
        assert "問題X" not in metadata_blob
        assert "回答Y" not in metadata_blob
        assert "筆記內容" not in metadata_blob


def test_async_llm_route_sqlite_does_not_block_event_loop(monkeypatch, tmp_path):
    """A slow SQLite phase in an async LLM route must run off the event loop.

    Compare is representative of suggestions, briefing, chat, follow-ups,
    minutes, artifacts, and translation: each route does a bounded SQLite phase
    before awaiting an LLM call.  A delayed connection used to freeze every
    request on that worker until the synchronous query returned.
    """
    main, db = _fresh_app(monkeypatch, tmp_path)
    db.init_db()
    user, notebook_id = _seed_notebook(db)
    source_ids = [
        _seed_indexed_source(db, user["id"], notebook_id, "a.pdf", "摘要 A"),
        _seed_indexed_source(db, user["id"], notebook_id, "b.pdf", "摘要 B"),
    ]
    with db.connect() as conn:
        conn.execute("UPDATE llm_settings SET chat_model = 'chat' WHERE id = 1")

    real_connect = main.connect

    def slow_connect():
        time.sleep(0.5)
        return real_connect()

    async def fake_compare(*_args, **_kwargs):
        return "比較完成"

    monkeypatch.setattr(main, "connect", slow_connect)
    monkeypatch.setattr(main, "compare_sources", fake_compare)
    request = Request({
        "type": "http",
        "method": "POST",
        "path": f"/notebooks/{notebook_id}/compare",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    })

    async def drive():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        beat = asyncio.create_task(ticker())
        response = await main.notebook_compare(
            request,
            notebook_id,
            user,
            source_ids=source_ids,
            focus="",
        )
        beat.cancel()
        return response, ticks

    response, ticks = asyncio.run(drive())
    assert response.status_code == 200
    assert ticks >= 3, f"event loop was starved during SQLite work (ticks={ticks})"


def test_chat_empty_shows_starter_questions(monkeypatch, tmp_path):
    """U16: starter questions render in the chat empty-state, not the Studio."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        _seed_indexed_source(db, user["id"], notebook_id, "a.pdf")
        with db.connect() as conn:
            conn.execute(
                "UPDATE notebooks SET suggestions_json = ?, suggestions_at = CURRENT_TIMESTAMP WHERE id = ?",
                ('["這份文件的重點是什麼？"]', notebook_id),
            )

        empty = client.get(f"/notebooks/{notebook_id}/_chat-empty")
        assert empty.status_code == 200
        assert 'class="suggestion-chip"' in empty.text
        assert "這份文件的重點是什麼？" in empty.text
        # The relocated section no longer self-polls indexed-sources-changed
        # (the empty-state owns that refresh).
        assert 'id="studio-suggestions"' in empty.text


def test_citation_payload_and_merge_carry_chunk_id(monkeypatch, tmp_path):
    """U3: citations carry the chunk row id so the chip can open the preview at it."""
    main, _db = _fresh_app(monkeypatch, tmp_path)

    chunks = [{"id": 7, "source_id": 3, "filename": "a.pdf", "location": "p1", "text": "hello", "score": 0.9}]
    cites = main.citation_payload(chunks)
    assert cites[0]["chunk_id"] == 7 and cites[0]["source_id"] == 3

    vec = [{"id": 5, "source_id": 2, "filename": "f", "location": "l", "text": "alpha beta", "vector_score": 0.8}]
    merged = main.merge_candidates(vec, [], ["alpha"])
    assert merged[5]["id"] == 5
