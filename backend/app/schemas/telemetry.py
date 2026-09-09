"""Telemetry series shapes."""

from __future__ import annotations

from pydantic import BaseModel

from app.schemas.time import UtcDatetime


class SeriesPoint(BaseModel):
    ts: UtcDatetime
    value: float


class SeriesRead(BaseModel):
    link_id: str
    metric: str
    points: list[SeriesPoint]
