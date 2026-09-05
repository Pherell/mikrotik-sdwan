"""Render, diff, and safe apply.

The two properties that matter most here:

* **Idempotency.** Applying a plan and re-planning must produce no changes. If
  this breaks, the controller pushes the same config to every device forever.
* **Ownership.** Hand-built rows on the same device must survive untouched.
"""

from __future__ import annotations

import httpx
import pytest

from app.drivers.base import ConfigItem, ConfigOp, DriverError, OpKind
from app.drivers.ros7_rest import Ros7RestDriver
from app.models.enums import SiteRole
from app.models.site import Site
from app.reconcile.apply import find_stale_rollbacks, safe_apply
from app.reconcile.diff import diff_section
from app.reconcile.plan import build_plan
from app.render.engine import ORDER, owner_tag, section
from app.render.site import LOOPBACK_NAME, render_site
from tests.fakeros.server import FakeRouterOS


def make_site(**overrides) -> Site:
    defaults = dict(
        name="branch-1",
        mgmt_host="203.0.113.10",
        username="admin",
        role=SiteRole.spoke,
        verify_tls=False,
        loopback_ip="10.254.0.7",
        local_prefixes=["192.168.10.0/24", "192.168.11.0/24"],
        tenant_id="default",
    )
    return Site(**{**defaults, **overrides})


@pytest.fixture
def bare_ros() -> FakeRouterOS:
    """A router with the menus the site renderer touches, all empty."""
    return FakeRouterOS(
        password="secret",
        menus={
            "interface/bridge": [],
            "ip/address": [],
            "ip/firewall/address-list": [],
            "system/scheduler": [],
        },
    )


@pytest.fixture
async def bare_driver(bare_ros: FakeRouterOS):
    d = Ros7RestDriver(
        "r", "admin", "secret", transport=httpx.ASGITransport(app=bare_ros.app)
    )
    await d.connect()
    try:
        yield d
    finally:
        await d.close()


# -- rendering --------------------------------------------------------------


def test_owner_tag_shape() -> None:
    assert owner_tag("site", "branch-1") == "sdwan:site:branch-1"
    assert owner_tag("site", "has space") == "sdwan:site:has-space"


def test_section_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown section kind"):
        section("/ip/address", "not-a-kind", owner="sdwan:x")


def test_render_site_produces_ordered_sections() -> None:
    sections = render_site(make_site())
    by_path = {s.path: s for s in sections}

    assert set(by_path) == {
        "/interface/bridge",
        "/ip/address",
        "/ip/firewall/address-list",
    }
    # Address lists come before interfaces, addresses after them.
    assert by_path["/ip/firewall/address-list"].order == ORDER["address_list"]
    assert by_path["/interface/bridge"].order < by_path["/ip/address"].order


def test_loopback_is_forced_to_a_host_route() -> None:
    """A /24 on the loopback would blackhole every other site's loopback."""
    sections = {s.path: s for s in render_site(make_site(loopback_ip="10.254.0.7"))}
    addresses = sections["/ip/address"].items

    assert len(addresses) == 1
    assert addresses[0].props["address"] == "10.254.0.7/32"
    assert addresses[0].props["interface"] == LOOPBACK_NAME


def test_site_without_a_loopback_renders_no_address() -> None:
    sections = {s.path: s for s in render_site(make_site(loopback_ip=None))}
    assert sections["/ip/address"].items == []


def test_local_prefixes_become_an_address_list() -> None:
    sections = {s.path: s for s in render_site(make_site())}
    items = sections["/ip/firewall/address-list"].items

    assert [i.props["address"] for i in items] == [
        "192.168.10.0/24",
        "192.168.11.0/24",
    ]
    assert {i.props["list"] for i in items} == {"sdwan-local-branch-1"}


# -- diffing ----------------------------------------------------------------


def test_diff_on_an_empty_device_is_all_adds() -> None:
    sections = render_site(make_site())
    bridge = next(s for s in sections if s.path == "/interface/bridge")

    result = diff_section(bridge, live_rows=[])

    assert [i.kind for i in result.items] == [OpKind.add]
    assert result.items[0].props["comment"].startswith("sdwan:site:branch-1")


