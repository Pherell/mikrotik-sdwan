"""QoS rendering: packet marks, per-uplink queue trees, cleanup, FastTrack."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.drivers.base import OpKind
from app.models.policy import AppGroup, Policy, SdwanGroup
from app.reconcile.diff import diff_section
from app.reconcile.merge import merge_sections
from app.render.policy import PathOption, SitePolicyView, _match_props, render_policies
from app.render.qos import (
    GUARANTEE_PERCENT,
    PRIORITY,
    QOS_CLASSES,
    QosUplink,
    SiteQosView,
    fasttrack_warnings,
    packet_mark,
    render_qos,
)
from app.schemas.policy import PolicyCreate, PolicyUpdate
from app.services.fabric import _cleanup_sections


def policy(name: str = "voice", **kw) -> Policy:
    defaults = dict(
        id=f"pol-{name}",
        name=name,
        priority=100,
        enabled=True,
        sdwan_group=SdwanGroup(
            id="grp", name="g", members=[{"uplink": "mpls", "weight": 1}],
            strategy="failover", tenant_id="default",
        ),
        src_prefixes=[],
        dst_prefixes=[],
        site_ids=[],
        fallback="any",
        tenant_id="default",
        qos_class=None,
    )
    return Policy(**{**defaults, **kw})


FIBRE = QosUplink(wan_name="fibre", interface="ether1", bandwidth_mbps=100)
LTE = QosUplink(wan_name="lte", interface="lte1", bandwidth_mbps=None)


def by_path(sections) -> dict:
    return {s.path: s for s in sections}


def qos(policies, uplinks=(FIBRE, LTE)) -> dict:
    return by_path(render_qos(SiteQosView("branch-1", list(policies), list(uplinks))))


# -- nothing to do ----------------------------------------------------------


def test_no_qos_class_renders_no_rows_but_keeps_the_sections() -> None:
    out = qos([policy()])
    assert out["/queue/tree"].items == []
    assert out["/ip/firewall/mangle"].items == []
    # Still emitted, so a cleared class is swept from the device.
    assert set(out) == {"/queue/tree", "/ip/firewall/mangle", "/ip/firewall/address-list"}


def test_no_uplink_bandwidth_renders_nothing() -> None:
    """Marks with no queue to use them only cost CPU per packet."""
    out = qos([policy(qos_class="realtime")], uplinks=[LTE])
    assert out["/queue/tree"].items == []
    assert out["/ip/firewall/mangle"].items == []


def test_disabled_policy_is_not_classified() -> None:
    out = qos([policy(qos_class="realtime", enabled=False)])
    assert out["/ip/firewall/mangle"].items == []


# -- queue tree -------------------------------------------------------------


def test_queues_only_for_wans_with_bandwidth() -> None:
    rows = [i.props for i in qos([policy(qos_class="bulk")])["/queue/tree"].items]
    parents = [r for r in rows if r["parent"] == "ether1"]
    assert len(parents) == 1
    assert parents[0]["max-limit"] == 100_000_000
    assert not any(r["parent"] == "lte1" for r in rows)
    assert all("lte" not in r["name"] for r in rows)


def test_class_leaves_have_priority_guarantee_and_mark() -> None:
    rows = {
        i.props["name"]: i.props
        for i in qos([policy(qos_class="realtime")])["/queue/tree"].items
    }
    # Parent first: a leaf names a parent that must already exist.
    first = qos([policy(qos_class="realtime")])["/queue/tree"].items[0].props
    assert first["name"] == "sdwan-qos-fibre"

    assert PRIORITY == {"realtime": 1, "interactive": 3, "default": 5, "bulk": 8}
    assert sum(GUARANTEE_PERCENT.values()) == 100
    for qos_class in QOS_CLASSES:
        leaf = rows[f"sdwan-qos-fibre-{qos_class}"]
        assert leaf["parent"] == "sdwan-qos-fibre"
        assert leaf["packet-mark"] == packet_mark(qos_class)
        assert leaf["priority"] == PRIORITY[qos_class]
        assert leaf["limit-at"] == 100_000_000 * GUARANTEE_PERCENT[qos_class] // 100
        assert leaf["max-limit"] == 100_000_000
        assert leaf["comment"].startswith("sdwan:qos:")
    assert rows["sdwan-qos-fibre-realtime"]["limit-at"] == 30_000_000
    assert rows["sdwan-qos-fibre-realtime"]["queue"] == "default-small"


# -- marking ----------------------------------------------------------------


def test_mark_rule_uses_the_policys_own_match() -> None:
    p = policy(
        qos_class="interactive",
        src_prefixes=["10.1.0.0/24"],
        protocol="udp",
        dst_ports="5060",
        dscp=46,
    )
    rules = qos([p])["/ip/firewall/mangle"].items
    mark = rules[0].props
    for k, v in _match_props(p).items():
        assert mark[k] == v
    assert mark["chain"] == "postrouting"
    assert mark["action"] == "mark-packet"
    assert mark["new-packet-mark"] == "sdwan-qos-interactive"
    assert mark["packet-mark"] == "no-mark"
    assert mark["passthrough"] is True
    assert rules[0].tag == "sdwan:qos:voice"


def test_marks_follow_priority_and_catch_all_is_last() -> None:
    rules = qos(
        [
            policy("backup", priority=200, qos_class="bulk"),
            policy("voice", priority=10, qos_class="realtime"),
        ]
    )["/ip/firewall/mangle"].items
    assert [r.tag for r in rules] == [
        "sdwan:qos:voice",
        "sdwan:qos:backup",
        "sdwan:qos:catch-all",
    ]
    catch_all = rules[-1].props
    assert catch_all["packet-mark"] == "no-mark"
    assert catch_all["new-packet-mark"] == "sdwan-qos-default"
    assert "src-address-list" not in catch_all


def test_qos_policy_without_a_path_still_gets_its_address_list() -> None:
    """Steering skips a policy with no uplink here; QoS still needs its list."""
    p = policy(qos_class="realtime", dst_prefixes=["192.0.2.0/24"])
    out = qos([p])
    lists = out["/ip/firewall/address-list"].items
    assert {(i.props["list"], i.props["address"]) for i in lists} == {
        ("sdwan-voice-dst", "192.0.2.0/24")
    }
    assert out["/ip/firewall/mangle"].items[0].props["dst-address-list"] == "sdwan-voice-dst"


def test_sni_policy_marks_by_the_steering_connection_mark() -> None:
    app = AppGroup(id="a", name="teams", sni_patterns=["*.teams.microsoft.com"],
                   prefixes=[], ports=[], tenant_id="default")
    p = policy(qos_class="realtime", app_group=app)
    mark = qos([p])["/ip/firewall/mangle"].items[0].props
    assert mark["connection-mark"] == "sdwan-voice-sni"


# -- coexisting with steering -----------------------------------------------


def _steered(p: Policy) -> list:
    return render_policies(
        SitePolicyView(
            site_name="branch-1",
            policies=[p],
            paths_by_tag={
                "mpls": [PathOption("mpls", "ether2", None, 1.0, ["10.255.0.2"])]
            },
            underlay_addresses=["203.0.113.9"],
            lan_prefixes=["10.1.0.0/24"],
        )
    )


def test_merge_with_policy_mangle_does_not_conflict() -> None:
    p = policy(qos_class="realtime", dst_prefixes=["192.0.2.0/24"])
    merged = by_path(
        merge_sections(
            [*_cleanup_sections(), *_steered(p), *render_qos(
                SiteQosView("branch-1", [p], [FIBRE])
            )]
        )
    )
    mangle = merged["/ip/firewall/mangle"]
    assert mangle.ordered and mangle.owner_tag == "sdwan:"
    tags = [i.tag for i in mangle.items]
    assert "sdwan:qos:voice" in tags and "sdwan:policy:voice" in tags
    # Steering's accept guards live in prerouting/output only; the QoS marks
    # are in postrouting, so no guard can stop traversal before them.
    guards = [i.props for i in mangle.items if i.props["action"] == "accept"]
    assert guards and all(g["chain"] in {"prerouting", "output"} for g in guards)
    qos_rows = [i.props for i in mangle.items if i.tag.startswith("sdwan:qos:")]
    assert all(r["chain"] == "postrouting" for r in qos_rows)
    # Packet marks never set a routing or connection mark.
    assert not any("new-routing-mark" in r or "new-connection-mark" in r for r in qos_rows)
    # The shared address list collapsed to one row instead of conflicting.
    lists = [
        i for i in merged["/ip/firewall/address-list"].items
        if i.props["list"] == "sdwan-voice-dst"
    ]
    assert len(lists) == 1
    assert "/queue/tree" in merged


def test_merge_sorts_queue_tree_with_the_cleanup_scope() -> None:
    merged = by_path(merge_sections([*_cleanup_sections(), *render_qos(
        SiteQosView("branch-1", [policy(qos_class="bulk")], [FIBRE]))]))
    assert merged["/queue/tree"].key == ("name",)
    assert merged["/queue/tree"].owner_tag == "sdwan:"


# -- cleanup ----------------------------------------------------------------


def _live_queue_rows() -> list[dict]:
    out = qos([policy(qos_class="bulk")], uplinks=[FIBRE])["/queue/tree"].items
    rows = [{".id": f"*{n}", **i.props} for n, i in enumerate(out, 1)]
    rows.append({".id": "*99", "name": "operator-queue", "parent": "ether1",
                 "comment": "hand made"})
    return rows


def test_removing_qos_sweeps_the_queue_tree() -> None:
    merged = by_path(merge_sections([*_cleanup_sections(), *render_qos(
        SiteQosView("branch-1", [policy()], [FIBRE]))]))
    diff = diff_section(merged["/queue/tree"], _live_queue_rows())
    removed = {i.item_id for i in diff.items if i.kind is OpKind.remove}
    assert len(removed) == 1 + len(QOS_CLASSES)
    assert "*99" not in removed, "an operator's own queue is never touched"


def test_clearing_bandwidth_sweeps_the_queue_tree() -> None:
    merged = by_path(merge_sections([*_cleanup_sections(), *render_qos(
        SiteQosView("branch-1", [policy(qos_class="bulk")],
                    [QosUplink("fibre", "ether1", None)]))]))
    diff = diff_section(merged["/queue/tree"], _live_queue_rows())
    assert sum(i.kind is OpKind.remove for i in diff.items) == 1 + len(QOS_CLASSES)


def test_rendered_queue_tree_is_clean_against_itself() -> None:
    sec = qos([policy(qos_class="bulk")], uplinks=[FIBRE])["/queue/tree"]
    live = [{".id": f"*{n}", **i.props, "bytes": 123, "rate": 5}
            for n, i in enumerate(sec.items, 1)]
    assert diff_section(sec, live).empty


# -- schema -----------------------------------------------------------------


def test_schema_accepts_known_classes_and_null() -> None:
    for qos_class in (*QOS_CLASSES, None):
        body = PolicyCreate(name="x", sdwan_group_id="g", qos_class=qos_class)
        assert body.qos_class == qos_class
    assert PolicyUpdate(qos_class=None).model_dump(exclude_unset=True) == {"qos_class": None}


def test_schema_rejects_unknown_class() -> None:
    with pytest.raises(ValidationError):
        PolicyCreate(name="x", sdwan_group_id="g", qos_class="platinum")
    with pytest.raises(ValidationError):
        PolicyUpdate(qos_class="platinum")


# -- FastTrack --------------------------------------------------------------


def test_fasttrack_rule_is_reported() -> None:
    rows = [
        {".id": "*1", "chain": "forward", "action": "fasttrack-connection",
         "connection-state": "established,related", "comment": "defconf: fasttrack"},
        {".id": "*2", "chain": "forward", "action": "accept"},
    ]
    warnings = fasttrack_warnings(rows)
    assert len(warnings) == 1
    assert "defconf: fasttrack" in warnings[0]


def test_disabled_fasttrack_rule_is_not_reported() -> None:
    assert fasttrack_warnings(
        [{".id": "*1", "action": "fasttrack-connection", "disabled": True}]
    ) == []
    assert fasttrack_warnings(
        [{".id": "*1", "action": "fasttrack-connection", "disabled": "true"}]
    ) == []
    assert fasttrack_warnings([]) == []
