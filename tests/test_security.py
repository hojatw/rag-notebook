from app.security import decrypt_secret, encrypt_secret


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
