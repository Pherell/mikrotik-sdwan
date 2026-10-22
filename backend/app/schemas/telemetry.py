"""Telemetry series shapes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.schemas.time import UtcDatetime


class SeriesPoint(BaseModel):
    ts: UtcDatetime
    value: float


class SeriesRead(BaseModel):
    link_id: str
    metric: str
    points: list[SeriesPoint]


class SlaEvaluationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    site_id: str
    link_id: str
    sdwan_group_id: str | None
    sla_profile_id: str | None
    bucket_start: UtcDatetime
    window_start: UtcDatetime
    window_end: UtcDatetime
    window_minutes: int
    loss_percent: float | None
    latency_ms: float | None
    jitter_ms: float | None
    classification: str
    previous_class: str | None
    evaluated_at: UtcDatetime


class SlaEvaluationListRead(BaseModel):
    link_id: str
    evaluations: list[SlaEvaluationRead]


class SlaClassCounts(BaseModel):
    in_sla: int = 0
    degraded: int = 0
    down: int = 0


class SlaClassShares(BaseModel):
    in_sla: float | None = None
    degraded: float | None = None
    down: float | None = None


class SlaComplianceTotals(BaseModel):
    buckets: int
    counts: SlaClassCounts
    shares: SlaClassShares
    violation_buckets: int
    violation_episodes: int
    links_total: int
    links_with_data: int
    paths_with_data: int


class SlaSiteCompliance(BaseModel):
    site_id: str
    site_name: str
    has_data: bool
    buckets: int
    counts: SlaClassCounts
    shares: SlaClassShares
    violation_episodes: int
    current_class: str | None
    last_bucket_start: UtcDatetime | None


class SlaLinkCompliance(BaseModel):
    link_id: str
    slug: str
    fabric_id: str
    fabric_name: str
    a_site_name: str
    a_wan_name: str
    b_site_name: str
    b_wan_name: str
    has_data: bool
    buckets: int
    counts: SlaClassCounts
    shares: SlaClassShares
    violation_episodes: int
    current_class: str | None
    last_bucket_start: UtcDatetime | None


class SlaComplianceRead(BaseModel):
    window: str
    window_hours: int
    since: UtcDatetime
    until: UtcDatetime
    totals: SlaComplianceTotals
    sites: list[SlaSiteCompliance]
    links: list[SlaLinkCompliance]
