"""Taking config *off* a router: detach, fabric delete, decommission.

The controller's model of a device is "what intent renders, diffed against what
the device holds". Every way a site leaves that model used to be a database
operation only:

- removing a member, or deleting a fabric, dropped the links from the
  database and left their tunnels, BGP sessions, mangle rules and netwatch
  probes running on every router involved until somebody happened to apply
  each of them again;
- deleting a site dropped the only record that the device had ever been
  touched, so everything tagged ``sdwan:`` on it stayed there for good, with
  nothing left that knew to remove it.

Detach and fabric delete now *queue* an apply for every router whose rendered
config changed -- the departing site and its former peers. Queued, not inline:
a fabric delete can touch every router in it, and an HTTP request is the wrong
place to hold N device locks. The jobs go through the existing scheduled-apply
path (worker.run_scheduled_applies, once a minute), so they carry the same
dead-man rollback, the same one-apply-per-site lock, and show up in the job log
like any other apply.

Decommission is inline, for one site, and is what delete runs first when asked
to: render *nothing* for the device and apply that. Because every managed menu
is always rendered (see services.fabric._cleanup_sections) and merging widens
each menu's ownership tag to ``sdwan:``, an all-empty render is precisely
"remove every row this controller owns here, and nothing else". The account and
certificate enrollment created are deliberately left: they are how the
controller reaches the device, and removing them mid-apply would trip the
rollback.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.drivers.base import ConfigSection, DriverError
from app.drivers.factory import open_driver
from app.models.enums import JobKind, JobState, SiteStatus
from app.models.job import Job
from app.models.site import Site
from app.reconcile.apply import safe_apply
from app.reconcile.plan import Plan, build_plan
from app.services.fabric import render_device
from app.services.reconcile import new_job

log = logging.getLogger(__name__)


async def queue_cleanup(
    session: AsyncSession, sites: list[Site], *, requested_by: str | None
) -> list[str]:
    """Queue an immediate apply for each site; return the job ids.

    A site that already has an apply queued or running is skipped rather than
    refused: that apply renders current intent too, so it converges the device
    just the same, and a 409 here would make detach fail for a reason that has
    nothing to do with it.
    """
    now = datetime.now(UTC)
    queued: list[str] = []
    seen: set[str] = set()
    for site in sites:
        if site.id in seen:
            continue
        seen.add(site.id)
        busy = await session.scalar(
            select(Job.id).where(
                Job.site_id == site.id,
                Job.kind == JobKind.apply,
                Job.state.in_([JobState.queued, JobState.running]),
            )
        )
        if busy is not None:
            continue
        job = new_job(site, JobKind.apply, requested_by)
        # scheduled_for=now is what run_scheduled_applies picks up; it is the
        # same row an operator-scheduled apply would be, just due at once.
        job.scheduled_for = now
        session.add(job)
        await session.flush()
        queued.append(job.id)
    return queued


def strip(sections: list[ConfigSection]) -> list[ConfigSection]:
    """The same sections with nothing in them: a sweep of everything owned."""
    for section in sections:
        section.items = []
    return sections


async def plan_decommission(session: AsyncSession, site: Site) -> Plan:
    sections = strip(await render_device(session, site))
    async with open_driver(site) as driver:
        return await build_plan(driver, sections)


async def decommission_site(
    session: AsyncSession, site: Site, *, requested_by: str | None, dry_run: bool = False
) -> Job:
    """Remove every controller-owned row from the device, inside the rollback.

    Refuses a site still in a fabric: tearing its tunnels down while intent
    still says they exist would read as drift on every peer, and the next
    apply would just build them again.
    """
    job = new_job(site, JobKind.apply, requested_by)
    session.add(job)
    await session.flush()
    job.state = JobState.running
    job.started_at = datetime.now(UTC)
    job.attempts = (job.attempts or 0) + 1
    await session.flush()

    if site.memberships:
        job.state = JobState.failed
        job.error = "Remove this site from its fabrics before decommissioning it"
        job.finished_at = datetime.now(UTC)
        await session.flush()
        return job

    try:
        sections = strip(await render_device(session, site))
        async with open_driver(site) as driver:
            plan = await build_plan(driver, sections)
            job.plan = plan.to_json()
            job.diff = {"text": plan.render()}
            if plan.unreadable:
                raise DriverError(
                    "Refusing to decommission: could not read "
                    + ", ".join(plan.unreadable)
                    + ". Rows in those menus would be left behind unnoticed."
                )
            if dry_run or plan.empty:
                job.state = JobState.succeeded
                job.result = {
                    "decommission": True,
                    "dry_run": dry_run,
                    "changes": plan.counts,
                    "applied": 0,
                }
            else:
                outcome = await safe_apply(
                    driver, plan.ops(), job_id=job.id,
                    timeout_seconds=site.rollback_timeout_seconds or 120,
                )
                job.backup_name = outcome.backup_name
                job.log = "\n".join(outcome.log)
                job.result = {
                    "decommission": True,
                    "applied": outcome.applied,
                    "changes": plan.counts,
                    "backup": outcome.backup_name,
                }
                if outcome.ok:
                    job.state = JobState.succeeded
                    site.status = SiteStatus.unprovisioned
                    site.last_error = None
                else:
                    job.error = outcome.error
                    job.state = (
                        JobState.rolled_back if outcome.rollback_armed else JobState.failed
                    )
    except DriverError as exc:
        job.state = JobState.failed
        job.error = str(exc)
    except Exception as exc:  # pragma: no cover - defensive, see services.drift
        log.exception("decommission of %s crashed", site.name)
        job.state = JobState.failed
        job.error = f"{type(exc).__name__}: {exc}"

    job.finished_at = datetime.now(UTC)
    await session.flush()
    return job
