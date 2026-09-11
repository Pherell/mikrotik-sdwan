"""Ping, traceroute, and the join that says why a tunnel is down.

The parsing tests carry most of the weight. RouterOS reports times as
"11ms391us" and a failed probe as a status with no time field at all, so a
naive float() reads a total outage as a run of very fast pings.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from app.drivers.ros7_rest import Ros7RestDriver
from app.models.enums import Topology, Transport
from app.models.fabric import Fabric, Link
from app.models.site import Site, Wan
from app.schemas.diagnostics import PingRequest, TracerouteRequest
from app.services.diagnostics import (
    parse_duration_ms,
    run_ping,
    run_traceroute,
    tunnel_health,
)
from tests.fakeros.server import FakeRouterOS


async def _driver(fake: FakeRouterOS) -> Ros7RestDriver:
    d = Ros7RestDriver(
        "test-router", "admin", "secret", transport=httpx.ASGITransport(app=fake.app)
    )
    await d.connect()
    return d


# -- durations --------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("11ms391us", 11.391),
        ("1s200ms", 1200.0),
        ("4ms", 4.0),
        ("500us", 0.5),
        ("1m30s", 90_000.0),
        ("12.5", 12.5),  # a build that reports bare milliseconds
        ("0", 0.0),
    ],
)
def test_routeros_durations_become_milliseconds(raw: str, expected: float) -> None:
    assert parse_duration_ms(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "abc", "timeout"])
def test_unparseable_durations_are_none_not_zero(raw: object) -> None:
    """Zero would read as an instant reply. None reads as no reply."""
    assert parse_duration_ms(raw) is None


# -- target validation ------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        "8.8.8.8; /system reboot",
        'x" ;:put [/system resource get]',
        "a b",
        "$(id)",
        "../../etc/passwd",
        "",
        "-leading-hyphen",
    ],
)
def test_a_target_that_could_end_a_console_command_is_refused(target: str) -> None:
    """The SSH driver builds a console line out of this. One rule in front of
    both drivers is the only version that stays true when a third arrives."""
    with pytest.raises(ValidationError):
        PingRequest(target=target)


@pytest.mark.parametrize(
    "target", ["8.8.8.8", "example.com", "2001:db8::1", "10.255.0.1", "fe80::1%ether1"]
)
def test_ordinary_targets_are_accepted(target: str) -> None:
    assert PingRequest(target=target).target == target


def test_a_hostile_interface_name_is_refused_too() -> None:
    with pytest.raises(ValidationError):
        PingRequest(target="8.8.8.8", interface="ether1; /quit")


def test_count_is_capped() -> None:
    """This runs inline and holds a device connection open for its duration."""
    with pytest.raises(ValidationError):
        PingRequest(target="8.8.8.8", count=500)


# -- ping -------------------------------------------------------------------


async def test_ping_summarises_a_reachable_target() -> None:
    fake = FakeRouterOS(password="secret")
    driver = await _driver(fake)
    try:
        result = await run_ping(driver, PingRequest(target="8.8.8.8", count=3))
    finally:
        await driver.close()

    assert result.sent == 3
    assert result.received == 3
    assert result.loss_percent == 0.0
    assert result.min_ms == 1.391
    assert result.max_ms == 3.393
    assert [p.ttl for p in result.probes] == [64, 64, 64]
    assert all(p.status is None for p in result.probes)


async def test_a_timed_out_probe_has_no_time_and_counts_as_loss() -> None:
    fake = FakeRouterOS(password="secret", reachable={"8.8.8.8"})
    driver = await _driver(fake)
    try:
        result = await run_ping(driver, PingRequest(target="10.9.9.9", count=4))
    finally:
        await driver.close()

    assert result.sent == 4
    assert result.received == 0
    assert result.loss_percent == 100.0
    # The interesting half: no time at all, rather than a time of zero.
    assert all(p.time_ms is None for p in result.probes)
    assert all(p.status == "timeout" for p in result.probes)
    assert result.avg_ms is None


async def test_ping_out_of_one_uplink_passes_the_interface_through() -> None:
    """Pinging out of a chosen uplink is the only way to tell "the internet is
    down" from "this one uplink is down"."""
    fake = FakeRouterOS(password="secret")
    driver = await _driver(fake)
    try:
        result = await run_ping(
            driver, PingRequest(target="8.8.8.8", count=1, interface="ether2")
        )
    finally:
        await driver.close()

    path, body = fake.commands[-1]
    assert path == "ping"
    assert body["interface"] == "ether2"
    assert result.interface == "ether2"


