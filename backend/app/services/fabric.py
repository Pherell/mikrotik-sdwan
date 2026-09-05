"""Fabric orchestration: expand a topology into links, then render a device.

This is the only place that knows both the database and the transport layer.
Expansion persists links and their generated secrets; rendering turns the links
touching one site into the sections that site needs.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.drivers.base import OWNER_PREFIX, ConfigSection
from app.fabric.allocate import allocate_loopback
from app.fabric.expand import ExpansionResult, expand
from app.models.enums import SiteRole
from app.models.fabric import Fabric, FabricMember, Link
from app.models.policy import Policy
from app.models.site import Site, Wan
from app.reconcile.merge import merge_sections
from app.render.engine import ORDER
from app.render.fabric import SiteFabricView, render_fabric
from app.render.policy import PathOption, SitePolicyView, render_policies
from app.render.site import render_site
from app.security import SecretBox
from app.transports.base import (
    Endpoint,
    FabricView,
    LinkView,
    TransportDriver,
    available,
    get_transport,
)

log = logging.getLogger(__name__)


# -- secrets ----------------------------------------------------------------


def link_secrets(link: Link, box: SecretBox | None = None) -> dict[str, str]:
    if not link.secrets_enc:
        return {}
    return json.loads((box or SecretBox()).decrypt(link.secrets_enc))


def set_link_secrets(link: Link, values: dict[str, str], box: SecretBox | None = None) -> None:
    link.secrets_enc = (box or SecretBox()).encrypt(json.dumps(values))


# -- expansion --------------------------------------------------------------


async def load_fabric(session: AsyncSession, fabric_id: str) -> Fabric | None:
    return await session.scalar(
        select(Fabric)
        .where(Fabric.id == fabric_id)
        .options(
            selectinload(Fabric.members)
            .selectinload(FabricMember.site)
            .selectinload(Site.wans),
            selectinload(Fabric.links),
        )
    )


async def expand_fabric(session: AsyncSession, fabric: Fabric) -> ExpansionResult:
    """Reconcile the fabric's link set, allocating addresses and keys.

    Nothing is pushed to a device here -- that happens when each affected site
    is applied.
    """
    transport = get_transport(fabric.transport)
    box = SecretBox()

    _assign_loopbacks(fabric)

    result = expand(fabric, list(fabric.members), list(fabric.links), transport)

    for link in result.created:
        # Keys are generated once, at link creation, and never regenerated --
        # rotating a PSK under a live tunnel drops it.
        set_link_secrets(link, transport.allocate(), box)
        session.add(link)

    for link in result.removed:
        await session.delete(link)

    await session.flush()
    return result


def _assign_loopbacks(fabric: Fabric) -> None:
    """Give every member a loopback from the fabric pool if it lacks one.

    BGP needs a stable router-id that does not move when an uplink flaps.
    """
    taken = [
        m.loopback_ip or m.site.loopback_ip
        for m in fabric.members
        if (m.loopback_ip or m.site.loopback_ip)
    ]
    for member in fabric.members:
        if member.loopback_ip or member.site.loopback_ip:
            continue
        address = allocate_loopback(fabric.loopback_pool, taken)
        member.loopback_ip = address
        taken.append(address)


# -- rendering --------------------------------------------------------------


async def links_for_site(session: AsyncSession, site_id: str) -> list[Link]:
    """Every enabled link with one end on this site."""
    wan_ids = list(
        await session.scalars(select(Wan.id).where(Wan.site_id == site_id))
    )
    if not wan_ids:
        return []
    return list(
        await session.scalars(
            select(Link)
            .where(
                Link.enabled.is_(True),
                (Link.a_wan_id.in_(wan_ids)) | (Link.b_wan_id.in_(wan_ids)),
            )
            .options(selectinload(Link.a_wan), selectinload(Link.b_wan))
        )
    )


async def render_device(session: AsyncSession, site: Site) -> list[ConfigSection]:
    """Everything the controller owns on this device.

    Baseline site config plus, for each fabric it belongs to, the tunnels and
    routing that fabric needs. Sections targeting the same menu are merged, so
    one diff covers every managed row in it.
    """
    sections = list(render_site(site))

    # Menus the controller manages are always represented, even with nothing in
    # them. Without this, deleting a link renders no section for its menu, the
    # orphaned rows match nothing, and the tunnel runs forever.
    sections.extend(_cleanup_sections())

    links = await links_for_site(session, site.id)
    by_fabric: dict[str, list[Link]] = {}
    for link in links:
        by_fabric.setdefault(link.fabric_id, []).append(link)

    box = SecretBox()
    for fabric_id, fabric_links in by_fabric.items():
        fabric = await load_fabric(session, fabric_id)
        if fabric is None or not fabric.enabled:
            continue
        transport = get_transport(fabric.transport)
        view = await _site_fabric_view(session, site, fabric, fabric_links, box)
        if view.links:
            sections.extend(render_fabric(view, transport))

    sections.extend(render_policies(await policy_view(session, site)))

    return merge_sections(sections)


def _cleanup_sections() -> list[ConfigSection]:
    """Empty, fabric-scoped sections for every menu any transport can write.

    Merging widens the ownership tag to the shared scope, so these turn into
    "remove every sdwan-owned row here that intent no longer asks for".
    """
    paths: dict[str, tuple[str, ...]] = {}
    for name in available():
        for path in getattr(get_transport(name), "owned_paths", ()):
            paths.setdefault(path, _KEYS.get(path, ()))
    # Routing and monitoring are rendered by app.render.fabric rather than by a
    # transport, so they are listed here directly.
    for path, key in _EXTRA_OWNED.items():
        paths.setdefault(path, key)

    return [
        ConfigSection(
            path=path,
            items=[],
            owner_tag=OWNER_PREFIX + "fabric:",
            key=key,
            order=ORDER["tunnel"],
        )
        for path, key in paths.items()
    ]


# Identity columns must match whatever the renderers use for the same menu, or
# merge_sections refuses to combine them -- which is the point: a mismatch is a
# bug, not something to paper over.
_KEYS: dict[str, tuple[str, ...]] = {
    "/ip/ipsec/profile": ("name",),
    "/ip/ipsec/proposal": ("name",),
    "/ip/ipsec/peer": ("name",),
    "/ip/ipsec/identity": ("peer",),
    "/ip/ipsec/policy": ("src-address", "dst-address", "protocol"),
    "/interface/gre": ("name",),
    "/interface/ipip": ("name",),
    "/interface/wireguard": ("name",),
    "/interface/wireguard/peers": ("interface", "public-key"),
    "/interface/vxlan": ("name",),
    "/interface/vxlan/vteps": ("interface", "remote-ip"),
    "/interface/eoip": ("name",),
    "/interface/bridge": ("name",),
    "/interface/bridge/port": ("bridge", "interface"),
    "/ip/address": ("address",),
}

_EXTRA_OWNED: dict[str, tuple[str, ...]] = {
    "/ip/firewall/mangle": ("comment",),
    "/routing/table": ("name",),
    "/ip/route": ("dst-address", "gateway", "routing-table"),
    "/routing/bgp/template": ("name",),
    "/routing/bgp/connection": ("name",),
    "/routing/bgp/network": ("network",),
    "/tool/netwatch": ("host",),
}


async def _site_fabric_view(
    session: AsyncSession,
    site: Site,
    fabric: Fabric,
    links: list[Link],
    box: SecretBox,
) -> SiteFabricView:
    membership = next((m for m in fabric.members if m.site_id == site.id), None)
    role = (membership.role_override if membership else None) or site.role
    loopback = (membership.loopback_ip if membership else None) or site.loopback_ip

    fabric_view = FabricView(
        name=fabric.name,
        asn=fabric.asn,
        mtu=fabric.mtu,
        params=dict(fabric.transport_params or {}),
    )

    site_wan_ids = {w.id for w in site.wans}
    views: list[LinkView] = []
    for link in links:
        local_is_a = link.a_wan_id in site_wan_ids
        local_wan, remote_wan = (
            (link.a_wan, link.b_wan) if local_is_a else (link.b_wan, link.a_wan)
        )
        local_ip, remote_ip = (
            (link.a_tunnel_ip, link.b_tunnel_ip)
            if local_is_a
            else (link.b_tunnel_ip, link.a_tunnel_ip)
        )
        remote_site = await session.get(Site, remote_wan.site_id)
        if remote_site is None:  # pragma: no cover - FK guarantees this
            continue

        views.append(
            LinkView(
                slug=link.slug,
                fabric=fabric_view,
                local=_endpoint(site, local_wan, local_ip, loopback),
                remote=_endpoint(
                    remote_site,
                    remote_wan,
                    remote_ip,
                    _member_loopback(fabric, remote_site),
                ),
                # link.initiator names the side that dials in a/b terms; convert
                # it to "is this side the dialler".
                initiator=(link.initiator == "a") == local_is_a,
                secrets=link_secrets(link, box),
            )
        )

    return SiteFabricView(
        fabric=fabric_view,
        site_name=site.name,
        role=SiteRole(role),
        loopback_ip=loopback,
        links=views,
        local_prefixes=list(site.local_prefixes or []),
    )


def _member_loopback(fabric: Fabric, site: Site) -> str | None:
    member = next((m for m in fabric.members if m.site_id == site.id), None)
    return (member.loopback_ip if member else None) or site.loopback_ip


def _endpoint(site: Site, wan: Wan, tunnel_ip: str, loopback: str | None) -> Endpoint:
    caps = site.capabilities or {}
    return Endpoint(
        site_name=site.name,
        wan_name=wan.name,
        interface=wan.interface,
        tunnel_ip=tunnel_ip,
        public_ip=wan.public_ip,
        nat_behind=wan.nat_behind,
        loopback_ip=loopback,
        ros_major=int(caps.get("ros_major") or 7),
    )


def transport_for(fabric: Fabric) -> TransportDriver:
    return get_transport(fabric.transport)


async def reallocate_secrets(
    session: AsyncSession, fabric: Fabric, transport: TransportDriver
) -> int:
    """Regenerate every link's key material for a new transport.

    Switching a fabric from IPsec to WireGuard leaves each link holding a PSK
    and no keypair, so the new renderer would emit empty keys and every tunnel
    would fail to authenticate. Re-keying here is safe because the migration
    tears the old tunnels down anyway.
    """
    box = SecretBox()
    links = list(
        await session.scalars(select(Link).where(Link.fabric_id == fabric.id))
    )
    for link in links:
        set_link_secrets(link, transport.allocate(), box)
        link.state = "pending"
    await session.flush()
    return len(links)


# -- policies ---------------------------------------------------------------


async def policy_view(session: AsyncSession, site: Site) -> SitePolicyView:
    """Which policies apply here, and which uplinks they can steer onto.

    Next hops are the far ends of this site's tunnels, not WAN gateways: policy
    traffic must ride the overlay, or steering would push it onto the internet
    in the clear.
    """
    policies = [
        p
        for p in await session.scalars(
            select(Policy).where(Policy.tenant_id == site.tenant_id, Policy.enabled.is_(True))
        )
        if not p.site_ids or site.id in p.site_ids
    ]

    links = await links_for_site(session, site.id)
    site_wan_ids = {w.id: w for w in site.wans}

    hops_by_wan: dict[str, list[str]] = {}
    for link in links:
        if link.a_wan_id in site_wan_ids:
            hops_by_wan.setdefault(link.a_wan_id, []).append(link.b_tunnel_ip)
        elif link.b_wan_id in site_wan_ids:
            hops_by_wan.setdefault(link.b_wan_id, []).append(link.a_tunnel_ip)

    paths_by_tag: dict[str, list[PathOption]] = {}
    for wan in site.wans:
        if not wan.enabled:
            continue
        option = PathOption(
            wan_name=wan.name,
            interface=wan.interface,
            gateway=wan.gateway,
            cost=wan.cost,
            next_hops=sorted(hops_by_wan.get(wan.id, [])),
        )
        # A WAN is reachable by any of its tags, and always by its own name, so
        # a policy can name one uplink directly without inventing a tag for it.
        for tag in {*(wan.tags or {}).keys(), wan.name}:
            paths_by_tag.setdefault(tag, []).append(option)

    return SitePolicyView(
        site_name=site.name, policies=policies, paths_by_tag=paths_by_tag
    )
