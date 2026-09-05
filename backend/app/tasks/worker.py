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


async def startup(ctx: dict[str, Any]) -> None:
    log.info("sdwan worker starting")


async def shutdown(ctx: dict[str, Any]) -> None:
    from app.db import engine

    await engine.dispose()
    log.info("sdwan worker stopped")


class WorkerSettings:
    functions: list[Any] = [drift_sweep]
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
