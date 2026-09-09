"""ARQ worker: the scheduled half of the controller.

Everything here is idempotent and safe to run twice. Cron jobs on several
workers would otherwise double-apply, and a drift sweep that ran twice must not
produce two remediation jobs for the same device.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models.base import utcnow
from app.models.enums import JobKind, JobState
from app.models.job import Job
from app.models.site import Site
from app.services.drift import check_all
from app.services.reconcile import apply_site
from app.telemetry.poller import poll_all

log = logging.getLogger(__name__)


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes even from a timezone-aware column
    -- the same problem deps._aware and services.enrollment._aware exist
    for, on different tables. Comparing one of those to an aware utcnow()
    raises TypeError rather than quietly comparing wrong, which is at least
    loud, but a maintenance-window sweep that crashes on its first due row
    never gets to the rest of them either."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def drift_sweep(ctx: dict[str, Any]) -> dict[str, int]:
    """Diff every provisioned site against intent.

    Sites configured for ``auto-remediate`` are re-applied; the rest are flagged
    for a human. Hourly by default -- often enough to catch a change the same
    day, rare enough that a fleet of a few hundred devices is not being polled
    constantly.
    """
    async with SessionLocal() as session:
        jobs = await check_all(session)
        await session.commit()

    drifted = sum(1 for j in jobs if (j.result or {}).get("drifted"))
    log.info("drift sweep: %d checked, %d drifted", len(jobs), drifted)
    return {"checked": len(jobs), "drifted": drifted}


async def run_scheduled_applies(ctx: dict[str, Any]) -> dict[str, int]:
    """Push every apply whose maintenance window has opened.

    Runs the existing Job row through the same services.reconcile.apply_site
    the interactive endpoint uses -- a scheduled apply is not a different
    kind of apply, only a different moment to start one. A window that
    closed before this sweep reached it is marked failed rather than pushed
    late: running a change outside the hours it was approved for is the one
    thing a maintenance window exists to prevent, and running late without
    saying so would be worse than not running at all.
    """
    now = utcnow()
    pushed = 0
    missed = 0

    async with SessionLocal() as session:
        due = await session.scalars(
            select(Job).where(
                Job.kind == JobKind.apply,
                Job.state == JobState.queued,
                Job.scheduled_for.isnot(None),
                Job.scheduled_for <= now,
            )
        )
        for job in due:
            if job.window_closes_at is not None and _aware(job.window_closes_at) < now:
                job.state = JobState.failed
                job.error = (
                    f"Missed its maintenance window (closed at "
                    f"{job.window_closes_at.isoformat()}); reschedule if the "
                    "change is still wanted."
                )
                job.finished_at = now
                missed += 1
                log.warning("job %s missed its maintenance window", job.id)
                continue

            site = await session.get(Site, job.site_id) if job.site_id else None
            if site is None:
                job.state = JobState.failed
                job.error = "Site no longer exists"
                job.finished_at = now
                missed += 1
                continue

            await apply_site(session, site, job)
            pushed += 1

        await session.commit()

    if pushed or missed:
        log.info("scheduled applies: %d pushed, %d missed their window", pushed, missed)
    return {"pushed": pushed, "missed": missed}


async def telemetry_poll(ctx: dict[str, Any]) -> dict[str, int]:
    """One netwatch + system/resource pass over every provisioned site.

    Re-enqueues itself rather than using ``cron``: arq's cron is minute-
    granular, and the interval here is 30s by default
    (``SDWAN_TELEMETRY_POLL_SECONDS``). Re-enqueuing from inside the job --
    rather than a fixed-rate scheduler -- means a slow poll pushes the next
    one back instead of overlapping it, which matters once a fleet is large
    enough that one pass can take a real fraction of the interval.
    """
    settings = get_settings()
    async with SessionLocal() as session:
        written = await poll_all(session)
        await session.commit()

    await ctx["redis"].enqueue_job(
        "telemetry_poll", _defer_by=settings.telemetry_poll_seconds
    )
    return {"samples": written}


async def startup(ctx: dict[str, Any]) -> None:
    log.info("sdwan worker starting")
    await ctx["redis"].enqueue_job("telemetry_poll")


async def shutdown(ctx: dict[str, Any]) -> None:
    from app.db import engine

    await engine.dispose()
    log.info("sdwan worker stopped")


class WorkerSettings:
    functions: list[Any] = [drift_sweep, telemetry_poll, run_scheduled_applies]
    cron_jobs = [
        # Offset off the hour so the sweep does not collide with whatever else
        # a fleet runs at :00.
        cron(drift_sweep, minute=17, run_at_startup=False),
        # Every minute: a maintenance window is approved to the minute, not
        # the half-hour, and this is cheap -- one indexed query when nothing
        # is due, which is nearly always.
        cron(run_scheduled_applies, second=0, run_at_startup=True),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    # A device apply that hangs must not wedge the queue. Individual jobs set
    # their own timeout; this is the backstop.
    job_timeout = 600
    max_tries = 3
    health_check_interval = 30
