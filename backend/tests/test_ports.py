"""The front-panel view.

Read-only by design. The value is entirely in the classification: an operator
looking at a list of eleven interfaces cannot tell which two are the uplinks
the fabric is built on, which are switch ports, and which the controller owns
and will overwrite on the next apply.
"""

from __future__ import annotations

import httpx
import pytest

from app.drivers.ros7_rest import Ros7RestDriver
from app.models.site import Site, Wan
from app.services.ports import read_ports
from tests.fakeros.server import FakeRouterOS

INTERFACES = [
    {"name": "ether1", "type": "ether", "running": True, "disabled": False,
     "mac-address": "AA:BB:CC:00:00:01", "mtu": 1500, "rx-byte": 1000, "tx-byte": 2000},
    {"name": "ether2", "type": "ether", "running": True, "disabled": False,
     "mac-address": "AA:BB:CC:00:00:02", "mtu": 1500, "rx-byte": 30, "tx-byte": 40},
    {"name": "ether3", "type": "ether", "running": True, "disabled": False,
     "mac-address": "AA:BB:CC:00:00:03", "mtu": 1500},
    {"name": "ether4", "type": "ether", "running": False, "disabled": False,
     "mac-address": "AA:BB:CC:00:00:04", "mtu": 1500},
    {"name": "ether5", "type": "ether", "running": False, "disabled": True,
     "mac-address": "AA:BB:CC:00:00:05", "mtu": 1500},
    {"name": "bridge", "type": "bridge", "running": True, "disabled": False, "mtu": 1500},
    {"name": "sdwan-dc1", "type": "gre", "running": True, "disabled": False,
     "mtu": 1400, "comment": "sdwan:core:link-7"},
]

ETHERNET = [
    {"name": "ether1", "default-name": "ether1", "speed": "1Gbps"},
    {"name": "ether2", "default-name": "ether2", "speed": "100Mbps"},
    {"name": "ether3", "default-name": "ether3", "speed": "1Gbps"},
    {"name": "ether4", "default-name": "ether4"},
    {"name": "ether5", "default-name": "ether5"},
]

ADDRESSES = [
    {"address": "203.0.113.10/24", "interface": "ether1", "disabled": False},
    {"address": "10.10.0.2/30", "interface": "ether2", "disabled": False},
    {"address": "192.168.88.1/24", "interface": "bridge", "disabled": False},
]

BRIDGE_PORTS = [
    {"interface": "ether3", "bridge": "bridge"},
    {"interface": "ether4", "bridge": "bridge"},
]


@pytest.fixture
def panel_ros() -> FakeRouterOS:
    return FakeRouterOS(
        password="secret",
        menus={
            "interface": INTERFACES,
            "interface/ethernet": ETHERNET,
            "ip/address": ADDRESSES,
            "interface/bridge/port": BRIDGE_PORTS,
        },
    )


@pytest.fixture
async def panel_driver(panel_ros: FakeRouterOS):
    d = Ros7RestDriver(
        "test-router", "admin", "secret",
        transport=httpx.ASGITransport(app=panel_ros.app),
    )
    await d.connect()
    try:
        yield d
    finally:
        await d.close()


def site_with_uplinks() -> Site:
    site = Site(id="s1", name="branch", mgmt_host="10.0.0.1", username="admin")
    site.wans = [
        Wan(name="wan1", interface="ether1", enabled=True),
        Wan(name="wan2", interface="ether2", enabled=False),
    ]
    return site


async def test_uplinks_are_identified_as_wan(panel_driver) -> None:
    ports = {p.name: p for p in await read_ports(panel_driver, site_with_uplinks())}

    assert ports["ether1"].role == "wan"
    assert ports["ether1"].wan_name == "wan1"
    assert ports["ether1"].wan_enabled is True
    assert ports["ether2"].role == "wan"
    assert ports["ether2"].wan_enabled is False, "a disabled uplink is still an uplink"


async def test_bridge_members_are_lan_and_the_bridge_is_a_bridge(panel_driver) -> None:
    ports = {p.name: p for p in await read_ports(panel_driver, site_with_uplinks())}

    assert ports["ether3"].role == "lan"
    assert ports["ether3"].bridge == "bridge"
    assert ports["bridge"].role == "bridge"


async def test_a_port_with_nothing_on_it_reads_as_unused(panel_driver) -> None:
    """The question the panel exists to answer: which socket is free."""
    ports = {p.name: p for p in await read_ports(panel_driver, site_with_uplinks())}

    assert ports["ether5"].role == "unused"
    assert ports["ether5"].disabled is True


async def test_controller_owned_interfaces_are_flagged(panel_driver) -> None:
    """Anything carrying the ownership comment is reverted on the next apply.
    Someone about to edit it by hand should be told that first."""
    ports = {p.name: p for p in await read_ports(panel_driver, site_with_uplinks())}

    assert ports["sdwan-dc1"].role == "tunnel"
    assert ports["sdwan-dc1"].managed is True
    assert ports["ether1"].managed is False


async def test_link_state_and_speed_come_through(panel_driver) -> None:
    ports = {p.name: p for p in await read_ports(panel_driver, site_with_uplinks())}

    assert ports["ether1"].running is True
    assert ports["ether1"].speed == "1Gbps"
    assert ports["ether2"].speed == "100Mbps"
    assert ports["ether4"].running is False
    assert ports["ether1"].addresses == ["203.0.113.10/24"]


async def test_ports_are_ordered_the_way_the_box_reads(panel_driver) -> None:
    """Uplinks first, then LAN, then spare sockets, then logical interfaces."""
    roles = [p.role for p in await read_ports(panel_driver, site_with_uplinks())]

    assert roles == sorted(
        roles, key=lambda r: {"wan": 0, "lan": 1, "unused": 2, "bridge": 3, "tunnel": 4}[r]
    )
    assert roles[0] == "wan"


async def test_a_device_without_an_ethernet_menu_still_renders() -> None:
    """A CHR has no /interface/ethernet. That must cost the speed column, not
    the whole panel."""
    ros = FakeRouterOS(
        password="secret",
        menus={"interface": [{"name": "ether1", "type": "ether", "running": True}]},
    )
    d = Ros7RestDriver(
        "chr", "admin", "secret", transport=httpx.ASGITransport(app=ros.app)
    )
    await d.connect()
    try:
        ports = await read_ports(d, site_with_uplinks())
    finally:
        await d.close()

    assert [p.name for p in ports] == ["ether1"]
    assert ports[0].speed is None
    assert ports[0].role == "wan"


async def test_nothing_is_written(panel_ros: FakeRouterOS, panel_driver) -> None:
    """A panel that mutates the device while you look at it would be a trap."""
    before = {k: list(v) for k, v in panel_ros.menus.items()}
    await read_ports(panel_driver, site_with_uplinks())
    assert panel_ros.menus == before
