import importlib
import json

from fastapi.testclient import TestClient as FastAPITestClient


class TestClient(FastAPITestClient):
    """Test client that behaves like the browser by echoing the CSRF token."""

    def post(self, url, *args, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        has_csrf_header = any(key.lower() == "x-csrf-token" for key in headers)
        data = kwargs.get("data")
        has_csrf_form_field = isinstance(data, dict) and "csrf_token" in data
        if not has_csrf_header and not has_csrf_form_field:
            token = self.cookies.get("csrf_token")
            if not token:
                self.get("/login")
                token = self.cookies.get("csrf_token")
            if token:
                headers["X-CSRF-Token"] = token
        if headers:
            kwargs["headers"] = headers
        return super().post(url, *args, **kwargs)


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


def test_csrf_token_required_for_login_post(monkeypatch, tmp_path):
    main, _db = _fresh_app(monkeypatch, tmp_path)

    with FastAPITestClient(main.app) as client:
        page = client.get("/login")
        assert page.status_code == 200
        assert 'name="csrf_token"' in page.text

        rejected = client.post(
            "/login",
            data={"username": "admin", "password": "admin123"},
            follow_redirects=False,
        )
        assert rejected.status_code == 403

        token = client.cookies.get("csrf_token")
        accepted = client.post(
            "/login",
            data={"username": "admin", "password": "admin123", "csrf_token": token},
            follow_redirects=False,
        )
        assert accepted.status_code == 303


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


def test_notebook_grid_caps_large_lists_with_hint(monkeypatch, tmp_path):
    """M4: the notebook landing page should not render an unbounded grid silently."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        with db.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
            for i in range(101):
                conn.execute(
                    "INSERT INTO notebooks (user_id, title, updated_at) VALUES (?, ?, datetime('now', ?))",
                    (user["id"], f"Notebook {i:03d}", f"-{i} seconds"),
                )

        home = client.get("/notebooks")
        assert home.status_code == 200
        assert home.text.count('class="card notebook-card"') == 100
        assert "Notebook 000" in home.text
        assert "Notebook 100" not in home.text
        assert "僅顯示最近 100 筆" in home.text


def test_search_caps_each_result_section_with_hint(monkeypatch, tmp_path):
    """M4: search should tell users when a per-type result cap hides older rows."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        with db.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
            notebook_id = conn.execute(
                "INSERT INTO notebooks (user_id, title) VALUES (?, 'Search container')",
                (user["id"],),
            ).lastrowid
            for i in range(13):
                conn.execute(
                    """
                    INSERT INTO notes (notebook_id, user_id, title, content, updated_at)
                    VALUES (?, ?, ?, 'content', datetime('now', ?))
                    """,
                    (notebook_id, user["id"], f"needle note {i:02d}", f"-{i} seconds"),
                )

        resp = client.get("/search?q=needle")
        assert resp.status_code == 200
        assert resp.text.count('<span class="result-type">筆記</span>') == 12
        assert "needle note 00" in resp.text
        assert "needle note 12" not in resp.text
        assert "僅顯示最近 12 筆" in resp.text


def test_notebook_renders_mobile_workspace_switcher(monkeypatch, tmp_path):
    """U10: narrow viewports get a pane switcher with chat selected by default."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        _user, notebook_id = _seed_notebook(db, title="Mobile")

        page = client.get(f"/notebooks/{notebook_id}")
        assert page.status_code == 200
        assert 'data-workspace-switcher' in page.text
        assert 'aria-label="工作區面板切換"' in page.text
        assert 'data-workspace-mobile-tabs' in page.text
        assert 'data-active-pane="chat"' in page.text
        assert 'data-workspace-tab="sources">來源</button>' in page.text
        assert 'data-workspace-tab="chat">對話</button>' in page.text
        assert 'data-workspace-tab="studio">工作台</button>' in page.text
        assert 'aria-controls="workspace-sources-pane"' in page.text
        assert 'aria-controls="chat-pane"' in page.text
        assert 'aria-controls="workspace-studio-pane"' in page.text
        assert 'id="workspace-sources-pane"' in page.text
        assert 'id="chat-pane"' in page.text
        assert 'id="workspace-studio-pane"' in page.text
        assert 'data-mobile-pane="sources"' in page.text
        assert 'data-mobile-pane="chat"' in page.text
        assert 'data-mobile-pane="studio"' in page.text
        assert 'class="workspace-mobile-tab is-active"' in page.text
        assert 'aria-selected="true"' in page.text


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
            audit = conn.execute(
                "SELECT * FROM audit_events WHERE action = 'source_uploaded' AND target_id = ?",
                (source["id"],),
            ).fetchone()
        # Source is parked until a worker picks it up; a queued job exists.
        assert source["status"] == "uploaded"
        assert job is not None
        assert job["status"] == "queued"
        assert audit is not None
        assert json.loads(audit["metadata_json"])["filename"] == "a.txt"


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


def test_minutes_renders_with_save_button_no_autosave(monkeypatch, tmp_path):
    """A1: minutes generation renders the result + a save button but does NOT
    auto-save; the model only offers a savable result when it produces minutes."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    async def fake_minutes(chunks, settings, **kwargs):
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
        assert "重要決議" in resp.text
        assert "存成筆記" in resp.text  # manual save offered
        assert resp.headers.get("HX-Trigger") is None  # not auto-saved
        with db.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) c FROM notes WHERE notebook_id = ?", (notebook_id,)
            ).fetchone()["c"] == 0


def test_minutes_warns_before_non_meeting_source(monkeypatch, tmp_path):
    """Non-meeting sources show a warning first and do not spend an LLM call."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    calls = {"n": 0}
    ambiguous = main.meeting_likelihood([{"text": "主持人：藥師\n這是一份藥品仿單，包含劑量資訊。"}])
    assert ambiguous["is_likely"] is False
    assert "只看到「主持人或講者欄位」" in ambiguous["reason"]
    assert "發言者標記" not in ambiguous["reason"]

    async def fake_minutes(chunks, settings, **kwargs):
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
        assert "存成筆記" in forced.text  # forced result offers manual save
        assert calls["n"] == 1
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) c FROM notes WHERE notebook_id = ?", (notebook_id,)).fetchone()["c"] == 0


def test_minutes_decline_is_not_saved(monkeypatch, tmp_path):
    """If the model says the source is not meeting-like, show it but don't save."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    async def fake_minutes(chunks, settings, **kwargs):
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
        assert "不像會議記錄" in resp.text
        assert "存成筆記" not in resp.text  # non-meeting source offers no save
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


