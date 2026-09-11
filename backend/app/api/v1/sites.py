"""Site and WAN CRUD, plus the device probe that backs the onboarding wizard."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.deps import (
    RequireAdmin,
    RequireOperator,
    RequireViewer,
    SessionDep,
    get_owned,
    write_audit,
)
from app.drivers.base import DriverError
from app.drivers.factory import open_driver
from app.models.fabric import Link
from app.models.site import Site, Wan
from app.schemas.diagnostics import (
    PingRequest,
    PingResult,
    TracerouteRequest,
    TracerouteResult,
    TunnelHealth,
)
from app.schemas.health import DeviceHealth
from app.schemas.ports import PortRead
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
from app.services.diagnostics import run_ping, run_traceroute, tunnel_health
from app.services.health import read_health
from app.services.ports import read_ports
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


async def _get_or_404(session: SessionDep, site_id: str, tenant_id: str) -> Site:
    site = await get_owned(session, Site, site_id, tenant_id)
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
    seen_public: dict[str, str] = {}
    for w in body.wans:
        if w.public_ip:
            if w.public_ip in seen_public:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"{w.public_ip} is given to both {seen_public[w.public_ip]!r} and "
                    f"{w.name!r} on this site. Each uplink needs its own address.",
                )
            seen_public[w.public_ip] = w.name
        await _reject_duplicate_public_ip(session, user.tenant_id, w.public_ip)
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
async def get_site(site_id: str, session: SessionDep, user: RequireViewer) -> SiteRead:
    return _to_read(await _get_or_404(session, site_id, user.tenant_id))


@router.patch("/{site_id}", response_model=SiteRead)
async def update_site(
    site_id: str,
    body: SiteUpdate,
    session: SessionDep,
    user: RequireOperator,
    request: Request,
) -> SiteRead:
    site = await _get_or_404(session, site_id, user.tenant_id)
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
    site = await _get_or_404(session, site_id, user.tenant_id)
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
    site = await _get_or_404(session, site_id, user.tenant_id)
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


async def _reject_duplicate_public_ip(
    session,
    tenant_id: str,
    public_ip: str | None,
    *,
    exclude_wan_id: str | None = None,
) -> None:
    """A tunnel endpoint has to be one address on one router.

    Two uplinks claiming the same public_ip cannot both be dialled: the fabric
    builds a link to an address that answers as somebody else, IKE negotiates
    with the wrong box or nothing at all, and the tunnel simply never comes up.
    Accepting it silently is how a site ends up with an endpoint copied from a
    different router, which reads as "neither end is publicly reachable" long
    after the mistake was made.
    """
    if not public_ip:
        return
    rows = await session.execute(
        select(Wan, Site.name)
        .join(Site, Wan.site_id == Site.id)
        .where(
            Site.tenant_id == tenant_id,
            Wan.public_ip == public_ip,
            Wan.enabled.is_(True),
        )
    )
    for wan, site_name in rows.all():
        if exclude_wan_id is not None and wan.id == exclude_wan_id:
            continue
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{public_ip} is already the public address of {site_name}/{wan.name}. "
            "A tunnel endpoint has to be one address on one router. If these are "
            "genuinely two routers behind one NAT, leave the public IP empty and "
            "mark them behind NAT instead -- they can dial out, but neither can "
            "be dialled.",
        )


# -- WAN uplinks ------------------------------------------------------------


@router.post("/{site_id}/wans", response_model=WanRead, status_code=status.HTTP_201_CREATED)
async def add_wan(
    site_id: str,
    body: WanCreate,
    session: SessionDep,
    user: RequireOperator,
    request: Request,
) -> WanRead:
    site = await _get_or_404(session, site_id, user.tenant_id)
    await _reject_duplicate_public_ip(session, user.tenant_id, body.public_ip)
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
    # Fetching the site first (tenant-scoped) before trusting site_id in the
    # WAN comparison below closes the same hole update/delete would otherwise
    # share with every other by-id lookup in this file.
    site = await _get_or_404(session, site_id, user.tenant_id)
    wan = await session.get(Wan, wan_id)
    if wan is None or wan.site_id != site.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such WAN on this site")
    data = body.model_dump(exclude_unset=True)
    if "public_ip" in data:
        await _reject_duplicate_public_ip(
            session, user.tenant_id, data["public_ip"], exclude_wan_id=wan.id
        )
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
    site = await _get_or_404(session, site_id, user.tenant_id)
    wan = await session.get(Wan, wan_id)
    if wan is None or wan.site_id != site.id:
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
        "interface/bridge/port",
        "interface/ethernet",
        "interface/gre",
        "interface/wireguard",
        "ip/address",
        "ip/route",
        "ip/dhcp-client",
        "ip/firewall/address-list",
        "ip/firewall/mangle",
        # The chain that actually drops things. Being able to read mangle but
        # not filter meant the one question worth asking when a tunnel will not
        # establish -- "is this device's own input chain eating IKE?" -- could
        # not be asked through the controller at all. Rules carry no secrets.
        "ip/firewall/filter",
        "ip/firewall/nat",
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


@router.get("/{site_id}/health", response_model=DeviceHealth)
async def site_health(
    site_id: str, session: SessionDep, user: RequireViewer
) -> DeviceHealth:
    """CPU, memory, disk and uptime, straight from the device. Read-only."""
    site = await _get_or_404(session, site_id, user.tenant_id)
    async with open_driver(site) as driver:
        return await read_health(driver)


@router.get("/{site_id}/ports", response_model=list[PortRead])
async def list_ports(
    site_id: str, session: SessionDep, user: RequireViewer
) -> list[PortRead]:
    """The device's interfaces, classified and ready to draw.

    Read-only, and a viewer may run it: it changes nothing and answers the
    question people otherwise answer by walking to the rack.
    """
    site = await _get_or_404(session, site_id, user.tenant_id)
    async with open_driver(site) as driver:
        return await read_ports(driver, site)


@router.get("/{site_id}/device/{device_path:path}")
async def read_device(
    site_id: str, device_path: str, session: SessionDep, user: RequireOperator
) -> list[dict]:
    """Read one RouterOS menu straight from the device. Never writes."""
    normalized = device_path.strip("/")
    if normalized not in READABLE_PATHS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{normalized!r} is not readable through the controller. "
            f"Allowed: {', '.join(sorted(READABLE_PATHS))}",
        )
    site = await _get_or_404(session, site_id, user.tenant_id)
    async with open_driver(site) as driver:
        rows = await driver.read("/" + normalized)
    return [{k: v for k, v in row.items() if k not in _SENSITIVE} for row in rows]


# -- diagnostics -------------------------------------------------------------

# Ping and traceroute are the only endpoints that ask a device to *do*
# something outside the reconciler. They are here rather than behind the
# reconciler because they change nothing: RouterOS ping writes no
# configuration, leaves no rows, and stops when the count runs out.
#
# Operator, not viewer. A viewer reading state is one thing; a viewer aiming
# traffic at an arbitrary address from someone else's router is another, and
# the audit trail below is the reason the distinction is worth keeping.


@router.post("/{site_id}/diagnostics/ping", response_model=PingResult)
async def ping_from_site(
    site_id: str,
    body: PingRequest,
    session: SessionDep,
    request: Request,
    user: RequireOperator,
) -> PingResult:
    """Ping an address from the device, optionally out of one uplink."""
    site = await _get_or_404(session, site_id, user.tenant_id)
    await write_audit(
        session,
        actor=user,
        action="site.ping",
        object_type="site",
        object_id=site.id,
        detail={"target": body.target, "interface": body.interface},
        request=request,
    )
    try:
        async with open_driver(site) as driver:
            return await run_ping(driver, body)
    except DriverError as exc:
        # The device being unreachable is an answer to a diagnostic question,
        # but it is not *this* diagnostic's answer, so it stays an error.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/{site_id}/diagnostics/traceroute", response_model=TracerouteResult)
async def traceroute_from_site(
    site_id: str,
    body: TracerouteRequest,
    session: SessionDep,
    request: Request,
    user: RequireOperator,
) -> TracerouteResult:
    """Trace the path from the device to an address."""
    site = await _get_or_404(session, site_id, user.tenant_id)
    await write_audit(
        session,
        actor=user,
        action="site.traceroute",
        object_type="site",
        object_id=site.id,
        detail={"target": body.target, "interface": body.interface},
        request=request,
    )
    try:
        async with open_driver(site) as driver:
            return await run_traceroute(driver, body)
    except DriverError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.get("/{site_id}/tunnels", response_model=list[TunnelHealth])
async def site_tunnel_health(
    site_id: str, session: SessionDep, user: RequireViewer
) -> list[TunnelHealth]:
    """Every tunnel this device has an end of, and why each one is or is not up.

    Read-only, so a viewer may run it. Returns rows with `error` set rather
    than failing when the device cannot be read: "we could not ask" is a
    different answer from "the tunnel is down", and conflating them is how an
    unreachable controller looks like a total outage.
    """
    site = await _get_or_404(session, site_id, user.tenant_id)

    wan_ids = select(Wan.id).where(Wan.site_id == site.id)
    links = (
        (
            await session.execute(
                select(Link)
                .where(or_(Link.a_wan_id.in_(wan_ids), Link.b_wan_id.in_(wan_ids)))
                # Every one of these is read while building the response, and
                # a lazy load inside an async request is a MissingGreenlet.
                .options(
                    selectinload(Link.fabric),
                    selectinload(Link.a_wan).selectinload(Wan.site),
                    selectinload(Link.b_wan).selectinload(Wan.site),
                )
                .order_by(Link.slug)
            )
        )
        .scalars()
        .all()
    )
    if not links:
        return []

    try:
        async with open_driver(site) as driver:
            return await tunnel_health(driver, site, list(links))
    except DriverError as exc:
        return [
            TunnelHealth(
                link_id=link.id,
                fabric_id=link.fabric_id,
                fabric_name=link.fabric.name,
                slug=link.slug,
                peer_site_id=(
                    link.b_wan.site_id
                    if link.a_wan.site_id == site.id
                    else link.a_wan.site_id
                ),
                peer_site_name=(
                    link.b_wan.site.name
                    if link.a_wan.site_id == site.id
                    else link.a_wan.site.name
                ),
                enabled=link.enabled,
                state=link.state,
                last_error=link.last_error,
                error=str(exc),
            )
            for link in links
        ]
