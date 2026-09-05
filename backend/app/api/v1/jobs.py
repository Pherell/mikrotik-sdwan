"""Plan, apply, and the job log."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import select

from app.deps import RequireOperator, RequireViewer, SessionDep, write_audit
from app.drivers.base import ConfigOp, OpKind
from app.drivers.factory import open_driver
from app.models.enums import JobKind, JobState
from app.models.job import Job
from app.models.site import Site
from app.reconcile.apply import find_stale_rollbacks
from app.schemas.job import ApplyRequest, JobRead, PlanRead
from app.services.reconcile import apply_site, new_job, plan_site

router = APIRouter(tags=["jobs"])


async def _site_or_404(session: SessionDep, site_id: str) -> Site:
    site = await session.get(Site, site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such site")
    return site


@router.post("/sites/{site_id}/plan", response_model=PlanRead)
async def plan(site_id: str, session: SessionDep, _: RequireViewer) -> PlanRead:
    """Render intent, diff it against the device, change nothing.

    Read-only, so a viewer may run it.
    """
    site = await _site_or_404(session, site_id)
    result = await plan_site(session, site)
    return PlanRead.model_validate(result.to_json())


@router.post("/sites/{site_id}/apply", response_model=JobRead)
async def apply(
    site_id: str,
    body: ApplyRequest,
    session: SessionDep,
    user: RequireOperator,
    request: Request,
) -> Job:
    """Push configuration inside the dead-man rollback.

    Runs inline rather than on the queue: the operator is watching, the whole
    cycle is bounded by the rollback timeout, and a job that outlives its HTTP
    request would still hold the device lock.
    """
    site = await _site_or_404(session, site_id)

    if not body.dry_run and not body.confirm:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Set confirm=true to apply. If the push breaks management access the "
            "router restores its pre-apply backup, which reboots it.",
        )

    running = await session.scalar(
        select(Job).where(
            Job.site_id == site.id,
            Job.kind == JobKind.apply,
            Job.state.in_([JobState.queued, JobState.running]),
        )
    )
    if running is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Job {running.id} is already running against this site",
        )

    job = new_job(site, JobKind.apply, user.id)
    session.add(job)
    await session.flush()

    await apply_site(session, site, job, dry_run=body.dry_run)

    await write_audit(
        session,
        actor=user,
        action="site.apply",
        object_type="site",
        object_id=site.id,
        detail={
            "job": job.id,
            "state": job.state,
            "dry_run": body.dry_run,
            "changes": (job.result or {}).get("changes"),
        },
        request=request,
    )
    return job


@router.get("/sites/{site_id}/rollbacks")
async def list_rollbacks(
    site_id: str, session: SessionDep, _: RequireViewer
) -> list[dict]:
    """Rollback schedulers still armed on the device.

    A controller that crashes between arming and disarming leaves one behind,
    and it will restore a perfectly good configuration when it fires. This is
    how an operator finds out before that happens.
    """
    site = await _site_or_404(session, site_id)
    async with open_driver(site) as driver:
        rows = await find_stale_rollbacks(driver)
    return [
        {
            "name": r.get("name"),
            "interval": r.get("interval"),
            "next_run": r.get("next-run"),
            "on_event": r.get("on-event"),
        }
        for r in rows
    ]


@router.delete("/sites/{site_id}/rollbacks/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def clear_rollback(
    site_id: str,
    name: str,
    session: SessionDep,
    user: RequireOperator,
    request: Request,
) -> None:
    """Disarm one leftover rollback."""
    site = await _site_or_404(session, site_id)
    async with open_driver(site) as driver:
        rows = await driver.read("/system/scheduler", {"name": name})
        if not rows:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such scheduler entry")
        if not str(rows[0].get("name", "")).startswith("sdwan-rollback-"):
            # Never let this become a generic "delete any scheduler" endpoint.
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Not a controller rollback entry"
            )
        result = await driver.apply(
            [
                ConfigOp(
                    kind=OpKind.remove,
                    path="/system/scheduler",
                    item_id=str(rows[0].get(".id", "")),
                )
            ]
        )
        if not result.ok:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, result.error or "failed")

    await write_audit(
        session,
        actor=user,
        action="site.rollback.clear",
        object_type="site",
        object_id=site.id,
        detail={"scheduler": name},
        request=request,
    )


@router.get("/jobs", response_model=list[JobRead])
async def list_jobs(
    session: SessionDep,
    user: RequireViewer,
    site_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[Job]:
    query = (
        select(Job)
        .where(Job.tenant_id == user.tenant_id)
        .order_by(Job.created_at.desc())
        .limit(limit)
    )
    if site_id:
        query = query.where(Job.site_id == site_id)
    return list(await session.scalars(query))


@router.get("/jobs/{job_id}", response_model=JobRead)
async def get_job(job_id: str, session: SessionDep, _: RequireViewer) -> Job:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such job")
    return job
