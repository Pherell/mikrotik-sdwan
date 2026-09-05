"""Operational endpoints: drift checks, intent export/import, and metrics."""

from __future__ import annotations

import yaml
from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import func, select

from app.deps import RequireAdmin, RequireOperator, RequireViewer, SessionDep, write_audit
from app.models.enums import JobState, SiteStatus
from app.models.fabric import Fabric, Link
from app.models.job import Job
from app.models.policy import Policy
from app.models.site import Site
from app.schemas.job import JobRead
from app.services.drift import check_all, check_site
from app.services.portable import ImportError_, export_intent, import_intent

router = APIRouter(tags=["ops"])


# -- drift ------------------------------------------------------------------


@router.post("/sites/{site_id}/drift", response_model=JobRead)
async def check_one(
    site_id: str, session: SessionDep, user: RequireOperator, request: Request
) -> Job:
    """Diff intent against the device without changing it.

    Honours the site's ``drift_action``: ``auto-remediate`` re-applies, which is
    why this needs operator rather than viewer.
    """
    site = await session.get(Site, site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such site")

    job = await check_site(session, site)
    await write_audit(
        session,
        actor=user,
        action="site.drift_check",
        object_type="site",
        object_id=site.id,
        detail={"drifted": (job.result or {}).get("drifted")},
        request=request,
    )
    return job


@router.post("/drift", response_model=list[JobRead])
async def check_everything(
    session: SessionDep, user: RequireOperator, request: Request
) -> list[Job]:
    jobs = await check_all(session, user.tenant_id)
    await write_audit(
        session,
        actor=user,
        action="drift_check.all",
        detail={
            "checked": len(jobs),
            "drifted": sum(1 for j in jobs if (j.result or {}).get("drifted")),
        },
        request=request,
    )
    return jobs


# -- portable intent --------------------------------------------------------


@router.get("/intent/export")
async def export_yaml(session: SessionDep, user: RequireViewer) -> Response:
    """The whole authored intent as YAML, ready to commit.

    Credentials and link keys are excluded: they are environment-specific and
    do not belong in a file people put in git.
    """
    document = await export_intent(session, user.tenant_id)
    body = yaml.safe_dump(document, sort_keys=False, default_flow_style=False)
    return Response(
        content=body,
        media_type="application/yaml",
        headers={"Content-Disposition": 'attachment; filename="sdwan-intent.yaml"'},
    )


@router.post("/intent/import")
async def import_yaml(
    request: Request,
    session: SessionDep,
    user: RequireAdmin,
    dry_run: bool = True,
) -> dict:
    """Create what is missing and update what exists, matching on name.

    Defaults to a dry run. Nothing is ever deleted -- a partial document is the
    normal case, and treating omissions as deletions would make sharing one
    fabric destructive.
    """
    raw = await request.body()
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Not valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Expected a YAML mapping")

    try:
        summary = await import_intent(
            session, document, tenant_id=user.tenant_id, dry_run=dry_run
        )
    except ImportError_ as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    if not dry_run:
        await write_audit(
            session,
            actor=user,
            action="intent.import",
            detail={k: v for k, v in summary.items() if isinstance(v, int)},
            request=request,
        )
    return {"dry_run": dry_run, **summary}


# -- metrics ----------------------------------------------------------------


@router.get("/metrics", include_in_schema=False)
async def metrics(session: SessionDep) -> Response:
    """Prometheus exposition.

    Deliberately unauthenticated but deliberately dull: counts and states only,
    no names, addresses, or anything that would leak the topology to whoever can
    reach the scrape endpoint.
    """
    lines: list[str] = []

    def gauge(name: str, help_text: str, samples: dict[str, int]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        for labels, value in sorted(samples.items()):
            lines.append(f"{name}{{{labels}}} {value}" if labels else f"{name} {value}")

    site_states = dict(
        (await session.execute(select(Site.status, func.count()).group_by(Site.status))).all()
    )
    gauge(
        "sdwan_sites",
        "Sites by status.",
        {f'status="{state}"': count for state, count in site_states.items()},
    )

    job_states = dict(
        (await session.execute(select(Job.state, func.count()).group_by(Job.state))).all()
    )
    gauge(
        "sdwan_jobs_total",
        "Jobs by terminal state.",
        {f'state="{state}"': count for state, count in job_states.items()},
    )

    gauge(
        "sdwan_links",
        "Configured tunnels.",
        {"": await session.scalar(select(func.count()).select_from(Link)) or 0},
    )
    gauge(
        "sdwan_fabrics",
        "Configured fabrics.",
        {"": await session.scalar(select(func.count()).select_from(Fabric)) or 0},
    )
    gauge(
        "sdwan_policies",
        "Steering policies.",
        {"": await session.scalar(select(func.count()).select_from(Policy)) or 0},
    )

    # The two numbers an alert should actually fire on.
    gauge(
        "sdwan_sites_drifted",
        "Sites whose configuration no longer matches intent.",
        {"": site_states.get(SiteStatus.drifted, 0)},
    )
    gauge(
        "sdwan_rollbacks_armed",
        "Jobs that left a dead-man rollback armed on a device.",
        {
            "": await session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.rollback_token.isnot(None), Job.state == JobState.rolled_back)
            )
            or 0
        },
    )

    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
