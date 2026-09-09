"""ARQ worker: the scheduled half of the controller.

Everything here is idempotent and safe to run twice. Cron jobs on several
workers would otherwise double-apply, and a drift sweep that ran twice must not
produce two remediation jobs for the same device.
"""

from __future__ import annotations

import logging
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from app.config import get_settings
from app.db import SessionLocal
from app.services.drift import check_all
from app.telemetry.poller import poll_all

log = logging.getLogger(__name__)


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
    functions: list[Any] = [drift_sweep, telemetry_poll]
    cron_jobs = [
        # Offset off the hour so the sweep does not collide with whatever else
        # a fleet runs at :00.
        cron(drift_sweep, minute=17, run_at_startup=False),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    # A device apply that hangs must not wedge the queue. Individual jobs set
    # their own timeout; this is the backstop.
    job_timeout = 600
    max_tries = 3
    health_check_interval = 30
