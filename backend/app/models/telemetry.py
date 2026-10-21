"""Telemetry: samples read back from netwatch and system/resource.

RouterOS netwatch already measures loss, jitter and RTT on every tunnel --
the SLA profiles that drive failover depend on exactly this data, written by
the controller and never read back. This table is that read, kept: see
app.telemetry.poller for what fills it and docs/plan-v2.md M7 for why.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint
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


class SlaEvaluation(Base, UUIDPk, Tenanted):
    """One windowed SLA verdict for one path, kept.

    Written by app.telemetry.evaluator from the samples the poller already
    collects. This table doubles as the Fase 2b compliance record: thresholds
    used to drive only momentary on-device failover decisions, so whether a
    path actually met its SLA over the last day was a question nobody could
    answer after the fact. One row per (site, link, group, bucket);
    re-evaluation inside the same bucket updates the row in place rather
    than appending, so a bucket always holds the freshest verdict for it.

    ``sdwan_group_id`` is null for a link no group covers (judged against the
    built-in default SLA); ``sla_profile_id`` is null whenever the default's
    thresholds were the ones used, so a compliance query can tell "judged
    against a profile the operator wrote" from "judged against the default".
    """

    __tablename__ = "sla_evaluations"

    site_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    link_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("links.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sdwan_group_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sdwan_groups.id", ondelete="SET NULL"), nullable=True
    )
    sla_profile_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sla_profiles.id", ondelete="SET NULL"), nullable=True
    )

    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    loss_percent: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    jitter_ms: Mapped[float | None] = mapped_column(Float)

    classification: Mapped[str] = mapped_column(String(16), nullable=False)
    # The classification of the previous bucket for the same path, recorded
    # when this row is first written and kept stable across in-bucket
    # updates -- this is the transition record (in_sla -> degraded -> down)
    # a compliance view reads, without re-deriving it from neighbours.
    previous_class: Mapped[str | None] = mapped_column(String(16))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "site_id",
            "link_id",
            "sdwan_group_id",
            "bucket_start",
            name="uq_sla_eval_path_bucket",
        ),
        Index("ix_sla_evaluations_link_bucket", "link_id", "bucket_start"),
        Index("ix_sla_evaluations_site_bucket", "site_id", "bucket_start"),
    )
