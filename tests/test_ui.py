import importlib

from fastapi.testclient import TestClient


def _fresh_app(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOTEBOOKLM_SECRET", "ui-test-secret")

    import app.security as security
    import app.db as db
    import app.vector_store as vector_store
    import app.ingest as ingest
    import app.main as main

    for module in (security, db, vector_store, ingest, main):
        importlib.reload(module)
    vector_store.reset_client()
    return main, db


def _login(client: TestClient):
    response = client.post(
        "/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_notebook_forms_render_preset_emoji_picker(monkeypatch, tmp_path):
    main, _db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)

        home = client.get("/notebooks")
        assert home.status_code == 200
        assert 'class="emoji-picker"' in home.text
        assert 'name="emoji"' in home.text
        assert "🧠" in home.text
        # The Alpine state must use SINGLE-quoted JS literals. Using tojson
        # (double quotes) collides with the double-quoted HTML attribute and
        # silently breaks selection — guard against that regression.
        assert "x-data=\"{ selected: '📓' }\"" in home.text
        assert "@click=\"selected = '🧠'\"" in home.text
        assert '{ selected: "' not in home.text

        created = client.post(
            "/notebooks/new",
            data={"title": "Research", "emoji": "🧠", "description": ""},
            follow_redirects=False,
        )
        assert created.status_code == 303

        notebook = client.get(created.headers["location"])
        assert notebook.status_code == 200
        assert notebook.text.count('class="emoji-picker"') >= 1
        assert "🧠" in notebook.text
        assert "⚙️" in notebook.text


def test_source_partial_splits_row_and_studio_refresh_events(monkeypatch, tmp_path):
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        with db.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
            notebook_id = conn.execute(
                "INSERT INTO notebooks (user_id, title) VALUES (?, 'Events')",
                (user["id"],),
            ).lastrowid
            processing_id = conn.execute(
                """
                INSERT INTO sources (user_id, notebook_id, filename, stored_path, status)
                VALUES (?, ?, 'processing.txt', '/tmp/processing.txt', 'processing')
                """,
                (user["id"], notebook_id),
            ).lastrowid
            indexed_id = conn.execute(
                """
                INSERT INTO sources (user_id, notebook_id, filename, stored_path, status)
                VALUES (?, ?, 'indexed.txt', '/tmp/indexed.txt', 'indexed')
                """,
                (user["id"], notebook_id),
            ).lastrowid

        processing = client.get(f"/notebooks/{notebook_id}/sources/{processing_id}/_partial")
        assert processing.status_code == 200
        assert processing.headers["HX-Trigger"] == "source-status-changed"

        indexed = client.get(f"/notebooks/{notebook_id}/sources/{indexed_id}/_partial")
        assert indexed.status_code == 200
        assert indexed.headers["HX-Trigger"] == "source-status-changed, indexed-sources-changed"


def test_chat_empty_partial_reflects_indexing_state(monkeypatch, tmp_path):
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        with db.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
            notebook_id = conn.execute(
                "INSERT INTO notebooks (user_id, title) VALUES (?, 'Empty')",
                (user["id"],),
            ).lastrowid

        url = f"/notebooks/{notebook_id}/_chat-empty"

        # No sources at all.
        empty = client.get(url)
        assert empty.status_code == 200
        assert "先新增一個來源開始使用" in empty.text
        # The container must re-fetch itself when indexing state changes.
        assert 'hx-trigger="indexed-sources-changed from:body"' in empty.text

        # A source mid-indexing.
        with db.connect() as conn:
            source_id = conn.execute(
                """
                INSERT INTO sources (user_id, notebook_id, filename, stored_path, status)
                VALUES (?, ?, 'doc.txt', '/tmp/doc.txt', 'processing')
                """,
                (user["id"], notebook_id),
            ).lastrowid
        processing = client.get(url)
        assert "索引建立中" in processing.text

        # Once indexed, the center flips to the ask prompt.
        with db.connect() as conn:
            conn.execute("UPDATE sources SET status = 'indexed' WHERE id = ?", (source_id,))
        indexed = client.get(url)
        assert "問任何關於你來源文件的問題" in indexed.text


def test_upload_enqueues_ingest_job_instead_of_running_inline(monkeypatch, tmp_path):
    """P1-1: uploading queues an ingest_jobs row; the source waits for a worker."""
    # Disable the inline worker so nothing drains the queue during the test.
    monkeypatch.setenv("NOTEBOOKLM_INLINE_WORKER", "0")
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        # Seed llm_settings directly: the /settings route does a live network
        # probe that can't run offline, but the upload route only needs a
        # "ready" config. (Schema exists now — init_db ran in the lifespan.)
        with db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_settings (id, provider, base_url, api_key, chat_model, embedding_model) "
                "VALUES (1, 'openai_compatible', 'https://api.example.com/v1', ?, 'chat', 'embed')",
                (db.encrypt_for_storage("sk-test"),),
            )
        with db.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
            notebook_id = conn.execute(
                "INSERT INTO notebooks (user_id, title) VALUES (?, 'Q')",
                (user["id"],),
            ).lastrowid

        resp = client.post(
            f"/notebooks/{notebook_id}/sources/upload",
            files={"files": ("a.txt", b"hello world", "text/plain")},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        with db.connect() as conn:
            source = conn.execute(
                "SELECT id, status FROM sources WHERE notebook_id = ?", (notebook_id,)
            ).fetchone()
            job = conn.execute(
                "SELECT status FROM ingest_jobs WHERE source_id = ?", (source["id"],)
            ).fetchone()
        # Source is parked until a worker picks it up; a queued job exists.
        assert source["status"] == "uploaded"
        assert job is not None
        assert job["status"] == "queued"


def _seed_notebook(db, title="NB"):
    with db.connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
        notebook_id = conn.execute(
            "INSERT INTO notebooks (user_id, title) VALUES (?, ?)", (user["id"], title)
        ).lastrowid
    return dict(user), notebook_id


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


def test_followups_generate_once_and_cache(monkeypatch, tmp_path):
    """A2: follow-up chips are generated once, cached into message metadata."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    calls = {"n": 0}

    async def fake_followups(question, answer, settings):
        calls["n"] += 1
        return ["追問一？", "追問二？"]

    monkeypatch.setattr(main, "suggest_followup_questions", fake_followups)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
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

        url = f"/notebooks/{notebook_id}/chat/{convo_id}/_followups?message_id={msg_id}"
        first = client.get(url)
        assert first.status_code == 200
        assert "追問一？" in first.text
        assert "data-fill-question" in first.text
        assert calls["n"] == 1

        # Cached in metadata_json -> the second request must not regenerate.
        second = client.get(url)
        assert "追問一？" in second.text
        assert calls["n"] == 1
        with db.connect() as conn:
            meta = conn.execute("SELECT metadata_json FROM messages WHERE id = ?", (msg_id,)).fetchone()
        assert "追問一" in meta["metadata_json"]


def test_minutes_saves_note_and_triggers_refresh(monkeypatch, tmp_path):
    """A1: minutes generation renders the result, saves a note, fires notes-changed."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    async def fake_minutes(chunks, settings):
        return "## 會議主題\n測試會議\n\n## 重要決議\n- 通過提案"

    monkeypatch.setattr(main, "generate_meeting_minutes", fake_minutes)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_settings (id, provider, base_url, api_key, chat_model, embedding_model) "
                "VALUES (1, 'openai_compatible', 'https://x/v1', ?, 'chat', 'embed')",
                (db.encrypt_for_storage("sk-test"),),
            )
            source_id = conn.execute(
                "INSERT INTO sources (user_id, notebook_id, filename, stored_path, status) "
                "VALUES (?, ?, 'meeting.txt', '/tmp/m.txt', 'indexed')",
                (user["id"], notebook_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO chunks (user_id, source_id, chunk_index, location, text, embedding_json) "
                "VALUES (?, ?, 0, 'document', '會議逐字稿內容', '[]')",
                (user["id"], source_id),
            )

        resp = client.post(f"/notebooks/{notebook_id}/minutes", data={"source_id": source_id})
        assert resp.status_code == 200
        assert "存入筆記" in resp.text
        assert resp.headers.get("HX-Trigger") == "notes-changed"
        with db.connect() as conn:
            note = conn.execute(
                "SELECT title, content FROM notes WHERE notebook_id = ?", (notebook_id,)
            ).fetchone()
        assert note is not None
        assert note["title"].startswith("會議整理")
        assert "重要決議" in note["content"]


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

        convo = client.get(f"/notebooks/{notebook_id}/chat/{convo_id}/export")
        assert convo.status_code == 200
        assert "text/markdown" in convo.headers["content-type"]
        assert "attachment" in convo.headers["content-disposition"]
        assert "問題X" in convo.text and "回答Y" in convo.text and "a.pdf" in convo.text

        notes = client.get(f"/notebooks/{notebook_id}/notes/export")
        assert notes.status_code == 200
        assert "筆記內容" in notes.text
        assert "attachment" in notes.headers["content-disposition"]
