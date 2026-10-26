"""Orchestrates plan / apply / drift for a site, and records the Job row."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.drivers.base import ConfigOp, ConfigSection, DeviceDriver, DriverError
from app.drivers.factory import build_safe_mode_session, open_driver
from app.drivers.redact import redact_props
from app.models.apply_journal import ApplyBackup, ApplyJournalEntry
from app.models.enums import JobKind, JobState, SiteStatus
from app.models.job import AuditEvent, Job
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

    if site.quarantined:
        # C3: a quarantined device gets no pushes, from any entry point --
        # interactive, scheduled, or drift auto-remediation all funnel
        # through here, so this one guard covers them all. Fail fast,
        # before a driver is opened: the device may be half-configured in
        # a way that makes even connecting unwise to automate, and the job
        # record should say *quarantine*, not some downstream symptom.
        since = f" since {site.quarantined_at}" if site.quarantined_at else ""
        reason = site.quarantine_reason or "no reason recorded"
        job.state = JobState.failed
        job.error = (
            f"Site is quarantined ({reason}{since}); "
            "release the quarantine before applying."
        )
        job.log = job.error
        return await _finish(session, job)

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

            # C3/C4 bookkeeping for a push that is actually happening. A
            # dry run consumes no generation: the counter means "the Nth
            # push against this device", and stamping it is what ties the
            # journal rows, the preimage backup and the job result of this
            # apply together.
            generation = site.apply_generation + 1
            site.apply_generation = generation
            await session.flush()

            # The scoped preimage: the owned rows the plan just read, no
            # second device read. Redacted here, at rest -- the payload is
            # a restore sketch, and a restore never needs the clear secret
            # (write-once props are re-rendered from intent, not restored).
            session.add(
                ApplyBackup(
                    tenant_id=site.tenant_id,
                    site_id=site.id,
                    job_id=job.id,
                    generation=generation,
                    kind="scoped_preimage",
                    payload={
                        path: [redact_props(dict(row)) for row in rows]
                        for path, rows in plan.preimage.items()
                    },
                )
            )
            await session.flush()

            async def record_step(
                index: int, op: ConfigOp, state: str, error: str | None
            ) -> None:
                # Flushed per entry, committed never (from here): the
                # rows share the caller's transaction, exactly like the
                # Job row itself, so an apply that finishes carries a
                # complete journal and one that crashes mid-flight is
                # rolled back as a unit rather than leaving half a story
                # that looks finished.
                session.add(
                    ApplyJournalEntry(
                        tenant_id=site.tenant_id,
                        site_id=site.id,
                        job_id=job.id,
                        generation=generation,
                        step=index,
                        event="op",
                        op_kind=str(op.kind),
                        op_path=op.path,
                        op_identity=op.item_id,
                        op_comment=op.comment or None,
                        state=state,
                        error=error,
                        detail=_op_summary(op),
                    )
                )
                await session.flush()

            async def record_event(event: str, detail: str) -> None:
                session.add(
                    ApplyJournalEntry(
                        tenant_id=site.tenant_id,
                        site_id=site.id,
                        job_id=job.id,
                        generation=generation,
                        step=None,
                        event=event,
                        state="info",
                        detail=detail,
                    )
                )
                await session.flush()

            outcome = await safe_apply(
                driver,
                plan.ops(),
                job_id=job.id,
                timeout_seconds=timeout,
                safe_mode=(
                    build_safe_mode_session(site)
                    if settings.safe_mode_enabled
                    else None
                ),
                chunk_size=settings.safe_mode_chunk_size,
                record_step=record_step,
                record_event=record_event,
            )
            _record(job, outcome, plan)
            job.result = {
                **(job.result or {}),
                "generation": generation,
                "safe_mode_used": outcome.safe_mode_used,
                "partial": outcome.partial,
            }

            if outcome.quarantine_recommended and not site.quarantined:
                reason = (
                    outcome.error
                    or "apply left the device in an unverifiable state"
                )
                await quarantine_site(session, site, reason=reason, job_id=job.id)
                session.add(
                    AuditEvent(
                        tenant_id=site.tenant_id,
                        actor_id=None,
                        actor_email=None,
                        action="site.quarantine",
                        object_type="site",
                        object_id=site.id,
                        detail={
                            "reason": reason,
                            "generation": generation,
                            "automatic": True,
                        },
                    )
                )
                await session.flush()

            if outcome.ok:
                site.status = SiteStatus.reachable
                site.last_error = None
                site.last_seen_at = datetime.now(UTC).isoformat()
                await _prune_preimages(session, site.id)
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


def _op_summary(op: ConfigOp) -> str:
    """One journal line for an op, secrets already masked.

    Built from ``op.redacted()`` -- never from the op itself -- so the
    journal cannot become the place a PSK survives in the clear.
    """
    red = op.redacted()
    shown = " ".join(f"{k}={v}" for k, v in sorted(red.props.items()))
    return f"{red.kind} {red.path} {shown}".strip()


async def quarantine_site(
    session: AsyncSession,
    site: Site,
    *,
    reason: str,
    job_id: str | None = None,
) -> None:
    """Put a site into C3 quarantine and journal the transition.

    Shared by the automatic path (an apply outcome asked for it) and the
    API (an operator asked for it). Auditing is the caller's job: the two
    paths have different actors, and this helper's one job is that the
    site state and the journal can never disagree about whether the
    quarantine happened.
    """
    site.quarantined = True
    site.quarantine_reason = reason
    site.quarantined_at = datetime.now(UTC)
    session.add(
        ApplyJournalEntry(
            tenant_id=site.tenant_id,
            site_id=site.id,
            job_id=job_id,
            generation=site.apply_generation,
            step=None,
            event="quarantine",
            state="info",
            detail=reason,
        )
    )
    await session.flush()


async def release_quarantine(session: AsyncSession, site: Site) -> None:
    """Clear a site's quarantine and journal the release.

    The reason is kept in the release row's detail before it is cleared
    from the site, so the journal still answers "why was it quarantined,
    and who let it out" after the site row itself has forgotten.
    """
    previous = site.quarantine_reason
    site.quarantined = False
    site.quarantine_reason = None
    site.quarantined_at = None
    session.add(
        ApplyJournalEntry(
            tenant_id=site.tenant_id,
            site_id=site.id,
            job_id=None,
            generation=site.apply_generation,
            step=None,
            event="release",
            state="info",
            detail=f"released (was: {previous})" if previous else "released",
        )
    )
    await session.flush()


# How many generations of scoped preimage to keep per site -- the same
# keep-2 pattern as the on-device backups (app.reconcile.apply._KEEP_BACKUPS):
# the last good state and the one before it are worth disk; a deeper history
# belongs to the journal's summaries, not to row snapshots.
_KEEP_PREIMAGE_GENERATIONS = 2


async def _prune_preimages(session: AsyncSession, site_id: str) -> None:
    """Drop scoped preimages older than the newest two generations."""
    generations = (
        await session.scalars(
            select(ApplyBackup.generation)
            .where(ApplyBackup.site_id == site_id)
            .distinct()
            .order_by(ApplyBackup.generation.desc())
        )
    ).all()
    stale = generations[_KEEP_PREIMAGE_GENERATIONS:]
    if not stale:
        return
    await session.execute(
        delete(ApplyBackup).where(
            ApplyBackup.site_id == site_id,
            ApplyBackup.generation.in_(stale),
        )
    )
    await session.flush()


def new_job(site: Site, kind: JobKind, user_id: str | None) -> Job:
    return Job(
        kind=kind,
        state=JobState.queued,
        site_id=site.id,
        tenant_id=site.tenant_id,
        requested_by=user_id,
    )
