"""Prometheus exposition: GET /metrics.

Hand-rolled text format (version 0.0.4) rather than prometheus_client: every
value here is a gauge or a count read fresh from the database at scrape time,
so there is no in-process registry to keep, and the format for that is a few
lines of string building -- not worth a dependency, or the multiprocess-mode
caveats prometheus_client brings under several uvicorn workers.

What is exported is exactly what the controller actually stores:

- ``sdwan_link_rtt_ms`` / ``_loss_percent`` / ``_jitter_ms`` -- the latest
  ``samples`` row per (site, link) for rtt_avg_ms / loss_percent /
  rtt_jitter_ms. Per *site* as well as link because both ends of a tunnel
  probe it and each end's view is its own fact.
- ``sdwan_link_up`` -- netwatch's own up/down, which samples do not keep (they
  hold numbers only); it comes from the state the alerting hooks record on
  every poll (``alert_states``). 1 up, 0 down or breaching its SLA.
- ``sdwan_device_cpu_percent`` / ``sdwan_device_free_memory_bytes`` -- latest
  site-level samples.
- ``sdwan_site_reachable`` / ``sdwan_site_drift`` -- from the site row's status.
- ``sdwan_job_total{state}`` -- every job ever recorded, by state.

Samples older than ``_freshness()`` are left out rather than exported with a
stale value: a link the poller has stopped hearing about must show as a gap
(absent series), not as its last good RTT forever.

Auth is a static bearer token, ``SDWAN_METRICS_TOKEN``. Unset means 404 -- the
route does not exist on an install that has not asked for it -- because these
series name every site and tunnel in every tenant, and Prometheus cannot do a
login flow.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import and_, func, select

from app.config import get_settings
from app.deps import SessionDep
from app.models.alert import AlertState
from app.models.base import utcnow
from app.models.enums import SiteStatus
from app.models.fabric import Link
from app.models.job import Job
from app.models.site import Site
from app.models.telemetry import Sample
from app.services.alerts import LINK_UP
from app.telemetry.poller import (
    CPU_PERCENT,
    FREE_MEMORY_BYTES,
    LOSS_PERCENT,
    RTT_AVG_MS,
    RTT_JITTER_MS,
)

router = APIRouter(tags=["meta"])

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# metric name -> (sample metric, help). Order is output order.
_LINK_GAUGES = {
    "sdwan_link_rtt_ms": (RTT_AVG_MS, "Latest netwatch average RTT to the far tunnel end, ms."),
    "sdwan_link_loss_percent": (LOSS_PERCENT, "Latest netwatch packet loss, percent."),
    "sdwan_link_jitter_ms": (RTT_JITTER_MS, "Latest netwatch RTT jitter, ms."),
}
_SITE_GAUGES = {
    "sdwan_device_cpu_percent": (CPU_PERCENT, "Latest /system/resource cpu-load, percent."),
    "sdwan_device_free_memory_bytes": (FREE_MEMORY_BYTES, "Latest free memory, bytes."),
}


def _freshness() -> timedelta:
    """Ten poll intervals, never under five minutes: long enough to ride
    out a slow pass or two, short enough that a dead poller shows."""
    return timedelta(seconds=max(300, 10 * get_settings().telemetry_poll_seconds))


def _escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**labels: object) -> str:
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items() if v is not None)
    return "{" + inner + "}" if inner else ""


def _num(value: float) -> str:
    # repr keeps full precision; Prometheus parses Python's float repr
    # including "inf"/"nan" spelled as below.
    if value != value:
        return "NaN"
    if value in (float("inf"), float("-inf")):
        return "+Inf" if value > 0 else "-Inf"
    return repr(float(value))


class _Out:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def family(self, name: str, kind: str, help_: str) -> None:
        self.lines.append(f"# HELP {name} {help_}")
        self.lines.append(f"# TYPE {name} {kind}")

    def sample(self, name: str, value: float, **labels: object) -> None:
        self.lines.append(f"{name}{_labels(**labels)} {_num(value)}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def _authorize(authorization: str | None) -> None:
    expected = get_settings().metrics_token
    if not expected:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    scheme, _, presented = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        presented.strip().encode(), expected.encode()
    ):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid metrics token",
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
async def metrics(
    session: SessionDep, authorization: str | None = Header(default=None)
) -> PlainTextResponse:
    _authorize(authorization)
    out = _Out()

    sites = {
        s.id: s
        for s in await session.scalars(
            select(Site).where(Site.status != SiteStatus.unprovisioned)
        )
    }
    link_names = dict((await session.execute(select(Link.id, Link.slug))).all())

    def site_labels(site_id: str) -> dict[str, object]:
        site = sites.get(site_id)
        return {
            "tenant": site.tenant_id if site else None,
            "site": site.name if site else None,
            "site_id": site_id,
        }

    # Latest sample per (site, link, metric), within the freshness window.
    since = utcnow() - _freshness()
    latest = (
        select(
            Sample.site_id,
            Sample.link_id,
            Sample.metric,
            func.max(Sample.collected_at).label("at"),
        )
        .where(Sample.collected_at >= since)
        .group_by(Sample.site_id, Sample.link_id, Sample.metric)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Sample.site_id, Sample.link_id, Sample.metric, Sample.value).join(
                latest,
                and_(
                    Sample.site_id == latest.c.site_id,
                    # IS, not =, so the NULL link_id of site-level rows joins.
                    Sample.link_id.is_not_distinct_from(latest.c.link_id),
                    Sample.metric == latest.c.metric,
                    Sample.collected_at == latest.c.at,
                ),
            )
        )
    ).all()
    by_metric: dict[str, dict[tuple[str, str | None], float]] = {}
    for site_id, link_id, metric, value in rows:
        if site_id in sites:
            by_metric.setdefault(metric, {})[(site_id, link_id)] = value

    # sdwan_link_up from the alerting state: subject "link:<link>@<site>".
    # Ordered oldest first into a dict, so if a race ever left two rows for
    # one subject (see AlertState's index comment) the newest wins.
    link_states = dict(
        (
            await session.execute(
                select(AlertState.subject, AlertState.state)
                .where(AlertState.subject.like("link:%"))
                .order_by(AlertState.changed_at)
            )
        ).all()
    )
    out.family("sdwan_link_up", "gauge", "Netwatch status of the tunnel: 1 up, 0 down/SLA breach.")
    for subject, state in sorted(link_states.items()):
        link_id, _, site_id = subject.removeprefix("link:").partition("@")
        if site_id not in sites:
            continue
        out.sample(
            "sdwan_link_up",
            1.0 if state == LINK_UP else 0.0,
            **site_labels(site_id),
            link=link_names.get(link_id),
            link_id=link_id,
        )

    for name, (metric, help_) in _LINK_GAUGES.items():
        out.family(name, "gauge", help_)
        for (site_id, link_id), value in sorted(by_metric.get(metric, {}).items(),
                                                key=lambda kv: (kv[0][0], kv[0][1] or "")):
            if link_id is None:
                continue
            out.sample(
                name, value, **site_labels(site_id),
                link=link_names.get(link_id), link_id=link_id,
            )

    # Two loops, each right after its own TYPE line: the exposition format
    # requires a family's samples to be contiguous and follow its header.
    out.family("sdwan_site_reachable", "gauge", "1 if the controller last reached the device.")
    for site_id, site in sorted(sites.items()):
        out.sample(
            "sdwan_site_reachable",
            0.0 if site.status == SiteStatus.unreachable else 1.0,
            **site_labels(site_id),
        )
    out.family("sdwan_site_drift", "gauge", "1 if the last drift check found drift.")
    for site_id, site in sorted(sites.items()):
        out.sample(
            "sdwan_site_drift",
            1.0 if site.status == SiteStatus.drifted else 0.0,
            **site_labels(site_id),
        )

    for name, (metric, help_) in _SITE_GAUGES.items():
        out.family(name, "gauge", help_)
        for (site_id, link_id), value in sorted(by_metric.get(metric, {}).items(),
                                                key=lambda kv: kv[0][0]):
            if link_id is None:
                out.sample(name, value, **site_labels(site_id))

    out.family("sdwan_job_total", "gauge", "Jobs recorded, by state.")
    for state, count in sorted(
        (await session.execute(select(Job.state, func.count()).group_by(Job.state))).all()
    ):
        out.sample("sdwan_job_total", float(count), state=state)

    return PlainTextResponse(out.text(), media_type=CONTENT_TYPE)
