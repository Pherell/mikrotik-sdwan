"""Read netwatch and system/resource back into samples.

RouterOS is already measuring loss, jitter and RTT for every tunnel -- the
SLA profiles that drive failover depend on it. The controller wrote those
probes and never read them back; this is that read, on a schedule, kept.

One extra read per site per interval: no synthetic probes, no agent, no
additional load the device was not already carrying. That is the finding
behind docs/plan-v2.md M7 -- telemetry is normally the expensive pillar, and
here it is the cheapest, so it goes first.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.drivers.base import DriverError
from app.drivers.factory import open_driver
from app.models.enums import SiteStatus
from app.models.fabric import Link
from app.models.site import Site, Wan
from app.models.telemetry import Sample
from app.services.diagnostics import parse_duration_ms

log = logging.getLogger(__name__)

# Metric names. Kept as plain strings on the row rather than an enum column:
# a new metric is then a poller change, not a migration.
LOSS_PERCENT = "loss_percent"
RTT_AVG_MS = "rtt_avg_ms"
RTT_MIN_MS = "rtt_min_ms"
RTT_MAX_MS = "rtt_max_ms"
RTT_JITTER_MS = "rtt_jitter_ms"
CPU_PERCENT = "cpu_percent"
FREE_MEMORY_BYTES = "free_memory_bytes"

LINK_METRICS = (LOSS_PERCENT, RTT_AVG_MS, RTT_MIN_MS, RTT_MAX_MS, RTT_JITTER_MS)
SITE_METRICS = (CPU_PERCENT, FREE_MEMORY_BYTES)


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).rstrip("%"))
    except ValueError:
        return None


async def poll_site(session: AsyncSession, site: Site) -> list[Sample]:
    """One pass for one site. Returns the samples written, so callers and
    tests can assert on them without a second query.

    Never raises on a device problem -- a site the poller cannot reach right
    now is a missing point on the chart, not a reason to skip every other
    site in the sweep.
    """
    wan_ids = select(Wan.id).where(Wan.site_id == site.id)
    links = list(
        (
            await session.execute(
                select(Link)
                .where(or_(Link.a_wan_id.in_(wan_ids), Link.b_wan_id.in_(wan_ids)))
                .options(selectinload(Link.a_wan), selectinload(Link.b_wan))
            )
        )
        .scalars()
        .all()
    )

    try:
        async with open_driver(site) as driver:
            netwatch = await driver.read("/tool/netwatch")
            resource_rows = await driver.read("/system/resource")
    except DriverError as exc:
        log.warning("telemetry poll failed for %s: %s", site.name, exc)
        return []

    now = datetime.now(UTC)
    samples: list[Sample] = []

    by_host = {str(row.get("host")): row for row in netwatch}
    for link in links:
        # Same match as app.services.diagnostics.tunnel_health: netwatch
        # probes the far tunnel IP, and which side is "far" depends on which
        # end of the link this site is.
        near_is_a = link.a_wan.site_id == site.id
        far_tunnel_ip = link.b_tunnel_ip if near_is_a else link.a_tunnel_ip
        row = by_host.get(far_tunnel_ip)
        if row is None:
            continue
        values = {
            LOSS_PERCENT: _float(row.get("loss-percent")),
            RTT_AVG_MS: parse_duration_ms(row.get("rtt-avg")),
            RTT_MIN_MS: parse_duration_ms(row.get("rtt-min")),
            RTT_MAX_MS: parse_duration_ms(row.get("rtt-max")),
            RTT_JITTER_MS: parse_duration_ms(row.get("rtt-jitter")),
        }
        for metric in LINK_METRICS:
            value = values[metric]
            if value is None:
                continue
            samples.append(
                Sample(
                    tenant_id=site.tenant_id,
                    site_id=site.id,
                    link_id=link.id,
                    metric=metric,
                    value=value,
                    collected_at=now,
                )
            )

    resource = resource_rows[0] if resource_rows else {}
    site_values = {
        CPU_PERCENT: _float(resource.get("cpu-load")),
        FREE_MEMORY_BYTES: _float(resource.get("free-memory")),
    }
    for metric in SITE_METRICS:
        value = site_values[metric]
        if value is None:
            continue
        samples.append(
            Sample(
                tenant_id=site.tenant_id,
                site_id=site.id,
                link_id=None,
                metric=metric,
                value=value,
                collected_at=now,
            )
        )

    session.add_all(samples)
    await session.flush()
    return samples


async def poll_all(session: AsyncSession, tenant_id: str | None = None) -> int:
    """Every provisioned site (optionally one tenant's). Returns the total
    number of samples written, for the caller to log."""
    query = select(Site).where(Site.status.notin_([SiteStatus.unprovisioned]))
    if tenant_id is not None:
        query = query.where(Site.tenant_id == tenant_id)
    sites = await session.scalars(query)

    total = 0
    for site in sites:
        try:
            total += len(await poll_site(session, site))
        except Exception:  # pragma: no cover - one bad site must not stop the sweep
            log.exception("telemetry poll crashed for %s", site.name)
    return total
