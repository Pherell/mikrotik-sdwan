"""Diff-level proof that unit spellings do not produce phantom drift.

The device side of each case is written the way RouterOS returns it --
durations re-rendered in compound form, scalars as strings. Intent is
written the way the renderers author it. A clean diff here is the whole
point of ``canonical_compare``; the negative controls prove the folding
did not become a blanket ignore.
"""

from __future__ import annotations

from app.drivers.base import ConfigItem, ConfigSection, OpKind
from app.reconcile.diff import diff_section

TAG = "sdwan:policy:sla:wan1:abcd1234"


def _netwatch_section(interval: str = "90s") -> ConfigSection:
    return ConfigSection(
        path="/tool/netwatch",
        owner_tag="sdwan:policy:",
        key=("host", "comment"),
        items=[
            ConfigItem(
                tag=TAG,
                props={
                    "host": "10.255.0.1",
                    "comment": TAG,
                    "interval": interval,
                    "thr-avg": "300ms",
                    "thr-loss-percent": 20,
                    "disabled": False,
                },
            )
        ],
    )


def _live_row(**overrides: str) -> dict:
    row = {
        ".id": "*1",
        "host": "10.255.0.1",
        "comment": TAG,
        "interval": "1m30s",  # RouterOS compound form of the intent's 90s
        "thr-avg": "300ms",
        "thr-loss-percent": "20",
        "disabled": "false",
    }
    row.update(overrides)
    return row


def test_compound_duration_from_device_diffs_clean() -> None:
    assert diff_section(_netwatch_section(), [_live_row()]).empty


def test_a_real_duration_change_still_diffs() -> None:
    result = diff_section(_netwatch_section(), [_live_row(interval="2m")])
    assert [i.kind for i in result.items] == [OpKind.set]
    assert [c.prop for c in result.items[0].changes] == ["interval"]


def test_keepalive_compound_head_folds_but_count_does_not() -> None:
    section = ConfigSection(
        path="/interface/gre",
        owner_tag="sdwan:fabric:",
        key=("name",),
        items=[
            ConfigItem(
                tag="sdwan:fabric:core:link1",
                props={"name": "gre-link1", "keepalive": "60s,3"},
            )
        ],
    )
    live = [
        {
            ".id": "*1",
            "name": "gre-link1",
            "comment": "sdwan:fabric:core:link1",
            "keepalive": "1m,3",
        }
    ]
    assert diff_section(section, live).empty

    live[0]["keepalive"] = "1m,4"
    assert not diff_section(section, live).empty
