"""Central SLA evaluation: windowed verdicts from samples already collected.

The on-device netwatch probes judge a path one test at a time and demote it
the moment a threshold trips. That is the right shape for failover and the
wrong shape for answering "did this path meet its SLA over the last half
hour" -- a single bad test and a sustained breach look identical in it, and
nothing was kept either way. This module is that second judgement, made
centrally from the samples app.telemetry.poller already reads: per path, per
SD-WAN group, the last window's loss / latency / jitter are reduced to
bucketed means (Cisco AAR style: mean of per-bucket means, so a burst of
samples in one minute cannot outvote the rest of the window), classified
against the group's SLA profile, and persisted as an SlaEvaluation -- which
doubles as the Fase 2b compliance record.

Scope, stated plainly because the boundary is the design:

- No actuation. This module never writes a route distance, here or anywhere
  else. Re-verified against the code before this was written: healthy and
  demoted distances exist only inside app.render.policy's privates
  (_probe_intents / _merge_probes), embedded by _health_script as absolute
  writes in the netwatch up-/down-scripts that own route distance at runtime
  (the up-script restores only after the profile's recovery_seconds
  hold-down); the /ip/route sections in app.render.policy._sections (and
  app.render.fabric) set ignore=("distance",), so the reconciler
  deliberately never manages distance either; and this evaluator runs inside
  the 30s telemetry sweep (app.tasks.worker.telemetry_poll) holding only a
  database session -- no driver session, no device write path. A controller
  distance-writer added here would fight the scripts' next event for the
  same value. Transitions are instead recorded (SlaEvaluation.previous_class)
  for whoever legitimately acts on them.
- No anti-flap. Nothing here damps or debounces a classification that
  oscillates bucket to bucket; that is plan item A3, a separate change.
- Hard-down stays on-device. A probe reporting down demotes its path via the
  netwatch script immediately; this module only records that it happened
  (classification "down") on the sweep's cadence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import SiteStatus
from app.models.fabric import Link
from app.models.policy import Policy, SdwanGroup
from app.models.site import Site, Wan
from app.models.telemetry import Sample, SlaEvaluation
from app.render.policy import DEFAULT_SLA
from app.telemetry.poller import (
    LOSS_PERCENT,
    NW_DOWN,
    NW_RESPONSE_COUNT,
    NW_SENT_COUNT,
    RTT_AVG_MS,
    RTT_JITTER_MS,
)

log = logging.getLogger(__name__)

BUCKET_MINUTES = 5
WINDOW_MINUTES = 30

IN_SLA = "in_sla"
DEGRADED = "degraded"
DOWN = "down"

# Metrics classify() reads. nw_done_tests / nw_failed_tests are stored by the
# poller but not judged here: sent/response counts already carry the loss
# signal, and done/failed add no threshold the profiles express.
_READ_METRICS = frozenset(
    {LOSS_PERCENT, RTT_AVG_MS, RTT_JITTER_MS, NW_SENT_COUNT, NW_RESPONSE_COUNT, NW_DOWN}
)


@dataclass(slots=True)
class Thresholds:
    loss_percent: float
    latency_ms: float
    jitter_ms: float | None


@dataclass(slots=True)
class WindowStats:
    loss_percent: float | None
    latency_ms: float | None
    jitter_ms: float | None
    latest_down: bool
    buckets_used: int
    samples_used: int


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes from a timezone-aware column; the
    samples this reads are written as UTC, so naive means UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _field(sample: Any, name: str) -> Any:
    return sample[name] if isinstance(sample, dict) else getattr(sample, name)


def _bucketed_mean(points: list[tuple[datetime, float]], bucket_minutes: int) -> float | None:
    """Mean of per-bucket means, or None when there are no points.

    Bucketing first is the point: the poller samples every 30s, but a device
    that was unreachable and then chatty (or a window edge) can leave one
    minute holding most of the samples. A flat mean would let that minute
    speak for the whole window; a mean of bucket means gives every bucket
    that has data one vote.
    """
    if not points:
        return None
    width = bucket_minutes * 60
    buckets: dict[int, list[float]] = {}
    for ts, value in points:
        buckets.setdefault(int(ts.timestamp() // width), []).append(value)
    means = [sum(values) / len(values) for values in buckets.values()]
    return sum(means) / len(means)


def _counter_deltas(
    sent: dict[datetime, float], resp: dict[datetime, float]
) -> tuple[float, float]:
    """Total (sent, responded) deltas across consecutive paired samples.

    The poller writes sent-count and response-count together, so samples are
    paired by identical collected_at. A pair whose delta is negative on
    either counter is a reset -- device reboot or probe re-render zeroes the
    counters -- and is skipped outright: counting it would fabricate loss
    (or negative loss) out of an accounting restart.
    """
    times = sorted(set(sent) & set(resp))
    total_sent = 0.0
    total_resp = 0.0
    for earlier, later in zip(times, times[1:], strict=False):
        d_sent = sent[later] - sent[earlier]
        d_resp = resp[later] - resp[earlier]
        if d_sent < 0 or d_resp < 0:
            continue
        total_sent += d_sent
        total_resp += d_resp
    return total_sent, total_resp


def classify(
    samples: Any,
    thresholds: Thresholds,
    *,
    now: datetime,
    window_minutes: int = WINDOW_MINUTES,
    bucket_minutes: int = BUCKET_MINUTES,
) -> tuple[WindowStats, str | None]:
    """Judge one path's samples over one window.

    ``samples`` are Sample rows (or dicts) with metric / value /
    collected_at. Returns (stats, classification); the classification is
    None when the window holds no usable signal at all, and the caller
    persists nothing then -- an absent row means "no evidence", which a
    compliance view must not read as "was fine".
    """
    now = _aware(now)
    window_start = now - timedelta(minutes=window_minutes)

    latency_pts: list[tuple[datetime, float]] = []
    jitter_pts: list[tuple[datetime, float]] = []
    loss_pts: list[tuple[datetime, float]] = []
    sent: dict[datetime, float] = {}
    resp: dict[datetime, float] = {}
    down_pts: list[tuple[datetime, float]] = []
    buckets_seen: set[int] = set()
    width = bucket_minutes * 60
    used = 0

    for sample in samples:
        metric = _field(sample, "metric")
        if metric not in _READ_METRICS:
            continue
        ts = _aware(_field(sample, "collected_at"))
        if ts < window_start or ts > now:
            continue
        value = float(_field(sample, "value"))
        used += 1
        buckets_seen.add(int(ts.timestamp() // width))
        if metric == RTT_AVG_MS:
            latency_pts.append((ts, value))
        elif metric == RTT_JITTER_MS:
            jitter_pts.append((ts, value))
        elif metric == LOSS_PERCENT:
            loss_pts.append((ts, value))
        elif metric == NW_SENT_COUNT:
            sent[ts] = value
        elif metric == NW_RESPONSE_COUNT:
            resp[ts] = value
        elif metric == NW_DOWN:
            down_pts.append((ts, value))

    latency = _bucketed_mean(latency_pts, bucket_minutes)
    jitter = _bucketed_mean(jitter_pts, bucket_minutes)

    # Loss, preferred: counter deltas -- what actually left vs what came
    # back across the whole window. The instantaneous loss_percent samples
    # are the fallback for windows where the counters cannot speak (probe
    # freshly rendered, every pair straddling a reset, counters absent).
    d_sent, d_resp = _counter_deltas(sent, resp)
    if d_sent > 0:
        loss = 100.0 * (1.0 - d_resp / d_sent)
    else:
        loss = _bucketed_mean(loss_pts, bucket_minutes)

    latest_down = bool(down_pts) and max(down_pts)[1] == 1.0
    is_down = latest_down or (d_sent > 0 and d_resp == 0)

    stats = WindowStats(
        loss_percent=loss,
        latency_ms=latency,
        jitter_ms=jitter,
        latest_down=latest_down,
        buckets_used=len(buckets_seen),
        samples_used=used,
    )

    if is_down:
        # Down regardless of the means: a path answering nothing has not
        # got a latency worth averaging, and its counters already said so.
        return stats, DOWN
    if loss is None and latency is None and jitter is None:
        return stats, None
    # Strictly greater breaches. Equality is in SLA, matching RouterOS's
    # thr-* semantics (fail *above*) that the on-device probes already use.
    if loss is not None and loss > thresholds.loss_percent:
        return stats, DEGRADED
    if latency is not None and latency > thresholds.latency_ms:
        return stats, DEGRADED
    if (
        jitter is not None
        and thresholds.jitter_ms is not None
        and jitter > thresholds.jitter_ms
    ):
        return stats, DEGRADED
    return stats, IN_SLA


# -- DB-facing ---------------------------------------------------------------


def _bucket_start(now: datetime) -> datetime:
    width = BUCKET_MINUTES * 60
    floored = int(now.timestamp() // width) * width
    return datetime.fromtimestamp(floored, tz=UTC)


def _group_for(policy: Policy) -> SdwanGroup | None:
    return policy.sdwan_group


async def evaluate_site(
    session: AsyncSession, site: Site, *, now: datetime | None = None
) -> list[SlaEvaluation]:
    """Evaluate every link touching one site, once, for the current bucket.

    Resolution mirrors app.services.fabric.policy_view: the site's enabled
    tenant policies, each contributing its group; a group covers a link when
    one of its member uplinks names the link's near-end WAN by tag key or by
    WAN name. A link no group covers is judged once against DEFAULT_SLA --
    uncovered must not mean unrecorded, or the compliance record would have
    holes exactly where nobody configured steering.
    """
    now = _aware(now) if now is not None else datetime.now(UTC)
    bucket_start = _bucket_start(now)

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
    if not links:
        return []

    policies = [
        p
        for p in await session.scalars(
            select(Policy).where(
                Policy.tenant_id == site.tenant_id, Policy.enabled.is_(True)
            )
        )
        if not p.site_ids or site.id in p.site_ids
    ]

    # (link, group-or-None) -> (thresholds, window_minutes, profile_id).
    # Groups deduplicated by id: two policies pointing at one group are one
    # health standard for the path, not two verdicts.
    jobs: dict[tuple[str, str | None], tuple[Thresholds, int, str | None]] = {}
    covered: set[str] = set()
    groups: dict[str, SdwanGroup] = {}
    for policy in policies:
        group = _group_for(policy)
        if group is not None:
            groups[group.id] = group
    for link in links:
        near_wan = link.a_wan if link.a_wan.site_id == site.id else link.b_wan
        names = {*(near_wan.tags or {}).keys(), near_wan.name}
        for group in groups.values():
            members = {
                str(m["uplink"])
                for m in (group.members or [])
                if isinstance(m, dict) and m.get("uplink")
            }
            if not (members & names):
                continue
            covered.add(link.id)
            profile = group.sla_profile or DEFAULT_SLA
            is_default = group.sla_profile is None
            jobs[(link.id, group.id)] = (
                Thresholds(
                    loss_percent=float(profile.loss_percent),
                    latency_ms=float(profile.latency_ms),
                    jitter_ms=(
                        float(profile.jitter_ms)
                        if profile.jitter_ms is not None
                        else None
                    ),
                ),
                getattr(profile, "eval_window_minutes", None) or WINDOW_MINUTES,
                None if is_default else profile.id,
            )
    for link in links:
        if link.id not in covered:
            jobs[(link.id, None)] = (
                Thresholds(
                    loss_percent=float(DEFAULT_SLA.loss_percent),
                    latency_ms=float(DEFAULT_SLA.latency_ms),
                    jitter_ms=(
                        float(DEFAULT_SLA.jitter_ms)
                        if DEFAULT_SLA.jitter_ms is not None
                        else None
                    ),
                ),
                WINDOW_MINUTES,
                None,
            )
    if not jobs:
        return []

    max_window = max(window for _, window, _ in jobs.values())
    rows = await session.scalars(
        select(Sample).where(
            Sample.site_id == site.id,
            Sample.link_id.in_([link.id for link in links]),
            Sample.collected_at >= now - timedelta(minutes=max_window),
        )
    )
    by_link: dict[str, list[Sample]] = {}
    for sample in rows:
        if sample.link_id is not None:
            by_link.setdefault(sample.link_id, []).append(sample)

    results: list[SlaEvaluation] = []
    for (link_id, group_id), (thresholds, window_minutes, profile_id) in jobs.items():
        stats, classification = classify(
            by_link.get(link_id, []),
            thresholds,
            now=now,
            window_minutes=window_minutes,
        )
        if classification is None:
            continue

        existing_query = select(SlaEvaluation).where(
            SlaEvaluation.site_id == site.id,
            SlaEvaluation.link_id == link_id,
            SlaEvaluation.bucket_start == bucket_start,
        )
        existing_query = (
            existing_query.where(SlaEvaluation.sdwan_group_id == group_id)
            if group_id is not None
            else existing_query.where(SlaEvaluation.sdwan_group_id.is_(None))
        )
        existing = await session.scalar(existing_query)

        if existing is not None:
            existing.loss_percent = stats.loss_percent
            existing.latency_ms = stats.latency_ms
            existing.jitter_ms = stats.jitter_ms
            existing.classification = classification
            existing.sla_profile_id = profile_id
            existing.window_start = now - timedelta(minutes=window_minutes)
            existing.window_end = now
            existing.window_minutes = window_minutes
            existing.evaluated_at = now
            results.append(existing)
            continue

        previous_query = (
            select(SlaEvaluation.classification)
            .where(
                SlaEvaluation.site_id == site.id,
                SlaEvaluation.link_id == link_id,
                SlaEvaluation.bucket_start < bucket_start,
            )
            .order_by(SlaEvaluation.bucket_start.desc())
            .limit(1)
        )
        previous_query = (
            previous_query.where(SlaEvaluation.sdwan_group_id == group_id)
            if group_id is not None
            else previous_query.where(SlaEvaluation.sdwan_group_id.is_(None))
        )
        previous_class = await session.scalar(previous_query)

        row = SlaEvaluation(
            tenant_id=site.tenant_id,
            site_id=site.id,
            link_id=link_id,
            sdwan_group_id=group_id,
            sla_profile_id=profile_id,
            bucket_start=bucket_start,
            window_start=now - timedelta(minutes=window_minutes),
            window_end=now,
            window_minutes=window_minutes,
            loss_percent=stats.loss_percent,
            latency_ms=stats.latency_ms,
            jitter_ms=stats.jitter_ms,
            classification=classification,
            previous_class=previous_class,
            evaluated_at=now,
        )
        session.add(row)
        results.append(row)

    await session.flush()
    return results


async def evaluate_all(session: AsyncSession, tenant_id: str | None = None) -> int:
    """Every provisioned site, same sweep shape as poll_all. Returns the
    number of evaluations written or updated. One site's bad data must not
    stop the sweep, so per-site failures are logged and isolated here."""
    query = select(Site).where(Site.status.notin_([SiteStatus.unprovisioned]))
    if tenant_id is not None:
        query = query.where(Site.tenant_id == tenant_id)
    sites = await session.scalars(query)

    total = 0
    for site in sites:
        try:
            total += len(await evaluate_site(session, site))
        except Exception:  # pragma: no cover - one bad site must not stop the sweep
            log.exception("SLA evaluation crashed for %s", site.name)
    return total