def test_diff_ignores_rows_the_controller_does_not_own() -> None:
    sections = render_site(make_site())
    bridge = next(s for s in sections if s.path == "/interface/bridge")

    live = [
        {".id": "*1", "name": "bridge-lan", "comment": "hand built, do not touch"},
        {".id": "*2", "name": "bridge-guest"},  # no comment at all
    ]
    result = diff_section(bridge, live_rows=live)

    # One add for our loopback; the operator's bridges are not removed.
    assert [i.kind for i in result.items] == [OpKind.add]
    assert all(i.identity != ("bridge-lan",) for i in result.items)


def test_diff_is_empty_when_the_device_already_matches() -> None:
    """The idempotency property, at section level. Note the device values are
    strings -- that is what RouterOS returns."""
    sections = render_site(make_site())
    bridge = next(s for s in sections if s.path == "/interface/bridge")

    live = [
        {
            ".id": "*1",
            "name": LOOPBACK_NAME,
            "protocol-mode": "none",
            "comment": "sdwan:site:branch-1:loopback",
            # Device-populated noise that is not intent.
            "running": "true",
            "actual-mtu": "1500",
            "dynamic": "false",
        }
    ]
    assert diff_section(bridge, live_rows=live).empty


def test_diff_detects_a_changed_property() -> None:
    sections = render_site(make_site())
    bridge = next(s for s in sections if s.path == "/interface/bridge")

    live = [
        {
            ".id": "*1",
            "name": LOOPBACK_NAME,
            "protocol-mode": "rstp",  # someone turned STP on
            "comment": "sdwan:site:branch-1:loopback",
        }
    ]
    result = diff_section(bridge, live_rows=live)

    assert [i.kind for i in result.items] == [OpKind.set]
    change = result.items[0]
    assert change.item_id == "*1"
    assert change.props == {"protocol-mode": "none"}
    assert change.changes[0].before == "rstp"


def test_diff_removes_a_managed_row_that_intent_dropped() -> None:
    site = make_site(local_prefixes=["192.168.10.0/24"])
    sections = {s.path: s for s in render_site(site)}
    lists = sections["/ip/firewall/address-list"]

    live = [
        {
            ".id": "*1",
            "list": "sdwan-local-branch-1",
            "address": "192.168.10.0/24",
            "comment": "sdwan:site:branch-1:local",
        },
        {
            ".id": "*2",
            "list": "sdwan-local-branch-1",
            "address": "192.168.99.0/24",  # no longer in intent
            "comment": "sdwan:site:branch-1:local",
        },
        {
            ".id": "*3",
            "list": "blocklist",
            "address": "203.0.113.0/24",  # someone else's list
        },
    ]
    result = diff_section(lists, live_rows=live)

    removals = [i for i in result.items if i.kind is OpKind.remove]
    assert len(removals) == 1
    assert removals[0].item_id == "*2"


def test_diff_corrects_a_retagged_row() -> None:
    sections = render_site(make_site())
    bridge = next(s for s in sections if s.path == "/interface/bridge")

    live = [
        {
            ".id": "*1",
            "name": LOOPBACK_NAME,
            "protocol-mode": "none",
            # Still owned (prefix matches) but the tag drifted.
            "comment": "sdwan:site:branch-1:loopback:stale",
        }
    ]
    result = diff_section(bridge, live_rows=live)

    assert [i.kind for i in result.items] == [OpKind.set]
    assert result.items[0].props == {"comment": "sdwan:site:branch-1:loopback"}


def test_write_once_properties_are_never_diffed() -> None:
    """Generated keys cannot be read back in a comparable form."""
    tag = "sdwan:test:wg"
    sec = section(
        "/interface/wireguard",
        "tunnel",
        owner=tag,
        key=("name",),
        items=[ConfigItem(props={"name": "wg0", "private-key": "AAAA"}, tag=tag)],
        write_once=("private-key",),
    )
    live = [{".id": "*1", "name": "wg0", "private-key": "different", "comment": tag}]

    assert diff_section(sec, live_rows=live).empty


