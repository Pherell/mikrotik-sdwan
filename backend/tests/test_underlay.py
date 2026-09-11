"""Pinning each tunnel's far endpoint to the underlay.

The fabric advertises connected networks, and a site's uplink subnet is one of
its connected networks -- so a member learns, over the overlay, the route to an
address the overlay depends on reaching. Observed on live hardware: Router-1's
route to 10.1.11.229, the far end of its own tunnel, became "via 10.255.0.3
through that tunnel", distance 200.

With one tunnel it survives, because the route is withdrawn along with the
tunnel that carried it. With two it does not: A's endpoint stays reachable via
B, so A can never re-handshake over its own uplink and the two paths stop being
independent -- which is the whole reason for having two.
"""

from __future__ import annotations

from app.models.enums import SiteRole
from app.render.fabric import SiteFabricView, render_fabric
from app.transports import get_transport
from app.transports.base import Endpoint, FabricView, LinkView

WG = get_transport("wireguard")
FABRIC = FabricView(name="core", asn=65000, mtu=1400)


def near(**kw) -> Endpoint:
    base = dict(
        site_name="branch",
        wan_name="wan1",
        interface="ether2",
        tunnel_ip="10.255.0.0",
        public_ip="10.10.10.70",
        gateway="10.10.10.254",
        prefix_len=24,
        cost=1.0,
    )
    return Endpoint(**{**base, **kw})  # type: ignore[arg-type]


def far(**kw) -> Endpoint:
    base = dict(
        site_name="hq",
        wan_name="wan1",
        interface="ether1",
        tunnel_ip="10.255.0.1",
        public_ip="10.1.11.229",
    )
    return Endpoint(**{**base, **kw})  # type: ignore[arg-type]


def view(*links: LinkView) -> SiteFabricView:
    return SiteFabricView(
        fabric=FABRIC,
        site_name="branch",
        role=SiteRole.spoke,
        loopback_ip="10.254.0.1",
        links=list(links),
        local_prefixes=[],
    )


def link(slug: str = "branch-hq", **kw) -> LinkView:
    return LinkView(
        slug=slug,
        fabric=FABRIC,
        local=kw.pop("local", near()),
        remote=kw.pop("remote", far()),
        initiator=True,
        secrets=WG.allocate(),
        listen_port=kw.pop("listen_port", 13231),
    )


def routes(v: SiteFabricView) -> list[dict]:
    for sec in render_fabric(v, WG):
        if sec.path == "/ip/route":
            return [dict(item.props) for item in sec.items]
    return []


def test_an_off_link_endpoint_is_pinned_to_the_uplink_gateway() -> None:
    """A /32 beats any /24 a neighbour advertises whatever its distance,
    because longest-prefix match is decided before distance is."""
    (route,) = routes(view(link()))

    assert route["dst-address"] == "10.1.11.229/32"
    assert route["gateway"] == "10.10.10.254"
    assert route["routing-table"] == "main"


def test_an_on_link_endpoint_is_pinned_to_the_interface_not_the_gateway() -> None:
    """Sending a neighbour's traffic to the gateway so it can send it straight
    back is a hairpin. On its own segment the device should ARP for it."""
    (route,) = routes(view(link(remote=far(public_ip="10.10.10.71"))))

    assert route["dst-address"] == "10.10.10.71/32"
    assert route["gateway"] == "ether2"


def test_an_uplink_with_no_gateway_gets_no_route() -> None:
    """Nothing to point at. Inventing one would be worse than the exposure --
    diagnostics reports it instead."""
    assert routes(view(link(local=near(gateway=None)))) == []


def test_an_unknown_mask_falls_back_to_the_gateway() -> None:
    """An uplink entered by hand has no mask. The gateway form is correct off
    link and merely hairpins on it, so it is the safe guess."""
    (route,) = routes(
        view(link(local=near(prefix_len=None), remote=far(public_ip="10.10.10.71")))
    )

    assert route["gateway"] == "10.10.10.254"


def test_a_peer_with_no_address_is_not_pinned() -> None:
    """A dial-out-only peer has no address to route to; its endpoint is learned
    from the handshake."""
    assert routes(view(link(remote=far(public_ip=None, nat_behind=True)))) == []


def test_two_uplinks_to_one_far_address_are_ordered_by_cost() -> None:
    """A dual-homed site pointing at a single-homed one has two links to the
    same far address. A destination has one best route, so both tunnels take
    the same path whatever is rendered -- but writing only the preferred
    uplink would throw away the backup, and writing them in an arbitrary order
    would put the underlay on an uplink the site ranked second.
    """
    both = view(
        link("via-isp2", local=near(interface="ether3", gateway="10.20.0.1", cost=2.0)),
        link("via-isp1", local=near(cost=1.0)),
    )

    got = [(r["gateway"], r["distance"]) for r in routes(both)]

    assert got == [("10.10.10.254", 1), ("10.20.0.1", 2)]


def test_the_distance_survives_the_menu_ignoring_it() -> None:
    """/ip/route is shared with the policy routes, whose distance netwatch
    owns at runtime -- so the merged menu ignores the field. These rows are
    ordered *by* distance, and left to the section they all reconcile to
    whatever the device already had. Two at one distance is ECMP: observed on
    hardware hashing a handshake across an uplink that could not carry it."""
    both = view(
        link("via-isp1", local=near(cost=1.0)),
        link("via-isp2", local=near(interface="ether3", gateway="10.20.0.1", cost=2.0)),
    )

    section = next(s for s in render_fabric(both, WG) if s.path == "/ip/route")

    assert "distance" in section.ignore
    assert all("distance" in item.enforce for item in section.items)


def test_every_distinct_endpoint_is_pinned() -> None:
    """Pinning only the first would leave the second tunnel exposed, which is
    the case that cannot self-heal."""
    both = view(
        link("to-hq"),
        link("to-dc", remote=far(site_name="dc", public_ip="198.51.100.7")),
    )

    assert {r["dst-address"] for r in routes(both)} == {
        "10.1.11.229/32",
        "198.51.100.7/32",
    }


def test_one_uplink_carrying_two_links_is_written_once() -> None:
    """Two tunnels out the same uplink to the same far address is one route.
    A duplicate row would collide on the section's identity columns and fail
    the whole render."""
    both = view(link("first"), link("second", listen_port=13232))

    assert len(routes(both)) == 1


def test_the_pin_does_not_sort_before_the_tunnels_it_protects() -> None:
    """/ip/route is shared with the policy routes, which name tunnel
    interfaces. merge_sections takes the minimum order per path, so ordering
    this menu early would drag those routes ahead of the interfaces they point
    at and the device would refuse them."""
    sections = {s.path: s for s in render_fabric(view(link()), WG)}

    assert sections["/ip/route"].order >= sections["/interface/wireguard"].order
