"""Drift detection.

A device can stop matching intent without the controller doing anything: someone
logs in and edits, a reboot loses an unsaved change, a firmware upgrade rewrites
a menu. Drift detection is the same diff the planner runs, on a schedule, with
the result recorded rather than applied.

Two modes per site:

``alert`` (default)
    Record the drift, mark the site, and leave it alone. Someone decides.

``auto-remediate``
    Re-apply immediately. Correct for a fleet nobody logs into by hand, and
    actively hostile on one where engineers do -- it silently reverts their work
    mid-troubleshooting.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.drivers.base import DriverError
from app.drivers.factory import open_driver
from app.models.enums import JobKind, JobState, SiteStatus
from app.models.job import Job
from app.models.site import Site
from app.reconcile.plan import build_plan
from app.services.fabric import render_device
from app.services.reconcile import apply_site, new_job

log = logging.getLogger(__name__)


async def check_site(session: AsyncSession, site: Site) -> Job:
    """Diff intent against the device and record what was found."""
    job = new_job(site, JobKind.drift_check, None)
    session.add(job)
    job.state = JobState.running
    job.started_at = datetime.now(UTC)
    await session.flush()

    try:
        sections = await render_device(session, site)
        async with open_driver(site) as driver:
            plan = await build_plan(driver, sections)
    except DriverError as exc:
        job.state = JobState.failed
        job.error = str(exc)
        job.finished_at = datetime.now(UTC)
        site.status = SiteStatus.unreachable
        site.last_error = str(exc)
        await session.flush()
        return job

    job.plan = plan.to_json()
    job.diff = {"text": plan.render()}
    job.finished_at = datetime.now(UTC)
    site.last_seen_at = datetime.now(UTC).isoformat()

    if plan.empty:
        job.state = JobState.succeeded
        job.result = {"drifted": False, "changes": plan.counts}
        # Only clear a drift flag; never overwrite an error someone should see.
        # The message has to go with the status: leaving it behind tells the
        # operator the site is still drifted when the diff says it is not.
        if site.status == SiteStatus.drifted:
            site.status = SiteStatus.reachable
            site.last_error = None
        await session.flush()
        return job

    job.state = JobState.succeeded
    job.result = {"drifted": True, "changes": plan.counts, "action": site.drift_action}
    site.status = SiteStatus.drifted
    site.last_error = (
        f"Drifted from intent: {plan.counts['add']} missing, "
        f"{plan.counts['set']} changed, {plan.counts['remove']} unexpected."
    )
    await session.flush()

    if site.drift_action == "auto-remediate":
        log.info("site %s drifted; auto-remediating", site.name)
        remediation = new_job(site, JobKind.apply, None)
        session.add(remediation)
        await session.flush()
        await apply_site(session, site, remediation)
        job.result["remediation_job"] = remediation.id

    return job


async def check_all(session: AsyncSession, tenant_id: str = "default") -> list[Job]:
    """Sweep every provisioned site. Sites that have never been applied are
    skipped -- everything is 'missing' on those, which is not drift."""
    sites = await session.scalars(
        select(Site).where(
            Site.tenant_id == tenant_id,
            Site.status.notin_([SiteStatus.unprovisioned]),
        )
    )
    jobs: list[Job] = []
    for site in sites:
        try:
            jobs.append(await check_site(session, site))
        except Exception:  # pragma: no cover - one bad site must not stop the sweep
            log.exception("drift check failed for %s", site.name)
    return jobs
