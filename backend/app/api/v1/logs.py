"""The controller's audit trail, and the router's own log.

Two of the three things called "the log". The third -- what happened during an
apply -- belongs to a job and lives in `app.api.v1.jobs`.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.deps import RequireAdmin, RequireOperator, SessionDep
from app.drivers.base import DriverError
from app.drivers.factory import open_driver
from app.models.job import AuditEvent
from app.models.site import Site
from app.schemas.log import AuditRead, DeviceLogEntry
from app.services.devicelog import read_device_log

router = APIRouter(tags=["logs"])


@router.get("/audit", response_model=list[AuditRead])
async def list_audit(
    session: SessionDep,
    user: RequireAdmin,
    action: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    actor_email: str | None = None,
    since: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AuditEvent]:
    """Who changed what, newest first.

    Admin only. This is the one endpoint that reports on *people*: it carries
    source addresses and, because failed logins are audited, the email
    addresses of accounts that do and do not exist. An operator does not need
    that to do their job, and handing it to every viewer turns the audit trail
    into an account-enumeration endpoint.
    """
    query = (
        select(AuditEvent)
        .where(AuditEvent.tenant_id == user.tenant_id)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(limit)
    )
    if action:
        query = query.where(AuditEvent.action == action)
    if object_type:
        query = query.where(AuditEvent.object_type == object_type)
    if object_id:
        query = query.where(AuditEvent.object_id == object_id)
    if actor_email:
        query = query.where(AuditEvent.actor_email == actor_email)
    if since:
        query = query.where(AuditEvent.created_at >= since)
    return list(await session.scalars(query))


@router.get("/audit/actions", response_model=list[str])
async def list_audit_actions(session: SessionDep, user: RequireAdmin) -> list[str]:
    """Every action name that appears in this tenant's trail.

    The filter list has to come from the data. A hardcoded list of action
    names goes stale the first time an endpoint is added, and a stale filter
    silently hides events rather than failing.
    """
    query = (
        select(AuditEvent.action)
        .where(AuditEvent.tenant_id == user.tenant_id)
        .distinct()
        .order_by(AuditEvent.action)
    )
    return list(await session.scalars(query))


@router.get("/sites/{site_id}/log", response_model=list[DeviceLogEntry])
async def device_log(
    site_id: str,
    session: SessionDep,
    _: RequireOperator,
    topic: str | None = None,
    contains: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[DeviceLogEntry]:
    """RouterOS's own log, newest first.

    Operator rather than viewer: the device log is unfiltered and unowned, so
    it can carry anything the router chose to write -- including lines about
    accounts, addresses and failed logins on the device itself.
    """
    site = await session.get(Site, site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such site")
    try:
        async with open_driver(site) as driver:
            return await read_device_log(
                driver, limit=limit, topic=topic, contains=contains
            )
    except DriverError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
