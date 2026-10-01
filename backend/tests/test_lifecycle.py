"""Config the controller does not own: collisions, adoption, shadowing, sweeps."""

from __future__ import annotations

from app.drivers.base import ConfigItem, ConfigSection, OpKind
from app.reconcile.diff import diff_section
from app.reconcile.plan import _shadow_warnings
from app.transports.ipsec_gre import PROFILE_NAME_PATTERN


def gre(**props) -> ConfigSection:
    return ConfigSection(
        path="/interface/gre",
        owner_tag="sdwan:fabric:",
        key=("name",),
        write_once=("ipsec-secret",),
        items=[
            ConfigItem(
                props={"name": "gre-a-b-1a2b3c", "remote-address": "198.51.100.5", **props},
                tag="sdwan:fabric:core:a-b-1a2b3c",
            )
        ],
    )


def test_an_unowned_row_with_our_identity_is_a_collision_not_an_add() -> None:
    live = [{".id": "*7", "name": "gre-a-b-1a2b3c", "remote-address": "192.0.2.1"}]
    result = diff_section(gre(), live)

    assert [i.kind for i in result.items] == []
    assert len(result.collisions) == 1
    assert result.collisions[0].item_id == "*7"
    assert not result.collisions[0].adopted


def test_adopt_takes_the_row_over_in_place() -> None:
    live = [{".id": "*7", "name": "gre-a-b-1a2b3c", "remote-address": "192.0.2.1"}]
    result = diff_section(gre(**{"ipsec-secret": "s3cret"}), live, adopt=True)

    (item,) = result.items
    assert item.kind is OpKind.set
    assert item.item_id == "*7"
    assert item.props["remote-address"] == "198.51.100.5"
    assert item.props["comment"] == "sdwan:fabric:core:a-b-1a2b3c"
    # The foreign row's secret is unknown, so a write-once property is written
    # on adoption rather than skipped as it would be on an owned row.
    assert item.props["ipsec-secret"] == "s3cret"
    assert result.collisions[0].adopted


def test_a_row_with_a_foreign_comment_still_collides() -> None:
    """A leftover from an earlier install whose comment someone edited."""
    live = [{".id": "*2", "name": "gre-a-b-1a2b3c", "comment": "old tunnel"}]
    result = diff_section(gre(), live)
    assert result.collisions and result.collisions[0].comment == "old tunnel"


def test_unrelated_unowned_rows_are_left_alone() -> None:
    live = [{".id": "*3", "name": "my-own-gre", "remote-address": "192.0.2.9"}]
    result = diff_section(gre(), live)
    assert result.collisions == []
    assert [i.kind for i in result.items] == [OpKind.add]


def test_comment_keyed_menus_never_collide() -> None:
    mangle = ConfigSection(
        path="/ip/firewall/mangle",
        owner_tag="sdwan:policy:",
        key=("comment",),
        items=[ConfigItem(props={"chain": "prerouting", "action": "accept"}, tag="sdwan:policy:x")],
    )
    result = diff_section(mangle, [{".id": "*1", "chain": "prerouting", "comment": ""}])
    assert result.collisions == []


def _profiles(*names: str) -> ConfigSection:
    return ConfigSection(
        path="/ip/ipsec/profile",
        owner_tag="sdwan:fabric:",
        key=("name",),
        comment_capable=False,
        name_pattern=PROFILE_NAME_PATTERN,
        items=[ConfigItem(props={"name": n}, tag="x") for n in names],
    )


def test_a_removed_links_profile_is_swept_by_its_name_pattern() -> None:
    live = [
        {".id": "*1", "name": "prof-hub1-wan-spok1-wan-1a2b3c"},
        {".id": "*2", "name": "prof-hub1-wan-spok2-wan-4d5e6f"},
        {".id": "*3", "name": "default"},
        {".id": "*4", "name": "prof-office-vpn"},
    ]
    result = diff_section(_profiles("prof-hub1-wan-spok1-wan-1a2b3c"), live)
    removed = {i.item_id for i in result.items if i.kind is OpKind.remove}
    # Only the departed link's profile -- never "default", never an
    # operator's own profile that merely starts with "prof-".
    assert removed == {"*2"}


def _mangle_section() -> ConfigSection:
    return ConfigSection(
        path="/ip/firewall/mangle",
        owner_tag="sdwan:",
        key=("comment",),
        items=[ConfigItem(props={"chain": "prerouting"}, tag="sdwan:policy:a")],
    )


def test_an_unmanaged_terminal_mark_above_ours_is_warned_about() -> None:
    live = [
        {".id": "*1", "chain": "prerouting", "action": "mark-routing", "passthrough": "false"},
        {".id": "*2", "chain": "prerouting", "action": "accept", "comment": "sdwan:policy:a"},
    ]
    warnings = _shadow_warnings(_mangle_section(), live)
    assert len(warnings) == 1 and "*1" in warnings[0]


def test_passthrough_marks_and_rules_below_ours_are_not_warned_about() -> None:
    live = [
        {".id": "*1", "chain": "prerouting", "action": "mark-packet", "passthrough": "true"},
        {".id": "*2", "chain": "prerouting", "action": "mark-connection", "passthrough": "true"},
        {".id": "*3", "chain": "prerouting", "action": "accept", "comment": "sdwan:policy:a"},
        {".id": "*4", "chain": "prerouting", "action": "mark-routing", "passthrough": "false"},
    ]
    assert _shadow_warnings(_mangle_section(), live) == []


def test_a_wireguard_port_already_in_use_is_warned_about() -> None:
    section = ConfigSection(
        path="/interface/wireguard",
        owner_tag="sdwan:fabric:",
        key=("name",),
        items=[ConfigItem(props={"name": "wg-x", "listen-port": 51820}, tag="sdwan:fabric:x")],
    )
    live = [{".id": "*1", "name": "road-warrior", "listen-port": "51820"}]
    warnings = _shadow_warnings(section, live)
    assert len(warnings) == 1 and "road-warrior" in warnings[0]