def test_user_data_lifecycle_actions_are_audited(monkeypatch, tmp_path):
    """Audit round 2: notebook/source/chat/note lifecycle mutations are traceable."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        created = client.post(
            "/notebooks/new",
            data={"title": "Audit NB", "emoji": "A", "description": "private description"},
            follow_redirects=False,
        )
        assert created.status_code == 303
        notebook_id = int(created.headers["location"].rstrip("/").split("/")[-1])

        renamed = client.post(
            f"/notebooks/{notebook_id}/rename",
            data={
                "title": "Audit NB renamed",
                "emoji": "B",
                "description": "",
                "followups_setting_present": "1",
                "followups_enabled": "1",
            },
            follow_redirects=False,
        )
        assert renamed.status_code == 303

        with db.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
            source_id = conn.execute(
                """
                INSERT INTO sources (user_id, notebook_id, filename, stored_path, status)
                VALUES (?, ?, 'audit-source.txt', '/tmp/missing-audit-source.txt', 'indexed')
                """,
                (user["id"], notebook_id),
            ).lastrowid

        reindexed = client.post(f"/notebooks/{notebook_id}/sources/{source_id}/reindex", follow_redirects=False)
        assert reindexed.status_code == 303
        deleted_source = client.post(f"/notebooks/{notebook_id}/sources/{source_id}/delete", follow_redirects=False)
        assert deleted_source.status_code == 303

        new_convo = client.post(f"/notebooks/{notebook_id}/chat/new", follow_redirects=False)
        assert new_convo.status_code == 303
        conversation_id = int(new_convo.headers["location"].split("conversation_id=")[-1])
        renamed_convo = client.post(
            f"/notebooks/{notebook_id}/chat/{conversation_id}/rename",
            data={"title": "Audit conversation"},
            follow_redirects=False,
        )
        assert renamed_convo.status_code == 303
        deleted_convo = client.post(f"/notebooks/{notebook_id}/chat/{conversation_id}/delete", follow_redirects=False)
        assert deleted_convo.status_code == 303

        added_note = client.post(
            f"/notebooks/{notebook_id}/notes/add",
            data={"title": "Audit note", "content": "Sensitive note content"},
        )
        assert added_note.status_code == 200
        with db.connect() as conn:
            note_id = conn.execute("SELECT id FROM notes WHERE notebook_id = ?", (notebook_id,)).fetchone()["id"]
        edited_note = client.post(
            f"/notebooks/{notebook_id}/notes/{note_id}/edit",
            data={"title": "Audit note edited", "content": "Updated sensitive note content"},
        )
        assert edited_note.status_code == 200
        deleted_note = client.post(f"/notebooks/{notebook_id}/notes/{note_id}/delete")
        assert deleted_note.status_code == 200

        deleted_notebook = client.post(f"/notebooks/{notebook_id}/delete", follow_redirects=False)
        assert deleted_notebook.status_code == 303

        with db.connect() as conn:
            events = [
                dict(row)
                for row in conn.execute(
                    "SELECT action, target_type, sensitivity, metadata_json FROM audit_events ORDER BY id"
                ).fetchall()
            ]
        actions = [event["action"] for event in events]
        for action in [
            "notebook_created",
            "notebook_renamed",
            "source_reindex_requested",
            "source_deleted",
            "conversation_created",
            "conversation_renamed",
            "conversation_deleted",
            "note_added",
            "note_edited",
            "note_deleted",
            "notebook_deleted",
        ]:
            assert action in actions
        high_actions = {event["action"] for event in events if event["sensitivity"] == "high"}
        assert {"source_reindex_requested", "source_deleted", "conversation_deleted", "note_deleted", "notebook_deleted"} <= high_actions
        metadata_blob = "\n".join(event["metadata_json"] for event in events)
        assert "Sensitive note content" not in metadata_blob
        assert "Updated sensitive note content" not in metadata_blob


def _seed_indexed_source(db, user_id, notebook_id, filename="a.pdf", summary="摘要內容"):
    """Insert an indexed source (with a summary) + one chunk; return source id."""
    with db.connect() as conn:
        source_id = conn.execute(
            "INSERT INTO sources (user_id, notebook_id, filename, stored_path, status, summary) "
            "VALUES (?, ?, ?, '/tmp/x', 'indexed', ?)",
            (user_id, notebook_id, filename, summary),
        ).lastrowid
        conn.execute(
            "INSERT INTO chunks (user_id, source_id, chunk_index, location, text, embedding_json) "
            "VALUES (?, ?, 0, 'document', ?, '[]')",
            (user_id, source_id, summary),
        )
    return source_id


def test_admin_eval_workbench_creates_default_profile(monkeypatch, tmp_path):
    """E1a: admin eval shell exposes the active profile and durable run history tables."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)

        resp = client.get("/admin/evals")
        assert resp.status_code == 200
        assert "評測工作台" in resp.text
        assert "目前系統預設" in resp.text           # active-profile line
        assert 'href="/admin/evals/profiles"' in resp.text
        assert "歷史執行紀錄" in resp.text
        assert '<pre class="config-preview">' not in resp.text
        assert "搜尋全站已索引筆記本" in resp.text

        # Profile params live on the dedicated profiles page now.
        profiles_resp = client.get("/admin/evals/profiles")
        assert profiles_resp.status_code == 200
        assert "最終 chunk 數" in profiles_resp.text

        with db.connect() as conn:
            profile = conn.execute("SELECT * FROM retrieval_profiles WHERE is_active = 1").fetchone()
            tables = conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name IN (
                    'retrieval_profiles', 'eval_sets', 'eval_items', 'eval_runs', 'eval_results'
                )
                """
            ).fetchall()

        assert profile is not None
        assert json.loads(profile["params_json"])["final_chunk_count"] == main.FINAL_CHUNK_COUNT
        assert {row["name"] for row in tables} == {
            "retrieval_profiles",
            "eval_sets",
            "eval_items",
            "eval_runs",
            "eval_results",
        }


def test_admin_eval_help_page_documents_tuning_workflow(monkeypatch, tmp_path):
    """E1f: the in-product help page exposes tuning guidance without using the PDF."""
    main, _db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)

        landing = client.get("/admin/evals")
        assert landing.status_code == 200
        assert 'href="/admin/evals/help"' in landing.text

        help_page = client.get("/admin/evals/help")
        assert help_page.status_code == 200
        assert "調參指南" in help_page.text
        assert "先分類錯誤" in help_page.text
        assert "調參方向速查" in help_page.text
        assert "領域提示與回答規則" in help_page.text
        assert "current_profile_fields" not in help_page.text
        assert "<code>vector_weight</code>" in help_page.text
        assert 'aria-current="page">調參指南</a>' in help_page.text


def test_base_and_notebook_include_accessibility_scaffolding(monkeypatch, tmp_path):
    """U13: core pages include skip navigation, modal labels, and menu state."""
    main, _db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        created = client.post(
            "/notebooks/new",
            data={"title": "A11y", "emoji": "📓", "description": ""},
            follow_redirects=False,
        )
        assert created.status_code == 303

        page = client.get(created.headers["location"])
        assert page.status_code == 200
        assert 'class="skip-link" href="#main-content"' in page.text
        assert '<main id="main-content" tabindex="-1"' in page.text
        assert 'aria-label="預覽與工具視窗"' in page.text
        assert 'tabindex="-1" x-ref="panel"' in page.text
        assert 'aria-controls="conversation-menu"' in page.text
        assert 'aria-controls="notebook-menu"' in page.text
        assert 'aria-label="複製回答 Markdown"' in page.text or "data-copy-message" not in page.text


def test_admin_eval_workbench_search_generate_approve_and_delete(monkeypatch, tmp_path):
    """E1b follow-up: all-site notebook search, draft generation, approval, and set deletion."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        admin_user, admin_notebook_id = _seed_notebook(db, "Admin indexed")
        admin_source_id = _seed_indexed_source(db, admin_user["id"], admin_notebook_id, "admin.pdf")
        admin_source_id_2 = _seed_indexed_source(db, admin_user["id"], admin_notebook_id, "admin-2.pdf", summary="second evidence")
        with db.connect() as conn:
            other_user = conn.execute("SELECT * FROM users WHERE username = 'user'").fetchone()
            other_notebook_id = conn.execute(
                "INSERT INTO notebooks (user_id, title) VALUES (?, ?)",
                (other_user["id"], "Customer indexed"),
            ).lastrowid
        _seed_indexed_source(db, other_user["id"], other_notebook_id, "customer.pdf", summary="customer evidence")

        resp = client.get("/admin/evals", params={"notebook_q": "Customer"})
        assert resp.status_code == 200
        assert "Customer indexed" in resp.text
        assert "user · 1 個已索引來源" in resp.text
        assert "Admin indexed" not in resp.text

        created = client.post(
            "/admin/evals/sets",
            data={"notebook_id": admin_notebook_id, "name": "Generated Eval", "description": ""},
            follow_redirects=False,
        )
        assert created.status_code == 303
        eval_set_id = int(created.headers["location"].rstrip("/").split("/")[-1])

        generated = client.post(
            f"/admin/evals/sets/{eval_set_id}/generate",
            data={"count": "2"},
            follow_redirects=False,
        )
        assert generated.status_code == 303
        assert generated.headers["location"] == f"/admin/evals/sets/{eval_set_id}#eval-items"
        generated_again = client.post(
            f"/admin/evals/sets/{eval_set_id}/generate",
            data={"count": "2"},
            follow_redirects=False,
        )
        assert generated_again.status_code == 303

        detail = client.get(f"/admin/evals/sets/{eval_set_id}")
        assert detail.status_code == 200
        assert '<nav aria-label="麵包屑導覽" class="breadcrumb">' in detail.text
        assert 'href="/admin/evals">評測工作台</a>' in detail.text
        assert 'id="eval-items"' in detail.text
        assert "返回 Eval 工作台" not in detail.text
        assert "自動生成 draft 題目" in detail.text
        assert "draft" in detail.text
        assert "approve" in detail.text
        assert "執行 retrieval eval</button>" in detail.text
        assert "disabled" in detail.text

        with db.connect() as conn:
            generated_items = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM eval_items WHERE eval_set_id = ? ORDER BY id ASC", (eval_set_id,)
                ).fetchall()
            ]
        assert len(generated_items) == 2
        assert {item["expected_source_id"] for item in generated_items} == {admin_source_id, admin_source_id_2}
        generated_item = generated_items[0]
        assert generated_item["approved"] == 0
        assert generated_item["expected_chunk_id"] is not None
        assert generated_item["question"].startswith("「")
        assert "來源：admin" in generated_item["question"]
        assert json.loads(generated_item["expected_substrings_json"])

        approved = client.post(
            f"/admin/evals/sets/{eval_set_id}/items/{generated_item['id']}/approve",
            follow_redirects=False,
        )
        assert approved.status_code == 303
        assert approved.headers["location"] == f"/admin/evals/sets/{eval_set_id}#eval-items"
        with db.connect() as conn:
            approved_item = conn.execute("SELECT approved FROM eval_items WHERE id = ?", (generated_item["id"],)).fetchone()
        assert approved_item["approved"] == 1

        htmx_approved = client.post(
            f"/admin/evals/sets/{eval_set_id}/items/{generated_items[1]['id']}/approve",
            headers={"HX-Request": "true"},
        )
        assert htmx_approved.status_code == 200
        assert 'id="eval-items"' in htmx_approved.text
        assert '<!doctype html>' not in htmx_approved.text
        assert "執行 retrieval eval" in htmx_approved.text

        deleted = client.post(f"/admin/evals/sets/{eval_set_id}/delete", follow_redirects=False)
        assert deleted.status_code == 303
        with db.connect() as conn:
            eval_set = conn.execute("SELECT id FROM eval_sets WHERE id = ?", (eval_set_id,)).fetchone()
            item = conn.execute("SELECT id FROM eval_items WHERE eval_set_id = ?", (eval_set_id,)).fetchone()
        assert eval_set is None
        assert item is None


