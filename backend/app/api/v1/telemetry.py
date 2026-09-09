"""Telemetry series: what a link's netwatch probe has measured over time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.deps import RequireViewer, SessionDep
from app.models.fabric import Fabric, Link
from app.models.telemetry import Sample
from app.schemas.telemetry import SeriesPoint, SeriesRead
from app.telemetry.poller import LINK_METRICS

router = APIRouter(prefix="/links", tags=["telemetry"])


@router.get("/{link_id}/series", response_model=SeriesRead)
async def link_series(
    link_id: str,
    session: SessionDep,
    user: RequireViewer,
    metric: str = Query(..., description=f"One of: {', '.join(LINK_METRICS)}"),
    since: datetime | None = None,
    until: datetime | None = None,
) -> SeriesRead:
    if metric not in LINK_METRICS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown metric {metric!r}. One of: {', '.join(LINK_METRICS)}",
        )

    # Link carries no tenant_id of its own -- it is scoped through the fabric
    # that owns it, the same reasoning fabrics.remove_member's fix rests on.
    owns = await session.scalar(
        select(Link.id)
        .join(Fabric, Link.fabric_id == Fabric.id)
        .where(Link.id == link_id, Fabric.tenant_id == user.tenant_id)
    )
    if owns is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such link")

    until = until or datetime.now(UTC)
    since = since or (until - timedelta(hours=1))

    rows = await session.scalars(
        select(Sample)
        .where(
            Sample.link_id == link_id,
            Sample.metric == metric,
            Sample.collected_at >= since,
            Sample.collected_at <= until,
        )
        .order_by(Sample.collected_at)
    )

    return SeriesRead(
        link_id=link_id,
        metric=metric,
        points=[SeriesPoint(ts=r.collected_at, value=r.value) for r in rows],
    )
