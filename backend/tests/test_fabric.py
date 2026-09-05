"""Fabric expansion, addressing, and the IPsec/GRE transport."""

from __future__ import annotations

import pytest

from app.fabric.allocate import (
    PoolExhausted,
    allocate_link_subnet,
    allocate_loopback,
    capacity,
    endpoints_of,
)
from app.fabric.expand import expand, link_slug, members_to_wans, wanted_pairs
from app.models.enums import SiteRole, Topology, Transport
from app.models.fabric import Fabric, FabricMember
from app.models.site import Site, Wan
from app.transports import get_transport
from app.transports.base import (
    Endpoint,
    FabricView,
    LinkView,
    TransportError,
    choose_initiator,
    validate_pair,
)

IPSEC = get_transport("ipsec_gre")


# -- fixtures ---------------------------------------------------------------


def site(name: str, role: SiteRole, wans: list[tuple[str, str | None, bool]], **kw) -> Site:
    """wans: (name, public_ip, nat_behind)."""
    s = Site(
        id=f"site-{name}",
        name=name,
        mgmt_host="10.0.0.1",
        username="admin",
        role=role,
        verify_tls=False,
        loopback_ip=kw.pop("loopback_ip", f"10.254.0.{abs(hash(name)) % 200 + 1}"),
        local_prefixes=kw.pop("local_prefixes", []),
        capabilities={"ros_major": kw.pop("ros_major", 7)},
        tenant_id="default",
    )
    s.wans = [
        Wan(
            id=f"wan-{name}-{wname}",
            site_id=s.id,
            name=wname,
            interface="ether1",
            public_ip=public,
            nat_behind=nat,
            enabled=True,
        )
        for wname, public, nat in wans
    ]
    return s


def fabric(**kw) -> Fabric:
    return Fabric(
        id="fab-1",
        name="core",
        transport=Transport.ipsec_gre,
        topology=kw.pop("topology", Topology.hub_spoke),
        ip_pool=kw.pop("ip_pool", "10.255.0.0/24"),
        loopback_pool="10.254.0.0/24",
        asn=65000,
        mtu=1400,
        transport_params={},
        tenant_id="default",
        **kw,
    )


def members(f: Fabric, *sites: Site) -> list[FabricMember]:
    out = []
    for s in sites:
        m = FabricMember(id=f"mem-{s.name}", fabric_id=f.id, site_id=s.id, enabled=True)
        m.site = s
        out.append(m)
    return out


# -- allocation -------------------------------------------------------------


def test_link_subnets_are_slash_31s() -> None:
    subnet = allocate_link_subnet("10.255.0.0/24", taken=[])
    assert str(subnet) == "10.255.0.0/31"
    assert endpoints_of(subnet) == ("10.255.0.0", "10.255.0.1")


def test_allocation_skips_what_is_already_taken() -> None:
    subnet = allocate_link_subnet("10.255.0.0/24", taken=["10.255.0.0/31", "10.255.0.2/31"])
    assert str(subnet) == "10.255.0.4/31"


def test_pool_capacity() -> None:
    assert capacity("10.255.0.0/24") == 128
    assert capacity("10.255.0.0/16") == 32768


def test_exhausted_pool_says_what_to_do() -> None:
    from ipaddress import ip_network

    taken = [str(n) for n in ip_network("10.255.0.0/28").subnets(new_prefix=31)]
    with pytest.raises(PoolExhausted, match="Widen fabric.ip_pool"):
        allocate_link_subnet("10.255.0.0/28", taken=taken)


def test_loopbacks_use_every_address_in_the_pool() -> None:
    assert allocate_loopback("10.254.0.0/24", taken=[]) == "10.254.0.1"
    assert allocate_loopback("10.254.0.0/24", taken=["10.254.0.1"]) == "10.254.0.2"


# -- initiator selection ----------------------------------------------------


def test_natted_endpoint_always_dials() -> None:
    natted = Endpoint("spoke", "wan1", "ether1", "10.255.0.0", public_ip=None, nat_behind=True)
    public = Endpoint("hub", "wan1", "ether1", "10.255.0.1", public_ip="198.51.100.5")

    assert choose_initiator(natted, public) == "a"
    assert choose_initiator(public, natted) == "b"


