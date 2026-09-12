"""全站外框與錯誤頁的 UI 測試。

涵蓋 /healthz 與頁尾的建置標示、關閉框架 API 文件、404/500 與 HTMX 錯誤片段、
主題（light/dark/system）的伺服器端渲染與白名單，以及無障礙骨架。
契約文件：docs/UI.md、docs/RELEASE.md。

由 tests/test_ui.py 拆出；共用固件見 tests/ui_helpers.py。
"""

import pytest
from fastapi.testclient import TestClient as FastAPITestClient

from tests.ui_helpers import TestClient, _fresh_app, _login


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


def test_healthz_reports_build(monkeypatch, tmp_path):
    """/healthz is unauthenticated and returns the build identifier for bug reports."""
    main, _db = _fresh_app(monkeypatch, tmp_path)

    with FastAPITestClient(main.app) as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        # version comes from the repo-root VERSION file; commit is best-effort.
        assert body["version"]
        assert "commit" in body


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_framework_api_docs_are_disabled(monkeypatch, tmp_path, path):
    """FastAPI's stock docs endpoints must not exist: unauthenticated they would
    publish every route (including /admin/* and /settings*) and their parameter
    names, and Swagger UI / ReDoc pull JS/CSS from a public CDN."""
    main, _db = _fresh_app(monkeypatch, tmp_path)

    with FastAPITestClient(main.app) as client:
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 404
        assert "swagger" not in resp.text.lower()
        assert '"paths"' not in resp.text


def test_footer_shows_build_label(monkeypatch, tmp_path):
    """Every page footer carries the version so a screenshot ties to a build."""
    main, _db = _fresh_app(monkeypatch, tmp_path)

    with FastAPITestClient(main.app) as client:
        page = client.get("/login")
        assert page.status_code == 200
        assert 'class="app-footer"' in page.text
        assert f"v{main.app_version()}" in page.text


def test_unknown_route_renders_styled_404(monkeypatch, tmp_path):
    """A typo'd URL gets the styled error page (not Starlette's raw JSON)."""
    main, _db = _fresh_app(monkeypatch, tmp_path)

    with FastAPITestClient(main.app) as client:
        resp = client.get("/this-route-does-not-exist")
        assert resp.status_code == 404
        # Localized friendly copy + the shared footer chrome, i.e. error.html.
        assert "找不到頁面" in resp.text
        assert 'class="app-footer"' in resp.text
        # No raw framework detail leaked.
        assert "Not Found" not in resp.text


def test_unhandled_exception_renders_styled_500(monkeypatch, tmp_path):
    """An unhandled error renders the 500 page instead of a bare 'Internal
    Server Error', and never leaks the raw exception text."""
    main, _db = _fresh_app(monkeypatch, tmp_path)

    secret = "leak-me-please-do-not-show-the-user"

    async def _boom():
        raise RuntimeError(secret)

    main.app.add_api_route("/__boom", _boom, methods=["GET"])
    try:
        # raise_server_exceptions=False so the registered handler's response is
        # returned rather than re-raised into the test.
        with FastAPITestClient(main.app, raise_server_exceptions=False) as client:
            resp = client.get("/__boom")
            assert resp.status_code == 500
            assert "伺服器發生錯誤" in resp.text
            assert secret not in resp.text
    finally:
        main.app.router.routes = [
            r for r in main.app.router.routes if getattr(r, "path", None) != "/__boom"
        ]


def test_hx_request_error_returns_fragment(monkeypatch, tmp_path):
    """HTMX requests get a compact notice fragment, not a full nested page."""
    main, _db = _fresh_app(monkeypatch, tmp_path)

    with FastAPITestClient(main.app) as client:
        resp = client.get("/this-route-does-not-exist", headers={"HX-Request": "true"})
        assert resp.status_code == 404
        assert 'class="notice failed"' in resp.text
        # Fragment only — no full document chrome.
        assert "<!doctype html>" not in resp.text.lower()
        assert 'class="app-footer"' not in resp.text


def test_theme_defaults_to_system_and_leaves_data_theme_unset(monkeypatch, tmp_path):
    """U11: a fresh account follows the OS preference, resolved client-side."""
    main, _db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        page = client.get("/notebooks")

        assert page.status_code == 200
        assert 'data-theme-pref="system"' in page.text
        # No server-rendered data-theme: base.html's inline script resolves it
        # against prefers-color-scheme, so CSS defaults to the light tokens.
        assert "data-theme=" not in page.text


def test_theme_choice_persists_and_renders_server_side(monkeypatch, tmp_path):
    """An explicit choice is stored per user and rendered without JS."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        saved = client.post("/account/theme", data={"theme": "dark"})

        assert saved.status_code == 200
        assert "外觀設定已更新" in saved.text
        assert 'data-theme="dark"' in saved.text
        with db.connect() as conn:
            row = conn.execute("SELECT theme FROM users WHERE username = 'admin'").fetchone()
        assert row["theme"] == "dark"

        # Survives a new request, and the radio reflects the stored choice.
        page = client.get("/account")
        assert 'data-theme="dark"' in page.text
        assert 'value="dark" checked' in page.text
        assert 'value="system" checked' not in page.text


def test_theme_rejects_values_outside_the_allowlist(monkeypatch, tmp_path):
    """Only THEME_CHOICES may be written to users.theme."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        response = client.post("/account/theme", data={"theme": "neon"})

        assert response.status_code == 400
        assert "不支援的外觀選項" in response.text
        with db.connect() as conn:
            row = conn.execute("SELECT theme FROM users WHERE username = 'admin'").fetchone()
        assert row["theme"] == "system"
