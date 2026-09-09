"""MSS clamping: a real defect, not a feature.

GRE adds 24 bytes and IPsec transport-mode ESP adds ~40 more, which is why the
tunnel MTU is 1400. A TCP endpoint that sets DF and never sees the ICMP
Fragmentation Needed reply blackholes silently -- small pages load, large ones
hang. Without a clamp rule on every tunnel interface, that is exactly what
happens.
"""

from __future__ import annotations

from app.models.enums import SiteRole
from app.render.fabric import SiteFabricView, render_fabric
from app.transports import get_transport
from app.transports.base import Endpoint, FabricView, LinkView

IPSEC = get_transport("ipsec_gre")

FABRIC = FabricView(name="core", asn=65001, mtu=1400)


def _link(slug: str, local_ip: str, remote_ip: str) -> LinkView:
    return LinkView(
        slug=slug,
        fabric=FABRIC,
        local=Endpoint(
            site_name="oslo",
            wan_name="wan1",
            interface="ether1",
            tunnel_ip=local_ip,
            public_ip="203.0.113.1",
        ),
        remote=Endpoint(
            site_name="bergen",
            wan_name="wan1",
            interface="ether1",
            tunnel_ip=remote_ip,
            public_ip="203.0.113.2",
        ),
        initiator=True,
    )


def view(**kwargs) -> SiteFabricView:
    base = {
        "fabric": FABRIC,
        "site_name": "oslo",
        "role": SiteRole.spoke,
        "loopback_ip": None,
        "links": [_link("oslo-bergen", "10.255.0.0", "10.255.0.1")],
        "local_prefixes": [],
    }
    return SiteFabricView(**{**base, **kwargs})


def _mangle(v: SiteFabricView) -> object:
    sections = {s.path: s for s in render_fabric(v, IPSEC)}
    return sections["/ip/firewall/mangle"]


def test_every_tunnel_gets_a_clamp_rule() -> None:
    mangle = _mangle(view())
    assert len(mangle.items) == 1
    rule = mangle.items[0].props
    assert rule["chain"] == "forward"
    assert rule["protocol"] == "tcp"
    assert rule["tcp-flags"] == "syn"
    assert rule["action"] == "change-mss"
    assert rule["new-mss"] == "clamp-to-pmtu"
    assert rule["out-interface"] == IPSEC.interface_name("oslo-bergen")


def test_a_site_with_two_tunnels_gets_a_rule_for_each() -> None:
    mangle = _mangle(
        view(
            links=[
                _link("oslo-bergen", "10.255.0.0", "10.255.0.1"),
                _link("oslo-trondheim", "10.255.0.2", "10.255.0.3"),
            ]
        )
    )
    interfaces = {i.props["out-interface"] for i in mangle.items}
    assert interfaces == {
        IPSEC.interface_name("oslo-bergen"),
        IPSEC.interface_name("oslo-trondheim"),
    }


def test_removing_every_link_removes_the_clamp_rule_too() -> None:
    """Always render the section, even empty: this is how a removed tunnel's
    clamp rule is removed with it, the same reason render_firewall's two
    sections are unconditional."""
    mangle = _mangle(view(links=[]))
    assert mangle.items == []


def test_the_clamp_rule_is_tagged_for_this_site_and_fabric() -> None:
    mangle = _mangle(view())
    tag = mangle.items[0].tag
    assert tag.startswith("sdwan:")
    assert "core" in tag
    assert "oslo" in tag
