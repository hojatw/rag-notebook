import pytest

from app.security import (
    ALLOW_INSECURE_DEV_SECRET_ENV,
    APP_SECRET_ENV,
    INSECURE_DEV_SECRET,
    decrypt_secret,
    encrypt_secret,
    get_app_secret,
    new_csrf_token,
    valid_csrf_token,
)


SECRET = "test-secret-do-not-use-in-prod"


def test_encrypt_decrypt_roundtrip():
    """A value encrypted with a secret round-trips back to the same plaintext."""
    token = encrypt_secret("sk-abc-123", SECRET)
    assert token != "sk-abc-123"
    assert token.startswith("gAAAAA")
    assert decrypt_secret(token, SECRET) == "sk-abc-123"


def test_encrypt_empty_returns_empty():
    """Encrypting an empty string should give an empty string back."""
    assert encrypt_secret("", SECRET) == ""
    assert decrypt_secret("", SECRET) == ""


def test_decrypt_legacy_plaintext_passthrough():
    """Values stored before encryption (no Fernet prefix) decrypt to themselves.

    This keeps existing API keys working on first read after the encryption
    column migration, without forcing an admin to re-enter them.
    """
    assert decrypt_secret("sk-old-plaintext", SECRET) == "sk-old-plaintext"


def test_decrypt_with_wrong_secret_returns_empty():
    """A cipher decrypted with the wrong secret returns empty, not garbage."""
    token = encrypt_secret("sk-abc-123", SECRET)
    assert decrypt_secret(token, "wrong-secret") == ""


def test_two_secrets_produce_different_ciphertexts():
    """Same plaintext + different secrets must not collide."""
    a = encrypt_secret("payload", SECRET)
    b = encrypt_secret("payload", SECRET + "x")
    assert a != b
    assert decrypt_secret(a, SECRET) == "payload"
    assert decrypt_secret(b, SECRET) == ""


def test_app_secret_requires_env_by_default(monkeypatch):
    """Production defaults must fail closed instead of silently using dev secret."""
    monkeypatch.delenv(APP_SECRET_ENV, raising=False)
    monkeypatch.delenv(ALLOW_INSECURE_DEV_SECRET_ENV, raising=False)

    with pytest.raises(RuntimeError, match=APP_SECRET_ENV):
        get_app_secret()


def test_app_secret_allows_explicit_local_dev_fallback(monkeypatch):
    """The insecure fallback is available only when explicitly opted in."""
    monkeypatch.delenv(APP_SECRET_ENV, raising=False)
    monkeypatch.setenv(ALLOW_INSECURE_DEV_SECRET_ENV, "1")

    assert get_app_secret() == INSECURE_DEV_SECRET


def test_app_secret_prefers_real_secret_over_dev_flag(monkeypatch):
    """A real secret wins even if the dev opt-in flag is present."""
    monkeypatch.setenv(APP_SECRET_ENV, "real-secret")
    monkeypatch.setenv(ALLOW_INSECURE_DEV_SECRET_ENV, "1")

    assert get_app_secret() == "real-secret"


def test_csrf_token_is_signed_and_secret_scoped():
    """CSRF tokens must validate only with the secret that created them."""
    token = new_csrf_token(SECRET)

    assert valid_csrf_token(token, SECRET)
    assert not valid_csrf_token(token, SECRET + "-other")


def test_csrf_token_rejects_missing_or_tampered_values():
    """Invalid CSRF values fail closed."""
    token = new_csrf_token(SECRET)

    assert not valid_csrf_token(None, SECRET)
    assert not valid_csrf_token("", SECRET)
    assert not valid_csrf_token(token + "x", SECRET)


# --- SEC-1: bootstrap accounts must not leave a standing default password ----


def _fresh_db(monkeypatch, tmp_path, *, seed_demo):
    """Reload app.db against an isolated data dir with a chosen seeding policy.

    `seed_demo=None` removes the override so the fallback (decide from the app
    secret) is what gets exercised.
    """
    import importlib

    monkeypatch.setenv("NOTEBOOKLM_DATA_DIR", str(tmp_path / "data"))
    if seed_demo is None:
        monkeypatch.delenv("NOTEBOOKLM_SEED_DEMO_USERS", raising=False)
    else:
        monkeypatch.setenv("NOTEBOOKLM_SEED_DEMO_USERS", seed_demo)
    import app.db as db

    importlib.reload(db)
    db.init_db()
    return db


def _user_row(db, username):
    with db.connect() as conn:
        return conn.execute(
            "SELECT id, username, is_admin, must_change_password, password_hash"
            " FROM users WHERE username = ?",
            (username,),
        ).fetchone()


def test_production_seeding_omits_demo_user_and_forces_admin_change(monkeypatch, tmp_path):
    """With demo seeding off, only `admin` is created — and it must change its password."""
    db = _fresh_db(monkeypatch, tmp_path, seed_demo="0")

    assert _user_row(db, "user") is None, "the `user` demo account must not be seeded"
    admin = _user_row(db, "admin")
    assert admin is not None, "a fresh deployment must stay enterable"
    assert admin["is_admin"] == 1
    assert admin["must_change_password"] == 1, "the bootstrap password must be one-time"


def test_deleted_demo_user_stays_deleted_across_restart(monkeypatch, tmp_path):
    """The SEC-1 regression: deleting a demo account must survive a restart.

    `init_db()` runs on every start of both the web app and the worker. Before
    this fix it re-created `user/user123` every time, so an operator who followed
    SECURITY.md's "remove them before exposing the app" got the account — and its
    original password — back on the next restart.
    """
    db = _fresh_db(monkeypatch, tmp_path, seed_demo="0")
    with db.connect() as conn:
        conn.execute("INSERT INTO users (username, password_hash, is_admin) VALUES ('user', 'x', 0)")
    assert _user_row(db, "user") is not None

    with db.connect() as conn:
        conn.execute("DELETE FROM users WHERE username = 'user'")

    db.init_db()  # simulate the restart

    assert _user_row(db, "user") is None