def test_initiator_choice_is_stable_when_both_are_reachable() -> None:
    a = Endpoint("alpha", "wan1", "ether1", "10.255.0.0", public_ip="198.51.100.1")
    b = Endpoint("bravo", "wan1", "ether1", "10.255.0.1", public_ip="198.51.100.2")

    # Same answer regardless of argument order -- otherwise the two sides
    # disagree on every render and flap the tunnel.
    assert choose_initiator(a, b) == "a"
    assert choose_initiator(b, a) == "b"


def test_two_natted_endpoints_cannot_be_linked() -> None:
    a = Endpoint("s1", "wan1", "ether1", "10.255.0.0", public_ip=None, nat_behind=True)
    b = Endpoint("s2", "wan1", "ether1", "10.255.0.1", public_ip=None, nat_behind=True)

    with pytest.raises(TransportError, match="through a hub"):
        validate_pair(a, b, IPSEC)


def test_transport_rejects_an_unsupported_routeros_version() -> None:
    from app.transports.base import TransportDriver

    class OnlyV7:
        name = "v7only"
        supported_ros = {7}
        requires_reachable_responder = True
        supports_dynamic_mesh = True

        def allocate(self) -> dict[str, str]:
            return {}

        def render(self, link):  # pragma: no cover
            return []

    driver: TransportDriver = OnlyV7()  # type: ignore[assignment]
    old = Endpoint("legacy", "wan1", "ether1", "10.255.0.0", public_ip="198.51.100.1", ros_major=6)
    new = Endpoint("modern", "wan1", "ether1", "10.255.0.1", public_ip="198.51.100.2")

    with pytest.raises(TransportError, match="RouterOS 6"):
        validate_pair(old, new, driver)


# -- topology ---------------------------------------------------------------


def test_hub_spoke_does_not_link_spokes_to_each_other() -> None:
    f = fabric(topology=Topology.hub_spoke)
    hub = site("hub1", SiteRole.hub, [("wan1", "198.51.100.5", False)])
    s1 = site("spoke1", SiteRole.spoke, [("wan1", "203.0.113.1", False)])
    s2 = site("spoke2", SiteRole.spoke, [("wan1", "203.0.113.2", False)])

    wans = members_to_wans(members(f, hub, s1, s2))
    pairs = wanted_pairs(wans, Topology.hub_spoke)

    names = {tuple(sorted((a.site_name, b.site_name))) for a, b in pairs}
    assert names == {("hub1", "spoke1"), ("hub1", "spoke2")}


def test_full_mesh_links_everything() -> None:
    f = fabric(topology=Topology.full_mesh)
    sites = [
        site("a", SiteRole.spoke, [("wan1", "203.0.113.1", False)]),
        site("b", SiteRole.spoke, [("wan1", "203.0.113.2", False)]),
        site("c", SiteRole.spoke, [("wan1", "203.0.113.3", False)]),
    ]
    pairs = wanted_pairs(members_to_wans(members(f, *sites)), Topology.full_mesh)
    assert len(pairs) == 3


def test_a_site_is_never_tunnelled_to_itself() -> None:
    f = fabric(topology=Topology.full_mesh)
    dual = site(
        "dual",
        SiteRole.spoke,
        [("wan1", "203.0.113.1", False), ("wan2", "203.0.113.9", False)],
    )

    pairs = wanted_pairs(members_to_wans(members(f, dual)), Topology.full_mesh)
    assert pairs == []


def test_dual_homed_spoke_gets_a_tunnel_per_uplink() -> None:
    f = fabric()
    hub = site("hub1", SiteRole.hub, [("wan1", "198.51.100.5", False)])
    spoke = site(
        "spoke1",
        SiteRole.spoke,
        [("wan1", "203.0.113.1", False), ("wan2", None, True)],
    )

    result = expand(f, members(f, hub, spoke), existing=[], transport=IPSEC)

    assert len(result.created) == 2
    assert {link.subnet for link in result.created} == {"10.255.0.0/31", "10.255.0.2/31"}


# -- expansion --------------------------------------------------------------


def test_expansion_is_idempotent() -> None:
    """Re-expanding must keep every link, not renumber the overlay."""
    f = fabric()
    hub = site("hub1", SiteRole.hub, [("wan1", "198.51.100.5", False)])
    spoke = site("spoke1", SiteRole.spoke, [("wan1", "203.0.113.1", False)])
    m = members(f, hub, spoke)

    first = expand(f, m, existing=[], transport=IPSEC)
    assert len(first.created) == 1

    second = expand(f, m, existing=first.created, transport=IPSEC)
    assert second.created == []
    assert len(second.kept) == 1
    assert second.removed == []