async def test_a_run_that_returned_no_rows_is_total_loss_not_a_zero_divide() -> None:
    """A run cut short reports nothing at all. Averaging over it must not
    raise, and it must not read as a clean 0% loss."""

    class Silent:
        async def run(self, command, params=None):
            return []

    result = await run_ping(Silent(), PingRequest(target="8.8.8.8", count=4))
    assert result.sent == 0
    assert result.loss_percent == 100.0
    assert result.avg_ms is None


# -- traceroute -------------------------------------------------------------


async def test_traceroute_numbers_hops_and_parses_times() -> None:
    fake = FakeRouterOS(password="secret")
    driver = await _driver(fake)
    try:
        result = await run_traceroute(driver, TracerouteRequest(target="8.8.8.8"))
    finally:
        await driver.close()

    assert [h.hop for h in result.hops] == [1, 2]
    assert result.hops[0].address == "10.0.0.1"
    assert result.hops[0].avg_ms == 1.3
    assert result.hops[1].address == "8.8.8.8"
    assert result.hops[1].avg_ms == 12.0


async def test_repeated_probe_rounds_do_not_invent_hops() -> None:
    """RouterOS streams a traceroute: it re-emits every hop each round until
    the duration expires. Numbering rows in arrival order turns an 11-hop path
    probed twice into 22 hops, with "hop 12" showing the first router again.
    The rounds have to collapse onto the hop they belong to.
    """
    fake = FakeRouterOS(password="secret")  # emits two rounds, as ROS does
    driver = await _driver(fake)
    try:
        result = await run_traceroute(driver, TracerouteRequest(target="8.8.8.8"))
    finally:
        await driver.close()

    assert [h.hop for h in result.hops] == [1, 2]
    # No hop repeats an earlier hop's address.
    addresses = [h.address for h in result.hops]
    assert len(addresses) == len(set(addresses))
    # The surviving sample is the latest round: ROS's stats are cumulative.
    assert result.hops[0].sent == 2


async def test_a_hop_that_never_answers_keeps_its_place_in_the_path() -> None:
    """Dropping unanswered hops would renumber every hop after them."""
    fake = FakeRouterOS(password="secret", reachable=set())
    driver = await _driver(fake)
    try:
        result = await run_traceroute(driver, TracerouteRequest(target="8.8.8.8"))
    finally:
        await driver.close()

    assert len(result.hops) == 2
    assert result.hops[1].hop == 2
    assert result.hops[1].address is None
    assert result.hops[1].status == "timeout"
    assert result.hops[1].loss_percent == 100.0


async def test_traceroute_is_bounded_by_wall_clock() -> None:
    """Without a duration RouterOS traceroute never returns."""
    fake = FakeRouterOS(password="secret")
    driver = await _driver(fake)
    try:
        await run_traceroute(driver, TracerouteRequest(target="8.8.8.8", seconds=3))
    finally:
        await driver.close()

    path, body = fake.commands[-1]
    assert path == "tool/traceroute"
    assert body["duration"] == "3"


# -- tunnel health ----------------------------------------------------------


def _fabric_and_link(transport: Transport = Transport.ipsec_gre) -> tuple:
    near_site = Site(id="site-a", name="branch", mgmt_host="10.0.0.2", tenant_id="t")
    far_site = Site(id="site-b", name="hq", mgmt_host="10.0.0.3", tenant_id="t")
    near = Wan(id="wan-a", site_id="site-a", name="wan1", interface="ether1",
               public_ip="203.0.113.10")
    far = Wan(id="wan-b", site_id="site-b", name="wan1", interface="ether1",
              public_ip="198.51.100.20")
    near.site = near_site
    far.site = far_site

    fabric = Fabric(id="fab-1", name="core", tenant_id="t", transport=transport,
                    topology=Topology.hub_spoke, asn=65000, mtu=1400)
    link = Link(
        id="link-1", fabric_id="fab-1", a_wan_id="wan-a", b_wan_id="wan-b",
        slug="branch-hq", a_tunnel_ip="10.255.0.0", b_tunnel_ip="10.255.0.1",
        subnet="10.255.0.0/31", enabled=True, state="applied",
    )
    link.fabric = fabric
    link.a_wan = near
    link.b_wan = far
    return near_site, link


