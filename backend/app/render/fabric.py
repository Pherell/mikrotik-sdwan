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
# RouterOS 7.24 accepts only ibgp / ebgp / ibgp-rr as local.role -- there is no
# "ibgp-rr-client" value (the code assumed one; real hardware rejects it). The
# hub is the route reflector; a spoke is a plain internal peer and still
# receives the routes the RR reflects to it. Spoke-to-spoke reachability under
# hub_spoke_dynamic comes from an on-demand direct tunnel, not from RR
# reflection, so a plain-ibgp spoke does not lose connectivity here.
_ROLE = {SiteRole.hub: "ibgp-rr", SiteRole.spoke: "ibgp"}


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
    sections.append(_mss_clamp(view, transport))
    return sections


def _mss_clamp(view: SiteFabricView, transport: TransportDriver) -> ConfigSection:
    """Clamp TCP MSS to PMTU on every tunnel this site has an end of.

    GRE adds 24 bytes and IPsec transport-mode ESP adds ~40 more, which is why
    the tunnel MTU is 1400 rather than 1500. A TCP endpoint that sets DF and
    never sees the ICMP Fragmentation Needed reply -- common behind a firewall
    that drops ICMP -- blackholes silently: small pages load, large ones hang
    forever. This is the single most common SD-WAN-over-IPsec support call,
    and it was a defect in what already shipped, not a missing feature.

    Always returns a section, even with no links: an empty one is how a
    tunnel that has been removed has its clamp rule removed with it, the same
    reason ``render_firewall`` always returns both of its sections.
    """
    scope = owner_tag("fabric", view.fabric.name, view.site_name, "mss") + ":"
    # key=("comment",), matching render.policy's own mangle section on this
    # same path: merge_sections requires every renderer sharing a device menu
    # to agree on the identity columns, since they end up diffed as one. The
    # comment is the tag itself, exactly as render.policy sets it -- not
    # left for a later stage to fill in.
    return section(
        "/ip/firewall/mangle",
        "firewall",
        owner=scope,
        key=("comment",),
        items=[
            ConfigItem(
                props={
                    "chain": "forward",
                    "protocol": "tcp",
                    "tcp-flags": "syn",
                    "action": "change-mss",
                    "new-mss": "clamp-to-pmtu",
                    "out-interface": transport.interface_name(link.slug),
                    "comment": f"{scope}{link.slug}",
                },
                tag=f"{scope}{link.slug}",
            )
            for link in view.links
        ],
    )


def _bgp(view: SiteFabricView) -> list[ConfigSection]:
    scope = owner_tag("fabric", view.fabric.name, view.site_name)
    if not view.loopback_ip:
        # Without a router-id BGP will not start, and guessing one from an
        # uplink address makes the session identity move when a WAN flaps.
        return []

    bgp_name = f"sdwan-{view.fabric.name}"[:31]
    template_name = bgp_name

    # RouterOS 7.20+ requires an explicit BGP instance, and that is where the
    # router-id lives now -- it is not a property of the template or the
    # connection on this version (both reject "router-id" as unknown, and a
    # connection with no instance is refused with "missing instance"). The
    # connection references this instance by name.
    instance_tag = f"{scope}:bgp-instance"
    instance = section(
        "/routing/bgp/instance",
        "bgp_instance",
        owner=instance_tag,
        key=("name",),
        items=[
            ConfigItem(
                props={
                    "name": bgp_name,
                    "as": view.fabric.asn,
                    "router-id": view.loopback_ip,
                },
                tag=instance_tag,
            )
        ],
    )

    template_tag = f"{scope}:bgp-template"
    template = section(
        "/routing/bgp/template",
        "bgp_template",
        owner=template_tag,
        key=("name",),
        items=[
            ConfigItem(
                props={
                    "name": template_name,
                    "as": view.fabric.asn,
                    # ROS 7.24 names the address-family property "afi", not
                    # "address-families"; the latter is rejected as unknown.
                    "afi": "ip",
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
                    "instance": bgp_name,
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

    return [instance, template, connections, networks]


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
