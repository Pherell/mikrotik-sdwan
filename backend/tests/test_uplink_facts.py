"""Checking stored uplink facts against what the device reports.

This is the gap that cost the most time on real hardware. Three uplinks were
entered by hand claiming public addresses their routers had never held, and
marked reachable when every one of them was behind NAT. The controller held
both numbers -- the stored row and the probe's view -- and never compared
them, so the contradiction was invisible.

What it cost: an IPsec peer is identified by source address. The far end was
told to dial 10.10.10.70; the traffic arrived translated, from 103.210.35.189,
matched no peer, and was discarded. Measured on the wire: UDP 500 and 4500
crossed the path perfectly well and were thrown away on arrival. Every layer
reported "no exchange" and both routers looked correctly configured.
"""

from __future__ import annotations

from app.models.site import Site, Wan
from app.netaddr import is_unroutable, private_uplink_note
from app.schemas.site import WanCreate, WanRead
from app.services.probe import compare_uplinks


def site_with(**wan_kwargs) -> Site:
    site = Site(id="s1", name="branch", mgmt_host="10.0.0.1", username="admin",
                tenant_id="t")
    base = dict(id="w1", site_id="s1", name="ISP-2", interface="ether2",
                enabled=True, nat_behind=False)
    site.wans = [Wan(**{**base, **wan_kwargs})]
    return site


def observed(**kw) -> list[WanCreate]:
    base = dict(name="wan1", interface="ether2", public_ip=None, nat_behind=True)
    return [WanCreate(**{**base, **kw})]


# -- the address test --------------------------------------------------------


def test_unroutable_covers_the_ranges_a_peer_cannot_dial() -> None:
    for address in ("10.10.10.70", "192.168.1.1", "172.16.0.1", "172.31.255.254",
                    "100.64.0.1", "169.254.1.1", "127.0.0.1"):
        assert is_unroutable(address), address

    for address in ("203.0.113.1", "8.8.8.8", "172.32.0.1", "172.15.0.1",
                    "100.128.0.1", "169.253.0.1"):
        assert not is_unroutable(address), address


def test_a_private_uplink_address_is_flagged_but_not_refused() -> None:
    """Private transit is real -- MPLS, a partner link, a lab -- and the model
    already carries masquerade=False for it. Refusing the address would break
    a legitimate setup; saying nothing let a NAT'd uplink look reachable."""
    note = private_uplink_note("10.1.11.229", nat_behind=False)

    assert note is not None
    assert "10.1.11.229" in note
    assert "behind NAT" in note


def test_an_uplink_already_marked_natted_is_not_lectured() -> None:
    """The operator has already said what the address says."""
    assert private_uplink_note("10.1.11.229", nat_behind=True) is None
    assert private_uplink_note("203.0.113.1", nat_behind=False) is None
    assert private_uplink_note(None, nat_behind=False) is None


def test_the_warning_rides_along_with_the_uplink() -> None:
    """Surfaced where the value is entered, not in a report nobody opens."""
    # Column defaults land at flush; this row has never seen a session, so
    # everything the schema requires is spelled out.
    wan = Wan(id="w1", site_id="s1", name="ISP-2", interface="ether2",
              public_ip="10.10.10.70", nat_behind=False, enabled=True,
              dynamic=False, cost=1.0, masquerade=True, tags={})

    read = WanRead.model_validate(wan)

    assert any("not routable" in w for w in read.warnings)


# -- stored against observed -------------------------------------------------


def test_an_uplink_marked_reachable_that_the_device_calls_natted_is_reported() -> None:
    """The exact shape that broke IPsec on real hardware."""
    conflicts = compare_uplinks(site_with(public_ip="10.10.10.70"), observed())

    assert len(conflicts) == 1
    assert conflicts[0].field == "nat_behind"
    assert conflicts[0].wan_name == "ISP-2"
    assert "IPsec" in conflicts[0].why


def test_an_address_the_device_no_longer_holds_is_reported() -> None:
    conflicts = compare_uplinks(
        site_with(public_ip="203.0.113.9"),
        observed(public_ip="203.0.113.10", nat_behind=False),
    )

    assert len(conflicts) == 1
    assert conflicts[0].field == "public_ip"
    assert conflicts[0].stored == "203.0.113.9"
    assert conflicts[0].observed == "203.0.113.10"


def test_an_uplink_the_operator_already_marked_natted_is_not_reported() -> None:
    """Agreement is not a conflict."""
    site = site_with(public_ip=None, nat_behind=True)

    assert compare_uplinks(site, observed()) == []


def test_a_port_forward_is_not_called_a_mistake() -> None:
    """A reachable uplink can hold a private address on the router while the
    world dials a public one somewhere upstream. The device's view is
    evidence, not authority -- so an uplink the operator has marked reachable
    on a public address is left alone when the device reports the same."""
    site = site_with(public_ip="203.0.113.9")

    assert compare_uplinks(site, observed(public_ip="203.0.113.9",
                                          nat_behind=False)) == []


def test_an_interface_the_probe_did_not_see_is_not_guessed_at() -> None:
    """A probe that missed an interface says nothing about it. Reporting a
    conflict there would train people to ignore the warning."""
    site = site_with(public_ip="10.10.10.70")

    assert compare_uplinks(site, observed(interface="ether9")) == []


def test_a_disabled_uplink_is_left_out_of_it() -> None:
    site = site_with(public_ip="10.10.10.70", enabled=False)

    assert compare_uplinks(site, observed()) == []
