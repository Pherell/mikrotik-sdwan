"""Password hashing, JWT issuance, and at-rest encryption for device secrets."""

from __future__ import annotations

import base64
import hashlib
import secrets
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


# -- API tokens --------------------------------------------------------------

# A recognisable prefix on the whole credential. Secret scanners key off
# patterns like this, and a string found in a log or a repository is worth
# nothing to the finder unless they can tell what it opens -- which cuts both
# ways, and on balance being able to revoke a leaked token quickly wins.
TOKEN_LABEL = "sdwan"
# Bytes of randomness in each half. The prefix only has to be unique; the
# secret has to be unguessable.
_PREFIX_BYTES = 6
_SECRET_BYTES = 32


def new_api_token() -> tuple[str, str, str]:
    """Mint a token. Returns (whole credential, prefix, hash of the secret).

    The whole credential is shown once, at creation, and never stored. What is
    stored is the prefix -- so the row can be found and named -- and a digest
    of the secret.
    """
    prefix = secrets.token_hex(_PREFIX_BYTES)
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    return f"{TOKEN_LABEL}_{prefix}_{secret}", prefix, hash_api_secret(secret)


def hash_api_secret(secret: str) -> str:
    """SHA-256, deliberately, not bcrypt.

    bcrypt makes guessing a human-chosen secret slow. This secret is 32 bytes
    of os.urandom, so there is nothing to guess and a KDF would only add a
    fixed delay to every API request. What must be true is that the database
    never holds the usable value, and one round of SHA-256 over 256 bits of
    entropy gives exactly that.
    """
    return hashlib.sha256(secret.encode()).hexdigest()


def split_api_token(credential: str) -> tuple[str, str] | None:
    """(prefix, secret) from a whole credential, or None if it is not one.

    Returning None rather than raising keeps the caller honest: a request that
    presents a JWT must fall through to the JWT path, not fail here.
    """
    # maxsplit=2: token_urlsafe emits "-" and "_", so the secret half can
    # contain underscores of its own. The prefix is hex and never can.
    parts = credential.split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_LABEL:
        return None
    prefix, secret = parts[1], parts[2]
    if not prefix or not secret:
        return None
    return prefix, secret


def api_secret_matches(secret: str, stored_hash: str) -> bool:
    """Compare in constant time.

    The prefix already narrowed this to one row, so a timing signal here would
    leak the secret of a *known* token rather than merely its existence.
    """
    return secrets.compare_digest(hash_api_secret(secret), stored_hash)


# -- Enrollment tokens --------------------------------------------------------

# A one-time bootstrap credential, fetched over an unauthenticated URL by a
# factory-default router that has no other credentials yet. Deliberately its
# own functions rather than new_api_token() with a different label: an
# enrollment credential is never presented as a Bearer token and never
# reaches current_user()'s split_api_token() path, and conflating the two
# would make it too easy for a future change to one to silently change the
# other's contract. hash_api_secret / api_secret_matches are reused as-is --
# the digest and the constant-time comparison are generic, not label-specific.
ENROLL_LABEL = "enroll"
_ENROLL_PREFIX_BYTES = 6
_ENROLL_SECRET_BYTES = 32


def new_enrollment_token() -> tuple[str, str, str]:
    """Mint an enrollment token. Returns (whole credential, prefix, hash).

    Shown once, in the response to creating it -- the same discipline as
    new_api_token, for the same reason.
    """
    prefix = secrets.token_hex(_ENROLL_PREFIX_BYTES)
    secret = secrets.token_urlsafe(_ENROLL_SECRET_BYTES)
    return f"{ENROLL_LABEL}_{prefix}_{secret}", prefix, hash_api_secret(secret)


def split_enrollment_token(credential: str) -> tuple[str, str] | None:
    """(prefix, secret) from a whole credential, or None if it is not one."""
    parts = credential.split("_", 2)
    if len(parts) != 3 or parts[0] != ENROLL_LABEL:
        return None
    prefix, secret = parts[1], parts[2]
    if not prefix or not secret:
        return None
    return prefix, secret
