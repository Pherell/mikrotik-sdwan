"""Steering policies, SLA profiles, and application groups."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.deps import RequireOperator, RequireViewer, SessionDep, write_audit
from app.models.policy import AppGroup, Policy, SlaProfile
from app.schemas.policy import (
    AppGroupCreate,
    AppGroupRead,
    PolicyCreate,
    PolicyRead,
    PolicyUpdate,
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
    profile = await session.get(SlaProfile, profile_id)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such SLA profile")

    users = list(
        await session.scalars(select(Policy).where(Policy.sla_profile_id == profile_id))
    )
    if users:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"In use by {', '.join(p.name for p in users)}. Point those policies at "
            "another profile first.",
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
    await _check_references(session, body.sla_profile_id, body.app_group_id)

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
    policy = await session.get(Policy, policy_id)
    if policy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such policy")

    data = body.model_dump(exclude_unset=True)
    await _check_references(session, data.get("sla_profile_id"), data.get("app_group_id"))
    if "prefer_tags" in data and not data["prefer_tags"]:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "prefer_tags must name at least one uplink tag or WAN name; a policy "
            "with none would mark traffic into an empty routing table.",
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
    policy = await session.get(Policy, policy_id)
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
    session: SessionDep, sla_id: str | None, app_group_id: str | None
) -> None:
    if sla_id and await session.get(SlaProfile, sla_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such SLA profile")
    if app_group_id and await session.get(AppGroup, app_group_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such app group")
