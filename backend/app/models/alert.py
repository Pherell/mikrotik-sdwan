"""Alerts: state transitions worth telling someone about, and where to tell them.

Three tables, each with one job:

``alerts``
    The event log. One row per transition (link went down, site stopped
    answering, an apply rolled back). The UI and ``GET /alerts`` read it; the
    worker's delivery sweep reads the ``pending`` rows and pushes them out.

``alert_states``
    The last state seen for each watched subject (a link as seen from one
    site, a site's reachability, a site's drift). This is what makes the log a
    log of *transitions*: the poller observes "down" every 30 seconds while a
    tunnel is down, and only the first of those observations differs from what
    is stored here. Kept in its own table rather than inferred from the last
    sample or the last alert because both of those are lossy -- samples are
    only written when netwatch returns numbers, and an alert row is only
    written on an edge, so "no alert yet" would be indistinguishable from
    "known good".

``notification_channels``
    Per-tenant destinations. The URL / bot token is a credential (a Slack or
    Teams webhook URL *is* the secret; a Telegram bot token can post as the
    bot anywhere), so it is stored encrypted with SecretBox exactly like
    device passwords, and only a masked hint is ever returned by the API.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Tenanted, Timestamps, UUIDPk, utcnow
from app.models.enums import AlertKind, AlertSeverity, ChannelType
from app.models.site import JSONCol


class Alert(Base, UUIDPk, Tenanted):
    __tablename__ = "alerts"

    # Python-side default for the same reason as AuditEvent.created_at:
    # SQLite's CURRENT_TIMESTAMP is whole seconds, and an event log listed
    # newest-first has to keep the order events happened in.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow, nullable=False
    )
    kind: Mapped[AlertKind] = mapped_column(String(32), nullable=False, index=True)
    severity: Mapped[AlertSeverity] = mapped_column(String(16), nullable=False)

    # SET NULL, not CASCADE: deleting a site must not rewrite history. The
    # name is snapshotted alongside so the row still reads sensibly after.
    site_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sites.id", ondelete="SET NULL"), index=True
    )
    site_name: Mapped[str | None] = mapped_column(String(128))
    link_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("links.id", ondelete="SET NULL")
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict | None] = mapped_column(JSONCol)

    # pending -> sending -> sent | partial | failed | skipped | expired.
    # See app.services.alerts.deliver_pending for what each one means.
    delivery_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Already redacted when written. Never holds a URL or token.
    delivery_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_alerts_tenant_created", "tenant_id", "created_at"),)


class AlertState(Base, UUIDPk, Tenanted):
    """Last observed state of one subject. Not exposed over the API."""

    __tablename__ = "alert_states"

    # "link:<link_id>@<site_id>", "site:<site_id>", "drift:<site_id>".
    subject: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    # CASCADE here, unlike Alert: the state of a deleted site is meaningless,
    # and a stale row would make a re-created site with a recycled id (it
    # cannot happen with UUIDs, but still) start from someone else's state.
    site_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sites.id", ondelete="CASCADE"), index=True
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # Indexed, deliberately *not* unique. A unique constraint would turn the
    # rare race of two writers observing the same subject at once (a poll and
    # an interactive probe) into an IntegrityError inside the caller's
    # transaction -- failing the poll that merely observed the state. A
    # duplicate row costs at worst one duplicate alert; the reader takes the
    # newest. Savepoints would be the other answer, but pysqlite's savepoint
    # handling is broken in the default isolation mode the tests run under.
    __table_args__ = (Index("ix_alert_states_subject", "subject"),)


class NotificationChannel(Base, UUIDPk, Timestamps, Tenanted):
    __tablename__ = "notification_channels"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[ChannelType] = mapped_column(String(16), nullable=False)
    # SecretBox-encrypted JSON: {"url": ...} for a webhook,
    # {"bot_token": ..., "chat_id": ...} for Telegram.
    config_enc: Mapped[str] = mapped_column(Text, nullable=False)
    # What the API shows instead: "https://hooks.example.com/***" or
    # "telegram chat -100***". Computed on write so a read never decrypts.
    target_hint: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Default info: recoveries (link_up, site_reachable) are info, and a
    # channel that says "down" but never "back up" leaves somebody paging
    # through the UI to find out whether to get out of bed.
    min_severity: Mapped[AlertSeverity] = mapped_column(
        String(16), nullable=False, default=AlertSeverity.info
    )
    # Bookkeeping for the UI: did the last push work, and if not, why
    # (redacted). Not used for any decision.
    last_status: Mapped[str | None] = mapped_column(String(16))
    last_error: Mapped[str | None] = mapped_column(Text)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_channel_tenant_name"),)
