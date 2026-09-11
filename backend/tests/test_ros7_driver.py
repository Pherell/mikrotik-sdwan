"""RouterOS 7 REST driver against the fake device."""

from __future__ import annotations

import httpx
import pytest

from app.drivers.base import ConfigOp, DeviceAuthError, DriverError, OpKind
from app.drivers.ros7_rest import Ros7RestDriver, _at_least, _parse_version
from app.models.enums import DeviceKind, SiteRole
from app.models.site import Site
from app.services.probe import _suggest_wans, probe_site
from tests.fakeros.server import FakeRouterOS


async def test_read_coerces_types(driver: Ros7RestDriver) -> None:
    rows = await driver.read("/ip/route")
    assert rows[0]["distance"] == 1          # not "1"
    assert rows[0]["disabled"] is False      # not "false"
    assert rows[0]["dst-address"] == "0.0.0.0/0"


async def test_read_single_row_menu_returns_list(driver: Ros7RestDriver) -> None:
    rows = await driver.read("/system/resource")
    assert len(rows) == 1
    assert rows[0]["board-name"] == "CCR2004-1G-12S+2XS"


async def test_read_unknown_menu_raises(driver: Ros7RestDriver) -> None:
    with pytest.raises(DriverError, match="no such path"):
        await driver.read("/does/not/exist")


async def test_capabilities(driver: Ros7RestDriver) -> None:
    caps = await driver.capabilities()
    assert caps.ros_major == 7
    assert caps.version.startswith("7.14.3")
    assert caps.has_rest is True
    assert caps.has_wireguard is True
    assert caps.has_netwatch_thresholds is True   # 7.14.3 >= 7.7
    assert caps.identity == "MikroTik"


async def test_capabilities_detects_missing_wireguard() -> None:
    ros = FakeRouterOS(password="secret", version="7.2.1 (stable)", wireguard=False)
    async with Ros7RestDriver(
        "r", "admin", "secret", transport=httpx.ASGITransport(app=ros.app)
    ) as d:
        caps = await d.capabilities()
        assert caps.has_wireguard is False
        assert caps.has_netwatch_thresholds is False  # 7.2.1 < 7.7
        assert caps.supports_transport("wireguard") is False


async def test_bad_credentials_raise_auth_error(fake_ros: FakeRouterOS) -> None:
    async with Ros7RestDriver(
        "r", "admin", "wrong", transport=httpx.ASGITransport(app=fake_ros.app)
    ) as d:
        with pytest.raises(DeviceAuthError):
            await d.read("/system/resource")


async def test_apply_add_set_remove(driver: Ros7RestDriver, fake_ros: FakeRouterOS) -> None:
    result = await driver.apply(
        [
            ConfigOp(
                kind=OpKind.add,
                path="/ip/ipsec/peer",
                props={"name": "peer-hub1", "address": "198.51.100.5", "passive": False},
                comment="sdwan:core:hub1-spoke1",
            )
        ]
    )
    assert result.ok and result.applied == 1

    rows = await driver.read("/ip/ipsec/peer")
    assert len(rows) == 1
    # ROS drops passive=false on read (it is the default), so it comes back
    # absent, not False -- the fake models that.
    assert rows[0].get("passive", False) is False
    assert rows[0]["comment"] == "sdwan:core:hub1-spoke1"

    item_id = rows[0][".id"]
    assert (await driver.apply(
        [ConfigOp(kind=OpKind.set, path="/ip/ipsec/peer", item_id=item_id, props={"passive": True})]
    )).ok
    assert (await driver.read("/ip/ipsec/peer"))[0]["passive"] is True

    assert (await driver.apply(
        [ConfigOp(kind=OpKind.remove, path="/ip/ipsec/peer", item_id=item_id)]
    )).ok
    assert await driver.read("/ip/ipsec/peer") == []


async def test_apply_stops_at_first_failure(driver: Ros7RestDriver) -> None:
    ops = [
        ConfigOp(kind=OpKind.add, path="/ip/ipsec/peer", props={"name": "ok"}),
        ConfigOp(kind=OpKind.set, path="/ip/ipsec/peer", props={"name": "boom"}),  # no item_id
        ConfigOp(kind=OpKind.add, path="/ip/ipsec/peer", props={"name": "never"}),
    ]
    result = await driver.apply(ops)

    assert result.ok is False
    assert result.applied == 1
    assert result.failed_op is not None
    assert "without an item id" in (result.error or "")
    # The op after the failure must not have run.
    assert [r["name"] for r in await driver.read("/ip/ipsec/peer")] == ["ok"]