def test_admin_eval_set_llm_authoring_generates_draft_items(monkeypatch, tmp_path):
    """E1e-1: LLM-assisted authoring stores reviewed-only draft eval candidates."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    async def fake_generate_eval_candidates(chunks, settings, count=5, item_types=None, target_language="", **kwargs):
        assert settings["chat_model"] == "chat"
        assert item_types == ["answerable", "cross_lingual", "unanswerable"]
        assert target_language == "Traditional Chinese"
        assert chunks
        chunk = chunks[0]
        return [
            {
                "question": "alpha 的關鍵數字是什麼？",
                "item_type": "answerable",
                "source_id": chunk["source_id"],
                "chunk_id": chunk["chunk_id"],
                "expected_answer": "alpha answer",
                "expected_substrings": ["alpha evidence"],
                "rationale": "covers exact evidence",
            },
            {
                "question": "What does alpha evidence describe?",
                "item_type": "cross_lingual",
                "source_id": chunk["source_id"],
                "chunk_id": chunk["chunk_id"],
                "expected_answer": "alpha answer",
                "expected_substrings": ["alpha evidence"],
                "rationale": "cross-language retrieval",
            },
            {
                "question": "這份文件是否提到 beta approval date?",
                "item_type": "unanswerable",
                "source_id": None,
                "chunk_id": None,
                "expected_answer": "",
                "expected_substrings": [],
                "rationale": "tests abstention later",
            },
        ]

    monkeypatch.setattr(main, "generate_eval_candidates", fake_generate_eval_candidates)

    with TestClient(main.app) as client:
        _login(client)
        admin_user, notebook_id = _seed_notebook(db, "LLM Eval")
        source_id = _seed_indexed_source(db, admin_user["id"], notebook_id, "alpha.pdf", summary="alpha evidence")
        with db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_settings (id, provider, base_url, api_key, chat_model, embedding_model) "
                "VALUES (1, 'openai_compatible', 'https://x/v1', ?, 'chat', 'embed')",
                (db.encrypt_for_storage("sk-test"),),
            )

        created = client.post(
            "/admin/evals/sets",
            data={"notebook_id": notebook_id, "name": "LLM Generated Eval", "description": ""},
            follow_redirects=False,
        )
        eval_set_id = int(created.headers["location"].rstrip("/").split("/")[-1])

        generated = client.post(
            f"/admin/evals/sets/{eval_set_id}/generate/llm",
            data={
                "count": "3",
                "item_types": ["answerable", "cross_lingual", "unanswerable"],
                "source_ids": [str(source_id)],
                "target_language": "Traditional Chinese",
            },
            headers={"HX-Request": "true"},
        )

        assert generated.status_code == 200
        assert 'id="eval-items"' in generated.text
        assert '<!doctype html>' not in generated.text
        assert "LLM 已建立 3 題 draft 候選題" in generated.text
        assert "跨語言" in generated.text
        assert "不可回答" in generated.text
        with db.connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM eval_items WHERE eval_set_id = ? ORDER BY id ASC",
                    (eval_set_id,),
                ).fetchall()
            ]
        assert [row["approved"] for row in rows] == [0, 0, 0]
        assert [row["item_type"] for row in rows] == ["answerable", "cross_lingual", "unanswerable"]
        assert rows[2]["expected_source_id"] is None
        assert rows[2]["expected_substrings_json"] == "[]"
        metadata = json.loads(rows[0]["metadata_json"])
        assert metadata["origin"] == "llm_generated"
        assert metadata["selected_source_ids"] == [source_id]
        assert "alpha evidence" not in rows[0]["metadata_json"]


def test_admin_eval_set_llm_authoring_requires_settings(monkeypatch, tmp_path):
    """E1e-1: missing LLM settings returns a partial error and creates no item."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        admin_user, notebook_id = _seed_notebook(db, "No Settings Eval")
        _seed_indexed_source(db, admin_user["id"], notebook_id, "alpha.pdf", summary="alpha evidence")
        created = client.post(
            "/admin/evals/sets",
            data={"notebook_id": notebook_id, "name": "No Settings", "description": ""},
            follow_redirects=False,
        )
        eval_set_id = int(created.headers["location"].rstrip("/").split("/")[-1])

        generated = client.post(
            f"/admin/evals/sets/{eval_set_id}/generate/llm",
            data={"count": "1", "item_types": "answerable"},
            headers={"HX-Request": "true"},
        )

        assert generated.status_code == 200
        assert "尚未完成 LLM 設定" in generated.text
        with db.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM eval_items WHERE eval_set_id = ?",
                (eval_set_id,),
            ).fetchone()["n"]
        assert count == 0


