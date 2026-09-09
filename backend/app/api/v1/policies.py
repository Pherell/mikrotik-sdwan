"""Steering policies, SLA profiles, and application groups."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.deps import RequireOperator, RequireViewer, SessionDep, get_owned, write_audit
from app.models.policy import AppGroup, Policy, SdwanGroup, SlaProfile
from app.schemas.policy import (
    AppGroupCreate,
    AppGroupRead,
    PolicyCreate,
    PolicyRead,
    PolicyUpdate,
    SdwanGroupCreate,
    SdwanGroupRead,
    SdwanGroupUpdate,
    SlaProfileCreate,
    SlaProfileRead,
)

router = APIRouter(tags=["policies"])


def _sla_read(profile: SlaProfile) -> SlaProfileRead:
    model = SlaProfileRead.model_validate(profile)
    # Detection is roughly one full probe cycle: the operator should see that
    # number, not have to derive it from interval and count.
    model.detection_seconds = profile.probe_interval_seconds * 2
    return model


# -- SLA profiles -----------------------------------------------------------


@router.get("/sla-profiles", response_model=list[SlaProfileRead])
async def list_slas(session: SessionDep, user: RequireViewer) -> list[SlaProfileRead]:
    rows = await session.scalars(
        select(SlaProfile).where(SlaProfile.tenant_id == user.tenant_id).order_by(SlaProfile.name)
    )
    return [_sla_read(r) for r in rows]


@router.post("/sla-profiles", response_model=SlaProfileRead, status_code=201)
async def create_sla(
    body: SlaProfileCreate, session: SessionDep, user: RequireOperator, request: Request
) -> SlaProfileRead:
    profile = SlaProfile(**body.model_dump(), tenant_id=user.tenant_id)
    session.add(profile)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"An SLA profile named {body.name!r} already exists"
        ) from exc
    await write_audit(
        session,
        actor=user,
        action="sla.create",
        object_type="sla_profile",
        object_id=profile.id,
        detail={"name": profile.name},
        request=request,
    )
    return _sla_read(profile)


@router.delete("/sla-profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sla(
    profile_id: str, session: SessionDep, user: RequireOperator, request: Request
) -> None:
    profile = await get_owned(session, SlaProfile, profile_id, user.tenant_id)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such SLA profile")

    # An SLA now hangs off a group rather than a rule, so checking only rules
    # would let a profile be deleted out from under the group depending on it --
    # silently dropping that path's health standard back to the default.
    holders = sorted(
        list(await session.scalars(
            select(SdwanGroup.name).where(SdwanGroup.sla_profile_id == profile_id)
        ))
        + list(await session.scalars(
            select(Policy.name).where(Policy.sla_profile_id == profile_id)
        ))
    )
    if holders:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"In use by {', '.join(holders)}. Point those at another profile first.",
        )
    await write_audit(
        session,
        actor=user,
        action="sla.delete",
        object_type="sla_profile",
        object_id=profile_id,
        detail={"name": profile.name},
        request=request,
    )
    await session.delete(profile)


# -- app groups -------------------------------------------------------------


@router.get("/app-groups", response_model=list[AppGroupRead])
async def list_app_groups(session: SessionDep, user: RequireViewer) -> list[AppGroup]:
    return list(
        await session.scalars(
            select(AppGroup).where(AppGroup.tenant_id == user.tenant_id).order_by(AppGroup.name)
        )
    )


@router.post("/app-groups", response_model=AppGroupRead, status_code=201)
async def create_app_group(
    body: AppGroupCreate, session: SessionDep, user: RequireOperator, request: Request
) -> AppGroup:
    group = AppGroup(**body.model_dump(), tenant_id=user.tenant_id)
    session.add(group)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"An app group named {body.name!r} already exists"
        ) from exc
    await write_audit(
        session,
        actor=user,
        action="appgroup.create",
        object_type="app_group",
        object_id=group.id,
        detail={"name": group.name, "prefixes": len(group.prefixes or [])},
        request=request,
    )
    return group


# -- policies ---------------------------------------------------------------


@router.get("/policies", response_model=list[PolicyRead])
async def list_policies(session: SessionDep, user: RequireViewer) -> list[Policy]:
    return list(
        await session.scalars(
            select(Policy)
            .where(Policy.tenant_id == user.tenant_id)
            .order_by(Policy.priority, Policy.name)
        )
    )


@router.post("/policies", response_model=PolicyRead, status_code=201)
async def create_policy(
    body: PolicyCreate, session: SessionDep, user: RequireOperator, request: Request
) -> Policy:
    await _check_references(
        session, None, body.app_group_id, body.sdwan_group_id, tenant_id=user.tenant_id
    )
    await _check_sni_load_balance_conflict(
        session, body.app_group_id, body.sdwan_group_id, tenant_id=user.tenant_id
    )

    policy = Policy(**body.model_dump(), tenant_id=user.tenant_id)
    session.add(policy)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"A policy named {body.name!r} already exists"
        ) from exc

    await write_audit(
        session,
        actor=user,
        action="policy.create",
        object_type="policy",
        object_id=policy.id,
        detail={"name": policy.name, "prefer": policy.prefer_tags},
        request=request,
    )
    return policy


@router.patch("/policies/{policy_id}", response_model=PolicyRead)
async def update_policy(
    policy_id: str,
    body: PolicyUpdate,
    session: SessionDep,
    user: RequireOperator,
    request: Request,
) -> Policy:
    policy = await get_owned(session, Policy, policy_id, user.tenant_id)
    if policy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such policy")

    data = body.model_dump(exclude_unset=True)
    await _check_references(
        session,
        None,
        data.get("app_group_id"),
        data.get("sdwan_group_id"),
        tenant_id=user.tenant_id,
    )
    await _check_sni_load_balance_conflict(
        session,
        data.get("app_group_id", policy.app_group_id),
        data.get("sdwan_group_id", policy.sdwan_group_id),
        tenant_id=user.tenant_id,
    )
    for field, value in data.items():
        setattr(policy, field, value)

    await write_audit(
        session,
        actor=user,
        action="policy.update",
        object_type="policy",
        object_id=policy.id,
        detail={"fields": sorted(data)},
        request=request,
    )
    return policy


@router.delete("/policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(
    policy_id: str, session: SessionDep, user: RequireOperator, request: Request
) -> None:
    policy = await get_owned(session, Policy, policy_id, user.tenant_id)
    if policy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such policy")
    await write_audit(
        session,
        actor=user,
        action="policy.delete",
        object_type="policy",
        object_id=policy_id,
        detail={"name": policy.name},
        request=request,
    )
    # The rules stay on the devices until each affected site is applied again.
    await session.delete(policy)


async def _check_references(
    session: SessionDep,
    sla_id: str | None,
    app_group_id: str | None,
    sdwan_group_id: str | None = None,
    *,
    tenant_id: str,
) -> None:
    if sla_id and await get_owned(session, SlaProfile, sla_id, tenant_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such SLA profile")
    if app_group_id and await get_owned(session, AppGroup, app_group_id, tenant_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such app group")
    if sdwan_group_id and await get_owned(session, SdwanGroup, sdwan_group_id, tenant_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such SD-WAN group")


async def _check_sni_load_balance_conflict(
    session: SessionDep,
    app_group_id: str | None,
    sdwan_group_id: str | None,
    *,
    tenant_id: str,
) -> None:
    """SNI matching marks a connection to identify it; PCC (load_balance)
    marks a connection to classify it into a bucket. Both want to be *the*
    connection mark for this policy, and combining them is a real design
    problem (see docs/model.md), not a validation this can wave through.
    Existence of each id is _check_references's job; this checks whether the
    two that exist are compatible.
    """
    if not app_group_id or not sdwan_group_id:
        return
    app_group = await get_owned(session, AppGroup, app_group_id, tenant_id)
    group = await get_owned(session, SdwanGroup, sdwan_group_id, tenant_id)
    if app_group is None or group is None:
        return  # _check_references already reports the real problem
    if (app_group.sni_patterns or []) and str(group.strategy) == "load_balance":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{app_group.name!r} matches by TLS SNI, which load_balance "
            "cannot combine with yet. Point this policy at a failover "
            "group, or remove the app group's SNI patterns.",
        )


# -- SD-WAN groups ----------------------------------------------------------


@router.get("/sdwan-groups", response_model=list[SdwanGroupRead])
async def list_groups(session: SessionDep, user: RequireViewer) -> list[SdwanGroup]:
    rows = await session.scalars(
        select(SdwanGroup)
        .where(SdwanGroup.tenant_id == user.tenant_id)
        .order_by(SdwanGroup.name)
    )
    return list(rows)


@router.post("/sdwan-groups", response_model=SdwanGroupRead, status_code=201)
async def create_group(
    body: SdwanGroupCreate, session: SessionDep, user: RequireOperator, request: Request
) -> SdwanGroup:
    await _check_references(session, body.sla_profile_id, None, tenant_id=user.tenant_id)

    data = body.model_dump()
    data["members"] = [m for m in data["members"]]
    group = SdwanGroup(**data, tenant_id=user.tenant_id)
    session.add(group)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"A group named {body.name!r} already exists"
        ) from exc

    await write_audit(
        session,
        actor=user,
        action="sdwan_group.create",
        object_type="sdwan_group",
        object_id=group.id,
        detail={"name": group.name, "members": data["members"]},
        request=request,
    )
    return group


@router.patch("/sdwan-groups/{group_id}", response_model=SdwanGroupRead)
async def update_group(
    group_id: str,
    body: SdwanGroupUpdate,
    session: SessionDep,
    user: RequireOperator,
    request: Request,
) -> SdwanGroup:
    group = await get_owned(session, SdwanGroup, group_id, user.tenant_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such SD-WAN group")

    data = body.model_dump(exclude_unset=True)
    await _check_references(session, data.get("sla_profile_id"), None, tenant_id=user.tenant_id)
    for field, value in data.items():
        setattr(group, field, value)

    await write_audit(
        session,
        actor=user,
        action="sdwan_group.update",
        object_type="sdwan_group",
        object_id=group.id,
        detail={"fields": sorted(data)},
        request=request,
    )
    await session.flush()
    await session.refresh(group)
    return group


@router.delete("/sdwan-groups/{group_id}", status_code=204)
async def delete_group(
    group_id: str, session: SessionDep, user: RequireOperator, request: Request
) -> None:
    group = await get_owned(session, SdwanGroup, group_id, user.tenant_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such SD-WAN group")

    # A rule pointing at a deleted group would steer into an empty table.
    # Refusing with the names is more useful than a foreign-key error.
    users = await session.scalars(
        select(Policy.name).where(Policy.sdwan_group_id == group_id)
    )
    names = sorted(users)
    if names:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{group.name!r} is used by {len(names)} traffic rule"
            f"{'s' if len(names) > 1 else ''}: {', '.join(names)}. "
            "Point them at another group first.",
        )

    await session.delete(group)
    await write_audit(
        session,
        actor=user,
        action="sdwan_group.delete",
        object_type="sdwan_group",
        object_id=group_id,
        detail={"name": group.name},
        request=request,
    )
