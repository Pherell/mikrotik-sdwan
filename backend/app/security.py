"""Password hashing, JWT issuance, and at-rest encryption for device secrets."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

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


class SecretBox:
    """Symmetric encryption for credentials and link secrets.

    The Fernet key is derived from ``SDWAN_SECRET_KEY`` so operators can supply a
    human-typed passphrase instead of a base64 key. Derivation is a plain SHA-256
    rather than a KDF because the input is a machine-generated high-entropy secret,
    not a user password.
    """

    def __init__(self, secret: str | None = None) -> None:
        raw = (secret or get_settings().secret_key).encode()
        self._f = Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw).digest()))

    def encrypt(self, plaintext: str) -> str:
        return self._f.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._f.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:  # pragma: no cover - operator error path
            raise ValueError(
                "Could not decrypt stored secret. SDWAN_SECRET_KEY has changed "
                "since this record was written."
            ) from exc


def mask(value: str, keep: int = 4) -> str:
    """Render a secret for logs and API responses without disclosing it."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)