def test_admin_eval_set_runner_records_results(monkeypatch, tmp_path):
    """E1b: admin can create a manual eval item, run it, and inspect stored metrics."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    async def fake_retrieve(question, conversation_id, settings, history, user_id, source_ids=None, params=None, **kwargs):
        assert question == "alpha?"
        assert conversation_id is None
        assert user_id == admin_user["id"]
        assert source_ids == [source_id]
        assert params is not None and "vector_weight" in params
        return [
            {
                "id": 42,
                "source_id": source_id,
                "filename": "a.pdf",
                "location": "document",
                "text": "alpha evidence is here",
                "score": 0.91,
                "vector_score": 0.8,
                "keyword_score": 0.6,
            }
        ]

    monkeypatch.setattr(main, "retrieve", fake_retrieve)

    with TestClient(main.app) as client:
        _login(client)
        admin_user, notebook_id = _seed_notebook(db)
        source_id = _seed_indexed_source(db, admin_user["id"], notebook_id, "a.pdf", summary="alpha evidence")

        created = client.post(
            "/admin/evals/sets",
            data={"notebook_id": notebook_id, "name": "Alpha Eval", "description": "Manual smoke"},
            follow_redirects=False,
        )
        assert created.status_code == 303
        eval_set_id = int(created.headers["location"].rstrip("/").split("/")[-1])

        bad_item = client.post(
            f"/admin/evals/sets/{eval_set_id}/items",
            data={"question": "bad?", "expected_source_id": "not-a-number", "expected_substrings": ""},
        )
        assert bad_item.status_code == 400

        item = client.post(
            f"/admin/evals/sets/{eval_set_id}/items",
            data={
                "question": "alpha?",
                "expected_source_id": str(source_id),
                "expected_substrings": "alpha evidence",
                "notes": "ground truth",
            },
            follow_redirects=False,
        )
        assert item.status_code == 303

        run = client.post(f"/admin/evals/sets/{eval_set_id}/run", follow_redirects=False)
        assert run.status_code == 303
        run_id = int(run.headers["location"].rstrip("/").split("/")[-1])

        detail = client.get(f"/admin/evals/runs/{run_id}")
        assert detail.status_code == 200
        assert "Alpha Eval" in detail.text
        assert '<nav aria-label="麵包屑導覽" class="breadcrumb">' in detail.text
        assert 'href="/admin/evals">評測工作台</a>' in detail.text
        assert f'href="/admin/evals/sets/{eval_set_id}">Alpha Eval</a>' in detail.text
        assert "返回 Eval Set" not in detail.text
        assert "hit" in detail.text
        assert "alpha evidence is here" in detail.text
        assert "預期依據" in detail.text
        assert "substrings: alpha evidence" in detail.text
        assert "診斷：命中預期依據" in detail.text
        assert '<pre class="config-preview">' not in detail.text
        assert "低信心閾值" in detail.text

        with db.connect() as conn:
            row = conn.execute("SELECT * FROM eval_runs WHERE id = ?", (run_id,)).fetchone()
            result = conn.execute("SELECT * FROM eval_results WHERE run_id = ?", (run_id,)).fetchone()

        metrics = json.loads(row["metrics_json"])
        retrieved = json.loads(result["retrieved_json"])
        assert row["status"] == "succeeded"
        assert metrics["recall_at_k"] == 1.0
        assert metrics["mrr"] == 1.0
        assert metrics["hits"] == 1
        assert result["status"] == "hit"
        assert result["hit_rank"] == 1
        assert result["top_score"] == 0.91
        assert retrieved[0]["chunk_id"] == 42


def test_create_apply_and_rollback_retrieval_profile(monkeypatch, tmp_path):
    """E1c: create a candidate profile, apply it to live retrieval, then roll back."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        # Eval-sets page links to the dedicated profiles page, not inlines it.
        landing = client.get("/admin/evals")
        assert landing.status_code == 200
        assert 'href="/admin/evals/profiles"' in landing.text
        assert "目前作用中的檢索 Profile" in landing.text

        profiles_page = client.get("/admin/evals/profiles")  # creates the baseline profile
        assert profiles_page.status_code == 200
        assert "檢索 Profile" in profiles_page.text
        assert "建立候選 Profile" in profiles_page.text
        assert "系統預設" in profiles_page.text

        defaults = main.current_retrieval_profile_params()
        form = {"name": "keyword-heavy", "description": "raise keyword weight",
                **{k: str(v) for k, v in defaults.items()}}
        form["keyword_weight"] = "0.9"
        form["vector_weight"] = "0.1"
        created = client.post("/admin/evals/profiles", data=form, follow_redirects=False)
        assert created.status_code == 303

        with db.connect() as conn:
            prof = conn.execute("SELECT * FROM retrieval_profiles WHERE name = 'keyword-heavy'").fetchone()
        assert prof["is_active"] == 0
        assert main.active_retrieval_params()["keyword_weight"] == defaults["keyword_weight"]

        applied = client.post(f"/admin/evals/profiles/{prof['id']}/apply", follow_redirects=False)
        assert applied.status_code == 303
        assert main.active_retrieval_params()["keyword_weight"] == 0.9
        assert main.active_retrieval_params()["vector_weight"] == 0.1
        with db.connect() as conn:
            assert conn.execute(
                "SELECT is_active FROM retrieval_profiles WHERE id = ?", (prof["id"],)
            ).fetchone()["is_active"] == 1

        # The system-default profile is now inactive but must still be undeletable.
        with db.connect() as conn:
            baseline = conn.execute("SELECT id FROM retrieval_profiles WHERE is_default = 1").fetchone()
        assert baseline is not None
        refused_default = client.post(f"/admin/evals/profiles/{baseline['id']}/delete", follow_redirects=False)
        assert refused_default.status_code == 400

        # Rollback = apply the default profile again.
        client.post(f"/admin/evals/profiles/{baseline['id']}/apply", follow_redirects=False)
        assert main.active_retrieval_params()["keyword_weight"] == defaults["keyword_weight"]

        # The active profile also cannot be deleted.
        with db.connect() as conn:
            active_id = conn.execute("SELECT id FROM retrieval_profiles WHERE is_active = 1").fetchone()["id"]
        refused = client.post(f"/admin/evals/profiles/{active_id}/delete", follow_redirects=False)
        assert refused.status_code == 400


