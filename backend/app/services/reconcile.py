"""Orchestrates plan / apply / drift for a site, and records the Job row."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.drivers.base import ConfigSection, DeviceDriver, DriverError
from app.drivers.factory import open_driver
from app.drivers.redact import redact_props
from app.models.enums import JobKind, JobState, SiteStatus
from app.models.job import Job
from app.models.site import Site
from app.reconcile.apply import ApplyOutcome, safe_apply
from app.reconcile.plan import Plan, build_plan
from app.services.fabric import render_device

log = logging.getLogger(__name__)


async def active_apply_job(
    session: AsyncSession, site_id: str, *, exclude_id: str | None = None
) -> Job | None:
    """The apply job currently holding this site's push slot, if any.

    At most one apply may push to a device at a time: concurrent pushes arm
    two dead-man rollback schedulers on the router, and their pre-apply
    backups can delete each other. The entry points that push -- the API,
    the scheduled-apply sweep, drift auto-remediation -- consult this before
    starting. Post-enrollment apply calls apply_site directly and relies on
    the index backstop alone; a just-enrolled site has no other apply in
    flight in practice. It is check-then-act, so the partial unique index on
    jobs is the backstop for the true race; this is the fast path that turns
    it into a clean refusal instead of a database error.
    """
    stmt = (
        select(Job)
        .where(
            Job.site_id == site_id,
            Job.kind == JobKind.apply,
            Job.state.in_([JobState.queued, JobState.running]),
        )
        .order_by(Job.created_at.desc())
    )
    if exclude_id is not None:
        stmt = stmt.where(Job.id != exclude_id)
    return await session.scalar(stmt)


async def plan_site(
    session: AsyncSession, site: Site, driver: DeviceDriver | None = None
) -> Plan:
    """Render intent and diff it against the device. Read-only."""
    sections = await render_device(session, site)
    if driver is not None:
        return await build_plan(driver, sections)
    async with open_driver(site) as d:
        return await build_plan(d, sections)


async def preview_intent(session: AsyncSession, site: Site) -> dict:
    """Render what the controller intends to own on this site. Device-free.

    This is B5 layer (1), and it is intent-only by design: the controller
    keeps no stored last-known device state anywhere, so there is nothing
    honest to diff against without contacting the device (that live diff is
    layer (2), ``plan_site``). The result is therefore labelled as intent --
    ``source``/``diffed_against_device`` -- so no consumer can mistake it
    for a plan.

    No driver is opened here, which is the point: the preview works for a
    site that is unprovisioned, has no credentials, or is simply unreachable,
    because ``render_device`` is a pure DB-to-sections render.
    """
    sections = await render_device(session, site)
    return _intent_preview(sections)


def _intent_preview(sections: list[ConfigSection]) -> dict:
    """Format rendered sections as an intent preview, secrets masked.

    Lines mirror ``ItemDiff.render`` for adds -- ``+ <path> <identity>
    k=v ...`` with the ownership comment included where the menu carries
    one, and props passed through ``redact_props`` -- so a secret rendered
    into intent never appears here in the clear. Sections with no items are
    cleanup scopes (they exist so orphaned rows can be swept); they intend
    to own nothing, so, like ``Plan.to_json``, they are omitted.
    """
    rendered: list[dict] = []
    total = 0
    for section in sorted(sections, key=lambda s: s.order):
        if not section.items:
            continue
        lines: list[str] = []
        for item in section.items:
            props = dict(item.props)
            if section.comment_capable:
                props["comment"] = item.tag or section.owner_tag
            shown = " ".join(
                f"{k}={v}" for k, v in sorted(redact_props(props).items())
            )
            ident = ",".join(str(i) for i in item.identity(section.key))
            lines.append(f"+ {section.path} {ident}  {shown}")
        total += len(section.items)
        rendered.append(
            {
                "path": section.path,
                "order": section.order,
                "item_count": len(section.items),
                "lines": lines,
            }
        )
    return {
        "source": "intent",
        "diffed_against_device": False,
        "section_count": len(rendered),
        "total_items": total,
        "sections": rendered,
    }


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
