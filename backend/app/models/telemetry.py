"""Telemetry: samples read back from netwatch and system/resource.

RouterOS netwatch already measures loss, jitter and RTT on every tunnel --
the SLA profiles that drive failover depend on exactly this data, written by
the controller and never read back. This table is that read, kept: see
app.telemetry.poller for what fills it and docs/plan-v2.md M7 for why.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Tenanted, UUIDPk


class Sample(Base, UUIDPk, Tenanted):
    """One measurement, one metric, one point in time.

    ``site_id`` is always set; ``link_id`` only for a per-tunnel metric (loss,
    RTT) -- a site-level metric (CPU, free memory) has none. Both are plain
    columns, not a relationship: this table is write-heavy and read in bulk by
    time range, and an ORM relationship here would load rows nobody asked for
    on every insert.
    """

    __tablename__ = "samples"

    site_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    link_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("links.id", ondelete="CASCADE"), nullable=True, index=True
    )
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # Every real query is "this link (or site), this metric, this time
        # range" -- match that shape directly instead of leaving the planner
        # to combine single-column indexes.
        Index("ix_samples_link_metric_time", "link_id", "metric", "collected_at"),
        Index("ix_samples_site_metric_time", "site_id", "metric", "collected_at"),
    )
