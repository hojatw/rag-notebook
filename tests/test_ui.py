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
        assert "上傳來源" in empty.text
        assert "等待索引" in empty.text
        assert "開始提問" in empty.text
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


def test_streaming_ask_saves_answer_and_returns_final_messages(monkeypatch, tmp_path):
    """U2: streaming ask emits chunks, saves the final assistant message, and returns refreshed HTML."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    async def fake_retrieve(question, conversation_id, settings, history, user_id, source_ids=None):
        return [{
            "id": 1,
            "source_id": 10,
            "filename": "source.md",
            "location": "section",
            "text": "答案依據",
            "score": 0.9,
        }]

    async def fake_stream(question, chunks, settings):
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


def test_global_search_scopes_to_current_user(monkeypatch, tmp_path):
    """U9: global search covers owned content and does not leak other users' rows."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        with db.connect() as conn:
            admin = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
            other = conn.execute("SELECT * FROM users WHERE username = 'user'").fetchone()
            notebook_id = conn.execute(
                "INSERT INTO notebooks (user_id, title, description) VALUES (?, '搜尋測試', 'alpha 專案')",
                (admin["id"],),
            ).lastrowid
            conn.execute(
                "INSERT INTO sources (user_id, notebook_id, filename, stored_path, status, summary) "
                "VALUES (?, ?, 'alpha.txt', '/tmp/a.txt', 'indexed', 'alpha 摘要')",
                (admin["id"], notebook_id),
            )
            convo_id = conn.execute(
                "INSERT INTO conversations (user_id, notebook_id, title) VALUES (?, ?, 'alpha 對話')",
                (admin["id"], notebook_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO notes (notebook_id, user_id, title, content) VALUES (?, ?, 'alpha 筆記', '內容')",
                (notebook_id, admin["id"]),
            )
            other_nb = conn.execute(
                "INSERT INTO notebooks (user_id, title, description) VALUES (?, 'alpha 不可見', '')",
                (other["id"],),
            ).lastrowid
            conn.execute(
                "INSERT INTO notes (notebook_id, user_id, title, content) VALUES (?, ?, 'alpha 私人', '不可見')",
                (other_nb, other["id"]),
            )

        resp = client.get("/search?q=alpha")
        assert resp.status_code == 200
        assert "搜尋測試" in resp.text
        assert "alpha.txt" in resp.text
        assert "alpha 對話" in resp.text
        assert "alpha 筆記" in resp.text
        assert f"conversation_id={convo_id}" in resp.text
        assert "alpha 不可見" not in resp.text
        assert "alpha 私人" not in resp.text


def test_followups_generate_once_and_cache(monkeypatch, tmp_path):
    """A2: follow-up chips are generated once, cached into message metadata."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    calls = {"n": 0}

    async def fake_followups(question, answer, settings, source_context=None):
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

    async def fake_followups(question, answer, settings, source_context=None):
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


def test_minutes_warns_before_non_meeting_source(monkeypatch, tmp_path):
    """Non-meeting sources show a warning first and do not spend an LLM call."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    calls = {"n": 0}
    ambiguous = main.meeting_likelihood([{"text": "主持人：藥師\n這是一份藥品仿單，包含劑量資訊。"}])
    assert ambiguous["is_likely"] is False
    assert "只看到「主持人或講者欄位」" in ambiguous["reason"]
    assert "發言者標記" not in ambiguous["reason"]

    async def fake_minutes(chunks, settings):
        calls["n"] += 1
        return "## 會議主題\n不應先產生"

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
                "VALUES (?, ?, 'label.txt', '/tmp/label.txt', 'indexed')",
                (user["id"], notebook_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO chunks (user_id, source_id, chunk_index, location, text, embedding_json) "
                "VALUES (?, ?, 0, 'document', '這是一份藥品仿單，包含劑量、禁忌症與副作用資訊。', '[]')",
                (user["id"], source_id),
            )

        resp = client.post(f"/notebooks/{notebook_id}/minutes", data={"source_id": source_id})
        assert resp.status_code == 200
        assert "不像會議逐字稿" in resp.text
        assert "仍然整理" in resp.text
        assert "發言者標記" not in resp.text
        assert calls["n"] == 0
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) c FROM notes WHERE notebook_id = ?", (notebook_id,)).fetchone()["c"] == 0

        forced = client.post(f"/notebooks/{notebook_id}/minutes", data={"source_id": source_id, "force": "1"})
        assert forced.status_code == 200
        assert "存入筆記" in forced.text
        assert calls["n"] == 1


def test_minutes_decline_is_not_saved(monkeypatch, tmp_path):
    """If the model says the source is not meeting-like, show it but don't save."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    async def fake_minutes(chunks, settings):
        return "This does not look like a meeting record."

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
                "VALUES (?, ?, 'meeting.txt', '/tmp/meeting.txt', 'indexed')",
                (user["id"], notebook_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO chunks (user_id, source_id, chunk_index, location, text, embedding_json) "
                "VALUES (?, ?, 0, 'document', '會議逐字稿\n主持人：今天討論專案進度。', '[]')",
                (user["id"], source_id),
            )

        resp = client.post(f"/notebooks/{notebook_id}/minutes", data={"source_id": source_id})
        assert resp.status_code == 200
        assert "未存入筆記" in resp.text
        assert resp.headers.get("HX-Trigger") is None
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) c FROM notes WHERE notebook_id = ?", (notebook_id,)).fetchone()["c"] == 0


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
