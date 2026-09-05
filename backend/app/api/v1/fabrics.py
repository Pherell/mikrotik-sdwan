"""Fabric CRUD, membership, and topology expansion."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.deps import RequireAdmin, RequireOperator, RequireViewer, SessionDep, write_audit
from app.fabric.allocate import capacity
from app.models.fabric import Fabric, FabricMember, Link
from app.models.site import Site
from app.schemas.fabric import (
    ExpansionRead,
    FabricCreate,
    FabricRead,
    FabricUpdate,
    LinkRead,
    MemberCreate,
    MemberRead,
)
from app.services.fabric import expand_fabric, load_fabric, reallocate_secrets
from app.transports.base import TransportDriver, TransportError, available, get_transport

router = APIRouter(prefix="/fabrics", tags=["fabrics"])


async def _get_or_404(session: SessionDep, fabric_id: str) -> Fabric:
    fabric = await load_fabric(session, fabric_id)
    if fabric is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such fabric")
    return fabric


async def _to_read(session: SessionDep, fabric: Fabric) -> FabricRead:
    model = FabricRead.model_validate(fabric)
    model.members = [
        MemberRead(
            id=m.id,
            site_id=m.site_id,
            site_name=m.site.name if m.site else "",
            role_override=m.role_override,
            loopback_ip=m.loopback_ip,
            enabled=m.enabled,
        )
        for m in fabric.members
    ]
    model.link_count = (
        await session.scalar(
            select(func.count()).select_from(Link).where(Link.fabric_id == fabric.id)
        )
        or 0
    )
    model.pool_capacity = capacity(fabric.ip_pool)
    return model


@router.get("/transports")
async def list_transports(_: RequireViewer) -> list[dict]:
    """Which overlays this build can render, and what each one requires."""
    out = []
    for name in available():
        driver = get_transport(name)
        out.append(
            {
                "name": name,
                "supported_ros": sorted(driver.supported_ros),
                "requires_reachable_responder": driver.requires_reachable_responder,
                "supports_dynamic_mesh": driver.supports_dynamic_mesh,
            }
        )
    return out


@router.get("", response_model=list[FabricRead])
async def list_fabrics(session: SessionDep, user: RequireViewer) -> list[FabricRead]:
    ids = list(
        await session.scalars(
            select(Fabric.id).where(Fabric.tenant_id == user.tenant_id).order_by(Fabric.name)
        )
    )
    return [await _to_read(session, await _get_or_404(session, fid)) for fid in ids]


@router.post("", response_model=FabricRead, status_code=status.HTTP_201_CREATED)
async def create_fabric(
    body: FabricCreate, session: SessionDep, user: RequireOperator, request: Request
) -> FabricRead:
    try:
        get_transport(body.transport)
    except TransportError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    data = body.model_dump(exclude={"member_site_ids"})
    fabric = Fabric(**data, tenant_id=user.tenant_id)
    session.add(fabric)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"A fabric named {body.name!r} already exists"
        ) from exc

    for site_id in body.member_site_ids:
        if await session.get(Site, site_id) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"No such site {site_id}")
        session.add(FabricMember(fabric_id=fabric.id, site_id=site_id))
    await session.flush()

    await write_audit(
        session,
        actor=user,
        action="fabric.create",
        object_type="fabric",
        object_id=fabric.id,
        detail={"name": fabric.name, "transport": fabric.transport},
        request=request,
    )
    return await _to_read(session, await _get_or_404(session, fabric.id))


@router.get("/{fabric_id}", response_model=FabricRead)
async def get_fabric(fabric_id: str, session: SessionDep, _: RequireViewer) -> FabricRead:
    return await _to_read(session, await _get_or_404(session, fabric_id))


@router.patch("/{fabric_id}", response_model=FabricRead)
async def update_fabric(
    fabric_id: str,
    body: FabricUpdate,
    session: SessionDep,
    user: RequireOperator,
    request: Request,
) -> FabricRead:
    fabric = await _get_or_404(session, fabric_id)
    data = body.model_dump(exclude_unset=True)

    switched_to: TransportDriver | None = None
    if "transport" in data and data["transport"] != fabric.transport:
        try:
            switched_to = get_transport(data["transport"])
        except TransportError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

        # Every member must be able to run the new transport, or the switch
        # half-lands: some sites migrate and the rest are stranded.
        unsupported = [
            m.site.name
            for m in fabric.members
            if m.site
            and int((m.site.capabilities or {}).get("ros_major") or 7)
            not in switched_to.supported_ros
        ]
        if unsupported:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{', '.join(sorted(unsupported))} cannot run the "
                f"{data['transport']} transport (needs RouterOS "
                f"{sorted(switched_to.supported_ros)}). Probe those sites, or "
                "move them to their own fabric.",
            )

    # Renumbering a live overlay drops every tunnel on it.
    if "ip_pool" in data and data["ip_pool"] != fabric.ip_pool:
        existing = await session.scalar(
            select(func.count()).select_from(Link).where(Link.fabric_id == fabric.id)
        )
        if existing:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{existing} link(s) are addressed out of {fabric.ip_pool}. Delete "
                "them before changing the pool, or every tunnel will renumber.",
            )

    for field, value in data.items():
        setattr(fabric, field, value)

    rekeyed = 0
    if switched_to is not None:
        rekeyed = await reallocate_secrets(session, fabric, switched_to)

    await write_audit(
        session,
        actor=user,
        action="fabric.update",
        object_type="fabric",
        object_id=fabric.id,
        detail={"fields": sorted(data), "rekeyed_links": rekeyed},
        request=request,
    )
    return await _to_read(session, await _get_or_404(session, fabric.id))


@router.delete("/{fabric_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_fabric(
    fabric_id: str, session: SessionDep, user: RequireAdmin, request: Request
) -> None:
    fabric = await _get_or_404(session, fabric_id)
    await write_audit(
        session,
        actor=user,
        action="fabric.delete",
        object_type="fabric",
        object_id=fabric.id,
        detail={"name": fabric.name},
        request=request,
    )
    # Links and members cascade. The tunnels stay on the devices until each
    # affected site is applied again -- deleting a fabric is not a push.
    await session.delete(fabric)


# -- membership -------------------------------------------------------------


@router.post("/{fabric_id}/members", response_model=MemberRead, status_code=201)
async def add_member(
    fabric_id: str,
    body: MemberCreate,
    session: SessionDep,
    user: RequireOperator,
    request: Request,
) -> MemberRead:
    fabric = await _get_or_404(session, fabric_id)
    site = await session.get(Site, body.site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such site")

    member = FabricMember(
        fabric_id=fabric.id, site_id=site.id, role_override=body.role_override
    )
    session.add(member)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"{site.name} is already in this fabric"
        ) from exc

    await write_audit(
        session,
        actor=user,
        action="fabric.member.add",
        object_type="fabric",
        object_id=fabric.id,
        detail={"site": site.name},
        request=request,
    )
    return MemberRead(
        id=member.id,
        site_id=site.id,
        site_name=site.name,
        role_override=member.role_override,
        loopback_ip=member.loopback_ip,
        enabled=member.enabled,
    )


@router.delete("/{fabric_id}/members/{site_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    fabric_id: str,
    site_id: str,
    session: SessionDep,
    user: RequireOperator,
    request: Request,
) -> None:
    member = await session.scalar(
        select(FabricMember).where(
            FabricMember.fabric_id == fabric_id, FabricMember.site_id == site_id
        )
    )
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That site is not in this fabric")
    await write_audit(
        session,
        actor=user,
        action="fabric.member.remove",
        object_type="fabric",
        object_id=fabric_id,
        detail={"site_id": site_id},
        request=request,
    )
    await session.delete(member)


# -- expansion --------------------------------------------------------------


@router.post("/{fabric_id}/expand", response_model=ExpansionRead)
async def expand_topology(
    fabric_id: str,
    session: SessionDep,
    user: RequireOperator,
    request: Request,
) -> ExpansionRead:
    """Recompute the link set from the topology.

    Allocates addresses and generates keys for new links. Nothing is pushed --
    apply each affected site to put the tunnels on the devices.
    """
    fabric = await _get_or_404(session, fabric_id)
    try:
        result = await expand_fabric(session, fabric)
    except (TransportError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    affected = {
        m.site_id
        for m in fabric.members
        if any(
            m.site_id
            in {w.site_id for w in (link.a_wan, link.b_wan) if w is not None}
            for link in (result.created + result.removed)
        )
    } or {m.site_id for m in fabric.members}

    await write_audit(
        session,
        actor=user,
        action="fabric.expand",
        object_type="fabric",
        object_id=fabric.id,
        detail=result.summary,
        request=request,
    )
    return ExpansionRead(
        **result.summary,
        problems=[{"a": a, "b": b, "reason": why} for a, b, why in result.skipped],
        affected_site_ids=sorted(affected),
    )


@router.get("/{fabric_id}/links", response_model=list[LinkRead])
async def list_links(
    fabric_id: str, session: SessionDep, _: RequireViewer
) -> list[LinkRead]:
    await _get_or_404(session, fabric_id)
    links = await session.scalars(
        select(Link).where(Link.fabric_id == fabric_id).order_by(Link.slug)
    )
    return [
        LinkRead.model_validate({**link.__dict__, "has_secrets": bool(link.secrets_enc)})
        for link in links
    ]