async def test_apply_masks_secrets_in_the_failed_op(driver: Ros7RestDriver) -> None:
    op = ConfigOp(
        kind=OpKind.set,
        path="/ip/ipsec/identity",
        props={"secret": "hunter2-super-secret", "peer": "hub1"},
    )
    result = await driver.apply([op])

    assert result.failed_op is not None
    leaked = result.failed_op.props["secret"]
    assert "hunter2-super-secret" not in leaked
    assert leaked.startswith("hunt")


async def test_backup_issues_the_console_command(
    driver: Ros7RestDriver, fake_ros: FakeRouterOS
) -> None:
    await driver.backup("sdwan-pre-job1")

    assert ("system/backup/save", {"name": "sdwan-pre-job1", "dont-encrypt": "true"}) in (
        fake_ros.commands
    )
    assert any(f["name"] == "sdwan-pre-job1.backup" for f in fake_ros.rows("file"))


# -- version parsing --------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("7.14.3 (stable)", (7, 14, 3)),
        ("6.49.10", (6, 49, 10)),
        ("7.1beta4", (7, 1)),
        ("", (0,)),
    ],
)
def test_parse_version(version: str, expected: tuple[int, ...]) -> None:
    assert _parse_version(version) == expected


def test_at_least_compares_component_wise() -> None:
    assert _at_least("7.10.1", (7, 7)) is True
    assert _at_least("7.7", (7, 7)) is True
    assert _at_least("7.6.9", (7, 7)) is False
    assert _at_least("6.49.10", (7, 7)) is False


# -- WAN discovery ----------------------------------------------------------


async def test_suggest_wans_finds_both_uplinks(driver: Ros7RestDriver) -> None:
    wans, _lan = await _suggest_wans(driver)
    by_iface = {w.interface: w for w in wans}

    assert set(by_iface) == {"ether1", "ether2"}

    # ether1 holds a public address on a default route.
    assert by_iface["ether1"].public_ip == "203.0.113.10"
    assert by_iface["ether1"].nat_behind is False
    assert by_iface["ether1"].dynamic is False

    # ether2 is a DHCP uplink on RFC1918 space: behind NAT, dial-out only.
    assert by_iface["ether2"].public_ip is None
    assert by_iface["ether2"].nat_behind is True
    assert by_iface["ether2"].dynamic is True

    # The LAN bridge is not offered as an uplink.
    assert "bridge" not in by_iface

    # The mask is kept alongside the address: it is the only thing that says
    # whether a tunnel's far end is on this segment or out through the
    # gateway, and recovering it later means going back to the device.
    assert by_iface["ether1"].prefix_len == 24


async def test_uplink_cost_follows_the_device_not_the_interface_name() -> None:
    """The router has already said which uplink it prefers, in the distance on
    each default route. Numbering by the order interfaces happen to sort in
    contradicts it -- and cost decides failover order and which uplink carries
    a tunnel's underlay route, so getting it backwards puts the overlay on the
    wrong link."""
    ros = FakeRouterOS(
        password="secret",
        menus={
            "ip/route": [
                {"dst-address": "0.0.0.0/0", "gateway": "10.0.0.1",
                 "immediate-gw": "10.0.0.1%ether9", "distance": 1},
                {"dst-address": "0.0.0.0/0", "gateway": "10.1.0.1",
                 "immediate-gw": "10.1.0.1%ether1", "distance": 5},
            ],
            "ip/address": [
                {"address": "10.0.0.2/24", "interface": "ether9"},
                {"address": "10.1.0.2/24", "interface": "ether1"},
            ],
        },
    )
    async with Ros7RestDriver(
        "r", "admin", "secret", transport=httpx.ASGITransport(app=ros.app)
    ) as d:
        wans, _lan = await _suggest_wans(d)

    # ether9 sorts after ether1 by name but is the router's preferred path.
    assert [w.interface for w in wans] == ["ether9", "ether1"]
    assert [w.cost for w in wans] == [1.0, 2.0]
    assert wans[0].name == "wan1"