def test_apply_profile_refuses_requires_reindex(monkeypatch, tmp_path):
    """E1c: index-affecting profiles must not be silently applied to live retrieval."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        client.get("/admin/evals")
        with db.connect() as conn:
            pid = conn.execute(
                "INSERT INTO retrieval_profiles (name, params_json, requires_reindex, is_active) "
                "VALUES ('needs-reindex', '{}', 1, 0)"
            ).lastrowid
        resp = client.post(f"/admin/evals/profiles/{pid}/apply", follow_redirects=False)
        assert resp.status_code == 400

        invalid = client.post("/admin/evals/profiles", data={
            "name": "bad", "description": "",
            "low_confidence_threshold": "x", "vector_weight": "0.7", "keyword_weight": "0.3",
            "candidate_pool_size": "20", "final_chunk_count": "6",
            "rerank_weight": "0.6", "rerank_base_weight": "0.4",
        }, follow_redirects=False)
        assert invalid.status_code == 400


def test_eval_compare_view_and_validation(monkeypatch, tmp_path):
    """E1c: comparison renders param/metric/per-question diffs; rejects bad pairs."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        admin_user, notebook_id = _seed_notebook(db)
        source_id = _seed_indexed_source(db, admin_user["id"], notebook_id, "a.pdf", summary="alpha")
        with db.connect() as conn:
            chunk_id = conn.execute("SELECT id FROM chunks WHERE source_id = ?", (source_id,)).fetchone()["id"]
            set_id = conn.execute(
                "INSERT INTO eval_sets (name, target_user_id, notebook_id, created_by) VALUES ('Cmp', ?, ?, ?)",
                (admin_user["id"], notebook_id, admin_user["id"]),
            ).lastrowid
            item_id = conn.execute(
                "INSERT INTO eval_items (eval_set_id, question, expected_chunk_id, approved) VALUES (?, 'alpha?', ?, 1)",
                (set_id, chunk_id),
            ).lastrowid
            base_params = main.current_retrieval_profile_params()
            cand_params = {**base_params, "keyword_weight": 0.9}

            def mk_run(params, metrics, status="succeeded"):
                return conn.execute(
                    "INSERT INTO eval_runs (eval_set_id, created_by, status, progress_total, progress_current, "
                    "profile_snapshot_json, metrics_json) VALUES (?, ?, ?, 1, 1, ?, ?)",
                    (set_id, admin_user["id"], status, db.dumps(params), db.dumps(metrics)),
                ).lastrowid

            base_id = mk_run(base_params, {"recall_at_k": 0.5, "mrr": 0.5, "hits": 1,
                                           "avg_latency_ms": 10, "avg_top_score": 0.4, "low_confidence_rate": 0.2})
            cand_id = mk_run(cand_params, {"recall_at_k": 1.0, "mrr": 1.0, "hits": 2,
                                           "avg_latency_ms": 8, "avg_top_score": 0.6, "low_confidence_rate": 0.1})
            running_id = mk_run(base_params, {}, status="running")
            conn.execute("INSERT INTO eval_results (run_id, eval_item_id, status, hit_rank, top_score) VALUES (?, ?, 'miss', NULL, 0.4)", (base_id, item_id))
            conn.execute("INSERT INTO eval_results (run_id, eval_item_id, status, hit_rank, top_score) VALUES (?, ?, 'hit', 1, 0.6)", (cand_id, item_id))

        page = client.get(f"/admin/evals/compare?base={base_id}&candidate={cand_id}")
        assert page.status_code == 200
        assert "參數差異" in page.text
        assert "Keyword 權重" in page.text
        assert "進步 1 題" in page.text       # the item went miss -> hit
        assert "指標差異" in page.text

        # A non-succeeded run in the pair is rejected.
        rejected = client.get(f"/admin/evals/compare?base={base_id}&candidate={running_id}")
        assert rejected.status_code == 400


def test_admin_eval_run_results_partial_polls_while_running(monkeypatch, tmp_path):
    """Eval run pages loaded mid-run must refresh results, not only the status card."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        admin_user, notebook_id = _seed_notebook(db)
        source_id = _seed_indexed_source(db, admin_user["id"], notebook_id, "a.pdf", summary="alpha evidence")
        with db.connect() as conn:
            chunk_id = conn.execute("SELECT id FROM chunks WHERE source_id = ?", (source_id,)).fetchone()["id"]
            eval_set_id = conn.execute(
                """
                INSERT INTO eval_sets (name, target_user_id, notebook_id, created_by)
                VALUES ('Polling Eval', ?, ?, ?)
                """,
                (admin_user["id"], notebook_id, admin_user["id"]),
            ).lastrowid
            item_id = conn.execute(
                """
                INSERT INTO eval_items
                (eval_set_id, question, expected_source_id, expected_chunk_id, expected_substrings_json, approved)
                VALUES (?, 'alpha?', ?, ?, ?, 1)
                """,
                (eval_set_id, source_id, chunk_id, db.dumps(["alpha evidence"])),
            ).lastrowid
            run_id = conn.execute(
                """
                INSERT INTO eval_runs
                (eval_set_id, created_by, status, progress_total, profile_snapshot_json, current_step)
                VALUES (?, ?, 'running', 1, ?, '檢索第 1 / 1 題')
                """,
                (eval_set_id, admin_user["id"], db.dumps(main.current_retrieval_profile_params())),
            ).lastrowid

        page = client.get(f"/admin/evals/runs/{run_id}")
        assert page.status_code == 200
        assert f'hx-get="/admin/evals/runs/{run_id}/_status"' in page.text
        assert f'hx-get="/admin/evals/runs/{run_id}/_results"' in page.text
        assert "尚未產生逐題結果" in page.text
        assert "低信心閾值" in page.text

        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO eval_results
                (run_id, eval_item_id, status, hit_rank, top_score, latency_ms, retrieved_json)
                VALUES (?, ?, 'hit', 1, 0.91, 12.5, ?)
                """,
                (
                    run_id,
                    item_id,
                    db.dumps([
                        {
                            "rank": 1,
                            "chunk_id": chunk_id,
                            "source_id": source_id,
                            "filename": "a.pdf",
                            "location": "document",
                            "score": 0.91,
                            "snippet": "alpha evidence",
                        }
                    ]),
                ),
            )
            conn.execute(
                "UPDATE eval_runs SET status = 'succeeded', metrics_json = ? WHERE id = ?",
                (db.dumps({"recall_at_k": 1.0, "mrr": 1.0, "hits": 1, "scored": 1, "avg_latency_ms": 12.5}), run_id),
            )

        results = client.get(f"/admin/evals/runs/{run_id}/_results")
        assert results.status_code == 200
        assert "alpha evidence" in results.text
        assert "預期依據" in results.text
        assert f'hx-get="/admin/evals/runs/{run_id}/_results"' not in results.text


