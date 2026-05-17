import pytest

from app.security import (
    ALLOW_INSECURE_DEV_SECRET_ENV,
    APP_SECRET_ENV,
    INSECURE_DEV_SECRET,
    decrypt_secret,
    encrypt_secret,
    get_app_secret,
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
