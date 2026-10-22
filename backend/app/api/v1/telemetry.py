"""Telemetry series: what a link's netwatch probe has measured over time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.deps import RequireViewer, SessionDep
from app.models.fabric import Fabric, Link
from app.models.site import Site, Wan
from app.models.telemetry import Sample, SlaEvaluation
from app.schemas.telemetry import (
    SeriesPoint,
    SeriesRead,
    SlaClassCounts,
    SlaClassShares,
    SlaComplianceRead,
    SlaComplianceTotals,
    SlaEvaluationListRead,
    SlaEvaluationRead,
    SlaLinkCompliance,
    SlaSiteCompliance,
)
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


@router.get("/{link_id}/evaluations", response_model=SlaEvaluationListRead)
async def link_evaluations(
    link_id: str,
    session: SessionDep,
    user: RequireViewer,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> SlaEvaluationListRead:
    """The central evaluator's windowed verdicts for one link, newest first.

    Same tenant-ownership check as link_series: a link is scoped through its
    fabric, and another tenant's link is a 404, not an empty list.
    """
    owns = await session.scalar(
        select(Link.id)
        .join(Fabric, Link.fabric_id == Fabric.id)
        .where(Link.id == link_id, Fabric.tenant_id == user.tenant_id)
    )
    if owns is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such link")

    until = until or datetime.now(UTC)
    since = since or (until - timedelta(hours=24))

    rows = await session.scalars(
        select(SlaEvaluation)
        .where(
            SlaEvaluation.link_id == link_id,
            SlaEvaluation.bucket_start >= since,
            SlaEvaluation.bucket_start <= until,
        )
        .order_by(SlaEvaluation.bucket_start.desc(), SlaEvaluation.evaluated_at.desc())
        .limit(limit)
    )

    return SlaEvaluationListRead(
        link_id=link_id,
        evaluations=[SlaEvaluationRead.model_validate(r) for r in rows],
    )


compliance_router = APIRouter(tags=["telemetry"])

_WINDOW_HOURS: dict[str, int] = {"24h": 24, "7d": 168, "30d": 720}
_CLASS_RANK: dict[str, int] = {"in_sla": 0, "degraded": 1, "down": 2}


def _counts(rows: list) -> SlaClassCounts:  # type: ignore[type-arg]
    return SlaClassCounts(
        in_sla=sum(1 for r in rows if r.classification == "in_sla"),
        degraded=sum(1 for r in rows if r.classification == "degraded"),
        down=sum(1 for r in rows if r.classification == "down"),
    )


def _shares(counts: SlaClassCounts, buckets: int) -> SlaClassShares:
    if buckets == 0:
        return SlaClassShares(in_sla=None, degraded=None, down=None)
    return SlaClassShares(
        in_sla=counts.in_sla / buckets,
        degraded=counts.degraded / buckets,
        down=counts.down / buckets,
    )


def _violation_episodes(rows: list) -> int:  # type: ignore[type-arg]
    return sum(
        1
        for r in rows
        if r.classification != "in_sla"
        and (r.previous_class is None or r.previous_class == "in_sla")
    )


def _current_class(rows: list):  # type: ignore[type-arg]
    if not rows:
        return None, None
    max_bucket = max(r.bucket_start for r in rows)
    at_max = [r for r in rows if r.bucket_start == max_bucket]
    worst = max(at_max, key=lambda r: _CLASS_RANK.get(r.classification, -1))
    return worst.classification, max_bucket


@compliance_router.get("/sla-compliance", response_model=SlaComplianceRead)
async def sla_compliance(
    session: SessionDep,
    user: RequireViewer,
    window: Literal["24h", "7d", "30d"] = "7d",
) -> SlaComplianceRead:
    window_hours = _WINDOW_HOURS[window]
    until = datetime.now(UTC)
    since = until - timedelta(hours=window_hours)

    # Server-side only, over persisted SlaEvaluation rows; no device/driver calls.
    # Select only the needed columns.
    result = await session.execute(
        select(
            SlaEvaluation.site_id,
            SlaEvaluation.link_id,
            SlaEvaluation.sdwan_group_id,
            SlaEvaluation.bucket_start,
            SlaEvaluation.classification,
            SlaEvaluation.previous_class,
            SlaEvaluation.evaluated_at,
        )
        .where(
            SlaEvaluation.tenant_id == user.tenant_id,
            SlaEvaluation.bucket_start >= since,
            SlaEvaluation.bucket_start <= until,
        )
        .order_by(SlaEvaluation.bucket_start, SlaEvaluation.evaluated_at)
    )
    eval_rows = list(result.all())

    # Totals
    total_buckets = len(eval_rows)
    total_counts = _counts(eval_rows)
    total_shares = _shares(total_counts, total_buckets)
    violation_buckets = total_counts.degraded + total_counts.down
    violation_episodes_total = _violation_episodes(eval_rows)
    paths_with_data = len(
        {(r.site_id, r.link_id, r.sdwan_group_id) for r in eval_rows}
    )

    # Links: every link belonging to a fabric of the user's tenant.
    links = list(
        await session.scalars(
            select(Link)
            .join(Fabric, Link.fabric_id == Fabric.id)
            .where(Fabric.tenant_id == user.tenant_id)
            .options(
                selectinload(Link.fabric),
                selectinload(Link.a_wan).selectinload(Wan.site),
                selectinload(Link.b_wan).selectinload(Wan.site),
            )
        )
    )

    # Per-link rollup groups rows by link_id. Rows may span multiple
    # sites/groups -- that is the evaluator's path granularity; a link
    # rollup therefore aggregates all paths that use that link.
    rows_by_link: dict[str, list] = {}  # type: ignore[type-arg]
    for r in eval_rows:
        rows_by_link.setdefault(r.link_id, []).append(r)

    link_entries: list[SlaLinkCompliance] = []
    for link in links:
        rows = rows_by_link.get(link.id, [])
        buckets = len(rows)
        counts = _counts(rows)
        shares = _shares(counts, buckets)
        episodes = _violation_episodes(rows)
        current_class, last_bucket = _current_class(rows)
        a_wan = link.a_wan
        b_wan = link.b_wan
        a_site_name = a_wan.site.name if a_wan is not None and a_wan.site is not None else ""
        b_site_name = b_wan.site.name if b_wan is not None and b_wan.site is not None else ""
        a_wan_name = a_wan.name if a_wan is not None else ""
        b_wan_name = b_wan.name if b_wan is not None else ""
        fabric_name = link.fabric.name if link.fabric is not None else ""
        link_entries.append(
            SlaLinkCompliance(
                link_id=link.id,
                slug=link.slug,
                fabric_id=link.fabric_id,
                fabric_name=fabric_name,
                a_site_name=a_site_name,
                a_wan_name=a_wan_name,
                b_site_name=b_site_name,
                b_wan_name=b_wan_name,
                has_data=buckets > 0,
                buckets=buckets,
                counts=counts,
                shares=shares,
                violation_episodes=episodes,
                current_class=current_class,
                last_bucket_start=last_bucket,
            )
        )

    links_total = len(links)
    links_with_data = sum(1 for e in link_entries if e.has_data)

    # Sites: every tenant site that is an endpoint of a tenant link OR
    # has selected evaluation rows.
    endpoint_site_ids: set[str] = set()
    for link in links:
        if link.a_wan is not None:
            endpoint_site_ids.add(link.a_wan.site_id)
        if link.b_wan is not None:
            endpoint_site_ids.add(link.b_wan.site_id)
    eval_site_ids = {r.site_id for r in eval_rows}
    all_site_ids = endpoint_site_ids | eval_site_ids

    sites_by_id: dict[str, Site] = {}
    if all_site_ids:
        site_rows = list(
            await session.scalars(
                select(Site).where(
                    Site.id.in_(all_site_ids), Site.tenant_id == user.tenant_id
                )
            )
        )
        sites_by_id = {s.id: s for s in site_rows}
        # Fallback: an evaluation site that is not a tenant site row (should
        # not happen with consistent data) is still surfaced by id so its
        # rows are not silently dropped.
        missing_ids = all_site_ids - set(sites_by_id.keys())
        # Only include missing ids that came from evaluations, and only if
        # a Site row exists at all; otherwise skip (no name available).
        if missing_ids:
            extra = list(
                await session.scalars(select(Site).where(Site.id.in_(missing_ids)))
            )
            for s in extra:
                if s.id in eval_site_ids:
                    sites_by_id[s.id] = s

    rows_by_site: dict[str, list] = {}  # type: ignore[type-arg]
    for r in eval_rows:
        rows_by_site.setdefault(r.site_id, []).append(r)

    site_entries: list[SlaSiteCompliance] = []
    for site_id, site in sites_by_id.items():
        rows = rows_by_site.get(site_id, [])
        buckets = len(rows)
        counts = _counts(rows)
        shares = _shares(counts, buckets)
        episodes = _violation_episodes(rows)
        current_class, last_bucket = _current_class(rows)
        site_entries.append(
            SlaSiteCompliance(
                site_id=site.id,
                site_name=site.name,
                has_data=buckets > 0,
                buckets=buckets,
                counts=counts,
                shares=shares,
                violation_episodes=episodes,
                current_class=current_class,
                last_bucket_start=last_bucket,
            )
        )

    # Sort worst offenders first: violation_episodes desc, down count desc,
    # then name/slug asc.
    link_entries.sort(
        key=lambda e: (-e.violation_episodes, -e.counts.down, e.slug)
    )
    site_entries.sort(
        key=lambda e: (-e.violation_episodes, -e.counts.down, e.site_name)
    )

    totals = SlaComplianceTotals(
        buckets=total_buckets,
        counts=total_counts,
        shares=total_shares,
        violation_buckets=violation_buckets,
        violation_episodes=violation_episodes_total,
        links_total=links_total,
        links_with_data=links_with_data,
        paths_with_data=paths_with_data,
    )

    return SlaComplianceRead(
        window=window,
        window_hours=window_hours,
        since=since,
        until=until,
        totals=totals,
        sites=site_entries,
        links=link_entries,
    )