def test_diff_masks_secrets_in_rendered_output() -> None:
    tag = "sdwan:test:ipsec"
    sec = section(
        "/ip/ipsec/identity",
        "crypto",
        owner=tag,
        key=("peer",),
        items=[
            ConfigItem(props={"peer": "hub1", "secret": "supersecretvalue"}, tag=tag)
        ],
    )
    rendered = "\n".join(diff_section(sec, live_rows=[]).render())

    assert "supersecretvalue" not in rendered
    assert "supe" in rendered


# -- planning ---------------------------------------------------------------


async def test_plan_orders_adds_low_to_high(bare_driver) -> None:
    plan = await build_plan(bare_driver, render_site(make_site()))
    paths = [op.path for op in plan.ops()]

    assert paths.index("/ip/firewall/address-list") < paths.index("/interface/bridge")
    assert paths.index("/interface/bridge") < paths.index("/ip/address")


async def test_plan_skips_a_menu_it_cannot_read(bare_ros: FakeRouterOS) -> None:
    """A failed read must never be treated as 'the menu is empty', which would
    diff as 'delete everything the controller manages in it'."""
    del bare_ros.menus["ip/firewall/address-list"]
    async with Ros7RestDriver(
        "r", "admin", "secret", transport=httpx.ASGITransport(app=bare_ros.app)
    ) as d:
        plan = await build_plan(d, render_site(make_site()))

    assert "/ip/firewall/address-list" in plan.unreadable
    assert all(s.path != "/ip/firewall/address-list" for s in plan.sections)


async def test_apply_then_replan_is_a_no_op(bare_driver) -> None:
    """The single most important test in the project."""
    site = make_site()
    first = await build_plan(bare_driver, render_site(site))
    assert not first.empty

    result = await bare_driver.apply(first.ops())
    assert result.ok, result.error

    second = await build_plan(bare_driver, render_site(site))
    assert second.empty, second.render()


async def test_apply_does_not_disturb_unmanaged_config(bare_driver, bare_ros) -> None:
    bare_ros.rows("interface/bridge").append(
        {".id": "*99", "name": "bridge-lan", "comment": "operator built"}
    )
    bare_ros.rows("ip/firewall/address-list").append(
        {".id": "*98", "list": "blocklist", "address": "198.51.100.0/24"}
    )

    plan = await build_plan(bare_driver, render_site(make_site()))
    await bare_driver.apply(plan.ops())

    bridges = {r["name"] for r in bare_ros.rows("interface/bridge")}
    assert "bridge-lan" in bridges
    assert any(r.get("list") == "blocklist" for r in bare_ros.rows("ip/firewall/address-list"))


async def test_plan_json_is_redacted_and_serializable(bare_driver) -> None:
    plan = await build_plan(bare_driver, render_site(make_site()))
    payload = plan.to_json()

    assert payload["counts"]["add"] == 4  # 1 bridge + 1 address + 2 prefixes
    assert payload["empty"] is False
    assert isinstance(payload["text"], str)
    import json

    json.dumps(payload)  # must survive going into a JSONB column


# -- safe apply -------------------------------------------------------------


async def test_safe_apply_backs_up_arms_and_disarms(bare_driver, bare_ros) -> None:
    plan = await build_plan(bare_driver, render_site(make_site()))

    outcome = await safe_apply(bare_driver, plan.ops(), job_id="job-abcdef12", timeout_seconds=90)

    assert outcome.ok, outcome.error
    assert outcome.backup_name == "sdwan-pre-job-abcd"
    # Backup was taken before the scheduler existed, so a restore removes it.
    assert ("system/backup/save", {"name": "sdwan-pre-job-abcd", "dont-encrypt": "true"}) in (
        bare_ros.commands
    )
    # Disarmed on success: nothing left to fire.
    assert await find_stale_rollbacks(bare_driver) == []
    assert outcome.rollback_armed is False


