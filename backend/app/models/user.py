"""Users and RBAC."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Tenanted, Timestamps, UUIDPk
from app.models.enums import Role


class User(Base, UUIDPk, Timestamps, Tenanted):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role] = mapped_column(String(16), nullable=False, default=Role.viewer)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # M10: token revocation. A JWT is a bearer credential the controller
    # never sees again after issuing it, so "revoke this token" is not
    # something a row can record -- there is no row. What can be recorded is
    # "nothing issued before this instant is valid any more": every token
    # carries the moment it was issued (iat), and deps.current_user refuses
    # one whose iat is older than this. Signing out everywhere, forcing a
    # stolen laptop's session to die now rather than at its 12-hour default
    # expiry, and disabling a user all set this to now() -- one mechanism,
    # not three.
    tokens_valid_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
