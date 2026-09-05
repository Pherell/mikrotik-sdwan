"""Section merging.

Several renderers write to one RouterOS menu. Diffing them separately leaves
rows from a deleted link matching no section at all, so the tunnel runs forever.
"""

from __future__ import annotations

import pytest

from app.drivers.base import ConfigItem, ConfigSection, OpKind
from app.reconcile.diff import diff_section
from app.reconcile.merge import common_scope, merge_sections


def sec(path: str, owner: str, items: list[ConfigItem], **kw) -> ConfigSection:
    return ConfigSection(
        path=path, owner_tag=owner, items=items, key=kw.pop("key", ("name",)), **kw
    )


def item(name: str, tag: str, **props) -> ConfigItem:
    return ConfigItem(props={"name": name, **props}, tag=tag)


# -- scope ------------------------------------------------------------------


def test_common_scope_stops_at_a_segment_boundary() -> None:
    assert common_scope(["sdwan:fabric:core:a", "sdwan:site:branch-1"]) == "sdwan:"
    assert common_scope(["sdwan:fabric:core:a", "sdwan:fabric:core:b"]) == "sdwan:fabric:core:"


def test_common_scope_never_cuts_mid_token() -> None:
    """sdwan:site must not become a prefix that also matches sdwan:sitewide."""
    scope = common_scope(["sdwan:site:a", "sdwan:sitewide:b"])
    assert scope == "sdwan:"
    assert not "sdwan:sitewide:b".startswith("sdwan:site:")


def test_common_scope_of_nothing_is_the_global_prefix() -> None:
    assert common_scope([]) == "sdwan:"


# -- merging ----------------------------------------------------------------


def test_sections_on_the_same_path_become_one() -> None:
    merged = merge_sections(
        [
            sec("/ip/address", "sdwan:site:", [item("a", "sdwan:site:loopback")]),
            sec("/ip/address", "sdwan:fabric:core:x", [item("b", "sdwan:fabric:core:x")]),
        ]
    )

    assert len(merged) == 1
    assert len(merged[0].items) == 2
    # Widened, so rows from either renderer are visible to the one diff.
    assert merged[0].owner_tag == "sdwan:"


def test_merged_section_applies_at_the_earliest_requested_order() -> None:
    merged = merge_sections(
        [
            sec("/ip/address", "sdwan:a", [item("a", "sdwan:a")], order=50),
            sec("/ip/address", "sdwan:b", [item("b", "sdwan:b")], order=20),
        ]
    )
    assert merged[0].order == 20


def test_write_once_and_ignore_are_unioned() -> None:
    merged = merge_sections(
        [
            sec("/x", "sdwan:a", [], write_once=("psk",), ignore=("running",)),
            sec("/x", "sdwan:b", [], write_once=("key",), ignore=("uptime",)),
        ]
    )
    assert merged[0].write_once == ("key", "psk")
    assert merged[0].ignore == ("running", "uptime")


def test_disagreeing_identity_columns_is_an_error() -> None:
    """Silently picking one would make rows match the wrong intent."""
    with pytest.raises(ValueError, match="disagree on the identity columns"):
        merge_sections(
            [
                sec("/ip/address", "sdwan:a", [], key=("address",)),
                sec("/ip/address", "sdwan:b", [], key=("name",)),
            ]
        )


def test_two_renderers_claiming_the_same_row_is_an_error() -> None:
    """Otherwise the device flaps between two intents on alternate applies."""
    with pytest.raises(ValueError, match="both claim"):
        merge_sections(
            [
                sec("/interface/gre", "sdwan:a", [item("gre-1", "sdwan:a")]),
                sec("/interface/gre", "sdwan:b", [item("gre-1", "sdwan:b")]),
            ]
        )


# -- the bug this exists to prevent -----------------------------------------


def test_an_empty_section_removes_rows_a_deleted_link_left_behind() -> None:
    """A link that no longer exists renders no section of its own. Only a
    fabric-scoped empty section can still see -- and remove -- its rows."""
    cleanup = sec("/interface/gre", "sdwan:fabric:", [], key=("name",))
    orphan = {
        ".id": "*1",
        "name": "gre-hub1-wan1-spoke2-wan1",
        "comment": "sdwan:fabric:core:hub1-wan1-spoke2-wan1:gre",
    }

    result = diff_section(cleanup, live_rows=[orphan])

    assert [i.kind for i in result.items] == [OpKind.remove]
    assert result.items[0].item_id == "*1"


def test_cleanup_still_ignores_rows_a_human_wrote() -> None:
    cleanup = sec("/interface/gre", "sdwan:fabric:", [], key=("name",))
    hand_built = {".id": "*9", "name": "gre-to-partner", "comment": "site-to-site, manual"}

    assert diff_section(cleanup, live_rows=[hand_built]).empty


def test_merged_cleanup_keeps_live_links_and_drops_stale_ones() -> None:
    live_link = sec(
        "/interface/gre",
        "sdwan:fabric:core:hub1-wan1-spoke1-wan1:gre",
        [item("gre-hub1-wan1-spoke1-wan1", "sdwan:fabric:core:hub1-wan1-spoke1-wan1:gre")],
    )
    cleanup = sec("/interface/gre", "sdwan:fabric:", [], key=("name",))

    merged = merge_sections([cleanup, live_link])[0]
    rows = [
        {
            ".id": "*1",
            "name": "gre-hub1-wan1-spoke1-wan1",
            "comment": "sdwan:fabric:core:hub1-wan1-spoke1-wan1:gre",
        },
        {
            ".id": "*2",
            "name": "gre-hub1-wan1-spoke2-wan1",
            "comment": "sdwan:fabric:core:hub1-wan1-spoke2-wan1:gre",
        },
        {".id": "*3", "name": "gre-manual", "comment": "operator built"},
    ]

    result = diff_section(merged, live_rows=rows)

    assert [(i.kind, i.item_id) for i in result.items] == [(OpKind.remove, "*2")]
