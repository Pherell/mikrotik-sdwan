"""Filtered reads (plan B6): a plan must ask the device only for what the
differ reads, and must reach exactly the same diff as a full read.

The correctness risk of asking for less is asymmetric: a property the
differ needs but the proplist forgot reads back as ``None`` and a fully
converged device diffs as "set everything, forever". So the centre of
this module is not that the requests are smaller -- it is that filtered
and unfiltered plans over the golden fixtures are *identical*, empty
when the device matches intent and equal when it has drifted.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.drivers.base import ConfigItem, ConfigSection
from app.drivers.ros7_rest import Ros7RestDriver
from app.reconcile.diff import SectionDiff, diff_section
from app.reconcile.merge import merge_sections
from app.reconcile.plan import build_plan, section_proplist
from app.render.fabric import render_fabric
from app.render.policy import render_policies
from app.render.site import render_site
from app.transports import get_transport
from tests.fakeros.server import FakeRouterOS
from tests.test_golden_render import _fabric_view, _policy_view, _site


def _golden_sections() -> list[ConfigSection]:
    """The golden renders combined the way render_device combines them --
    through the real merge, which among other things widens the owner tag
    to the scope every merged renderer shares. (A hand-rolled merge that
    keeps the first section's narrow tag makes rows owned under a wider
    tag invisible to the differ; that mistake is easy to make and looks
    exactly like mass drift.)"""
    renders = [
        render_site(_site()),
        render_fabric(_fabric_view(), get_transport("wireguard")),
        render_policies(_policy_view()),
    ]
    return merge_sections([s for sections in renders for s in sections])


def _menus_for(sections: list[ConfigSection]) -> dict[str, list[dict[str, Any]]]:
    """A device that already matches intent exactly, as raw menu rows."""
    menus: dict[str, list[dict[str, Any]]] = {}
    for section in sections:
        rows = menus.setdefault(section.path.strip("/"), [])
        for item in section.items:
            row = dict(item.props)
            if section.comment_capable:
                row["comment"] = item.tag or section.owner_tag
            rows.append(row)
    return menus


def _driver(fake: FakeRouterOS) -> Ros7RestDriver:
    return Ros7RestDriver(
        "test-router",
        "admin",
        "secret",
        transport=httpx.ASGITransport(app=fake.app),
    )


def _signature(diff: SectionDiff) -> list[tuple[str, str, str, str]]:
    """A comparable fingerprint of a section diff: kind, identity, the
    props an op would carry, and the field changes behind a set."""
    out = []
    for item in diff.items:
        changes = ",".join(f"{c.prop}:{c.before}->{c.after}" for c in item.changes)
        props = ",".join(f"{k}={v!r}" for k, v in sorted(item.props.items()))
        out.append((item.kind.value, repr(item.identity), props, changes))
    return sorted(out)


# -- the driver's half --------------------------------------------------------


async def test_read_with_props_sends_proplist_and_returns_only_those_props(
    fake_ros: FakeRouterOS, driver: Ros7RestDriver
) -> None:
    rows = await driver.read("/ip/address", props=["address", "interface"])
    assert rows, "the fixture menu is not empty"
    assert all(set(row) <= {"address", "interface"} for row in rows)
    path, query = fake_ros.reads[-1]
    assert path == "ip/address"
    assert query[".proplist"] == "address,interface"

    full = await driver.read("/ip/address")
    assert any("disabled" in row for row in full), (
        "an unfiltered read must still return everything -- props is opt-in"
    )


# -- the derivation -----------------------------------------------------------


def test_proplist_covers_everything_the_differ_reads() -> None:
    section = ConfigSection(
        path="/ip/firewall/filter",
        key=("chain", "src-address"),
        before={"chain": "output", "action": "accept"},
        items=[
            ConfigItem(
                props={"chain": "input", "src-address": "10.0.0.0/8", "protocol": "tcp"},
                tag="sdwan:x",
            )
        ],
    )
    props = set(section_proplist(section))
    assert props == {
        ".id",
        "comment",  # ownership + identity
        "name",  # identity for comment-less sections
        "chain",
        "src-address",  # key props
        "action",  # only in the before predicate -- still read by _order
        "protocol",  # only in intent -- _compare reads it back
    }


def test_proplist_for_a_cleanup_section_is_minimal() -> None:
    """An empty section removes orphans by identity alone; asking for more
    would download the whole menu to delete nothing."""
    section = ConfigSection(path="/ip/ipsec/peer", key=("name",), items=[])
    assert set(section_proplist(section)) == {".id", "comment", "name"}


def test_proplist_omits_comment_where_the_menu_rejects_it() -> None:
    section = ConfigSection(
        path="/ip/ipsec/profile",
        key=("name",),
        comment_capable=False,
        items=[ConfigItem(props={"name": "p", "hash-algorithm": "sha256"}, tag="")],
    )
    props = set(section_proplist(section))
    assert "comment" not in props
    assert {"name", "hash-algorithm", ".id"} <= props


# -- plan-level equivalence -----------------------------------------------------


async def test_converged_device_diffs_empty_under_filtered_reads() -> None:
    sections = _golden_sections()
    fake = FakeRouterOS(password="secret", menus=_menus_for(sections))
    async with _driver(fake) as driver:
        fake.reads.clear()
        plan = await build_plan(driver, sections)

        assert plan.empty, plan.render()
        assert plan.unreadable == {}

        # The read profile: exactly one read per rendered section, every
        # one carrying a proplist, and no menu no section named was read.
        read_paths = [path for path, _ in fake.reads]
        assert sorted(read_paths) == sorted(s.path.strip("/") for s in sections)
        assert all(".proplist" in query for _, query in fake.reads)

        # The same sections diffed against full, unfiltered reads are
        # equally empty -- filtering changed the wire, not the answer.
        for section in sections:
            rows = await driver.read(section.path)
            assert diff_section(section, rows).empty


async def test_filtered_and_unfiltered_plans_agree_on_a_drifted_device() -> None:
    sections = _golden_sections()
    menus = _menus_for(sections)

    # Drift of each kind: a row the device lost (becomes an add), a row
    # whose comparable property moved (becomes a set), and an extra owned
    # row intent no longer asks for (becomes a remove).
    victim = next(s for s in sections if len(s.items) >= 2)
    menus[victim.path.strip("/")].pop()

    changed = next(
        s
        for s in sections
        if any(p not in s.key for p in s.items[0].props)
    )
    row = menus[changed.path.strip("/")][0]
    prop = next(p for p in changed.items[0].props if p not in changed.key)
    row[prop] = f"{row[prop]}-drifted" if isinstance(row[prop], str) else 424242

    extra_section = sections[0]
    orphan = dict(extra_section.items[0].props)
    if extra_section.comment_capable:
        orphan["comment"] = "sdwan:orphan-from-a-deleted-intent"
    if extra_section.key:
        for k in extra_section.key:
            orphan[k] = f"{orphan.get(k, '')}-orphan"
    menus[extra_section.path.strip("/")].append(orphan)

    fake = FakeRouterOS(password="secret", menus=menus)
    async with _driver(fake) as driver:
        plan = await build_plan(driver, sections)
        assert not plan.empty
        filtered = {d.path: _signature(d) for d in plan.sections}

        baseline = {}
        for section in sections:
            rows = await driver.read(section.path)
            baseline[section.path] = _signature(diff_section(section, rows))

    assert filtered == baseline
    assert plan.counts == {"add": 1, "set": 1, "remove": 1}


async def test_cleanup_read_still_finds_orphans_with_a_minimal_proplist() -> None:
    section = ConfigSection(path="/ip/ipsec/peer", key=("name",), items=[])
    fake = FakeRouterOS(
        password="secret",
        menus={
            "ip/ipsec/peer": [
                {
                    "name": "gone-peer",
                    "comment": "sdwan:gone",
                    "address": "198.51.100.1/32",
                    "exchange-mode": "ike2",
                    "passive": False,
                },
                {"name": "operators-own", "address": "203.0.113.5/32"},
            ]
        },
    )
    async with _driver(fake) as driver:
        plan = await build_plan(driver, [section])

    assert plan.counts == {"add": 0, "set": 0, "remove": 1}
    _, query = fake.reads[-1]
    assert set(query[".proplist"].split(",")) == {".id", "comment", "name"}
