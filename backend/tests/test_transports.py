"""The transport plugins beyond ipsec_gre.

The point of the abstraction is that adding an overlay is a driver, not a fork.
These tests check each one renders coherently and that the shared contract --
ownership tags, capability gating, /31 addressing -- holds for all of them.
"""

from __future__ import annotations

import base64

import pytest

from app.transports import available, get_transport
from app.transports.base import Endpoint, FabricView, LinkView, TransportError, validate_pair
from app.transports.wireguard import generate_keypair


def link(**kw) -> LinkView:
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
        secrets=kw.pop("secrets", {}),
    )


ALL = [get_transport(n) for n in available()]


# -- the shared contract ----------------------------------------------------


@pytest.mark.parametrize("driver", ALL, ids=lambda d: d.name)
def test_every_transport_tags_everything_it_renders(driver) -> None:
    """Untagged rows are invisible to the reconciler and can never be cleaned
    up. This is the invariant that keeps the controller safe on a shared
    device."""
    view = link(secrets=driver.allocate())

    for sec in driver.render(view):
        assert sec.owner_tag.startswith("sdwan:"), f"{driver.name}: {sec.path}"
        for item in sec.items:
            assert item.tag.startswith("sdwan:"), f"{driver.name}: {sec.path}"


@pytest.mark.parametrize("driver", ALL, ids=lambda d: d.name)
def test_every_rendered_path_is_declared_as_owned(driver) -> None:
    """A path a transport writes but does not declare will never be swept when
    a link is deleted, and the tunnel is orphaned forever."""
    rendered = {s.path for s in driver.render(link(secrets=driver.allocate()))}
    undeclared = rendered - set(driver.owned_paths)

    assert not undeclared, f"{driver.name} writes but does not declare: {undeclared}"


@pytest.mark.parametrize("driver", ALL, ids=lambda d: d.name)
def test_interface_names_fit_routeros(driver) -> None:
    for sec in driver.render(link(secrets=driver.allocate())):
        for item in sec.items:
            if "name" in item.props:
                assert len(str(item.props["name"])) <= 31, f"{driver.name}: {sec.path}"


@pytest.mark.parametrize("driver", ALL, ids=lambda d: d.name)
def test_rendering_is_deterministic(driver) -> None:
    """Two renders of the same intent must be byte-identical, or every apply
    produces a diff and the controller never converges."""
    secrets = driver.allocate()
    first = [(s.path, [i.props for i in s.items]) for s in driver.render(link(secrets=secrets))]
    second = [(s.path, [i.props for i in s.items]) for s in driver.render(link(secrets=secrets))]

    assert first == second


# -- capability gating ------------------------------------------------------


@pytest.mark.parametrize("name", ["wireguard", "vxlan"])
def test_v7_only_transports_reject_a_v6_site(name: str) -> None:
    driver = get_transport(name)
    old = Endpoint("legacy", "wan1", "ether1", "10.255.0.0", public_ip="198.51.100.1", ros_major=6)
    new = Endpoint("modern", "wan1", "ether1", "10.255.0.1", public_ip="198.51.100.2")

    with pytest.raises(TransportError, match="RouterOS 6"):
        validate_pair(old, new, driver)


@pytest.mark.parametrize("name", ["ipsec_gre", "gre", "ipip", "eoip"])
def test_v6_capable_transports_accept_a_v6_site(name: str) -> None:
    driver = get_transport(name)
    old = Endpoint("legacy", "wan1", "ether1", "10.255.0.0", public_ip="198.51.100.1", ros_major=6)
    new = Endpoint("modern", "wan1", "ether1", "10.255.0.1", public_ip="198.51.100.2")

    validate_pair(old, new, driver)  # must not raise


# -- wireguard --------------------------------------------------------------


def test_x25519_matches_the_rfc7748_vector() -> None:
    """RFC 7748 6.1. If this drifts, every WireGuard tunnel silently fails to
    authenticate and the cause is not obvious from the device."""
    from app.transports.wireguard import _x25519_base

    private = bytearray(
        bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    )
    private[0] &= 248
    private[31] &= 127
    private[31] |= 64

    public = _x25519_base(bytes(private))

    assert public.hex() == "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"


def test_keypairs_are_unique_and_well_formed() -> None:
    a_priv, a_pub = generate_keypair()
    b_priv, b_pub = generate_keypair()

    assert a_priv != b_priv and a_pub != b_pub
    for key in (a_priv, a_pub, b_priv, b_pub):
        assert len(base64.b64decode(key)) == 32


def test_wireguard_gives_each_side_its_own_private_key() -> None:
    driver = get_transport("wireguard")
    secrets = driver.allocate()

    spoke = link(secrets=secrets)  # local=spoke1, remote=hub1
    hub = link(
        secrets=secrets,
        local=Endpoint("hub1", "wan1", "ether1", "10.255.0.0", public_ip="198.51.100.5"),
        remote=Endpoint("spoke1", "wan1", "ether1", "10.255.0.1", public_ip="203.0.113.1"),
        initiator=False,
    )

    spoke_iface = next(s for s in driver.render(spoke) if s.path == "/interface/wireguard")
    hub_iface = next(s for s in driver.render(hub) if s.path == "/interface/wireguard")
    spoke_peer = next(
        s for s in driver.render(spoke) if s.path == "/interface/wireguard/peers"
    )
    hub_peer = next(s for s in driver.render(hub) if s.path == "/interface/wireguard/peers")

    spoke_private = spoke_iface.items[0].props["private-key"]
    hub_private = hub_iface.items[0].props["private-key"]
    assert spoke_private != hub_private

    # Each side's peer entry carries the *other* side's public key.
    assert hub_peer.items[0].props["public-key"] == _public_of(spoke_private, secrets)
    assert spoke_peer.items[0].props["public-key"] == _public_of(hub_private, secrets)


