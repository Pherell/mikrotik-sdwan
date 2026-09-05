"""Jobs and the audit trail.

Every mutating action against a device produces a Job row holding the rendered
config, the diff, and the outcome. This is both the UI's job log and the audit
record, so it is never deleted by the application.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Tenanted, Timestamps, UUIDPk
from app.models.enums import JobKind, JobState
from app.models.site import JSONCol


class Job(Base, UUIDPk, Timestamps, Tenanted):
    __tablename__ = "jobs"

    kind: Mapped[JobKind] = mapped_column(String(24), nullable=False, index=True)
    state: Mapped[JobState] = mapped_column(
        String(24), nullable=False, default=JobState.queued, index=True
    )

    site_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sites.id", ondelete="SET NULL"), index=True
    )
    fabric_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("fabrics.id", ondelete="SET NULL"), index=True
    )
    requested_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )

    # Rendered config and diff, secrets already masked. Safe to show in the UI.
    plan: Mapped[dict | None] = mapped_column(JSONCol)
    diff: Mapped[dict | None] = mapped_column(JSONCol)
    result: Mapped[dict | None] = mapped_column(JSONCol)
    log: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    # Set while a dead-man rollback scheduler is armed on the device.
    rollback_token: Mapped[str | None] = mapped_column(String(64))
    backup_name: Mapped[str | None] = mapped_column(String(128))

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AuditEvent(Base, UUIDPk, Timestamps, Tenanted):
    """Append-only record of who did what. Written for auth events and every
    state-changing API call."""

    __tablename__ = "audit_events"

    actor_id: Mapped[str | None] = mapped_column(String(36), index=True)
    actor_email: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    object_type: Mapped[str | None] = mapped_column(String(64), index=True)
    object_id: Mapped[str | None] = mapped_column(String(36), index=True)
    detail: Mapped[dict | None] = mapped_column(JSONCol)
    source_ip: Mapped[str | None] = mapped_column(String(64))
