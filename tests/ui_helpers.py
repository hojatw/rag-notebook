"""UI 測試共用的固件與輔助函式。

`tests/test_ui_*.py` 全部從這裡取用。名稱與原本 `tests/test_ui.py` 內的完全一致，
所以那次拆檔是純搬移——每個測試的內容一行未改。

`_fresh_app` 是這批測試最貴的一步（約 0.43 秒／次，見 docs/DEVELOPMENT.md），
因為 app 模組在 import 當下就綁定了 data dir，換一個 tmp_path 就得整組 reload。
測試平行執行的前提也在這裡：每個測試都拿到自己的 NOTEBOOKLM_DATA_DIR，
絕不共用 data/。
"""

import importlib

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

    # Import app.main FIRST: it is the package's import root. Its bottom-of-file
    # `app.include_router` block pulls in app.admin / app.evals / app.settings,
    # which in turn import shared helpers back from app.main. Touching one of
    # those route modules before app.main is in sys.modules would hit the
    # circular import mid-initialisation (ImportError). Loading app.main first
    # resolves the whole graph; the later imports just grab cached modules.
    import app.main as main
    import app.config as app_config
    import app.security as security
    import app.db as db
    import app.vector_store as vector_store
    import app.ingest as ingest
    import app.retrieval as retrieval
    import app.admin as admin
    import app.evals as evals
    import app.settings as app_settings
    import app.feedback as feedback_lib

    # Reload in dependency order; main last so its bottom-of-file router includes
    # pick up the freshly reloaded app.admin / app.evals / app.settings modules.
    for module in (app_config, security, db, vector_store, ingest, retrieval, feedback_lib, admin, evals, app_settings, main):
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


def _seed_notebook(db, title="NB"):
    with db.connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
        notebook_id = conn.execute(
            "INSERT INTO notebooks (user_id, title) VALUES (?, ?)", (user["id"], title)
        ).lastrowid
    return dict(user), notebook_id


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


def _make_notebook(db, username="admin"):
    """Create a notebook (plus a conversation + assistant message) for shelf tests."""
    with db.connect() as conn:
        user_id = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
        nb_id = conn.execute(
            "INSERT INTO notebooks (user_id, title) VALUES (?, '產出架測試')", (user_id,)
        ).lastrowid
        conv_id = conn.execute(
            "INSERT INTO conversations (user_id, notebook_id, title) VALUES (?, ?, '對話')",
            (user_id, nb_id),
        ).lastrowid
        conn.execute(
            "INSERT INTO messages (conversation_id, user_id, role, content) VALUES (?, ?, 'user', '問題')",
            (conv_id, user_id),
        )
        msg_id = conn.execute(
            "INSERT INTO messages (conversation_id, user_id, role, content) VALUES (?, ?, 'assistant', '回答')",
            (conv_id, user_id),
        ).lastrowid
    return nb_id, msg_id


# --- SEC-2: upload size limits, and no multipart body buffering ---------------


def _upload(client, notebook_id, name, payload, *, headers=None):
    return client.post(
        f"/notebooks/{notebook_id}/sources/upload",
        files={"files": (name, payload, "text/plain")},
        headers=headers,
        follow_redirects=False,
    )


def _ready_notebook(main, db, client):
    """A notebook on a deployment whose LLM settings pass the upload gate."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE llm_settings SET base_url = 'http://llm.test/v1', chat_model = 'm',"
            " embedding_base_url = 'http://llm.test/v1', embedding_model = 'e' WHERE id = 1"
        )
    client.post("/notebooks/new", data={"title": "N"}, follow_redirects=False)
    with db.connect() as conn:
        return conn.execute("SELECT id FROM notebooks ORDER BY id DESC LIMIT 1").fetchone()["id"]
