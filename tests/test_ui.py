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
