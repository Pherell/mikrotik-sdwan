"""Site and WAN CRUD, plus the device probe that backs the onboarding wizard."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.deps import RequireAdmin, RequireOperator, RequireViewer, SessionDep, write_audit
from app.drivers.factory import open_driver
from app.models.site import Site, Wan
from app.schemas.site import (
    ProbeResult,
    SiteCreate,
    SiteRead,
    SiteUpdate,
    WanCreate,
    WanRead,
    WanUpdate,
)
from app.security import SecretBox
from app.services.probe import apply_probe, probe_site

router = APIRouter(prefix="/sites", tags=["sites"])


def _to_read(site: Site) -> SiteRead:
    model = SiteRead.model_validate(site)
    model.has_credentials = bool(site.password_enc or site.ssh_key_enc)
    # The SSH key itself is never returned -- it is long, and only its presence
    # is actionable in the UI.
    model.has_ssh_host_key = bool(site.ssh_host_key)
    model.wans = [
        WanRead.model_validate({**w.__dict__, "dial_out_only": w.dial_out_only})
        for w in site.wans
    ]
    return model


async def _get_or_404(session: SessionDep, site_id: str) -> Site:
    site = await session.get(Site, site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such site")
    return site


@router.get("", response_model=list[SiteRead])
async def list_sites(session: SessionDep, user: RequireViewer) -> list[SiteRead]:
    sites = await session.scalars(
        select(Site).where(Site.tenant_id == user.tenant_id).order_by(Site.name)
    )
    return [_to_read(s) for s in sites]


@router.post("", response_model=SiteRead, status_code=status.HTTP_201_CREATED)
async def create_site(
    body: SiteCreate, session: SessionDep, user: RequireOperator, request: Request
) -> SiteRead:
    box = SecretBox()
    data = body.model_dump(exclude={"password", "ssh_key", "wans"})
    site = Site(
        **data,
        tenant_id=user.tenant_id,
        password_enc=box.encrypt(body.password) if body.password else None,
        ssh_key_enc=box.encrypt(body.ssh_key) if body.ssh_key else None,
    )
    site.wans = [Wan(**w.model_dump()) for w in body.wans]
    session.add(site)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"A site named {body.name!r} already exists"
        ) from exc

    await write_audit(
        session,
        actor=user,
        action="site.create",
        object_type="site",
        object_id=site.id,
        detail={"name": site.name, "host": site.mgmt_host},
        request=request,
    )
    return _to_read(site)


@router.get("/{site_id}", response_model=SiteRead)
async def get_site(site_id: str, session: SessionDep, _: RequireViewer) -> SiteRead:
    return _to_read(await _get_or_404(session, site_id))


@router.patch("/{site_id}", response_model=SiteRead)
async def update_site(
    site_id: str,
    body: SiteUpdate,
    session: SessionDep,
    user: RequireOperator,
    request: Request,
) -> SiteRead:
    site = await _get_or_404(session, site_id)
    data = body.model_dump(exclude_unset=True)
    box = SecretBox()
    if (pw := data.pop("password", None)) is not None:
        site.password_enc = box.encrypt(pw) if pw else None
    if (key := data.pop("ssh_key", None)) is not None:
        site.ssh_key_enc = box.encrypt(key) if key else None
    for field, value in data.items():
        setattr(site, field, value)

    await write_audit(
        session,
        actor=user,
        action="site.update",
        object_type="site",
        object_id=site.id,
        detail={"fields": sorted(data)},
        request=request,
    )
    return _to_read(site)


@router.delete("/{site_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_site(
    site_id: str, session: SessionDep, user: RequireAdmin, request: Request
) -> None:
    site = await _get_or_404(session, site_id)
    if site.memberships:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Remove this site from its fabrics before deleting it",
        )
    await write_audit(
        session,
        actor=user,
        action="site.delete",
        object_type="site",
        object_id=site.id,
        detail={"name": site.name},
        request=request,
    )
    await session.delete(site)


@router.post("/{site_id}/probe", response_model=ProbeResult)
async def probe(
    site_id: str, session: SessionDep, user: RequireOperator, request: Request
) -> ProbeResult:
    """Read-only: connect, report version and capabilities, suggest uplinks."""
    site = await _get_or_404(session, site_id)
    result = await probe_site(site)
    apply_probe(site, result)
    await write_audit(
        session,
        actor=user,
        action="site.probe",
        object_type="site",
        object_id=site.id,
        detail={"reachable": result.reachable, "version": result.version},
        request=request,
    )
    return result


# -- WAN uplinks ------------------------------------------------------------


@router.post("/{site_id}/wans", response_model=WanRead, status_code=status.HTTP_201_CREATED)
async def add_wan(
    site_id: str,
    body: WanCreate,
    session: SessionDep,
    user: RequireOperator,
    request: Request,
) -> WanRead:
    site = await _get_or_404(session, site_id)
    wan = Wan(site_id=site.id, **body.model_dump())
    session.add(wan)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Site already has a WAN named {body.name!r}"
        ) from exc
    await write_audit(
        session,
        actor=user,
        action="wan.create",
        object_type="wan",
        object_id=wan.id,
        detail={"site": site.name, "name": wan.name},
        request=request,
    )
    return WanRead.model_validate({**wan.__dict__, "dial_out_only": wan.dial_out_only})


@router.patch("/{site_id}/wans/{wan_id}", response_model=WanRead)
async def update_wan(
    site_id: str,
    wan_id: str,
    body: WanUpdate,
    session: SessionDep,
    user: RequireOperator,
    request: Request,
) -> WanRead:
    wan = await session.get(Wan, wan_id)
    if wan is None or wan.site_id != site_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such WAN on this site")
    data = body.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(wan, field, value)
    await write_audit(
        session,
        actor=user,
        action="wan.update",
        object_type="wan",
        object_id=wan.id,
        detail={"fields": sorted(data)},
        request=request,
    )
    return WanRead.model_validate({**wan.__dict__, "dial_out_only": wan.dial_out_only})


@router.delete("/{site_id}/wans/{wan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_wan(
    site_id: str,
    wan_id: str,
    session: SessionDep,
    user: RequireOperator,
    request: Request,
) -> None:
    wan = await session.get(Wan, wan_id)
    if wan is None or wan.site_id != site_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such WAN on this site")
    await write_audit(
        session,
        actor=user,
        action="wan.delete",
        object_type="wan",
        object_id=wan.id,
        detail={"name": wan.name},
        request=request,
    )
    await session.delete(wan)


# -- read-only device passthrough -------------------------------------------

# Menus an operator may read straight off a device, for troubleshooting and for
# the lab verifier. Strictly an allowlist: /user leaks accounts and
# /ip/ipsec/identity leaks pre-shared keys, so nothing may be readable by
# default just because RouterOS exposes it.
READABLE_PATHS = frozenset(
    {
        "system/resource",
        "system/identity",
        "system/package",
        "system/scheduler",
        "system/routerboard",
        "interface",
        "interface/bridge",
        "interface/gre",
        "interface/wireguard",
        "ip/address",
        "ip/route",
        "ip/dhcp-client",
        "ip/firewall/address-list",
        "ip/firewall/mangle",
        "ip/ipsec/active-peers",
        "ip/ipsec/installed-sa",
        "ip/ipsec/policy",
        "ip/ipsec/peer",
        "ip/ipsec/profile",
        "ip/ipsec/proposal",
        "routing/bgp/session",
        "routing/bgp/connection",
        "routing/bgp/template",
        "routing/bgp/network",
        "routing/table",
        "tool/netwatch",
    }
)

# Properties to strip from a passthrough response even on an allowed path.
_SENSITIVE = frozenset({"secret", "private-key", "password", "ipsec-secret", "preshared-key"})


@router.get("/{site_id}/device/{device_path:path}")
async def read_device(
    site_id: str, device_path: str, session: SessionDep, _: RequireOperator
) -> list[dict]:
    """Read one RouterOS menu straight from the device. Never writes."""
    normalized = device_path.strip("/")
    if normalized not in READABLE_PATHS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{normalized!r} is not readable through the controller. "
            f"Allowed: {', '.join(sorted(READABLE_PATHS))}",
        )
    site = await _get_or_404(session, site_id)
    async with open_driver(site) as driver:
        rows = await driver.read("/" + normalized)
    return [{k: v for k, v in row.items() if k not in _SENSITIVE} for row in rows]