def _public_of(private: str, secrets: dict[str, str]) -> str:
    return secrets["a_public"] if private == secrets["a_private"] else secrets["b_public"]


def test_wireguard_key_assignment_does_not_move_when_the_initiator_changes() -> None:
    """Initiator flips when a WAN gains or loses a public address. If the keys
    moved with it, the tunnel would break on an unrelated change."""
    driver = get_transport("wireguard")
    secrets = driver.allocate()

    dialing = link(secrets=secrets, initiator=True)
    listening = link(secrets=secrets, initiator=False)

    a = next(s for s in driver.render(dialing) if s.path == "/interface/wireguard")
    b = next(s for s in driver.render(listening) if s.path == "/interface/wireguard")

    assert a.items[0].props["private-key"] == b.items[0].props["private-key"]


def test_wireguard_allows_only_the_overlay_subnet() -> None:
    """allowed-address is WireGuard's routing table. 0.0.0.0/0 here would
    swallow every packet on the device."""
    driver = get_transport("wireguard")
    peer = next(
        s
        for s in driver.render(link(secrets=driver.allocate()))
        if s.path == "/interface/wireguard/peers"
    )
    assert peer.items[0].props["allowed-address"] == "10.255.0.0/31"


def test_wireguard_keeps_a_natted_peer_alive() -> None:
    driver = get_transport("wireguard")
    natted = Endpoint("spoke9", "wan1", "ether1", "10.255.0.1", public_ip=None, nat_behind=True)

    peer = next(
        s
        for s in driver.render(link(local=natted, secrets=driver.allocate()))
        if s.path == "/interface/wireguard/peers"
    )
    assert peer.items[0].props["persistent-keepalive"] == "25s"


def test_wireguard_omits_keepalive_when_neither_side_is_natted() -> None:
    driver = get_transport("wireguard")
    peer = next(
        s
        for s in driver.render(link(secrets=driver.allocate()))
        if s.path == "/interface/wireguard/peers"
    )
    assert "persistent-keepalive" not in peer.items[0].props


def test_wireguard_private_keys_are_write_once() -> None:
    driver = get_transport("wireguard")
    iface = next(
        s
        for s in driver.render(link(secrets=driver.allocate()))
        if s.path == "/interface/wireguard"
    )
    assert "private-key" in iface.write_once


# -- plain tunnels ----------------------------------------------------------


@pytest.mark.parametrize(("name", "path"), [("gre", "/interface/gre"), ("ipip", "/interface/ipip")])
def test_plain_tunnels_render_an_interface_and_an_address(name: str, path: str) -> None:
    driver = get_transport(name)
    sections = {s.path: s for s in driver.render(link())}

    assert set(sections) == {path, "/ip/address"}
    assert sections[path].items[0].props["remote-address"] == "198.51.100.5"
    assert sections[path].items[0].props["keepalive"] == "10s,3"
    assert sections["/ip/address"].items[0].props["address"] == "10.255.0.1/31"


@pytest.mark.parametrize("name", ["gre", "ipip"])
def test_plain_tunnels_generate_no_key_material(name: str) -> None:
    """They offer no confidentiality, and pretending otherwise with a unused
    key would be worse than being plain about it."""
    assert get_transport(name).allocate() == {}
    assert get_transport(name).encrypted is False


# -- layer 2 ----------------------------------------------------------------


def test_vxlan_rides_the_overlay_not_the_underlay() -> None:
    """Binding to the public address would put the stretched LAN on the
    internet in the clear."""
    driver = get_transport("vxlan")
    sections = {s.path: s for s in driver.render(link())}

    vxlan = sections["/interface/vxlan"].items[0].props
    vtep = sections["/interface/vxlan/vteps"].items[0].props

    assert vxlan["local-address"] == "10.255.0.1"   # tunnel IP, not 203.0.113.1
    assert vtep["remote-ip"] == "10.255.0.0"


def test_vxlan_leaves_headroom_in_the_mtu() -> None:
    driver = get_transport("vxlan")
    vxlan = next(s for s in driver.render(link()) if s.path == "/interface/vxlan")
    assert vxlan.items[0].props["mtu"] == 1350  # 1400 parent minus VXLAN overhead


def test_eoip_rides_the_overlay_too() -> None:
    driver = get_transport("eoip")
    eoip = next(s for s in driver.render(link()) if s.path == "/interface/eoip")
    props = eoip.items[0].props

    assert props["local-address"] == "10.255.0.1"
    assert props["remote-address"] == "10.255.0.0"
    assert props["keepalive"] == "10s,3"


@pytest.mark.parametrize("name", ["vxlan", "eoip"])
def test_l2_stretch_lands_on_a_bridge_with_stp_on(name: str) -> None:
    """A loop on a stretched segment is an outage at every site at once."""
    driver = get_transport(name)
    sections = {s.path: s for s in driver.render(link())}

    bridge = sections["/interface/bridge"].items[0].props
    port = sections["/interface/bridge/port"].items[0].props

    assert bridge["protocol-mode"] == "rstp"
    assert port["bridge"] == "sdwan-l2"
    assert port["interface"].startswith(("vxl-", "eoip-"))


@pytest.mark.parametrize("name", ["vxlan", "eoip"])
def test_l2_stretch_declares_its_encrypted_parent(name: str) -> None:
    assert get_transport(name).parent_transport == "ipsec_gre"
    assert get_transport(name).encrypted is False
