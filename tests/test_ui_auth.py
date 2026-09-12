"""登入、工作階段與 SSO 的 UI 測試。

涵蓋本機登入與 CSRF、SEC-4 失敗桶與 PBKDF2 並行 lease、SEC-1 首次登入強制改密碼、
SEC-3 改密碼撤銷其他工作階段、SEC-7/8 管理錯誤與登出 cookie 衛生、多段式表單的
CSRF 例外，以及 I1a 信任標頭與 I1b OIDC 兩種外部驗證模式。
契約文件：docs/AUTHENTICATION.md、docs/SECURITY.md。

由 tests/test_ui.py 拆出；共用固件見 tests/ui_helpers.py。
"""

import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient as FastAPITestClient

from tests.ui_helpers import TestClient, _fresh_app, _login, _ready_notebook, _upload


def _oidc_metadata():
    return {
        "issuer": "https://idp.example.test",
        "authorization_endpoint": "https://idp.example.test/authorize",
        "token_endpoint": "https://idp.example.test/token",
        "jwks_uri": "https://idp.example.test/jwks",
    }


def _oidc_id_token(key, *, nonce: str, subject: str = "oidc-subject", groups=None, issuer: str = "https://idp.example.test", audience: str = "oidc-client") -> str:
    from joserfc import jwt

    now = int(time.time())
    return jwt.encode(
        {"alg": "RS256", "kid": "oidc-test-key"},
        {
            "iss": issuer,
            "aud": audience,
            "sub": subject,
            "nonce": nonce,
            "exp": now + 600,
            "iat": now,
            "email": "oidc@example.com",
            "name": "OIDC User",
            "groups": groups or ["staff"],
        },
        key,
    )


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


def test_local_login_rate_limit_blocks_and_stores_only_hashed_buckets(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_AUTH_LOGIN_ACCOUNT_ATTEMPT_LIMIT", "2")
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        first = client.post("/login", data={"username": "admin", "password": "wrong"})
        second = client.post("/login", data={"username": "admin", "password": "wrong"})
        blocked_correct_password = client.post(
            "/login",
            data={"username": "admin", "password": "admin123"},
            follow_redirects=False,
        )

    assert first.status_code == 400
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) > 0
    assert blocked_correct_password.status_code == 429
    assert "登入嘗試過於頻繁" in second.text
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT bucket_type, bucket_hash FROM login_rate_limits ORDER BY bucket_type"
        ).fetchall()
        active_leases = conn.execute("SELECT COUNT(*) FROM login_verification_leases").fetchone()[0]
    assert {row["bucket_type"] for row in rows} == {"account"}
    assert all("admin" not in row["bucket_hash"] for row in rows)
    assert active_leases == 0


def test_successful_login_clears_the_account_failure_bucket(monkeypatch, tmp_path):
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        failed = client.post("/login", data={"username": "admin", "password": "wrong"})
        succeeded = client.post(
            "/login",
            data={"username": "admin", "password": "admin123"},
            follow_redirects=False,
        )

    assert failed.status_code == 400
    assert succeeded.status_code == 303
    with db.connect() as conn:
        bucket_count = conn.execute("SELECT COUNT(*) FROM login_rate_limits").fetchone()[0]
        active_leases = conn.execute("SELECT COUNT(*) FROM login_verification_leases").fetchone()[0]
    assert bucket_count == 0
    assert active_leases == 0


def test_unknown_username_spray_does_not_block_or_persist_buckets(monkeypatch, tmp_path):
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        for index in range(8):
            failed = client.post(
                "/login",
                data={"username": f"missing-{index}", "password": "wrong"},
            )
            assert failed.status_code == 400
        succeeded = client.post(
            "/login",
            data={"username": "admin", "password": "admin123"},
            follow_redirects=False,
        )

    assert succeeded.status_code == 303
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM login_rate_limits").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM login_verification_leases").fetchone()[0] == 0


def test_login_verification_leases_bound_capacity_serialize_accounts_and_expire(monkeypatch, tmp_path):
    main, db = _fresh_app(monkeypatch, tmp_path)
    db.init_db()
    main.config.auth.login_verification_max_concurrency = 2
    main.config.auth.login_verification_lease_seconds = 10
    main.config.auth.login_verification_busy_retry_after_seconds = 3

    first, retry_after = main._acquire_login_verification_lease("first", now=100.0)
    assert first and retry_after == 0
    duplicate, retry_after = main._acquire_login_verification_lease("first", now=100.0)
    assert duplicate is None and retry_after == 3
    second, retry_after = main._acquire_login_verification_lease("second", now=100.0)
    assert second and retry_after == 0
    full, retry_after = main._acquire_login_verification_lease("third", now=100.0)
    assert full is None and retry_after == 3

    main._release_login_verification_lease(first)
    third, retry_after = main._acquire_login_verification_lease("third", now=101.0)
    assert third and retry_after == 0

    recovered, retry_after = main._acquire_login_verification_lease("recovered", now=111.0)
    assert recovered and retry_after == 0
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT lease_id, account_hash FROM login_verification_leases ORDER BY lease_id"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["lease_id"] == recovered
    assert "recovered" not in rows[0]["account_hash"]