async def test_a_tunnel_never_pushed_says_so() -> None:
    """Absent is not down. Building a tunnel network only works the tunnel out;
    until the device is applied there is nothing on it to be down."""
    near_site, link = _fabric_and_link()
    fake = FakeRouterOS(password="secret", menus={"interface": []})
    driver = await _driver(fake)
    try:
        (row,) = await tunnel_health(driver, near_site, [link])
    finally:
        await driver.close()

    assert "has not been written to the device yet" in (row.diagnosis or "")
    assert "Apply" in (row.diagnosis or "")


async def test_a_tunnel_sourced_off_its_own_path_is_named_as_such() -> None:
    """The one that cost an afternoon: the peer is sourced from ether1, but the
    route to the far end leaves by ether2. IKE then carries the wrong source
    and is dropped upstream as spoofed -- nothing arrives, nothing is logged,
    and every layer just reports "down"."""
    near_site, link = _fabric_and_link()
    fake = FakeRouterOS(
        password="secret",
        menus={
            "interface": [{"name": "gre-branch-hq", "type": "gre", "running": False}],
            "ip/ipsec/active-peers": [],
            "ip/ipsec/peer": [
                {"name": "peer-branch-hq", "address": "198.51.100.20/32",
                 "local-address": "192.168.203.175"}
            ],
            "ip/address": [
                {"address": "192.168.203.175/24", "interface": "ether1"},
                {"address": "10.10.10.70/24", "interface": "ether2"},
            ],
            "ip/route": [
                {"dst-address": "0.0.0.0/0", "gateway": "10.10.10.254",
                 "immediate-gw": "10.10.10.254%ether2", "active": True},
                {"dst-address": "192.168.203.0/24", "gateway": "ether1",
                 "immediate-gw": "ether1", "active": True},
            ],
        },
    )
    driver = await _driver(fake)
    try:
        (row,) = await tunnel_health(driver, near_site, [link])
    finally:
        await driver.close()

    d = row.diagnosis or ""
    assert "192.168.203.175" in d and "ether1" in d
    assert "ether2" in d
    assert "spoofed" in d


async def test_two_links_to_one_far_address_are_diagnosed_separately() -> None:
    """A dual-homed site pointing at a single-homed one has two links to the
    *same* far address. Identifying the peer by that address reports one
    tunnel's source for both -- and sends the operator after the wrong uplink.
    """
    near_site, link = _fabric_and_link()
    fake = FakeRouterOS(
        password="secret",
        menus={
            "interface": [{"name": "gre-branch-hq", "type": "gre", "running": False}],
            "ip/ipsec/active-peers": [],
            "ip/ipsec/peer": [
                # A different link's peer, to the same far end, listed first.
                {"name": "peer-other-link", "address": "198.51.100.20/32",
                 "local-address": "192.168.203.175"},
                {"name": "peer-branch-hq", "address": "198.51.100.20/32",
                 "local-address": "10.10.10.70"},
            ],
            "ip/address": [
                {"address": "192.168.203.175/24", "interface": "ether1"},
                {"address": "10.10.10.70/24", "interface": "ether2"},
            ],
            "ip/route": [
                {"dst-address": "0.0.0.0/0", "gateway": "10.10.10.254",
                 "immediate-gw": "10.10.10.254%ether2", "active": True},
            ],
        },
    )
    driver = await _driver(fake)
    try:
        (row,) = await tunnel_health(driver, near_site, [link])
    finally:
        await driver.close()

    # This link is sourced from ether2, which *is* the egress -- so the answer
    # is "no IKE", not a sourcing complaint about the other link's uplink.
    d = row.diagnosis or ""
    assert "spoofed" not in d
    assert "500" in d