def test_demo_seeding_when_explicitly_enabled(monkeypatch, tmp_path):
    """Local development keeps both accounts with their advertised passwords."""
    db = _fresh_db(monkeypatch, tmp_path, seed_demo="1")

    demo_user = _user_row(db, "user")
    admin = _user_row(db, "admin")
    assert demo_user is not None and admin is not None
    # No forced change: the login page advertises these credentials on purpose.
    assert demo_user["must_change_password"] == 0
    assert admin["must_change_password"] == 0


def test_seeding_falls_back_to_the_app_secret(monkeypatch, tmp_path):
    """Unset, the policy follows the same signal the login page's demo hint uses."""
    monkeypatch.setenv(APP_SECRET_ENV, "a-real-deployment-secret")
    db = _fresh_db(monkeypatch, tmp_path, seed_demo=None)
    assert db.seed_demo_users_enabled() is False
    assert _user_row(db, "user") is None

    monkeypatch.delenv(APP_SECRET_ENV, raising=False)
    monkeypatch.setenv(ALLOW_INSECURE_DEV_SECRET_ENV, "1")
    db = _fresh_db(monkeypatch, tmp_path / "dev", seed_demo=None)
    assert db.seed_demo_users_enabled() is True
    assert _user_row(db, "user") is not None


def test_existing_default_passwords_are_flagged_on_upgrade(monkeypatch, tmp_path):
    """A database seeded by an older version gets its standing defaults neutralised.

    Not seeding any more only protects new deployments. Every database created
    before this change already carries `admin/admin123` and `user/user123`, so
    startup verifies each seeded username against the password it *would* have
    been created with and forces a change on the ones that never changed.
    """
    db = _fresh_db(monkeypatch, tmp_path, seed_demo="1")
    assert _user_row(db, "admin")["must_change_password"] == 0
    assert _user_row(db, "user")["must_change_password"] == 0

    monkeypatch.setenv("NOTEBOOKLM_SEED_DEMO_USERS", "0")
    db.init_db()  # the upgrade restart

    assert _user_row(db, "admin")["must_change_password"] == 1
    assert _user_row(db, "user")["must_change_password"] == 1, (
        "a pre-existing `user` account is not deleted, but its default password is spent"
    )


def test_changed_passwords_are_left_alone(monkeypatch, tmp_path):
    """An account that already moved off its default is never re-flagged."""
    from app.security import hash_password

    db = _fresh_db(monkeypatch, tmp_path, seed_demo="1")
    with db.connect() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = 'admin'",
            (hash_password("something-the-operator-chose"),),
        )

    monkeypatch.setenv("NOTEBOOKLM_SEED_DEMO_USERS", "0")
    db.init_db()
    db.init_db()  # idempotent across repeated restarts

    assert _user_row(db, "admin")["must_change_password"] == 0


def test_only_the_seeded_usernames_are_considered(monkeypatch, tmp_path):
    """The back-fill is scoped to the accounts we shipped, not a weak-password sweep.

    An ordinary account that happens to use `admin123` is left alone: the point
    is to spend the credentials *this project handed out*, not to audit user
    password choices, which would be a different (and much more intrusive)
    feature.
    """
    from app.security import hash_password

    db = _fresh_db(monkeypatch, tmp_path, seed_demo="1")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES ('bob', ?, 1)",
            (hash_password("admin123"),),
        )

    monkeypatch.setenv("NOTEBOOKLM_SEED_DEMO_USERS", "0")
    db.init_db()

    assert _user_row(db, "admin")["must_change_password"] == 1
    assert _user_row(db, "bob")["must_change_password"] == 0


def test_sso_linked_accounts_are_never_flagged(monkeypatch, tmp_path):
    """Guards against a permanent lockout, not just an inconvenience.

    A flagged account may only change its password, but `/account/password`
    refuses SSO-linked accounts (`auth.password_change_sso_blocked`). An account
    that was both flagged *and* SSO-linked would therefore have no way out.

    External auth cannot produce that state today — it only ever creates fresh
    usernames with an unguessable `sso:<uuid>` hash. `_flag_default_passwords`
    skips linked accounts anyway, so the property holds by construction rather
    than by coincidence, and a future "link SSO to an existing local account"
    feature cannot lock an operator out.
    """
    import uuid

    from app.security import hash_password

    db = _fresh_db(monkeypatch, tmp_path, seed_demo="0")
    with db.connect() as conn:
        # Worst case: an SSO identity attached to the seeded bootstrap admin,
        # which still holds `admin123`.
        admin_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
        conn.execute(
            "INSERT INTO external_identities (user_id, provider, subject) VALUES (?, 'oidc', 'sub-1')",
            (admin_id,),
        )
        conn.execute("UPDATE users SET must_change_password = 0 WHERE id = ?", (admin_id,))
        # And an ordinary SSO-provisioned account.
        conn.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES ('sso-user', ?, 0)",
            (hash_password(f"sso:{uuid.uuid4().hex}"),),
        )

    db.init_db()

    assert _user_row(db, "sso-user")["must_change_password"] == 0
    assert _user_row(db, "admin")["must_change_password"] == 0, (
        "flagging an SSO-linked account would lock it out: it may only change its "
        "password, and /account/password refuses external identities"
    )
