"""The firewall rules that make an overlay pass traffic.

Each test here names a way the product was broken before the rules existed.
"""

from __future__ import annotations

from app.render.firewall import (
    BEFORE_INPUT_DROP,
    BEFORE_MASQUERADE,
    FirewallView,
    UplinkNat,
    render_firewall,
)


def view(**kwargs) -> FirewallView:
    base = {
        "site_name": "oslo",
        "peers_by_transport": {"ipsec_gre": {"203.0.113.1"}},
        "uplinks": [UplinkNat(interface="ether1", masquerade=True)],
    }
    return FirewallView(**{**base, **kwargs})


def sections(v: FirewallView) -> dict[str, object]:
    return {s.path: s for s in render_firewall(v)}


# -- steering used to leave traffic unNATted -------------------------------


def test_a_masquerade_rule_is_emitted_for_every_uplink() -> None:
    """The defect: a policy steers traffic onto a second uplink that the site's
    own NAT rules were never written for, so it leaves with a private source
    address and dies upstream."""
    nat = sections(
        view(
            uplinks=[
                UplinkNat(interface="ether1", masquerade=True),
                UplinkNat(interface="ether2", masquerade=True),
            ]
        )
    )["/ip/firewall/nat"]

    masq = [i for i in nat.items if i.props["action"] == "masquerade"]
    assert {i.props["out-interface"] for i in masq} == {"ether1", "ether2"}


def test_an_uplink_marked_no_masquerade_gets_none() -> None:
    """Private transit -- MPLS, a partner link -- must not be NATted, and
    guessing wrong there is worse than not guessing."""
    nat = sections(
        view(
            uplinks=[
                UplinkNat(interface="ether1", masquerade=True),
                UplinkNat(interface="mpls", masquerade=False),
            ]
        )
    )["/ip/firewall/nat"]

    masq = [i for i in nat.items if i.props["action"] == "masquerade"]
    assert {i.props["out-interface"] for i in masq} == {"ether1"}


# -- tunnel traffic used to be masqueraded ---------------------------------


def test_tunnel_traffic_to_a_peer_is_excluded_from_nat() -> None:
    nat = sections(view(peers_by_transport={"ipsec_gre": {"203.0.113.1", "198.51.100.7"}}))[
        "/ip/firewall/nat"
    ]

    bypass = [i for i in nat.items if i.props["action"] == "accept"]
    assert {i.props["dst-address"] for i in bypass} == {"203.0.113.1", "198.51.100.7"}


def test_the_bypass_comes_before_our_own_masquerade() -> None:
    """Section order is device order. A masquerade rule above the bypass would
    catch tunnel traffic on its way out and break the SA."""
    nat = sections(view())["/ip/firewall/nat"]

    actions = [i.props["action"] for i in nat.items]
    assert actions.index("accept") < actions.index("masquerade")


def test_the_whole_block_is_anchored_above_the_operators_masquerade() -> None:
    """Appended at the end it would never match, and the property diff would
    still read clean."""
    nat = sections(view())["/ip/firewall/nat"]

    assert nat.before == BEFORE_MASQUERADE


# -- a default-drop input chain used to block establishment ----------------


def test_ipsec_gre_opens_ike_esp_and_gre() -> None:
    fw = sections(view())["/ip/firewall/filter"]

    by_protocol = {i.props["protocol"]: i.props for i in fw.items}
    assert by_protocol["udp"]["dst-port"] == "500,4500"
    assert "ipsec-esp" in by_protocol
    assert "gre" in by_protocol, "the IPsec policy protects GRE; it must be allowed too"
    assert all(i.props["src-address"] == "203.0.113.1" for i in fw.items)


def test_wireguard_opens_its_configured_port() -> None:
    fw = sections(
        view(
            peers_by_transport={"wireguard": {"203.0.113.1"}},
            wireguard_ports={"51820"},
        )
    )["/ip/firewall/filter"]

    assert [i.props["protocol"] for i in fw.items] == ["udp"]
    assert fw.items[0].props["dst-port"] == "51820"


def test_wireguard_falls_back_to_the_routeros_default_port() -> None:
    fw = sections(view(peers_by_transport={"wireguard": {"203.0.113.1"}}))[
        "/ip/firewall/filter"
    ]

    assert fw.items[0].props["dst-port"] == "13231"


def test_plain_gre_opens_only_gre() -> None:
    fw = sections(view(peers_by_transport={"gre": {"203.0.113.1"}}))["/ip/firewall/filter"]

    assert [i.props["protocol"] for i in fw.items] == ["gre"]


def test_input_rules_are_anchored_above_the_drop() -> None:
    fw = sections(view())["/ip/firewall/filter"]

    assert fw.before == BEFORE_INPUT_DROP


# -- general -----------------------------------------------------------------


def test_a_peer_with_no_address_contributes_nothing() -> None:
    """A CGNAT site dials out. There is no address to except from NAT and
    nothing to accept inbound from."""
    both = sections(view(peers_by_transport={"ipsec_gre": set()}))

    assert [i for i in both["/ip/firewall/nat"].items if i.props["action"] == "accept"] == []
    assert both["/ip/firewall/filter"].items == []


def test_both_sections_are_always_rendered() -> None:
    """A section that is not rendered leaves its rows orphaned on the device
    forever -- the exact bug merge.py exists to prevent."""
    both = sections(FirewallView(site_name="oslo"))

    assert set(both) == {"/ip/firewall/nat", "/ip/firewall/filter"}


def test_every_row_is_ownership_tagged() -> None:
    """A hand-written firewall is read to find the anchor and never touched."""
    for section in render_firewall(view()):
        assert section.owner_tag.startswith("sdwan:")
        for item in section.items:
            assert item.tag.startswith(section.owner_tag)


def test_tags_are_unique_within_a_section() -> None:
    """The comment is the diff identity here, because two rules can share
    chain and action and differ only in what they match."""
    for section in render_firewall(
        view(
            peers_by_transport={"ipsec_gre": {"203.0.113.1", "198.51.100.7"}},
            uplinks=[
                UplinkNat(interface="ether1", masquerade=True),
                UplinkNat(interface="ether2", masquerade=True),
            ],
        )
    ):
        tags = [i.tag for i in section.items]
        assert len(tags) == len(set(tags)), tags