async def _lan_driver(**menus) -> tuple[Ros7RestDriver, FakeRouterOS]:
    """A router whose bridge carries the default route -- so it *is* an uplink
    candidate by the old rules, and only the LAN signals can rule it out."""
    base = {
        "ip/address": [
            {"address": "192.168.1.1/24", "interface": "bridge-local", "disabled": False},
        ],
        "ip/route": [
            {
                "dst-address": "0.0.0.0/0",
                "gateway": "192.168.1.254",
                "distance": 1,
                "disabled": False,
                "inactive": False,
            },
        ],
        "ip/dhcp-client": [],
        "interface/bridge": [{"name": "bridge-local"}],
        "interface/bridge/port": [
            {"interface": "ether3", "bridge": "bridge-local", "disabled": False}
        ],
        "ip/dhcp-server": [],
    }
    base.update(menus)
    fake = FakeRouterOS(password="secret", menus=base)
    d = Ros7RestDriver(
        "test-router", "admin", "secret", transport=httpx.ASGITransport(app=fake.app)
    )
    await d.connect()
    return d, fake


async def test_a_bridge_serving_dhcp_is_reported_as_a_lan_not_an_uplink() -> None:
    """Bridged *and* handing out addresses is a switched segment, not a WAN.
    Offering it produced a site whose 'uplink' could never accept a tunnel."""
    d, _ = await _lan_driver(
        **{"ip/dhcp-server": [{"interface": "bridge-local", "disabled": False}]}
    )
    try:
        wans, lan = await _suggest_wans(d)
    finally:
        await d.close()

    assert [w.interface for w in wans] == []
    assert [n.interface for n in lan] == ["bridge-local"]
    assert "DHCP server" in lan[0].reason


async def test_a_bridge_without_a_dhcp_server_is_still_a_valid_uplink() -> None:
    """Either signal alone is too weak: a bridge can legitimately carry the
    uplink, so excluding on bridging alone would throw away real WANs."""
    d, _ = await _lan_driver()
    try:
        wans, lan = await _suggest_wans(d)
    finally:
        await d.close()

    assert [w.interface for w in wans] == ["bridge-local"]
    assert lan == []


async def test_dropping_a_lan_leaves_no_gap_in_the_uplink_numbering() -> None:
    """wan1/wan2 and the cost ladder come from the position in the list."""
    d, _ = await _lan_driver(
        **{
            "ip/address": [
                {"address": "192.168.1.1/24", "interface": "bridge-local", "disabled": False},
                {"address": "203.0.113.10/24", "interface": "ether1", "disabled": False},
            ],
            "ip/route": [
                {
                    "dst-address": "0.0.0.0/0",
                    "gateway": "192.168.1.254",
                    "distance": 1,
                    "disabled": False,
                    "inactive": False,
                },
                {
                    "dst-address": "0.0.0.0/0",
                    "gateway": "203.0.113.1",
                    "distance": 2,
                    "disabled": False,
                    "inactive": False,
                },
            ],
            "ip/dhcp-server": [{"interface": "bridge-local", "disabled": False}],
        }
    )
    try:
        wans, lan = await _suggest_wans(d)
    finally:
        await d.close()

    assert [n.interface for n in lan] == ["bridge-local"]
    assert [(w.name, w.interface, w.cost) for w in wans] == [
        ("wan1", "ether1", 1.0)
    ]


async def test_a_disabled_dhcp_server_does_not_make_an_uplink_a_lan() -> None:
    d, _ = await _lan_driver(
        **{"ip/dhcp-server": [{"interface": "bridge-local", "disabled": True}]}
    )
    try:
        wans, lan = await _suggest_wans(d)
    finally:
        await d.close()

    assert [w.interface for w in wans] == ["bridge-local"]
    assert lan == []


async def test_probe_site_reports_unreachable_without_raising() -> None:
    site = Site(
        name="down",
        mgmt_host="192.0.2.99",
        username="admin",
        device_kind=DeviceKind.ros7,
        role=SiteRole.spoke,
        password_enc=None,
        verify_tls=False,
    )
    # No credentials and an unroutable host: the probe must degrade, not explode.
    result = await probe_site(site)
    assert result.reachable is False
    assert result.error
