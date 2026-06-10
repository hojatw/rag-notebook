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
        assert "Add a source to get started" in empty.text
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
        assert "Indexing in progress" in processing.text

        # Once indexed, the center flips to the ask prompt.
        with db.connect() as conn:
            conn.execute("UPDATE sources SET status = 'indexed' WHERE id = ?", (source_id,))
        indexed = client.get(url)
        assert "Ask anything about your sources" in indexed.text


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