def test_removing_a_member_removes_its_links() -> None:
    f = fabric()
    hub = site("hub1", SiteRole.hub, [("wan1", "198.51.100.5", False)])
    spoke = site("spoke1", SiteRole.spoke, [("wan1", "203.0.113.1", False)])

    full = members(f, hub, spoke)
    existing = expand(f, full, existing=[], transport=IPSEC).created

    shrunk = expand(f, members(f, hub), existing=existing, transport=IPSEC)

    assert shrunk.created == []
    assert len(shrunk.removed) == 1


def test_an_impossible_pair_is_skipped_not_fatal() -> None:
    """One unlinkable pair must not stop the rest of the fabric being built."""
    f = fabric(topology=Topology.full_mesh)
    natted_hub = site("hub1", SiteRole.hub, [("wan1", None, True)])
    natted_spoke = site("spoke1", SiteRole.spoke, [("wan1", None, True)])
    public_spoke = site("spoke2", SiteRole.spoke, [("wan1", "203.0.113.2", False)])

    result = expand(
        f, members(f, natted_hub, natted_spoke, public_spoke), existing=[], transport=IPSEC
    )

    assert len(result.skipped) == 1
    assert "through a hub" in result.skipped[0][2]
    # The two links involving the reachable spoke were still created.
    assert len(result.created) == 2


def test_new_links_reuse_free_addresses_after_a_removal() -> None:
    f = fabric()
    hub = site("hub1", SiteRole.hub, [("wan1", "198.51.100.5", False)])
    s1 = site("spoke1", SiteRole.spoke, [("wan1", "203.0.113.1", False)])
    s2 = site("spoke2", SiteRole.spoke, [("wan1", "203.0.113.2", False)])

    existing = expand(f, members(f, hub, s1, s2), existing=[], transport=IPSEC).created
    keep = [link for link in existing if "spoke2" in link.slug]

    s3 = site("spoke3", SiteRole.spoke, [("wan1", "203.0.113.3", False)])
    result = expand(f, members(f, hub, s2, s3), existing=keep, transport=IPSEC)

    # spoke1's /31 is free again and gets handed to spoke3.
    assert len(result.created) == 1
    assert result.created[0].subnet == "10.255.0.0/31"


def test_slug_fits_in_a_routeros_interface_name() -> None:
    f = fabric()
    a = site("a-very-long-site-name-indeed", SiteRole.hub, [("wan1", "198.51.100.5", False)])
    b = site("another-extremely-long-name", SiteRole.spoke, [("wan1", "203.0.113.1", False)])

    result = expand(f, members(f, a, b), existing=[], transport=IPSEC)
    slug = result.created[0].slug

    assert len(slug) <= 26
    # gre- prefix plus slug must still fit RouterOS's 31-character limit.
    assert len(f"gre-{slug}") <= 31


def test_slug_is_order_independent() -> None:
    f = fabric()
    a = site("alpha", SiteRole.hub, [("wan1", "198.51.100.5", False)])
    b = site("bravo", SiteRole.spoke, [("wan1", "203.0.113.1", False)])
    refs = members_to_wans(members(f, a, b))

    assert link_slug(refs[0], refs[1]) == link_slug(refs[1], refs[0])


# -- ipsec_gre transport ----------------------------------------------------


def make_link(**kw) -> LinkView:
    local = kw.pop(
        "local",
        Endpoint("spoke1", "wan1", "ether1", "10.255.0.1", public_ip="203.0.113.1"),
    )
    remote = kw.pop(
        "remote",
        Endpoint("hub1", "wan1", "ether1", "10.255.0.0", public_ip="198.51.100.5"),
    )
    return LinkView(
        slug="hub1-wan1-spoke1-wan1",
        fabric=FabricView(name="core", asn=65000, mtu=1400, params=kw.pop("params", {})),
        local=local,
        remote=remote,
        initiator=kw.pop("initiator", True),
        secrets=kw.pop("secrets", {"psk": "test-psk-value"}),
    )


def test_psk_is_long_and_unique() -> None:
    a = IPSEC.allocate()["psk"]
    b = IPSEC.allocate()["psk"]
    assert a != b
    assert len(a) >= 48