def test_admin_eval_run_results_explain_miss(monkeypatch, tmp_path):
    """Miss rows show expected evidence and why the retrieved chunks did not score."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        admin_user, notebook_id = _seed_notebook(db)
        source_id = _seed_indexed_source(db, admin_user["id"], notebook_id, "a.pdf", summary="expected alpha")
        with db.connect() as conn:
            expected_chunk_id = conn.execute("SELECT id FROM chunks WHERE source_id = ?", (source_id,)).fetchone()["id"]
            other_chunk_id = conn.execute(
                """
                INSERT INTO chunks (user_id, source_id, chunk_index, location, text, embedding_json)
                VALUES (?, ?, 1, 'document p2', 'retrieved beta', '[]')
                """,
                (admin_user["id"], source_id),
            ).lastrowid
            eval_set_id = conn.execute(
                """
                INSERT INTO eval_sets (name, target_user_id, notebook_id, created_by)
                VALUES ('Miss Eval', ?, ?, ?)
                """,
                (admin_user["id"], notebook_id, admin_user["id"]),
            ).lastrowid
            item_id = conn.execute(
                """
                INSERT INTO eval_items
                (eval_set_id, question, expected_source_id, expected_chunk_id, expected_substrings_json, approved)
                VALUES (?, 'why alpha?', ?, ?, ?, 1)
                """,
                (eval_set_id, source_id, expected_chunk_id, db.dumps(["expected alpha"])),
            ).lastrowid
            run_id = conn.execute(
                """
                INSERT INTO eval_runs
                (eval_set_id, created_by, status, progress_total, profile_snapshot_json)
                VALUES (?, ?, 'succeeded', 1, ?)
                """,
                (eval_set_id, admin_user["id"], db.dumps(main.current_retrieval_profile_params())),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO eval_results
                (run_id, eval_item_id, status, hit_rank, top_score, latency_ms, retrieved_json)
                VALUES (?, ?, 'miss', NULL, 0.77, 9.0, ?)
                """,
                (
                    run_id,
                    item_id,
                    db.dumps([
                        {
                            "rank": 1,
                            "chunk_id": other_chunk_id,
                            "source_id": source_id,
                            "filename": "a.pdf",
                            "location": "document p2",
                            "score": 0.77,
                            "snippet": "retrieved beta",
                        }
                    ]),
                ),
            )

        results = client.get(f"/admin/evals/runs/{run_id}/_results")
        assert results.status_code == 200
        assert "why alpha?" in results.text
        assert f"chunk #{expected_chunk_id}" in results.text
        assert "expected alpha" in results.text
        assert "retrieved beta" in results.text
        assert "診斷：有找回同一來源，但不是預期 chunk/片段。" in results.text
        assert "預期 chunk 不在目前 top-k 結果中。" in results.text


def test_eval_run_exports_sanitized_and_full_report_with_audit(monkeypatch, tmp_path):
    """E1d: sanitized export omits evidence text; full export is explicit and audited."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        admin_user, notebook_id = _seed_notebook(db)
        source_id = _seed_indexed_source(db, admin_user["id"], notebook_id, "secret.pdf", summary="expected alpha")
        with db.connect() as conn:
            expected_chunk_id = conn.execute("SELECT id FROM chunks WHERE source_id = ?", (source_id,)).fetchone()["id"]
            eval_set_id = conn.execute(
                """
                INSERT INTO eval_sets (name, target_user_id, notebook_id, created_by)
                VALUES ('Exportable', ?, ?, ?)
                """,
                (admin_user["id"], notebook_id, admin_user["id"]),
            ).lastrowid
            item_id = conn.execute(
                """
                INSERT INTO eval_items
                (eval_set_id, question, expected_source_id, expected_chunk_id, expected_substrings_json, approved)
                VALUES (?, 'where is secret alpha?', ?, ?, ?, 1)
                """,
                (eval_set_id, source_id, expected_chunk_id, db.dumps(["expected alpha"])),
            ).lastrowid
            run_id = conn.execute(
                """
                INSERT INTO eval_runs
                (eval_set_id, created_by, status, progress_total, progress_current,
                 profile_snapshot_json, metrics_json)
                VALUES (?, ?, 'succeeded', 1, 1, ?, ?)
                """,
                (
                    eval_set_id,
                    admin_user["id"],
                    db.dumps(main.current_retrieval_profile_params()),
                    db.dumps({"recall_at_k": 1.0, "mrr": 1.0, "hits": 1}),
                ),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO eval_results
                (run_id, eval_item_id, status, hit_rank, top_score, latency_ms, retrieved_json)
                VALUES (?, ?, 'hit', 1, 0.91, 12.3, ?)
                """,
                (
                    run_id,
                    item_id,
                    db.dumps([
                        {
                            "rank": 1,
                            "chunk_id": expected_chunk_id,
                            "source_id": source_id,
                            "filename": "secret.pdf",
                            "location": "document",
                            "score": 0.91,
                            "snippet": "retrieved customer secret alpha",
                        }
                    ]),
                ),
            )

        page = client.get(f"/admin/evals/runs/{run_id}")
        assert page.status_code == 200
        assert f"/admin/evals/runs/{run_id}/export/sanitized" in page.text
        assert f"/admin/evals/runs/{run_id}/export/full?confirm=1" in page.text
        assert "Full internal report 會包含題目、預期依據與 retrieved snippets" in page.text

        sanitized = client.get(f"/admin/evals/runs/{run_id}/export/sanitized")
        assert sanitized.status_code == 200
        assert "attachment" in sanitized.headers["content-disposition"]
        assert sanitized.json()["export_type"] == "sanitized_run_report"
        assert "where is secret alpha?" not in sanitized.text
        assert "expected alpha" not in sanitized.text
        assert "retrieved customer secret alpha" not in sanitized.text

        refused = client.get(f"/admin/evals/runs/{run_id}/export/full")
        assert refused.status_code == 400

        full = client.get(f"/admin/evals/runs/{run_id}/export/full?confirm=1")
        assert full.status_code == 200
        full_json = full.json()
        assert full_json["export_type"] == "full_internal_run_report"
        assert full_json["results"][0]["question"] == "where is secret alpha?"
        assert full_json["results"][0]["expected"]["substrings"] == ["expected alpha"]
        assert full_json["results"][0]["retrieved"][0]["snippet"] == "retrieved customer secret alpha"

        with db.connect() as conn:
            events = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM audit_events WHERE target_type = 'eval_run' ORDER BY id"
                ).fetchall()
            ]
        assert [event["action"] for event in events] == ["eval_run_export_sanitized", "eval_run_export_full"]
        assert events[1]["sensitivity"] == "high"
        assert json.loads(events[1]["metadata_json"])["contains_retrieved_snippets"] is True


