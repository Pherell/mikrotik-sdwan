"""Password hashing, JWT issuance, and at-rest encryption for device secrets."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.config import get_settings

# bcrypt is used directly rather than through passlib: passlib is unmaintained
# and imports the stdlib ``crypt`` module, which was removed in Python 3.13.
_BCRYPT_ROUNDS = 12
# bcrypt silently truncates at 72 bytes, so reject longer input rather than
# letting two different passwords authenticate the same account.
_MAX_PASSWORD_BYTES = 72


def hash_password(plain: str) -> str:
    raw = plain.encode()
    if len(raw) > _MAX_PASSWORD_BYTES:
        raise ValueError(
            f"Password must be at most {_MAX_PASSWORD_BYTES} bytes (bcrypt truncates beyond that)"
        )
    return bcrypt.hashpw(raw, bcrypt.gensalt(_BCRYPT_ROUNDS)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    raw = plain.encode()
    if len(raw) > _MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(raw, hashed.encode())
    except ValueError:
        # Malformed stored hash. Fail closed.
        return False


def create_access_token(subject: str, claims: dict[str, Any] | None = None) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=s.access_token_ttl_minutes),
        **(claims or {}),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    s = get_settings()
    return jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])


# Iteration count for the key derivation. Deliberately high: the derived key
# protects every router credential in the fleet, and it is computed once per
# SecretBox rather than per record, so the cost is paid on a handful of
# operations per request at most.
_KDF_ITERATIONS = 480_000
_KDF_SALT = b"mikrotik-sdwan/secret-key/v2"


class SecretBox:
    """Symmetric encryption for credentials and link secrets.

    The Fernet key is derived from ``SDWAN_SECRET_KEY`` with PBKDF2. An earlier
    version used a bare SHA-256, on the argument that the input was always a
    machine-generated high-entropy secret -- but nothing enforces that, and
    ``min_length=32`` counts characters, not entropy. Someone typing a
    memorable passphrase got no stretching at all.

    Values written under the old scheme are still readable: decryption falls
    back to it, and anything re-encrypted afterwards is written with the new
    one. There is no flag day.
    """

    def __init__(self, secret: str | None = None) -> None:
        raw = (secret or get_settings().secret_key).encode()
        self._f = Fernet(_derive(raw))
        # Legacy reader, kept only so existing records stay decryptable.
        self._legacy = Fernet(
            base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
        )

    def encrypt(self, plaintext: str) -> str:
        return self._f.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        token = ciphertext.encode()
        try:
            return self._f.decrypt(token).decode()
        except InvalidToken:
            pass
        try:
            return self._legacy.decrypt(token).decode()
        except InvalidToken as exc:
            raise ValueError(
                "Could not decrypt stored secret. SDWAN_SECRET_KEY has changed "
                "since this record was written."
            ) from exc


@lru_cache(maxsize=4)
def _derive(raw: bytes) -> bytes:
    """PBKDF2-HMAC-SHA256 over the configured secret.

    Cached because the iteration count makes this expensive and the input is a
    single process-wide value; without the cache every SecretBox() would cost
    hundreds of milliseconds.
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_KDF_SALT,
        iterations=_KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(raw))


def mask(value: str, keep: int = 4) -> str:
    """Render a secret for logs and API responses without disclosing it."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)
