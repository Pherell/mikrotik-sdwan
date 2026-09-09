"""Enrollment tokens: one-touch provisioning for a factory-default device.

An operator mints one of these; a field tech pastes one line into the
router's terminal. See app.services.enrollment for the script it fetches and
what happens when the router calls back, and docs/plan-v2.md M8 for why.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Tenanted, Timestamps, UUIDPk
from app.models.enums import SiteRole
from app.models.site import JSONCol


class EnrollmentToken(Base, UUIDPk, Timestamps, Tenanted):
    __tablename__ = "enrollment_tokens"

    # Operator-facing label ("branch-42"), not shown to the device.
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    # Same shape as ApiToken: the row holds only what identifies and verifies
    # the credential, never the credential itself.
    prefix: Mapped[str] = mapped_column(String(16), nullable=False, unique=True, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # What the enrolled site gets created with.
    site_name: Mapped[str] = mapped_column(String(128), nullable=False)
    site_role: Mapped[SiteRole] = mapped_column(String(16), nullable=False, default=SiteRole.spoke)
    local_prefixes: Mapped[list | None] = mapped_column(JSONCol, default=list)
    fabric_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("fabrics.id", ondelete="SET NULL"), nullable=True
    )

    # The device password the bootstrap script sets on the router. Generated
    # once at token creation, encrypted at rest exactly like Site.password_enc
    # -- the router has to be told this value, and the controller has to
    # remember it afterward to actually manage the device. Nobody types or
    # sees it; it is generated, encrypted, and used exactly once to build the
    # script and once to build the resulting Site row.
    device_password_enc: Mapped[str] = mapped_column(Text, nullable=False)

    # Optional: refuse a fetch or confirm from outside this range. A stolen
    # URL is then useless off the network it was meant for.
    source_cidr: Mapped[str | None] = mapped_column(String(64))

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_from_ip: Mapped[str | None] = mapped_column(String(64))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )
    enrolled_site_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sites.id", ondelete="SET NULL")
    )
