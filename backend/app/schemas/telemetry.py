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