def test_profile_export_and_audit_page(monkeypatch, tmp_path):
    """E1d: profile exports are sanitized and the audit page can review events."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        profiles = client.get("/admin/evals/profiles")
        assert profiles.status_code == 200
        with db.connect() as conn:
            profile = conn.execute("SELECT * FROM retrieval_profiles WHERE is_default = 1").fetchone()

        exported = client.get(f"/admin/evals/profiles/{profile['id']}/export")
        assert exported.status_code == 200
        body = exported.json()
        assert body["export_type"] == "sanitized_profile"
        assert body["profile"]["id"] == profile["id"]
        assert "api_key" not in exported.text

        audit = client.get("/admin/audit", params={"action": "profile_export", "sensitivity": "normal"})
        assert audit.status_code == 200
        assert "retrieval_profile_export_sanitized" in audit.text
        assert "retrieval_profile" in audit.text
        assert "admin" in audit.text


def test_high_risk_admin_actions_are_audited(monkeypatch, tmp_path):
    """E1d: user-management and profile-apply changes are queryable audit events."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        create_user = client.post(
            "/admin/users/new",
            data={"username": "audited-user", "password": "password1", "is_admin": "1"},
        )
        assert create_user.status_code == 200

        client.get("/admin/evals/profiles")
        defaults = main.current_retrieval_profile_params()
        created_profile = client.post(
            "/admin/evals/profiles",
            data={
                "name": "audited-profile",
                "description": "",
                **{key: str(value) for key, value in defaults.items()},
            },
            follow_redirects=False,
        )
        assert created_profile.status_code == 303
        with db.connect() as conn:
            profile_id = conn.execute(
                "SELECT id FROM retrieval_profiles WHERE name = 'audited-profile'"
            ).fetchone()["id"]
        applied = client.post(f"/admin/evals/profiles/{profile_id}/apply", follow_redirects=False)
        assert applied.status_code == 303

        audit = client.get("/admin/audit", params={"sensitivity": "high"})
        assert audit.status_code == 200
        assert "user_created" in audit.text
        assert "retrieval_profile_applied" in audit.text
        assert "audited-user" in audit.text
        assert "audited-profile" in audit.text


def test_settings_diagnostics_store_compact_results_and_audit(monkeypatch, tmp_path):
    """O1 Phase 1: admins can test chat/embedding without storing prompts or secrets."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    async def fake_chat_probe(settings, include_image=False, usage_context=None):
        assert settings["api_key"] == "sk-stored"
        assert settings["chat_model"] == "chat-candidate"
        assert include_image is True
        assert usage_context["user_id"] == 1
        return {
            "status": "succeeded",
            "provider": settings["provider"],
            "model": settings["chat_model"],
            "latency_ms": 12.3,
            "capabilities": {
                "streaming": {"status": "succeeded", "latency_ms": 4.0, "usage_available": True},
                "usage_reporting": {"status": "succeeded", "usage_available": True},
                "json_following": {"status": "succeeded", "latency_ms": 8.0, "json_valid": True},
                "image_understanding": {"status": "succeeded", "latency_ms": 9.0, "json_valid": True},
            },
        }

    async def fake_embedding_probe(settings, usage_context=None):
        assert settings["api_key"] == "sk-stored"
        assert settings["embedding_model"] == "embed-candidate"
        assert usage_context["user_id"] == 1
        return {
            "status": "succeeded",
            "provider": settings["provider"],
            "model": settings["embedding_model"],
            "latency_ms": 6.5,
            "embedding_dimension": 384,
        }

    monkeypatch.setattr(main, "probe_chat_diagnostics", fake_chat_probe)
    monkeypatch.setattr(main, "probe_embedding_diagnostics", fake_embedding_probe)

    form = {
        "provider": "openai_compatible",
        "base_url": "http://model/v1",
        "embedding_base_url": "",
        "api_key": "",
        "chat_model": "chat-candidate",
        "embedding_model": "embed-candidate",
        "embedding_query_prefix": "query:",
        "embedding_passage_prefix": "passage:",
        "api_version": "2024-02-15-preview",
        "temperature": "0.2",
        "timeout_seconds": "60",
    }

    with TestClient(main.app) as client:
        _login(client)
        with db.connect() as conn:
            encrypted = db.encrypt_for_storage("sk-stored")
            conn.execute("UPDATE llm_settings SET api_key = ? WHERE id = 1", (encrypted,))

        page = client.get("/settings")
        assert page.status_code == 200
        assert 'formaction="/settings/test-chat"' in page.text
        assert 'formaction="/settings/test-embedding"' in page.text

        chat = client.post(
            "/settings/test-chat",
            data={**form, "include_image_understanding": "1"},
        )
        assert chat.status_code == 200
        assert "聊天模型測試已完成" in chat.text
        assert "chat-candidate" in chat.text
        assert "Image understanding" in chat.text

        embedding = client.post("/settings/test-embedding", data=form)
        assert embedding.status_code == 200
        assert "Embedding 模型測試已完成" in embedding.text
        assert "embed-candidate" in embedding.text
        assert "384" in embedding.text

    with db.connect() as conn:
        row = conn.execute("SELECT diagnostics_json FROM llm_settings WHERE id = 1").fetchone()
        audit_rows = conn.execute("SELECT action, metadata_json FROM audit_events ORDER BY id").fetchall()
    diagnostics = json.loads(row["diagnostics_json"])
    assert diagnostics["chat"]["status"] == "succeeded"
    assert diagnostics["chat"]["include_image_understanding"] is True
    assert diagnostics["embedding"]["embedding_dimension"] == 384
    stored = row["diagnostics_json"] + "".join(item["metadata_json"] for item in audit_rows)
    assert "sk-stored" not in stored
    assert "prompt" not in stored
    assert "output" not in stored
    assert "content" not in stored
    assert [item["action"] for item in audit_rows] == [
        "llm_settings_test_chat",
        "llm_settings_test_embedding",
    ]


def test_studio_tools_tile_gating(monkeypatch, tmp_path):
    """U16: the tools launcher enables the compare tile only with >=2 indexed sources."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)

        # No indexed sources: grid present, compare disabled.
        resp = client.get(f"/notebooks/{notebook_id}/_tools")
        assert resp.status_code == 200
        assert 'class="tool-grid"' in resp.text
        assert "來源比較" in resp.text and "學習指南" in resp.text
        assert resp.text.count("disabled") >= 5  # every tile disabled at 0 indexed

        # One indexed source: artifact/minutes tiles enabled, compare still disabled.
        _seed_indexed_source(db, user["id"], notebook_id, "a.pdf")
        resp = client.get(f"/notebooks/{notebook_id}/_tools")
        assert "/notebooks/%d/tools/study_guide" % notebook_id in resp.text
        # compare tile (needs 2) is still disabled -> no compare tool link
        assert "/tools/compare" not in resp.text

        # Two indexed sources: compare enabled.
        _seed_indexed_source(db, user["id"], notebook_id, "b.pdf")
        resp = client.get(f"/notebooks/{notebook_id}/_tools")
        assert "/tools/compare" in resp.text


