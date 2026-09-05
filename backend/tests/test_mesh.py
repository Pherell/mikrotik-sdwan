"""Dynamic spoke-to-spoke mesh decisions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.fabric.expand import WanRef
from app.fabric.mesh import (
    DEFAULT_THRESHOLD_BYTES,
    PairTraffic,
    build_links,
    decide,
)
from app.models.enums import SiteRole, Topology, Transport
from app.models.fabric import Fabric, Link
from app.models.site import Wan
from app.transports import get_transport

IPSEC = get_transport("ipsec_gre")
NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def fabric(**params) -> Fabric:
    return Fabric(
        id="fab-1",
        name="core",
        transport=Transport.ipsec_gre,
        topology=Topology.hub_spoke_dynamic,
        ip_pool="10.255.0.0/24",
        loopback_pool="10.254.0.0/24",
        asn=65000,
        mtu=1400,
        transport_params=params,
        tenant_id="default",
    )


def ref(site: str, role: SiteRole = SiteRole.spoke, public: str | None = "203.0.113.1",
        nat: bool = False) -> WanRef:
    wan = Wan(
        id=f"wan-{site}",
        site_id=f"site-{site}",
        name="wan1",
        interface="ether1",
        public_ip=public,
        nat_behind=nat,
        enabled=True,
    )
    return WanRef(site_id=f"site-{site}", site_name=site, role=role, wan=wan)


def link(a: str, b: str, *, age_minutes: int = 60, dynamic: bool = True) -> Link:
    return Link(
        id=f"link-{a}-{b}",
        fabric_id="fab-1",
        a_wan_id=f"wan-{a}",
        b_wan_id=f"wan-{b}",
        slug=f"{a}-{b}",
        subnet="10.255.0.8/31",
        a_tunnel_ip="10.255.0.8",
        b_tunnel_ip="10.255.0.9",
        initiator="a",
        dynamic=dynamic,
        enabled=True,
        state="up",
        created_at=NOW - timedelta(minutes=age_minutes),
    )


def busy(a: str, b: str, mb: int = 100, idle_minutes: int = 0) -> PairTraffic:
    return PairTraffic(
        a_wan_id=f"wan-{a}",
        b_wan_id=f"wan-{b}",
        bytes_seen=mb * 1024 * 1024,
        last_active=NOW - timedelta(minutes=idle_minutes),
    )


# -- building ---------------------------------------------------------------


def test_busy_spoke_pair_gets_a_direct_tunnel() -> None:
    wans = [ref("spoke1", public="203.0.113.1"), ref("spoke2", public="203.0.113.2")]

    d = decide(fabric(), wans, [], [busy("spoke1", "spoke2")], IPSEC, now=NOW)

    assert len(d.build) == 1
    assert {r.site_name for r in d.build[0]} == {"spoke1", "spoke2"}


def test_quiet_pair_stays_hub_transited() -> None:
    wans = [ref("spoke1", public="203.0.113.1"), ref("spoke2", public="203.0.113.2")]
    trickle = PairTraffic("wan-spoke1", "wan-spoke2", DEFAULT_THRESHOLD_BYTES - 1, NOW)

    assert decide(fabric(), wans, [], [trickle], IPSEC, now=NOW).build == []


def test_hub_pairs_are_never_built_dynamically() -> None:
    """Hub links are permanent and already exist; this only adds spoke-to-spoke."""
    wans = [
        ref("hub1", role=SiteRole.hub, public="198.51.100.5"),
        ref("spoke1", public="203.0.113.1"),
    ]

    assert decide(fabric(), wans, [], [busy("hub1", "spoke1")], IPSEC, now=NOW).build == []


def test_two_natted_spokes_stay_hub_transited() -> None:
    """Neither can accept a tunnel, so a direct one could never establish."""
    wans = [
        ref("spoke1", public=None, nat=True),
        ref("spoke2", public=None, nat=True),
    ]

    d = decide(fabric(), wans, [], [busy("spoke1", "spoke2")], IPSEC, now=NOW)

    assert d.build == []
    assert len(d.skipped) == 1
    assert "through a hub" in d.skipped[0][2]


def test_an_existing_tunnel_is_not_rebuilt() -> None:
    wans = [ref("spoke1", public="203.0.113.1"), ref("spoke2", public="203.0.113.2")]

    d = decide(
        fabric(), wans, [link("spoke1", "spoke2")], [busy("spoke1", "spoke2")], IPSEC, now=NOW
    )
    assert d.build == []


def test_threshold_is_configurable_per_fabric() -> None:
    wans = [ref("spoke1", public="203.0.113.1"), ref("spoke2", public="203.0.113.2")]
    small = PairTraffic("wan-spoke1", "wan-spoke2", 1024 * 1024, NOW)

    f = fabric(mesh_threshold_bytes=1024)
    assert len(decide(f, wans, [], [small], IPSEC, now=NOW).build) == 1


# -- tearing down -----------------------------------------------------------


def test_an_idle_tunnel_is_torn_down() -> None:
    wans = [ref("spoke1", public="203.0.113.1"), ref("spoke2", public="203.0.113.2")]
    existing = [link("spoke1", "spoke2", age_minutes=120)]

    d = decide(fabric(), wans, existing, [busy("spoke1", "spoke2", idle_minutes=90)],
               IPSEC, now=NOW)

    assert d.tear_down == existing


def test_a_young_tunnel_is_never_torn_down() -> None:
    """Traffic that sits near the threshold would otherwise build and destroy a
    tunnel repeatedly, paying an IKE negotiation each time."""
    wans = [ref("spoke1", public="203.0.113.1"), ref("spoke2", public="203.0.113.2")]
    existing = [link("spoke1", "spoke2", age_minutes=2)]

    d = decide(fabric(), wans, existing, [], IPSEC, now=NOW)

    assert d.tear_down == []


def test_an_active_tunnel_is_kept() -> None:
    wans = [ref("spoke1", public="203.0.113.1"), ref("spoke2", public="203.0.113.2")]
    existing = [link("spoke1", "spoke2", age_minutes=120)]

    d = decide(fabric(), wans, existing, [busy("spoke1", "spoke2", idle_minutes=1)],
               IPSEC, now=NOW)

    assert d.tear_down == []


def test_a_naive_timestamp_does_not_crash_the_sweep() -> None:
    """SQLite returns naive datetimes even for an aware column, and comparing
    one to an aware `now` raises rather than answering wrongly."""
    wans = [ref("spoke1", public="203.0.113.1"), ref("spoke2", public="203.0.113.2")]
    stale = link("spoke1", "spoke2", age_minutes=120)
    stale.created_at = stale.created_at.replace(tzinfo=None)

    d = decide(fabric(), wans, [stale], [], IPSEC, now=NOW)

    assert d.tear_down == [stale]


# -- addressing -------------------------------------------------------------


def test_built_links_take_free_addresses() -> None:
    pairs = [(ref("spoke1", public="203.0.113.1"), ref("spoke2", public="203.0.113.2"))]

    created = build_links(fabric(), pairs, ["10.255.0.0/31", "10.255.0.2/31"], IPSEC)

    assert len(created) == 1
    assert created[0].subnet == "10.255.0.4/31"
    assert created[0].dynamic is True
    assert created[0].a_tunnel_ip == "10.255.0.4"
