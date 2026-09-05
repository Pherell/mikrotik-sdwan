"""Orchestrates plan / apply / drift for a site, and records the Job row."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.drivers.base import DeviceDriver, DriverError
from app.drivers.factory import open_driver
from app.models.enums import JobKind, JobState, SiteStatus
from app.models.job import Job
from app.models.site import Site
from app.reconcile.apply import ApplyOutcome, safe_apply
from app.reconcile.plan import Plan, build_plan
from app.services.fabric import render_device

log = logging.getLogger(__name__)


async def plan_site(
    session: AsyncSession, site: Site, driver: DeviceDriver | None = None
) -> Plan:
    """Render intent and diff it against the device. Read-only."""
    sections = await render_device(session, site)
    if driver is not None:
        return await build_plan(driver, sections)
    async with open_driver(site) as d:
        return await build_plan(d, sections)


async def apply_site(
    session: AsyncSession,
    site: Site,
    job: Job,
    *,
    dry_run: bool = False,
) -> Job:
    """Plan, then push inside the dead-man rollback, recording everything.

    The Job row is the audit record and the UI's job log, so it is updated on
    every path out of here -- including the ones that fail.
    """
    settings = get_settings()
    timeout = site.rollback_timeout_seconds or settings.rollback_timeout_seconds

    job.state = JobState.running
    job.started_at = datetime.now(UTC)
    job.attempts += 1
    await session.flush()

    try:
        async with open_driver(site) as driver:
            plan = await build_plan(driver, await render_device(session, site))
            job.plan = plan.to_json()
            job.diff = {"text": plan.render()}

            if plan.unreadable:
                raise DriverError(
                    "Refusing to apply: could not read "
                    + ", ".join(plan.unreadable)
                    + ". Applying now would look like a request to delete "
                    "everything managed in those menus."
                )

            if dry_run or plan.empty:
                job.state = JobState.succeeded
                job.result = {
                    "dry_run": dry_run,
                    "changes": plan.counts,
                    "applied": 0,
                    "message": "no changes" if plan.empty else "dry run, nothing pushed",
                }
                return await _finish(session, job)

            outcome = await safe_apply(
                driver, plan.ops(), job_id=job.id, timeout_seconds=timeout
            )
            _record(job, outcome, plan)

            if outcome.ok:
                site.status = SiteStatus.reachable
                site.last_error = None
                site.last_seen_at = datetime.now(UTC).isoformat()
            else:
                site.status = SiteStatus.error
                site.last_error = outcome.error

    except DriverError as exc:
        job.state = JobState.failed
        job.error = str(exc)
        site.status = SiteStatus.unreachable
        site.last_error = str(exc)
    except Exception as exc:  # pragma: no cover - unexpected
        log.exception("apply job %s crashed", job.id)
        job.state = JobState.failed
        job.error = f"{type(exc).__name__}: {exc}"

    return await _finish(session, job)


def _record(job: Job, outcome: ApplyOutcome, plan: Plan) -> None:
    job.backup_name = outcome.backup_name
    job.rollback_token = outcome.rollback_token if outcome.rollback_armed else None
    job.log = "\n".join(outcome.log)
    job.result = {
        "applied": outcome.applied,
        "planned": sum(plan.counts.values()),
        "changes": plan.counts,
        "rollback_armed": outcome.rollback_armed,
        "backup": outcome.backup_name,
    }
    if outcome.ok:
        job.state = JobState.succeeded
        return

    job.error = outcome.error
    # An armed rollback means the router is restoring itself; that is a distinct
    # outcome from a plain failure and the UI colours it differently.
    job.state = JobState.rolled_back if outcome.rollback_armed else JobState.failed


async def _finish(session: AsyncSession, job: Job) -> Job:
    job.finished_at = datetime.now(UTC)
    await session.flush()
    return job


def new_job(site: Site, kind: JobKind, user_id: str | None) -> Job:
    return Job(
        kind=kind,
        state=JobState.queued,
        site_id=site.id,
        tenant_id=site.tenant_id,
        requested_by=user_id,
    )
