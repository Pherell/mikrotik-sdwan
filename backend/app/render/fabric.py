"""Render a site's fabric participation: tunnels plus iBGP over them.

Hubs are route reflectors, spokes are clients, and everything shares one AS.
Loopbacks are advertised so a spoke-to-spoke tunnel can later be built to a
stable address rather than to whichever uplink happened to be up.

**No netwatch here.** Liveness is already covered twice -- GRE keepalives take a
tunnel down in ~30s, and BGP's hold timer clears the routes over it. Netwatch
exists for the different job of spotting a path that is up but degraded, and
that threshold belongs to an SLA profile, so `app.render.policy` owns the menu.
Rendering it in both places produced two rows claiming the same probe host and
made the device flap between them on alternate applies.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.drivers.base import ConfigItem, ConfigSection
from app.models.enums import SiteRole
from app.render.engine import owner_tag, section
from app.transports.base import Endpoint, FabricView, LinkView, TransportDriver

# RouterOS 7 BGP roles. A hub reflects between spokes that have no session with
# each other; a spoke is a plain client.
_ROLE = {SiteRole.hub: "ibgp-rr", SiteRole.spoke: "ibgp-rr-client"}


@dataclass(slots=True)
class SiteFabricView:
    """One site's slice of a fabric: its role, its loopback, and its links."""

    fabric: FabricView
    site_name: str
    role: SiteRole
    loopback_ip: str | None
    links: list[LinkView]
    local_prefixes: list[str]


def render_fabric(view: SiteFabricView, transport: TransportDriver) -> list[ConfigSection]:
    """Every section this site needs for this fabric."""
    sections: list[ConfigSection] = []
    for link in view.links:
        sections.extend(transport.render(link))
    sections.extend(_bgp(view))
    return sections


def _bgp(view: SiteFabricView) -> list[ConfigSection]:
    scope = owner_tag("fabric", view.fabric.name, view.site_name)
    if not view.loopback_ip:
        # Without a router-id BGP will not start, and guessing one from an
        # uplink address makes the session identity move when a WAN flaps.
        return []

    template_tag = f"{scope}:bgp-template"
    template_name = f"sdwan-{view.fabric.name}"[:31]

    template = section(
        "/routing/bgp/template",
        "routing",
        owner=template_tag,
        key=("name",),
        items=[
            ConfigItem(
                props={
                    "name": template_name,
                    "as": view.fabric.asn,
                    "router-id": view.loopback_ip,
                    "address-families": "ip",
                    "output.redistribute": "connected",
                    "hold-time": "30s",
                    "keepalive-time": "10s",
                },
                tag=template_tag,
            )
        ],
    )

    conn_tag = f"{scope}:bgp"
    connections = section(
        "/routing/bgp/connection",
        "routing",
        owner=conn_tag,
        key=("name",),
        # RouterOS reports these back on established sessions; they are state,
        # not intent.
        ignore=("remote.id", "remote.capabilities", "local.role", "established"),
        items=[
            ConfigItem(
                props={
                    "name": f"bgp-{link.slug}"[:31],
                    "templates": template_name,
                    "remote.address": link.remote.tunnel_ip,
                    "remote.as": view.fabric.asn,
                    "local.address": link.local.tunnel_ip,
                    "local.role": _ROLE[view.role],
                    "routing-table": "main",
                },
                tag=f"{conn_tag}:{link.slug}",
            )
            for link in view.links
        ],
    )

    networks = section(
        "/routing/bgp/network",
        "routing",
        owner=f"{scope}:bgp-network",
        key=("network",),
        items=[
            ConfigItem(
                props={"network": prefix, "synchronize": False},
                tag=f"{scope}:bgp-network",
            )
            for prefix in sorted(set(view.local_prefixes))
        ],
    )

    return [template, connections, networks]


def link_view(
    fabric: FabricView,
    slug: str,
    local: Endpoint,
    remote: Endpoint,
    *,
    initiator: bool,
    secrets: dict[str, str],
) -> LinkView:
    return LinkView(
        slug=slug,
        fabric=fabric,
        local=local,
        remote=remote,
        initiator=initiator,
        secrets=secrets,
    )
