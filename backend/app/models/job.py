"""Jobs and the audit trail.

Every mutating action against a device produces a Job row holding the rendered
config, the diff, and the outcome. This is both the UI's job log and the audit
record, so it is never deleted by the application.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Tenanted, Timestamps, UUIDPk, utcnow
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

    # M10 maintenance windows. Null on every job before this and on every
    # apply that still runs the old way, now or never. Set together: an
    # apply queued for a window is approved *at scheduling time* (confirm is
    # still required to create it) and pushed unattended once the window
    # opens, by app.tasks.worker.run_scheduled_applies -- reusing
    # services.reconcile.apply_site exactly as the interactive endpoint
    # does, on the same Job row rather than a new one.
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    # Optional. Past this instant the window has closed: the sweep marks the
    # job failed rather than push a change outside the hours it was approved
    # for, which is the entire point of naming a window in the first place.
    window_closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base, UUIDPk, Timestamps, Tenanted):
    """Append-only record of who did what. Written for auth events and every
    state-changing API call."""

    __tablename__ = "audit_events"

    # created_at is generated in Python here, overriding the mixin's
    # server_default. func.now() is CURRENT_TIMESTAMP, which SQLite resolves to
    # whole seconds -- so two events written in the same second came back in
    # UUID order, which is to say in no order at all. For an append-only trail
    # the order *is* the content. No migration: the column is unchanged, only
    # who fills it in.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow, nullable=False
    )

    actor_id: Mapped[str | None] = mapped_column(String(36), index=True)
    actor_email: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    object_type: Mapped[str | None] = mapped_column(String(64), index=True)
    object_id: Mapped[str | None] = mapped_column(String(36), index=True)
    detail: Mapped[dict | None] = mapped_column(JSONCol)
    source_ip: Mapped[str | None] = mapped_column(String(64))