def test_active_account_verification_lease_returns_429_without_hashing(monkeypatch, tmp_path):
    main, db = _fresh_app(monkeypatch, tmp_path)
    db.init_db()
    lease_id, _retry_after = main._acquire_login_verification_lease("admin", now=time.time())
    assert lease_id

    def unexpected_verify(_password, _encoded):
        raise AssertionError("busy account must not reach PBKDF2")

    monkeypatch.setattr(main, "verify_password", unexpected_verify)
    try:
        with TestClient(main.app) as client:
            blocked = client.post(
                "/login",
                data={"username": "admin", "password": "admin123"},
                follow_redirects=False,
            )
    finally:
        main._release_login_verification_lease(lease_id)

    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "1"


def test_current_user_context_excludes_password_hash(monkeypatch, tmp_path):
    from types import SimpleNamespace

    main, _db = _fresh_app(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        _login(client)
        session = client.cookies.get("session")

    user = main.current_user(SimpleNamespace(cookies={"session": session}))
    assert set(user) == {
        "id",
        "username",
        "is_admin",
        "theme",
        "must_change_password",
        "password_version",
    }
    assert "password_hash" not in user


def test_trusted_header_auth_is_disabled_by_default(monkeypatch, tmp_path):
    main, _db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        response = client.get("/auth/trusted-header", follow_redirects=False)
        assert response.status_code == 404


def test_trusted_header_auth_rejects_missing_shared_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_AUTH_TRUSTED_HEADER_ENABLED", "true")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_TRUSTED_HEADER_SECRET", "proxy-secret")
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        response = client.get(
            "/auth/trusted-header",
            headers={"X-Forwarded-User": "DOMAIN\\alice"},
            follow_redirects=False,
        )
        assert response.status_code == 403
        with db.connect() as conn:
            users = conn.execute("SELECT COUNT(*) FROM users WHERE username LIKE 'alice%'").fetchone()[0]
            audit = conn.execute(
                "SELECT action, metadata_json FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert users == 0
        assert audit["action"] == "trusted_header_login_rejected"
        assert "shared_secret_mismatch" in audit["metadata_json"]


def test_trusted_header_auth_provisions_user_maps_admin_and_audits(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_AUTH_TRUSTED_HEADER_ENABLED", "true")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_TRUSTED_HEADER_SECRET", "proxy-secret")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_TRUSTED_HEADER_ADMIN_GROUPS", "rag-admins")
    main, db = _fresh_app(monkeypatch, tmp_path)

    headers = {
        "X-NotebookLM-Auth-Secret": "proxy-secret",
        "X-Forwarded-User": "DOMAIN\\jane",
        "X-Forwarded-Email": "jane@example.com",
        "X-Forwarded-Name": "Jane Doe",
        "X-Forwarded-Groups": "staff, rag-admins",
    }
    with TestClient(main.app) as client:
        response = client.get("/auth/trusted-header", headers=headers, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/notebooks"

        with db.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = 'jane@example.com'").fetchone()
            identity = conn.execute(
                "SELECT * FROM external_identities WHERE provider = 'trusted_header' AND subject = ?",
                ("DOMAIN\\jane",),
            ).fetchone()
            actions = [
                row["action"]
                for row in conn.execute("SELECT action FROM audit_events ORDER BY id").fetchall()
            ]
            stored_audit = "\n".join(
                row["metadata_json"]
                for row in conn.execute("SELECT metadata_json FROM audit_events ORDER BY id").fetchall()
            )

        assert user is not None
        assert user["is_admin"] == 1
        assert identity is not None
        assert identity["user_id"] == user["id"]
        assert json.loads(identity["groups_json"]) == ["staff", "rag-admins"]
        assert actions == [
            "trusted_header_user_provisioned",
            "trusted_header_login_succeeded",
        ]
        assert "DOMAIN\\jane" not in stored_audit
        assert "subject_hash" in stored_audit

        second = client.get("/auth/trusted-header", headers=headers, follow_redirects=False)
        assert second.status_code == 303
        with db.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM external_identities WHERE subject = ?",
                ("DOMAIN\\jane",),
            ).fetchone()[0] == 1


def test_trusted_header_auth_respects_auto_provision_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_AUTH_TRUSTED_HEADER_ENABLED", "true")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_TRUSTED_HEADER_SECRET", "proxy-secret")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_TRUSTED_HEADER_AUTO_PROVISION", "false")
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        response = client.get(
            "/auth/trusted-header",
            headers={
                "X-NotebookLM-Auth-Secret": "proxy-secret",
                "X-Forwarded-User": "unknown-user",
            },
            follow_redirects=False,
        )
        assert response.status_code == 403
        with db.connect() as conn:
            users = conn.execute("SELECT COUNT(*) FROM users WHERE username = 'unknown-user'").fetchone()[0]
            audit = conn.execute(
                "SELECT action, metadata_json FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert users == 0
        assert audit["action"] == "trusted_header_login_rejected"
        assert "unknown_external_identity" in audit["metadata_json"]
        assert "unknown-user" not in audit["metadata_json"]


def test_ip_in_allowlist_matches_exact_and_cidr(monkeypatch, tmp_path):
    main, _db = _fresh_app(monkeypatch, tmp_path)
    allow = main._ip_in_allowlist
    assert allow("10.0.0.5", "") is True                       # empty = no restriction
    assert allow("10.0.0.5", "10.0.0.5") is True               # exact match
    assert allow("10.0.0.5", "10.0.0.0/8") is True             # CIDR match
    assert allow("192.168.1.1", "10.0.0.0/8, 192.168.0.0/16") is True
    assert allow("172.16.0.1", "10.0.0.0/8") is False          # outside allowlist
    assert allow("testclient", "10.0.0.0/8") is False          # non-IP peer host
    assert allow("", "10.0.0.0/8") is False                    # missing peer host


def test_trusted_header_auth_rejects_untrusted_source_ip(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_AUTH_TRUSTED_HEADER_ENABLED", "true")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_TRUSTED_HEADER_SECRET", "proxy-secret")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_TRUSTED_HEADER_ALLOWED_IPS", "10.0.0.0/8")
    main, db = _fresh_app(monkeypatch, tmp_path)

    # The TestClient peer host ("testclient") is not inside the allowlist, so the
    # request is rejected before the (correct) shared secret is even compared.
    with TestClient(main.app) as client:
        response = client.get(
            "/auth/trusted-header",
            headers={
                "X-NotebookLM-Auth-Secret": "proxy-secret",
                "X-Forwarded-User": "DOMAIN\\alice",
            },
            follow_redirects=False,
        )
        assert response.status_code == 403
        with db.connect() as conn:
            users = conn.execute("SELECT COUNT(*) FROM users WHERE username LIKE 'alice%'").fetchone()[0]
            audit = conn.execute(
                "SELECT action, metadata_json FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert users == 0
        assert audit["action"] == "trusted_header_login_rejected"
        assert "untrusted_source_ip" in audit["metadata_json"]


def test_trusted_header_auth_can_disable_local_login(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_AUTH_LOCAL_LOGIN_ENABLED", "false")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_TRUSTED_HEADER_ENABLED", "true")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_TRUSTED_HEADER_SECRET", "proxy-secret")
    main, _db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        page = client.get("/login")
        assert page.status_code == 200
        assert "企業登入" in page.text
        assert 'name="username"' not in page.text

        response = client.post(
            "/login",
            data={"username": "admin", "password": "admin123"},
            follow_redirects=False,
        )
        assert response.status_code == 403
        assert "本機帳號登入已停用" in response.text


def test_sso_linked_users_cannot_set_local_passwords(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_AUTH_TRUSTED_HEADER_ENABLED", "true")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_TRUSTED_HEADER_SECRET", "proxy-secret")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_TRUSTED_HEADER_ADMIN_GROUPS", "rag-admins")
    main, db = _fresh_app(monkeypatch, tmp_path)

    headers = {
        "X-NotebookLM-Auth-Secret": "proxy-secret",
        "X-Forwarded-User": "sso-admin",
        "X-Forwarded-Groups": "rag-admins",
    }
    with TestClient(main.app) as client:
        response = client.get("/auth/trusted-header", headers=headers, follow_redirects=False)
        assert response.status_code == 303
        with db.connect() as conn:
            user_id = conn.execute("SELECT id FROM users WHERE username = 'sso-admin'").fetchone()["id"]

        account = client.get("/account")
        assert account.status_code == 200
        assert "不能設定本機密碼" in account.text
        assert 'action="/account/password"' not in account.text

        own_reset = client.post(
            "/account/password",
            data={
                "current_password": "anything",
                "new_password": "new-password",
                "confirm_password": "new-password",
            },
        )
        assert own_reset.status_code == 400
        assert "不能變更本機密碼" in own_reset.text

        admin_reset = client.post(
            f"/admin/users/{user_id}/reset-password",
            data={"new_password": "new-password"},
            follow_redirects=False,
        )
        assert admin_reset.status_code == 400


def test_sso_linked_users_cannot_have_local_admin_role_toggled(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_AUTH_TRUSTED_HEADER_ENABLED", "true")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_TRUSTED_HEADER_SECRET", "proxy-secret")
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        response = client.get(
            "/auth/trusted-header",
            headers={
                "X-NotebookLM-Auth-Secret": "proxy-secret",
                "X-Forwarded-User": "sso-user",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        client.post("/logout", follow_redirects=False)
        _login(client)

        with db.connect() as conn:
            target_id = conn.execute("SELECT id FROM users WHERE username = 'sso-user'").fetchone()["id"]

        page = client.get("/admin/users")
        assert page.status_code == 200
        assert f'action="/admin/users/{target_id}/toggle-admin"' not in page.text
        assert "管理員角色由企業群組映射管理" in page.text

        toggled = client.post(f"/admin/users/{target_id}/toggle-admin", follow_redirects=False)
        assert toggled.status_code == 400
        assert "管理員角色由企業群組映射管理" in toggled.text


def test_external_auth_auto_provision_recovers_from_identity_race(monkeypatch, tmp_path):
    main, db = _fresh_app(monkeypatch, tmp_path)
    db.init_db()

    def insert_racing_identity(conn, subject, email, display_name):
        user_id = conn.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 0)",
            ("race-winner", main.hash_password("placeholder")),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO external_identities
            (user_id, provider, subject, email, display_name, groups_json)
            VALUES (?, 'trusted_header', ?, '', '', '[]')
            """,
            (user_id, subject),
        )
        return "race-loser"

    monkeypatch.setattr(main, "_unique_external_username", insert_racing_identity)

    signed_in = main._external_auth_login(
        None,
        provider="trusted_header",
        subject="race-subject",
        email="race@example.com",
        display_name="Race User",
        groups=["rag-admins"],
        admin_groups=["rag-admins"],
        auto_provision=True,
        audit_prefix="trusted_header",
        unknown_user_message="unknown",
    )

    assert signed_in["username"] == "race-winner"
    assert signed_in["is_admin"] == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE username = 'race-loser'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM external_identities WHERE provider = 'trusted_header' AND subject = 'race-subject'"
        ).fetchone()[0] == 1
        identity = conn.execute(
            "SELECT email, display_name, groups_json FROM external_identities WHERE subject = 'race-subject'"
        ).fetchone()
        actions = [row["action"] for row in conn.execute("SELECT action FROM audit_events ORDER BY id").fetchall()]
    assert identity["email"] == "race@example.com"
    assert identity["display_name"] == "Race User"
    assert json.loads(identity["groups_json"]) == ["rag-admins"]
    assert actions == ["trusted_header_role_mapped", "trusted_header_login_succeeded"]


def test_oidc_login_redirect_sets_signed_state_cookie(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_ENABLED", "true")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_ISSUER", "https://idp.example.test")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_CLIENT_ID", "oidc-client")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_CLIENT_SECRET", "oidc-secret")
    main, _db = _fresh_app(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_oidc_discover", lambda auth: _oidc_metadata())

    with TestClient(main.app) as client:
        page = client.get("/login")
        assert page.status_code == 200
        assert "OIDC 登入" in page.text

        response = client.get("/auth/oidc/login", follow_redirects=False)
        assert response.status_code == 303
        location = response.headers["location"]
        parsed = urlparse(location)
        params = parse_qs(parsed.query)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://idp.example.test/authorize"
        assert params["client_id"] == ["oidc-client"]
        assert params["response_type"] == ["code"]
        assert params["scope"] == ["openid profile email"]
        assert params["redirect_uri"] == ["http://testserver/auth/oidc/callback"]
        assert params.get("state", [""])[0]
        assert params.get("nonce", [""])[0]
        assert client.cookies.get(main.OIDC_STATE_COOKIE_NAME)


def test_oidc_state_rejects_expired_or_future_iat(monkeypatch, tmp_path):
    main, _db = _fresh_app(monkeypatch, tmp_path)
    now = int(time.time())

    fresh = main._sign_oidc_state({"state": "s", "nonce": "n", "iat": now})
    expired = main._sign_oidc_state({
        "state": "s",
        "nonce": "n",
        "iat": now - main.OIDC_STATE_COOKIE_MAX_AGE - 1,
    })
    future = main._sign_oidc_state({"state": "s", "nonce": "n", "iat": now + 61})
    missing_iat = main._sign_oidc_state({"state": "s", "nonce": "n"})

    assert main._unsign_oidc_state(fresh)["state"] == "s"
    assert main._unsign_oidc_state(expired) is None
    assert main._unsign_oidc_state(future) is None
    assert main._unsign_oidc_state(missing_iat) is None


def test_oidc_discovery_requires_https_and_matching_issuer(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_ENABLED", "true")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_ISSUER", "https://idp.example.test")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_CLIENT_ID", "oidc-client")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_CLIENT_SECRET", "oidc-secret")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_DISCOVERY_URL", "http://idp.example.test/.well-known/openid-configuration")
    main, _db = _fresh_app(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        insecure = client.get("/auth/oidc/login", follow_redirects=False)
        assert insecure.status_code == 502

    monkeypatch.delenv("NOTEBOOKLM_AUTH_OIDC_DISCOVERY_URL", raising=False)
    main, _db = _fresh_app(monkeypatch, tmp_path)
    bad_metadata = {
        "issuer": "https://evil.example.test",
        "authorization_endpoint": "https://idp.example.test/authorize",
        "token_endpoint": "https://idp.example.test/token",
        "jwks_uri": "https://idp.example.test/jwks",
    }
    monkeypatch.setattr(main, "_oidc_fetch_json", lambda url, **kwargs: bad_metadata)
    with TestClient(main.app) as client:
        mismatch = client.get("/auth/oidc/login", follow_redirects=False)
        assert mismatch.status_code == 502

    main, _db = _fresh_app(monkeypatch, tmp_path)
    insecure_endpoint = {
        "issuer": "https://idp.example.test",
        "authorization_endpoint": "https://idp.example.test/authorize",
        "token_endpoint": "http://idp.example.test/token",
        "jwks_uri": "https://idp.example.test/jwks",
    }
    monkeypatch.setattr(main, "_oidc_fetch_json", lambda url, **kwargs: insecure_endpoint)
    with TestClient(main.app) as client:
        rejected = client.get("/auth/oidc/login", follow_redirects=False)
        assert rejected.status_code == 502


def test_oidc_callback_provisions_user_maps_admin_and_audits(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_ENABLED", "true")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_ISSUER", "https://idp.example.test")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_CLIENT_ID", "oidc-client")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_CLIENT_SECRET", "oidc-secret")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_ADMIN_GROUPS", "rag-admins")
    main, db = _fresh_app(monkeypatch, tmp_path)

    from joserfc import jwk

    key = jwk.generate_key("RSA", 2048, {"kid": "oidc-test-key", "alg": "RS256", "use": "sig"})
    metadata = _oidc_metadata()
    monkeypatch.setattr(main, "_oidc_discover", lambda auth: metadata)

    with TestClient(main.app) as client:
        start = client.get("/auth/oidc/login", follow_redirects=False)
        assert start.status_code == 303
        params = parse_qs(urlparse(start.headers["location"]).query)
        state = params["state"][0]
        nonce = params["nonce"][0]
        id_token = _oidc_id_token(key, nonce=nonce, subject="subject-123", groups=["staff", "rag-admins"])
        monkeypatch.setattr(main, "_oidc_exchange_code", lambda auth, token_endpoint, code, redirect_uri: {"id_token": id_token})
        monkeypatch.setattr(main, "_oidc_fetch_json", lambda url, **kwargs: {"keys": [key.as_dict(private=False)]})

        callback = client.get(f"/auth/oidc/callback?code=abc&state={state}", follow_redirects=False)
        assert callback.status_code == 303
        assert callback.headers["location"] == "/notebooks"

        with db.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = 'oidc@example.com'").fetchone()
            identity = conn.execute(
                "SELECT * FROM external_identities WHERE provider = 'oidc' AND subject = 'subject-123'"
            ).fetchone()
            actions = [
                row["action"]
                for row in conn.execute("SELECT action FROM audit_events ORDER BY id").fetchall()
            ]
            audit_metadata = "\n".join(
                row["metadata_json"]
                for row in conn.execute("SELECT metadata_json FROM audit_events ORDER BY id").fetchall()
            )

        assert user is not None
        assert user["is_admin"] == 1
        assert identity is not None
        assert identity["user_id"] == user["id"]
        assert json.loads(identity["groups_json"]) == ["staff", "rag-admins"]
        assert actions == ["oidc_user_provisioned", "oidc_login_succeeded"]
        assert "subject-123" not in audit_metadata
        assert "id_token" not in audit_metadata
        assert "subject_hash" in audit_metadata


def test_oidc_callback_rejects_state_and_nonce_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_ENABLED", "true")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_ISSUER", "https://idp.example.test")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_CLIENT_ID", "oidc-client")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_CLIENT_SECRET", "oidc-secret")
    main, db = _fresh_app(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_oidc_discover", lambda auth: _oidc_metadata())

    with TestClient(main.app) as client:
        start = client.get("/auth/oidc/login", follow_redirects=False)
        assert start.status_code == 303
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

        bad_state = client.get("/auth/oidc/callback?code=abc&state=wrong", follow_redirects=False)
        assert bad_state.status_code == 400
        assert "OIDC 登入失敗" in bad_state.text

        start = client.get("/auth/oidc/login", follow_redirects=False)
        assert start.status_code == 303
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

        from joserfc import jwk

        key = jwk.generate_key("RSA", 2048, {"kid": "oidc-test-key", "alg": "RS256", "use": "sig"})
        id_token = _oidc_id_token(key, nonce="wrong-nonce")
        monkeypatch.setattr(main, "_oidc_exchange_code", lambda auth, token_endpoint, code, redirect_uri: {"id_token": id_token})
        monkeypatch.setattr(main, "_oidc_fetch_json", lambda url, **kwargs: {"keys": [key.as_dict(private=False)]})

        bad_nonce = client.get(f"/auth/oidc/callback?code=abc&state={state}", follow_redirects=False)
        assert bad_nonce.status_code == 400
        with db.connect() as conn:
            actions = [row["action"] for row in conn.execute("SELECT action FROM audit_events ORDER BY id").fetchall()]
        assert actions == ["oidc_login_rejected", "oidc_login_rejected"]


def test_oidc_auto_provision_can_be_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_ENABLED", "true")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_ISSUER", "https://idp.example.test")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_CLIENT_ID", "oidc-client")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_CLIENT_SECRET", "oidc-secret")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_AUTO_PROVISION", "false")
    main, db = _fresh_app(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_oidc_discover", lambda auth: _oidc_metadata())

    from joserfc import jwk

    key = jwk.generate_key("RSA", 2048, {"kid": "oidc-test-key", "alg": "RS256", "use": "sig"})

    with TestClient(main.app) as client:
        start = client.get("/auth/oidc/login", follow_redirects=False)
        params = parse_qs(urlparse(start.headers["location"]).query)
        id_token = _oidc_id_token(key, nonce=params["nonce"][0], subject="not-provisioned")
        monkeypatch.setattr(main, "_oidc_exchange_code", lambda auth, token_endpoint, code, redirect_uri: {"id_token": id_token})
        monkeypatch.setattr(main, "_oidc_fetch_json", lambda url, **kwargs: {"keys": [key.as_dict(private=False)]})

        callback = client.get(f"/auth/oidc/callback?code=abc&state={params['state'][0]}", follow_redirects=False)
        assert callback.status_code == 403
        with db.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM external_identities WHERE subject = 'not-provisioned'"
            ).fetchone()[0] == 0
            audit = conn.execute(
                "SELECT action, metadata_json FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert audit["action"] == "oidc_login_rejected"
        assert "unknown_external_identity" in audit["metadata_json"]
        assert "not-provisioned" not in audit["metadata_json"]


def test_admin_auth_diagnostics_page_shows_modes_and_checks(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_AUTH_TRUSTED_HEADER_ENABLED", "true")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_TRUSTED_HEADER_SECRET", "proxy-secret")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_ENABLED", "true")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_ISSUER", "https://idp.example.test")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_CLIENT_ID", "oidc-client")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_OIDC_CLIENT_SECRET", "oidc-secret")
    main, _db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        page = client.get("/admin/auth")
        assert page.status_code == 200
        assert "認證狀態" in page.text
        assert "本機帳號" in page.text
        assert "Trusted header" in page.text
        assert "OIDC" in page.text
        assert "OIDC endpoint 使用 HTTPS" in page.text
        assert "trusted_header_login_rejected" in page.text


# --- SEC-1: the bootstrap admin cannot use the app until it changes password --


def _bootstrap_app(monkeypatch, tmp_path):
    """Fresh app running the production seeding policy (no demo pair)."""
    monkeypatch.setenv("NOTEBOOKLM_SEED_DEMO_USERS", "0")
    return _fresh_app(monkeypatch, tmp_path)


def test_bootstrap_admin_is_pinned_to_the_account_page(monkeypatch, tmp_path):
    """A seeded admin that never changed its password can reach nothing else.

    The gate lives in `require_login`, so it covers every authenticated route
    including the admin routers (which reach it through `require_admin`).
    """
    main, db = _bootstrap_app(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        _login(client)

        for path in ("/notebooks", "/search", "/admin/users", "/settings"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 303, path
            assert response.headers["location"] == "/account", path

        # The one page it may reach explains why, through the i18n catalog.
        account = client.get("/account")
        assert account.status_code == 200
        import app.i18n as i18n

        assert i18n.t("account.force_change_title") in account.text
        # No escape hatch anywhere on the page: not in the form footer, and not
        # in the chrome either — every nav target would just bounce back here.
        assert 'href="/notebooks"' not in account.text
        assert 'href="/search"' not in account.text
        assert 'href="/admin/users"' not in account.text
        assert 'class="primary-nav"' not in account.text
        # Theme editing is hidden too — /account/theme would only bounce back.
        assert 'action="/account/theme"' not in account.text
        # Signing out stays available; it is the one way out that is not the form.
        assert 'action="/logout"' in account.text


def test_forced_password_change_releases_the_account(monkeypatch, tmp_path):
    """Setting a real password clears the flag, audits it, and unpins the account."""
    main, db = _bootstrap_app(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        _login(client)

        response = client.post(
            "/account/password",
            data={
                "current_password": "admin123",
                "new_password": "a-real-password",
                "confirm_password": "a-real-password",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/notebooks"

        with db.connect() as conn:
            row = conn.execute(
                "SELECT must_change_password FROM users WHERE username = 'admin'"
            ).fetchone()
            audit = conn.execute(
                "SELECT action, sensitivity FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row["must_change_password"] == 0
        assert audit["action"] == "bootstrap_password_changed"
        assert audit["sensitivity"] == "high"

        # The pin is lifted.
        assert client.get("/notebooks", follow_redirects=False).status_code == 200

        # And the old bootstrap credential no longer works.
        client.post("/logout", follow_redirects=False)
        rejected = client.post(
            "/login",
            data={"username": "admin", "password": "admin123"},
            follow_redirects=False,
        )
        assert rejected.status_code == 400


def test_forced_change_rejects_a_bad_confirmation(monkeypatch, tmp_path):
    """A failed attempt leaves the account pinned rather than quietly releasing it."""
    main, db = _bootstrap_app(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        _login(client)

        response = client.post(
            "/account/password",
            data={
                "current_password": "admin123",
                "new_password": "a-real-password",
                "confirm_password": "mismatched",
            },
            follow_redirects=False,
        )
        assert response.status_code == 400

        with db.connect() as conn:
            row = conn.execute(
                "SELECT must_change_password FROM users WHERE username = 'admin'"
            ).fetchone()
        assert row["must_change_password"] == 1
        assert client.get("/notebooks", follow_redirects=False).status_code == 303


def test_csrf_middleware_never_reads_a_multipart_body(monkeypatch, tmp_path):
    """The core of SEC-2: uploads must not be materialised in memory.

    `request.body()` is what used to buffer the entire upload before the route
    ran. Making it explode proves the middleware no longer calls it — if this
    test starts failing, the buffering regression is back.
    """
    from starlette.requests import Request as StarletteRequest

    main, db = _fresh_app(monkeypatch, tmp_path)

    async def exploding_body(self):
        raise AssertionError("multipart body was buffered into memory")

    with TestClient(main.app) as client:
        _login(client)
        notebook_id = _ready_notebook(main, db, client)
        monkeypatch.setattr(StarletteRequest, "body", exploding_body)

        assert _upload(client, notebook_id, "a.txt", b"hello").status_code == 303


def test_multipart_csrf_accepts_the_form_field_and_rejects_a_bad_token(monkeypatch, tmp_path):
    """A browser form submit carries the token as a field, not a header."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        _login(client)
        notebook_id = _ready_notebook(main, db, client)
        token = client.cookies.get("csrf_token")

        # The plain-form path: token as a multipart field, no header at all.
        accepted = client.post(
            f"/notebooks/{notebook_id}/sources/upload",
            files={"files": ("a.txt", b"hello", "text/plain")},
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert accepted.status_code == 303

        forged = client.post(
            f"/notebooks/{notebook_id}/sources/upload",
            files={"files": ("b.txt", b"hello", "text/plain")},
            data={"csrf_token": "not-a-signed-token"},
            headers={"X-CSRF-Token": ""},
            follow_redirects=False,
        )
        assert forged.status_code == 403

        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"] == 1


def test_startup_refuses_a_multipart_route_without_the_csrf_dependency(monkeypatch, tmp_path):
    """The fail-closed guard: forgetting the check breaks the boot, loudly.

    Because the middleware no longer validates multipart requests, a new upload
    route that omits `verify_multipart_csrf` would be silently unprotected. This
    pins that it cannot start at all.
    """
    from fastapi import FastAPI, File, UploadFile

    main, _ = _fresh_app(monkeypatch, tmp_path)

    unguarded = FastAPI()

    @unguarded.post("/unguarded-upload")
    def _unguarded(files: list[UploadFile] = File(...)):  # pragma: no cover - never runs
        return {}

    with pytest.raises(RuntimeError, match="verify_multipart_csrf"):
        main.assert_multipart_routes_check_csrf(unguarded)

    # And the real app passes the same check.
    main.assert_multipart_routes_check_csrf(main.app)


# --- SEC-7/8: admin errors and logout cookie hygiene --------------------------


def test_admin_create_user_failure_does_not_echo_database_exception(
    monkeypatch, tmp_path, caplog
):
    main, _db = _fresh_app(monkeypatch, tmp_path)
    caplog.set_level("ERROR", logger="app.admin")

    with TestClient(main.app) as client:
        _login(client)
        response = client.post(
            "/admin/users/new",
            data={"username": "admin", "password": "password1"},
        )

    assert response.status_code == 400
    assert main.i18n.t("admin_users.create_failed") in response.text
    assert "UNIQUE constraint failed" not in response.text
    assert "users.username" not in response.text
    failure_records = [
        record for record in caplog.records if record.getMessage().startswith("user_create_failed")
    ]
    assert len(failure_records) == 1
    assert failure_records[0].exc_info is not None
    assert "UNIQUE constraint failed" in str(failure_records[0].exc_info[1])


def _response_cookie_header(response, name: str) -> str:
    prefix = f"{name}="
    matches = [
        value for value in response.headers.get_list("set-cookie") if value.startswith(prefix)
    ]
    assert len(matches) == 1
    return matches[0].lower()


@pytest.mark.parametrize(
    ("base_url", "expects_secure"),
    [("http://testserver", False), ("https://testserver", True)],
)
def test_logout_cookie_attributes_match_session_issuance(
    monkeypatch, tmp_path, base_url, expects_secure
):
    main, _db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app, base_url=base_url) as client:
        issued = client.post(
            "/login",
            data={"username": "admin", "password": "admin123"},
            follow_redirects=False,
        )
        deleted = client.post("/logout", follow_redirects=False)

    assert issued.status_code == 303
    assert deleted.status_code == 303
    issued_cookie = _response_cookie_header(issued, "session")
    deleted_cookie = _response_cookie_header(deleted, "session")
    for attribute in ("path=/", "httponly", "samesite=lax"):
        assert attribute in issued_cookie
        assert attribute in deleted_cookie
    assert ("secure" in issued_cookie) is expects_secure
    assert ("secure" in deleted_cookie) is expects_secure
    assert "max-age=0" in deleted_cookie


# --- SEC-3: password changes revoke other sessions, sessions expire -----------


def _settled_admin(main, tmp_path):
    """Get past SEC-1's forced bootstrap change so the account is in normal state."""
    client = TestClient(main.app)
    client.__enter__()
    _login(client)
    token = client.cookies.get("csrf_token")
    client.post(
        "/account/password",
        data={
            "current_password": "admin123",
            "new_password": "password-one",
            "confirm_password": "password-one",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    return client


def test_changing_a_password_revokes_other_sessions_but_not_your_own(monkeypatch, tmp_path):
    """The SEC-1 acceptance gap this closes.

    SEC-1 spends the bootstrap credential for *login*, but a session established
    with it used to survive the forced change untouched — so anyone who had
    already signed in with `admin123` stayed signed in. Bumping
    `users.password_version` and comparing it per request fixes that, and
    re-issuing the actor's cookie keeps them from logging themselves out.
    """
    monkeypatch.setenv("NOTEBOOKLM_SEED_DEMO_USERS", "0")
    main, db = _fresh_app(monkeypatch, tmp_path)

    laptop = _settled_admin(main, tmp_path)
    try:
        copied = laptop.cookies.get("session")
        phone = TestClient(main.app)
        phone.cookies.set("session", copied)
        assert phone.get("/notebooks", follow_redirects=False).status_code == 200

        token = laptop.cookies.get("csrf_token")
        changed = laptop.post(
            "/account/password",
            data={
                "current_password": "password-one",
                "new_password": "password-two",
                "confirm_password": "password-two",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert changed.status_code == 200

        stale = TestClient(main.app)
        stale.cookies.set("session", copied)
        assert stale.get("/notebooks", follow_redirects=False).status_code == 303, (
            "the session issued under the old password must be revoked"
        )
        assert laptop.get("/notebooks", follow_redirects=False).status_code == 200, (
            "the session that performed the change must be re-issued, not dropped"
        )

        with db.connect() as conn:
            version = conn.execute(
                "SELECT password_version FROM users WHERE username = 'admin'"
            ).fetchone()["password_version"]
        assert version == 3  # 1 seeded, +1 forced change, +1 this change
    finally:
        laptop.__exit__(None, None, None)


def test_admin_reset_signs_the_target_out_everywhere(monkeypatch, tmp_path):
    """Resetting a compromised account is pointless if the intruder stays in.

    Unlike a self-service change there is no session to preserve — the target is
    signed out on every device, and the audit event records that.
    """
    monkeypatch.setenv("NOTEBOOKLM_SEED_DEMO_USERS", "0")
    main, db = _fresh_app(monkeypatch, tmp_path)

    admin = _settled_admin(main, tmp_path)
    try:
        admin.post(
            "/admin/users/new",
            data={"username": "victim", "password": "victim-password"},
            follow_redirects=False,
        )
        victim = TestClient(main.app)
        victim.get("/login")
        victim.post(
            "/login",
            data={
                "username": "victim",
                "password": "victim-password",
                "csrf_token": victim.cookies.get("csrf_token"),
            },
            follow_redirects=False,
        )
        assert victim.get("/notebooks", follow_redirects=False).status_code == 200

        with db.connect() as conn:
            target_id = conn.execute(
                "SELECT id FROM users WHERE username = 'victim'"
            ).fetchone()["id"]
        admin.post(
            f"/admin/users/{target_id}/reset-password",
            data={"new_password": "reset-by-admin"},
            follow_redirects=False,
        )

        assert victim.get("/notebooks", follow_redirects=False).status_code == 303
        with db.connect() as conn:
            audit = conn.execute(
                "SELECT action, metadata_json FROM audit_events"
                " WHERE action = 'user_password_reset' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert '"sessions_revoked": true' in audit["metadata_json"].lower()
    finally:
        admin.__exit__(None, None, None)


def test_an_expired_session_is_refused(monkeypatch, tmp_path):
    """Past the configured lifetime the token stops working, clock-based."""
    import itsdangerous.timed

    monkeypatch.setenv("NOTEBOOKLM_SEED_DEMO_USERS", "0")
    main, db = _fresh_app(monkeypatch, tmp_path)

    client = _settled_admin(main, tmp_path)
    try:
        assert client.get("/notebooks", follow_redirects=False).status_code == 200

        real_time = itsdangerous.timed.time.time
        monkeypatch.setattr(
            itsdangerous.timed.time,
            "time",
            lambda: real_time() + main.SESSION_MAX_AGE_SECONDS + 60,
        )
        assert client.get("/notebooks", follow_redirects=False).status_code == 303
    finally:
        client.__exit__(None, None, None)