def test_full_mode_renders_the_whole_ipsec_stack() -> None:
    sections = {s.path: s for s in IPSEC.render(make_link())}

    assert set(sections) == {
        "/ip/ipsec/profile",
        "/ip/ipsec/proposal",
        "/ip/ipsec/peer",
        "/ip/ipsec/identity",
        "/ip/ipsec/policy",
        "/interface/gre",
        "/ip/address",
    }


def test_ipsec_policy_encrypts_only_gre_between_the_two_uplinks() -> None:
    policy = next(s for s in IPSEC.render(make_link()) if s.path == "/ip/ipsec/policy")
    props = policy.items[0].props

    assert props["protocol"] == "gre"
    assert props["src-address"] == "203.0.113.1/32"
    assert props["dst-address"] == "198.51.100.5/32"
    assert props["tunnel"] is False       # transport mode: GRE does the tunnelling
    assert props["level"] == "unique"     # a shared SA would cross links


def test_initiator_dials_and_responder_listens() -> None:
    dialer = next(s for s in IPSEC.render(make_link(initiator=True)) if s.path == "/ip/ipsec/peer")
    assert dialer.items[0].props["passive"] is False
    assert dialer.items[0].props["address"] == "198.51.100.5"

    listener = next(
        s for s in IPSEC.render(make_link(initiator=False)) if s.path == "/ip/ipsec/peer"
    )
    assert listener.items[0].props["passive"] is True


def test_responder_accepts_from_anywhere_when_the_peer_has_no_fixed_address() -> None:
    """A CGNAT spoke's source address is unknowable, so the hub cannot pin it."""
    roaming = Endpoint("spoke9", "wan1", "ether1", "10.255.0.1", public_ip=None, nat_behind=True)
    peer = next(
        s
        for s in IPSEC.render(make_link(remote=roaming, initiator=False))
        if s.path == "/ip/ipsec/peer"
    )
    assert peer.items[0].props["address"] == "0.0.0.0/0"


def test_psk_is_write_once_so_it_never_diffs() -> None:
    identity = next(
        s for s in IPSEC.render(make_link()) if s.path == "/ip/ipsec/identity"
    )
    assert "secret" in identity.write_once


def test_aead_cipher_omits_a_separate_auth_algorithm() -> None:
    """RouterOS rejects auth-algorithms alongside GCM."""
    proposal = next(
        s for s in IPSEC.render(make_link()) if s.path == "/ip/ipsec/proposal"
    )
    props = proposal.items[0].props

    assert props["enc-algorithms"] == "aes-256-gcm"
    assert "auth-algorithms" not in props


def test_cbc_cipher_keeps_its_auth_algorithm() -> None:
    proposal = next(
        s
        for s in IPSEC.render(make_link(params={"enc_algorithm": "aes-256-cbc"}))
        if s.path == "/ip/ipsec/proposal"
    )
    assert proposal.items[0].props["auth-algorithms"] == "sha256"


def test_phase1_does_not_use_an_aead_name() -> None:
    """IKE in RouterOS takes aes-256, not aes-256-gcm."""
    profile = next(s for s in IPSEC.render(make_link()) if s.path == "/ip/ipsec/profile")
    assert profile.items[0].props["enc-algorithm"] == "aes-256"


def test_gre_has_keepalives_so_dead_tunnels_go_down() -> None:
    gre = next(s for s in IPSEC.render(make_link()) if s.path == "/interface/gre")
    props = gre.items[0].props

    assert props["keepalive"] == "10s,3"
    assert props["mtu"] == 1400
    assert props["name"] == "gre-hub1-wan1-spoke1-wan1"
    assert len(props["name"]) <= 31


def test_tunnel_address_is_a_slash_31() -> None:
    address = next(s for s in IPSEC.render(make_link()) if s.path == "/ip/address")
    assert address.items[0].props["address"] == "10.255.0.1/31"


def test_simple_mode_collapses_to_gre_with_an_ipsec_secret() -> None:
    sections = {s.path: s for s in IPSEC.render(make_link(params={"mode": "simple"}))}

    assert set(sections) == {"/interface/gre", "/ip/address"}
    gre = sections["/interface/gre"]
    assert gre.items[0].props["ipsec-secret"] == "test-psk-value"
    assert "ipsec-secret" in gre.write_once


def test_every_rendered_section_is_ownership_tagged() -> None:
    for sec in IPSEC.render(make_link()):
        assert sec.owner_tag.startswith("sdwan:fabric:core:")
        for item in sec.items:
            assert item.tag.startswith("sdwan:fabric:core:")