async def test_no_ike_at_all_points_at_the_ports_that_carry_it() -> None:
    """Sourcing is fine, so the next thing worth checking is whether IKE can
    reach the far end at all. Ping proves nothing about UDP 500."""
    near_site, link = _fabric_and_link()
    fake = FakeRouterOS(
        password="secret",
        menus={
            "interface": [{"name": "gre-branch-hq", "type": "gre", "running": False}],
            "ip/ipsec/active-peers": [],
            "ip/ipsec/peer": [
                {"name": "peer-branch-hq", "address": "198.51.100.20/32",
                 "local-address": "10.10.10.70"}
            ],
            "ip/address": [{"address": "10.10.10.70/24", "interface": "ether2"}],
            "ip/route": [
                {"dst-address": "0.0.0.0/0", "gateway": "10.10.10.254",
                 "immediate-gw": "10.10.10.254%ether2", "active": True},
            ],
        },
    )
    driver = await _driver(fake)
    try:
        (row,) = await tunnel_health(driver, near_site, [link])
    finally:
        await driver.close()

    d = row.diagnosis or ""
    assert "500" in d and "4500" in d
    assert "ping" in d.lower()
    # Name the first device in the path, because "somewhere between them" is
    # not something an operator can act on.
    assert "ether2" in d and "10.10.10.254" in d

    # And hand over the rules for it. That router is not one the controller
    # manages, so the exact commands are the most it can do.
    assert row.transit_rules, "expected rules for the device in the path"
    joined = "\n".join(row.transit_rules)
    assert "chain=forward" in joined
    assert "protocol=udp dst-port=500,4500" in joined
    assert "protocol=ipsec-esp" in joined and "protocol=gre" in joined
    # Both directions: a firewall in the middle sees both.
    # The uplink's configured endpoint, which is what the packets carry.
    assert "src-address=203.0.113.10 dst-address=198.51.100.20" in joined
    assert "src-address=198.51.100.20 dst-address=203.0.113.10" in joined


async def test_a_healthy_tunnel_is_not_diagnosed_at_all() -> None:
    near_site, link = _fabric_and_link()
    fake = FakeRouterOS(
        password="secret",
        menus={
            "interface": [{"name": "gre-branch-hq", "type": "gre", "running": True}],
            "ip/ipsec/active-peers": [
                {"remote-address": "198.51.100.20", "state": "established"}
            ],
            "routing/bgp/session": [
                {"remote.address": "10.255.0.1", "established": True}
            ],
        },
    )
    driver = await _driver(fake)
    try:
        (row,) = await tunnel_health(driver, near_site, [link])
    finally:
        await driver.close()

    assert row.diagnosis is None


async def test_a_healthy_tunnel_reports_every_layer_up() -> None:
    near_site, link = _fabric_and_link()
    fake = FakeRouterOS(
        password="secret",
        menus={
            "interface": [{"name": "gre-branch-hq", "type": "gre", "running": True}],
            "ip/ipsec/active-peers": [
                {"remote-address": "198.51.100.20", "state": "established",
                 "uptime": "2h11m"}
            ],
            "routing/bgp/session": [
                {"remote.address": "10.255.0.1", "established": True,
                 "prefix-count": 4}
            ],
            "tool/netwatch": [
                {"host": "10.255.0.1", "status": "up", "loss-percent": "0",
                 "rtt-avg": "8ms120us"}
            ],
        },
    )
    driver = await _driver(fake)
    try:
        rows = await tunnel_health(driver, near_site, [link])
    finally:
        await driver.close()

    assert len(rows) == 1
    row = rows[0]
    assert row.peer_site_name == "hq"
    assert row.interface == "gre-branch-hq"
    assert row.interface_running is True
    assert row.ipsec_established is True
    assert "2h11m" in (row.ipsec_detail or "")
    assert row.bgp_established is True
    assert row.bgp_detail == "established, 4 prefixes"
    assert row.netwatch_status == "up"
    assert row.netwatch_latency_ms == 8.12


async def test_ipsec_up_but_bgp_down_is_the_answer_worth_having() -> None:
    """The case that sends people to SSH: the tunnel is up and nothing routes."""
    near_site, link = _fabric_and_link()
    fake = FakeRouterOS(
        password="secret",
        menus={
            "interface": [{"name": "gre-branch-hq", "type": "gre", "running": True}],
            "ip/ipsec/active-peers": [
                {"remote-address": "198.51.100.20", "state": "established"}
            ],
            "routing/bgp/session": [],
        },
    )
    driver = await _driver(fake)
    try:
        (row,) = await tunnel_health(driver, near_site, [link])
    finally:
        await driver.close()

    assert row.interface_running is True
    assert row.ipsec_established is True
    assert row.bgp_established is False
    assert "no BGP session" in (row.bgp_detail or "")


async def test_a_never_applied_tunnel_is_unknown_not_down() -> None:
    """An interface the device does not have has not failed; it was never made."""
    near_site, link = _fabric_and_link()
    fake = FakeRouterOS(password="secret", menus={"interface": []})
    driver = await _driver(fake)
    try:
        (row,) = await tunnel_health(driver, near_site, [link])
    finally:
        await driver.close()

    assert row.interface == "gre-branch-hq"
    assert row.interface_running is None