def test_tool_panel_renders_each_kind(monkeypatch, tmp_path):
    """U16: each tool kind returns a modal panel; unknown kind 404s."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        _seed_indexed_source(db, user["id"], notebook_id, "a.pdf")
        _seed_indexed_source(db, user["id"], notebook_id, "b.pdf")

        compare = client.get(f"/notebooks/{notebook_id}/tools/compare")
        assert compare.status_code == 200
        assert "開始比較" in compare.text and 'id="tool-result"' in compare.text

        guide = client.get(f"/notebooks/{notebook_id}/tools/study_guide")
        assert guide.status_code == 200
        assert "生成學習指南" in guide.text

        assert client.get(f"/notebooks/{notebook_id}/tools/nope").status_code == 404


def test_artifact_renders_with_save_button_no_autosave(monkeypatch, tmp_path):
    """A4: artifact generation shows the result + a save button but does NOT
    auto-save; the user saves manually via /notes/add."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    async def fake_artifact(kind, summaries, settings, **kwargs):
        assert kind == "study_guide"
        return "## 核心概念\n- 測試"

    monkeypatch.setattr(main, "generate_artifact", fake_artifact)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db, title="A4測試")
        with db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_settings (id, provider, base_url, api_key, chat_model, embedding_model) "
                "VALUES (1, 'openai_compatible', 'https://x/v1', ?, 'chat', 'embed')",
                (db.encrypt_for_storage("sk-test"),),
            )
        _seed_indexed_source(db, user["id"], notebook_id, "a.pdf", summary="一份關於主題的摘要。")

        resp = client.post(f"/notebooks/{notebook_id}/artifacts/study_guide")
        assert resp.status_code == 200
        assert "核心概念" in resp.text
        # Manual save: a save button is offered, nothing auto-saved.
        assert "存成筆記" in resp.text
        assert 'name="title" value="學習指南 — A4測試"' in resp.text
        assert resp.headers.get("HX-Trigger") is None
        with db.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) c FROM notes WHERE notebook_id = ?", (notebook_id,)
            ).fetchone()["c"] == 0

        # Saving manually via /notes/add persists it.
        saved = client.post(
            f"/notebooks/{notebook_id}/notes/add",
            data={"title": "學習指南 — A4測試", "content": "## 核心概念\n- 測試"},
        )
        assert saved.status_code == 200
        with db.connect() as conn:
            note = conn.execute(
                "SELECT title, content FROM notes WHERE notebook_id = ?", (notebook_id,)
            ).fetchone()
        assert note is not None and note["title"].startswith("學習指南")
        assert "核心概念" in note["content"]

        # Unknown artifact kind -> 404.
        assert client.post(f"/notebooks/{notebook_id}/artifacts/nope").status_code == 404


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


def test_edit_note_updates_in_place(monkeypatch, tmp_path):
    """U8: editing a note updates title/content and returns the refreshed shelf."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            note_id = conn.execute(
                "INSERT INTO notes (notebook_id, user_id, title, content) VALUES (?, ?, '舊標題', '舊內容')",
                (notebook_id, user["id"]),
            ).lastrowid

        # The notes shelf offers an edit affordance.
        shelf = client.get(f"/notebooks/{notebook_id}/_notes")
        assert "編輯" in shelf.text and "note-edit-form" in shelf.text

        resp = client.post(
            f"/notebooks/{notebook_id}/notes/{note_id}/edit",
            data={"title": "新標題", "content": "新內容"},
        )
        assert resp.status_code == 200
        assert "新標題" in resp.text and "新內容" in resp.text
        with db.connect() as conn:
            note = conn.execute("SELECT title, content FROM notes WHERE id = ?", (note_id,)).fetchone()
        assert note["title"] == "新標題" and note["content"] == "新內容"

        # Empty content is rejected; a foreign/missing note 404s.
        assert client.post(
            f"/notebooks/{notebook_id}/notes/{note_id}/edit", data={"title": "x", "content": "   "}
        ).status_code == 400
        assert client.post(
            f"/notebooks/{notebook_id}/notes/999999/edit", data={"title": "x", "content": "y"}
        ).status_code == 404


def test_translate_renders_with_save_button(monkeypatch, tmp_path):
    """A5: translate-summary shows the translation + a save button; bad language 400s."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    async def fake_translate(text, target_language, settings, **kwargs):
        assert target_language == "English"
        return "Translated summary."

    monkeypatch.setattr(main, "translate_summary", fake_translate)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_settings (id, provider, base_url, api_key, chat_model, embedding_model) "
                "VALUES (1, 'openai_compatible', 'https://x/v1', ?, 'chat', 'embed')",
                (db.encrypt_for_storage("sk-test"),),
            )
        source_id = _seed_indexed_source(db, user["id"], notebook_id, "a.pdf", summary="一份摘要。")

        # Tool panel offers a source + language picker.
        panel = client.get(f"/notebooks/{notebook_id}/tools/translate")
        assert panel.status_code == 200
        assert "翻譯摘要" in panel.text and 'name="target_language"' in panel.text

        resp = client.post(
            f"/notebooks/{notebook_id}/translate",
            data={"source_id": source_id, "target_language": "English"},
        )
        assert resp.status_code == 200
        assert "Translated summary." in resp.text
        assert "存成筆記" in resp.text
        assert resp.headers.get("HX-Trigger") is None

        # Non-allowlisted language is rejected (no arbitrary prompt input).
        assert client.post(
            f"/notebooks/{notebook_id}/translate",
            data={"source_id": source_id, "target_language": "Klingon"},
        ).status_code == 400


def test_citation_payload_and_merge_carry_chunk_id(monkeypatch, tmp_path):
    """U3: citations carry the chunk row id so the chip can open the preview at it."""
    main, _db = _fresh_app(monkeypatch, tmp_path)

    chunks = [{"id": 7, "source_id": 3, "filename": "a.pdf", "location": "p1", "text": "hello", "score": 0.9}]
    cites = main.citation_payload(chunks)
    assert cites[0]["chunk_id"] == 7 and cites[0]["source_id"] == 3

    vec = [{"id": 5, "source_id": 2, "filename": "f", "location": "l", "text": "alpha beta", "vector_score": 0.8}]
    merged = main.merge_candidates(vec, [], ["alpha"])
    assert merged[5]["id"] == 5
