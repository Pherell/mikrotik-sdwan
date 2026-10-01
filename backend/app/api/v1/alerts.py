"""Alerts and where they go.

Listing alerts is viewer-level: it is the same information the UI already
shows as site and job status, as a timeline. Channels are admin-only, all of
it, reads included -- a channel is a credential to somebody else's chat, and
which chats this controller can post into is itself worth keeping from a
viewer.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.deps import RequireAdmin, RequireViewer, SessionDep, get_owned, write_audit
from app.models.alert import Alert, NotificationChannel
from app.models.base import utcnow
from app.models.enums import AlertKind, AlertSeverity, ChannelType
from app.schemas.alert import (
    AlertPage,
    AlertRead,
    ChannelCreate,
    ChannelRead,
    ChannelTestResult,
    ChannelUpdate,
)
from app.services import alerts as alert_service

router = APIRouter(prefix="/alerts", tags=["alerts"])


async def _channel_or_404(
    session: SessionDep, channel_id: str, tenant_id: str
) -> NotificationChannel:
    channel = await get_owned(session, NotificationChannel, channel_id, tenant_id)
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such channel")
    return channel


@router.get("", response_model=AlertPage)
async def list_alerts(
    session: SessionDep,
    user: RequireViewer,
    site_id: str | None = None,
    kind: AlertKind | None = None,
    severity: AlertSeverity | None = None,
    since: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> AlertPage:
    """Newest first. ``total`` counts every match, for the pager."""
    where = [Alert.tenant_id == user.tenant_id]
    if site_id:
        where.append(Alert.site_id == site_id)
    if kind:
        where.append(Alert.kind == kind)
    if severity:
        where.append(Alert.severity == severity)
    if since:
        where.append(Alert.created_at >= since)

    total = await session.scalar(select(func.count()).select_from(Alert).where(*where))
    rows = await session.scalars(
        select(Alert)
        .where(*where)
        .order_by(Alert.created_at.desc(), Alert.id)
        .limit(limit)
        .offset(offset)
    )
    return AlertPage(
        items=[AlertRead.model_validate(r) for r in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.get("/channels", response_model=list[ChannelRead])
async def list_channels(session: SessionDep, user: RequireAdmin) -> list[NotificationChannel]:
    return list(
        await session.scalars(
            select(NotificationChannel)
            .where(NotificationChannel.tenant_id == user.tenant_id)
            .order_by(NotificationChannel.name)
        )
    )


@router.post("/channels", response_model=ChannelRead, status_code=status.HTTP_201_CREATED)
async def create_channel(
    body: ChannelCreate, session: SessionDep, user: RequireAdmin, request: Request
) -> NotificationChannel:
    config = body.destination_for(body.type)
    assert config is not None  # enforced by ChannelCreate's validator
    config_enc, hint = alert_service.encode_config(body.type, config)
    channel = NotificationChannel(
        tenant_id=user.tenant_id,
        name=body.name,
        type=body.type,
        config_enc=config_enc,
        target_hint=hint,
        enabled=body.enabled,
        min_severity=body.min_severity,
    )
    session.add(channel)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"A channel named {body.name!r} already exists"
        ) from exc
    await write_audit(
        session,
        actor=user,
        action="alert_channel.create",
        object_type="alert_channel",
        object_id=channel.id,
        # The hint, never the destination: an audit trail that records a
        # webhook URL is a second place to steal it from.
        detail={"name": channel.name, "type": str(channel.type), "target": hint},
        request=request,
    )
    return channel


@router.patch("/channels/{channel_id}", response_model=ChannelRead)
async def update_channel(
    channel_id: str,
    body: ChannelUpdate,
    session: SessionDep,
    user: RequireAdmin,
    request: Request,
) -> NotificationChannel:
    channel = await _channel_or_404(session, channel_id, user.tenant_id)
    try:
        config = body.destination_for(ChannelType(channel.type))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    changed: list[str] = []
    if config is not None:
        channel.config_enc, channel.target_hint = alert_service.encode_config(
            ChannelType(channel.type), config
        )
        # A new destination starts with a clean record.
        channel.last_status, channel.last_error, channel.failure_count = None, None, 0
        changed.append("destination")
    for name in ("name", "enabled", "min_severity"):
        value = getattr(body, name)
        if value is not None and value != getattr(channel, name):
            setattr(channel, name, value)
            changed.append(name)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"A channel named {body.name!r} already exists"
        ) from exc
    if changed:
        await write_audit(
            session,
            actor=user,
            action="alert_channel.update",
            object_type="alert_channel",
            object_id=channel.id,
            detail={"name": channel.name, "changed": changed},
            request=request,
        )
    return channel


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    channel_id: str, session: SessionDep, user: RequireAdmin, request: Request
) -> None:
    channel = await _channel_or_404(session, channel_id, user.tenant_id)
    await write_audit(
        session,
        actor=user,
        action="alert_channel.delete",
        object_type="alert_channel",
        object_id=channel.id,
        detail={"name": channel.name},
        request=request,
    )
    await session.delete(channel)


@router.post("/channels/{channel_id}/test", response_model=ChannelTestResult)
async def test_channel(
    channel_id: str, session: SessionDep, user: RequireAdmin
) -> ChannelTestResult:
    """Send a ``test`` event now, synchronously, and say whether it landed.

    Works on a disabled channel too: checking a destination before switching
    it on is the point. Always 200 -- a destination that refuses is a fact
    about the destination, reported in the body (redacted), not an error in
    this API. Nothing is written to ``alerts``.
    """
    channel = await _channel_or_404(session, channel_id, user.tenant_id)
    payload = {
        "version": 1,
        "source": "mikrotik-sdwan",
        "id": None,
        "kind": "test",
        "severity": "info",
        "tenant_id": user.tenant_id,
        "site": None,
        "link_id": None,
        "message": f"Test alert for channel {channel.name!r}, sent by {user.email}",
        "details": {},
        "created_at": utcnow().isoformat(),
    }
    async with alert_service.make_client() as client:
        error = await alert_service.deliver_to_channel(client, channel, payload)
    return ChannelTestResult(ok=error is None, error=error)