async def test_a_transport_without_ipsec_reports_unknown_rather_than_down() -> None:
    """A plain GRE fabric has no SA to be up or down. Reporting "no security
    association" in red would be a wrong answer to a question never asked --
    and the far end has a public address, so the lookup would otherwise run
    and find nothing."""
    near_site, link = _fabric_and_link(Transport.gre)
    fake = FakeRouterOS(
        password="secret",
        menus={"interface": [{"name": "gre-branch-hq", "type": "gre", "running": True}]},
    )
    driver = await _driver(fake)
    try:
        (row,) = await tunnel_health(driver, near_site, [link])
    finally:
        await driver.close()

    assert row.ipsec_established is None
    assert row.ipsec_detail is None


# -- wireguard ---------------------------------------------------------------


async def test_a_wireguard_listener_beaten_to_its_port_says_which_one_took_it() -> None:
    """The worst failure shape there is. RouterOS accepts a second WireGuard
    interface on a port that is taken, leaves it running=false, and says
    nothing -- no error at apply, nothing in the log. Verified on 7.24.2."""
    near_site, link = _fabric_and_link(Transport.wireguard)
    link.listen_port = 13231
    fake = FakeRouterOS(
        password="secret",
        menus={
            "interface": [
                {"name": "wg-branch-hq", "type": "wireguard", "running": False},
                {"name": "wg-other", "type": "wireguard", "running": True},
            ],
            "interface/wireguard": [
                {"name": "wg-other", "listen-port": "13231", "running": True},
                {"name": "wg-branch-hq", "listen-port": "13231", "running": False},
            ],
            "interface/wireguard/peers": [],
        },
    )
    driver = await _driver(fake)
    try:
        (row,) = await tunnel_health(driver, near_site, [link])
    finally:
        await driver.close()

    assert "wg-other" in (row.diagnosis or "")
    assert "13231" in (row.diagnosis or "")
    assert row.listen_port == 13231


async def test_a_wireguard_tunnel_with_no_handshake_names_its_own_port() -> None:
    """Quoting the fabric's base port would send the operator to open a port
    this tunnel does not use -- every link listens on a different one."""
    near_site, link = _fabric_and_link(Transport.wireguard)
    link.listen_port = 13233
    fake = FakeRouterOS(
        password="secret",
        menus={
            "interface": [
                {"name": "wg-branch-hq", "type": "wireguard", "running": True}
            ],
            "interface/wireguard": [
                {"name": "wg-branch-hq", "listen-port": "13233", "running": True}
            ],
            "interface/wireguard/peers": [
                {"interface": "wg-branch-hq", "public-key": "k", "rx": "0", "tx": "0"}
            ],
            "ip/address": [{"address": "203.0.113.10/24", "interface": "ether1"}],
            "ip/route": [
                {"dst-address": "0.0.0.0/0", "gateway": "203.0.113.1",
                 "active": "true", "disabled": "false"}
            ],
        },
    )
    driver = await _driver(fake)
    try:
        (row,) = await tunnel_health(driver, near_site, [link])
    finally:
        await driver.close()

    assert "No WireGuard handshake" in (row.diagnosis or "")
    assert "UDP 13233" in (row.diagnosis or "")
    # The same phrase that offers paste-able transit rules for ipsec.
    assert "permitted all the way" in (row.diagnosis or "")
    assert row.transit_rules and any("13233" in r for r in row.transit_rules)


async def test_bytes_received_are_not_mistaken_for_a_handshake() -> None:
    """Taken verbatim from a live RouterOS 7.24.2 peer that had never
    completed a handshake: rx was 148 and current-endpoint-address was
    populated, because bytes arriving and a handshake finishing are different
    things. Reading either as success reports a dead tunnel as healthy and
    sends the operator off to check BGP."""
    near_site, link = _fabric_and_link(Transport.wireguard)
    link.listen_port = 13231
    fake = FakeRouterOS(
        password="secret",
        menus={
            "interface": [
                {"name": "wg-branch-hq", "type": "wireguard", "running": True}
            ],
            "interface/wireguard": [
                {"name": "wg-branch-hq", "listen-port": "13231", "running": True}
            ],
            "interface/wireguard/peers": [
                {"interface": "wg-branch-hq", "public-key": "k", "rx": "148",
                 "tx": "2608", "current-endpoint-address": "103.210.35.189"}
            ],
        },
    )
    driver = await _driver(fake)
    try:
        (row,) = await tunnel_health(driver, near_site, [link])
    finally:
        await driver.close()

    assert "No WireGuard handshake" in (row.diagnosis or "")
    assert "BGP" not in (row.diagnosis or "")
    # And the mismatch itself is worth reporting: the far end is answering
    # from an address nobody dialled.
    assert "103.210.35.189" in (row.diagnosis or "")