async def test_safe_apply_arms_before_pushing(bare_ros) -> None:
    """The scheduler must exist before the first mutating op, or a push that
    kills management access has no safety net."""
    order: list[str] = []
    original_put = bare_ros._put

    async def spy(path, request):
        order.append(path)
        return await original_put(path, request)

    bare_ros._put = spy  # type: ignore[method-assign]

    async with Ros7RestDriver(
        "r", "admin", "secret", transport=httpx.ASGITransport(app=bare_ros.app)
    ) as d:
        plan = await build_plan(d, render_site(make_site()))
        await safe_apply(d, plan.ops(), job_id="job-11112222")

    assert order[0] == "system/scheduler"


async def test_safe_apply_leaves_rollback_armed_when_the_device_stops_answering(
    bare_ros,
) -> None:
    """The failure this whole mechanism exists for."""
    async with Ros7RestDriver(
        "r", "admin", "secret", transport=httpx.ASGITransport(app=bare_ros.app)
    ) as d:
        plan = await build_plan(d, render_site(make_site()))

        # Simulate the push cutting off management: everything after this 401s.
        async def brick(*_args, **_kwargs):
            bare_ros.password = "changed-by-the-push"

        original = d._apply_one

        async def apply_then_brick(op):
            await original(op)
            # Brick partway through, so ops after this one fail too. The
            # operator must still be told the rollback is armed, not just handed
            # the 401 from the next request.
            if op.path == "/interface/bridge":
                await brick()

        d._apply_one = apply_then_brick  # type: ignore[method-assign]

        outcome = await safe_apply(d, plan.ops(), job_id="job-deadbeef")

    assert outcome.ok is False
    assert outcome.rollback_armed is True
    assert "rollback is armed" in (outcome.error or "")
    # The underlying failure is kept, but after the thing that matters.
    assert "authentication rejected" in (outcome.error or "")
    # The scheduler is still there, so the router restores itself.
    assert any(
        r["name"] == "sdwan-rollback-job-dead" for r in bare_ros.rows("system/scheduler")
    )


async def test_safe_apply_refuses_to_push_without_a_backup(bare_ros) -> None:
    async with Ros7RestDriver(
        "r", "admin", "secret", transport=httpx.ASGITransport(app=bare_ros.app)
    ) as d:
        async def no_backup(_name: str) -> None:
            raise DriverError("no free space on the device")

        d.backup = no_backup  # type: ignore[method-assign]
        outcome = await safe_apply(
            d,
            [ConfigOp(kind=OpKind.add, path="/interface/bridge", props={"name": "x"})],
            job_id="job-nobackup",
        )

    assert outcome.ok is False
    assert "backup" in (outcome.error or "")
    assert outcome.rollback_armed is False
    # Nothing was pushed.
    assert bare_ros.rows("interface/bridge") == []


async def test_safe_apply_with_no_ops_does_nothing(bare_driver, bare_ros) -> None:
    outcome = await safe_apply(bare_driver, [], job_id="job-empty")

    assert outcome.ok is True
    assert bare_ros.commands == []
    assert bare_ros.rows("system/scheduler") == []


async def test_rollback_scheduler_restores_the_backup_it_was_given(
    bare_driver, bare_ros
) -> None:
    """Guard the on-event string: a typo here is invisible until the day it is
    the only thing standing between you and a bricked WAN edge."""
    async def never_reachable() -> bool:
        return False

    import app.reconcile.apply as apply_mod

    original = apply_mod._verify
    apply_mod._verify = lambda _d: never_reachable()  # type: ignore[assignment]
    try:
        await safe_apply(
            bare_driver,
            [ConfigOp(kind=OpKind.add, path="/interface/bridge", props={"name": "x"})],
            job_id="job-cafe0001",
            timeout_seconds=45,
        )
    finally:
        apply_mod._verify = original  # type: ignore[assignment]

    entry = bare_ros.rows("system/scheduler")[0]
    assert entry["interval"] == "45s"
    assert entry["on-event"] == '/system/backup/load name=sdwan-pre-job-cafe password=""'
    assert "reboot" in entry["policy"]
