"""API tokens.

Automation needs a credential that is not a person. Today the only one is a
user's login JWT, so every script runs as somebody with that somebody's full
rights, expires when their session does, and cannot be revoked without
disabling the human.

A token's permission is a **role**, not a separate scope vocabulary. The
product already has three roles that mean something -- viewer reads, operator
applies configuration, admin manages people and credentials -- and inventing a
second, orthogonal permission system next to them produces two models that
have to agree and eventually do not. What a token adds on top of the role is
the rest of a credential's life cycle: a name, an owner, an expiry, a last-used
time, and revocation.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Tenanted, Timestamps, UUIDPk
from app.models.enums import Role


class ApiToken(Base, UUIDPk, Timestamps, Tenanted):
    __tablename__ = "api_tokens"

    name: Mapped[str] = mapped_column(String(128), nullable=False)

    # The lookup key: the token's public half, unique and indexed. Carrying it
    # separately is what makes verification one indexed read rather than a
    # scan that hashes every stored token.
    prefix: Mapped[str] = mapped_column(
        String(16), unique=True, nullable=False, index=True
    )
    # SHA-256 of the secret half, not bcrypt.
    #
    # bcrypt exists to make guessing a *human-chosen* secret slow. This secret
    # is 32 bytes from os.urandom, so there is nothing to guess, and a KDF
    # would only buy a deliberate delay on every single API request. What
    # matters here is that the database never holds the usable value, and a
    # single SHA-256 of a 256-bit random string gives exactly that.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # The ceiling on what this token may do. The effective permission is the
    # lesser of this and its owner's role, so demoting a person weakens their
    # tokens too -- a token must never be a way to keep rights you have lost.
    role: Mapped[Role] = mapped_column(String(16), nullable=False, default=Role.viewer)

    owner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Null means no expiry. Allowed, because a CI token that dies unannounced
    # at 3am is its own kind of outage -- but the UI defaults to an expiry and
    # says what "never" costs.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set on use, best effort. This is what answers "is anything still using
    # this token?" before you revoke it.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Revocation is a timestamp rather than a delete: a token that authorised
    # something last week must still be nameable in the audit trail.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