async def test_a_handshaking_wireguard_tunnel_is_not_blamed_for_the_path() -> None:
    """Once a handshake has happened the wire demonstrably works, so the
    diagnosis has to move on to what is actually wrong above it."""
    near_site, link = _fabric_and_link(Transport.wireguard)
    link.listen_port = 13231
    fake = FakeRouterOS(
        password="secret",
        menus={
            "interface": [
                {"name": "wg-branch-hq", "type": "wireguard", "running": True}
            ],
            "interface/wireguard": [
                {"name": "wg-branch-hq", "listen-port": "13231", "running": True}
            ],
            "interface/wireguard/peers": [
                {"interface": "wg-branch-hq", "public-key": "k",
                 "last-handshake": "1m20s", "rx": "4096"}
            ],
            "routing/bgp/session": [],
        },
    )
    driver = await _driver(fake)
    try:
        (row,) = await tunnel_health(driver, near_site, [link])
    finally:
        await driver.close()

    assert "BGP has not established" in (row.diagnosis or "")


async def test_the_interface_name_matches_what_the_transport_renders() -> None:
    """The whole join depends on this. If the name a transport *renders* and the
    name it *reports* ever diverge, every tunnel reads as never-applied."""
    from app.transports import get_transport
    from tests.test_fabric import make_link

    link = make_link(params={"bridge": "br-lan", "vni": 100})
    for transport, expected in [
        ("ipsec_gre", "gre-hub1-wan1-spoke1-wan1"),
        ("gre", "gre-hub1-wan1-spoke1-wan1"),
        ("ipip", "ipip-hub1-wan1-spoke1-wan1"),
        ("wireguard", "wg-hub1-wan1-spoke1-wan1"),
        ("eoip", "eoip-hub1-wan1-spoke1-wan1"),
        ("vxlan", "vxl-hub1-wan1-spoke1-wan1"),
    ]:
        driver = get_transport(transport)
        rendered = {
            item.props["name"]
            for section in driver.render(link)
            for item in section.items
            if "name" in item.props
        }
        assert driver.interface_name(link.slug) == expected, transport
        assert expected in rendered, transport


async def test_a_transport_read_back_as_a_plain_string_still_resolves() -> None:
    """The column is a String, so a row loaded from the database hands you the
    transport name as a str and a row still in the session hands you the enum.
    Only one of those has `.value`, and getting it wrong made every tunnel read
    as never-applied."""
    near_site, link = _fabric_and_link()
    link.fabric.transport = "ipsec_gre"  # what SQLite gives back
    fake = FakeRouterOS(
        password="secret",
        menus={"interface": [{"name": "gre-branch-hq", "type": "gre", "running": True}]},
    )
    driver = await _driver(fake)
    try:
        (row,) = await tunnel_health(driver, near_site, [link])
    finally:
        await driver.close()

    assert row.interface == "gre-branch-hq"
    assert row.interface_running is True


async def test_a_transport_this_build_no_longer_has_is_unknown_not_a_500() -> None:
    near_site, link = _fabric_and_link()
    link.fabric.transport = "some_removed_transport"
    fake = FakeRouterOS(password="secret", menus={"interface": []})
    driver = await _driver(fake)
    try:
        (row,) = await tunnel_health(driver, near_site, [link])
    finally:
        await driver.close()

    assert row.interface is None
    assert row.interface_running is None


async def test_a_dial_out_only_far_end_cannot_be_matched_on_an_address() -> None:
    """An SA is matched on the far side's public address. Without one, finding
    nothing is not evidence of anything."""
    near_site, link = _fabric_and_link()
    link.b_wan.public_ip = None
    fake = FakeRouterOS(password="secret", menus={"ip/ipsec/active-peers": []})
    driver = await _driver(fake)
    try:
        (row,) = await tunnel_health(driver, near_site, [link])
    finally:
        await driver.close()

    assert row.ipsec_established is None
    assert row.ipsec_detail is None
