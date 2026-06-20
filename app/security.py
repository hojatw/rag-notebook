import base64
import hashlib
import hmac
import os
import secrets
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from itsdangerous import BadSignature, URLSafeSerializer


# Fernet ciphertext begins with "gAAAAA" (b"\x80\x00...") so we can detect
# unencrypted legacy values stored before this column was encrypted.
FERNET_PREFIX = "gAAAAA"
# Stable salt for deriving the encryption key from NOTEBOOKLM_SECRET. Rotating
# this salt invalidates every stored ciphertext, so it lives in code, not env.
ENCRYPTION_SALT = b"notebooklm-rag-poc.api-key.v1"
APP_SECRET_ENV = "NOTEBOOKLM_SECRET"
ALLOW_INSECURE_DEV_SECRET_ENV = "NOTEBOOKLM_ALLOW_INSECURE_DEV_SECRET"
INSECURE_DEV_SECRET = "dev-secret-change-me"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def get_app_secret() -> str:
    """Return the app secret, failing closed unless dev fallback is explicit."""
    secret = os.environ.get(APP_SECRET_ENV, "").strip()
    if secret:
        return secret
    if _truthy(os.environ.get(ALLOW_INSECURE_DEV_SECRET_ENV)):
        return INSECURE_DEV_SECRET
    raise RuntimeError(
        f"{APP_SECRET_ENV} is required. Generate one with: "
        "python -c \"import secrets; print(secrets.token_urlsafe(48))\". "
        f"For local-only development, set {ALLOW_INSECURE_DEV_SECRET_ENV}=1 "
        "to explicitly allow the insecure fallback."
    )


def _fernet(secret: str) -> Fernet:
    """Derive a Fernet cipher from the application secret."""
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=ENCRYPTION_SALT, iterations=200_000)
    key = base64.urlsafe_b64encode(kdf.derive(secret.encode("utf-8")))
    return Fernet(key)


def encrypt_secret(plaintext: str, secret: str) -> str:
    """Encrypt a sensitive value (e.g. an API key) for storage at rest."""
    if not plaintext:
        return ""
    return _fernet(secret).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str, secret: str) -> str:
    """Decrypt a value previously written by encrypt_secret.

    Returns the input unchanged when it does not look like Fernet ciphertext;
    this preserves backward compatibility with API keys stored before
    encryption was enabled. Returns "" if decryption fails so a wrong secret
    cannot leak garbage downstream.
    """
    if not token:
        return ""
    if not token.startswith(FERNET_PREFIX):
        return token  # legacy plaintext value
    try:
        return _fernet(secret).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Return a PBKDF2-SHA256 password hash string with an embedded salt."""
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return "pbkdf2_sha256$200000${}${}".format(
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    """Check a plaintext password against a stored PBKDF2 hash string."""
    try:
        algorithm, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def serializer(secret: str) -> URLSafeSerializer:
    """Create the signed-cookie serializer for the given application secret."""
    return URLSafeSerializer(secret, salt="notebooklm-rag-poc")


def csrf_serializer(secret: str) -> URLSafeSerializer:
    """Create the CSRF-token serializer for the given application secret."""
    return URLSafeSerializer(secret, salt="notebooklm-rag-poc.csrf")


def sign_user_id(user_id: int, secret: str) -> str:
    """Encode a user id into a tamper-resistant session cookie value."""
    return serializer(secret).dumps({"uid": user_id})


def unsign_user_id(value: str | None, secret: str) -> int | None:
    """Decode a session cookie value and return its user id when valid."""
    if not value:
        return None
    try:
        data = serializer(secret).loads(value)
        return int(data["uid"])
    except (BadSignature, KeyError, TypeError, ValueError):
        return None


def new_csrf_token(secret: str) -> str:
    """Return a signed, random CSRF token suitable for a double-submit cookie."""
    return csrf_serializer(secret).dumps({"nonce": secrets.token_urlsafe(32)})


def valid_csrf_token(value: str | None, secret: str) -> bool:
    """Return True when a CSRF token was signed by this application."""
    if not value:
        return False
    try:
        data = csrf_serializer(secret).loads(value)
        return isinstance(data.get("nonce"), str) and bool(data["nonce"])
    except (BadSignature, AttributeError, TypeError, ValueError):
        return False
