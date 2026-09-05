"""Policy rendering: mangle marks, routing tables, and SLA-driven failover."""

from __future__ import annotations

import pytest

from app.models.policy import AppGroup, Policy, SlaProfile
from app.render.policy import (
    SLA_PENALTY,
    PathOption,
    SitePolicyView,
    render_policies,
)


def path(name: str, hops: list[str], cost: float = 1.0) -> PathOption:
    return PathOption(
        wan_name=name, interface=f"ether-{name}", gateway=None, cost=cost, next_hops=hops
    )


def policy(**kw) -> Policy:
    defaults = dict(
        id=f"pol-{kw.get('name', 'p')}",
        name="voice",
        priority=100,
        enabled=True,
        prefer_tags=["mpls"],
        src_prefixes=[],
        dst_prefixes=[],
        site_ids=[],
        fallback="any",
        tenant_id="default",
    )
    return Policy(**{**defaults, **kw})


def view(policies: list[Policy], **paths: list[PathOption]) -> SitePolicyView:
    return SitePolicyView(site_name="branch-1", policies=policies, paths_by_tag=paths)


def sections_of(v: SitePolicyView) -> dict:
    return {s.path: s for s in render_policies(v)}


# -- the pipeline -----------------------------------------------------------


def test_a_policy_renders_the_whole_chain() -> None:
    s = sections_of(
        view([policy(dst_prefixes=["10.9.0.0/24"])], mpls=[path("wan1", ["10.255.0.0"])])
    )

    assert set(s) == {
        "/ip/firewall/address-list",
        "/routing/table",
        "/ip/route",
        "/ip/firewall/mangle",
        "/tool/netwatch",
    }
    assert s["/ip/firewall/mangle"].items[0].props["new-routing-mark"] == "sdwan-voice"
    assert s["/routing/table"].items[0].props["name"] == "sdwan-voice"


def test_routing_table_installs_into_the_fib() -> None:
    """Without fib=yes the table exists but never installs a route, and marked
    traffic silently falls through to main."""
    s = sections_of(view([policy()], mpls=[path("wan1", ["10.255.0.0"])]))
    assert s["/routing/table"].items[0].props["fib"] is True


def test_steering_points_at_the_overlay_not_the_wan_gateway() -> None:
    """Routing to the WAN gateway would push policy traffic onto the internet
    in the clear."""
    s = sections_of(view([policy()], mpls=[path("wan1", ["10.255.0.0"])]))
    route = s["/ip/route"].items[0].props

    assert route["gateway"] == "10.255.0.0"
    assert route["check-gateway"] == "ping"


def test_preference_order_becomes_route_distance() -> None:
    p = policy(prefer_tags=["mpls", "broadband"])
    s = sections_of(
        view(
            [p],
            mpls=[path("wan1", ["10.255.0.0"])],
            broadband=[path("wan2", ["10.255.0.2"])],
        )
    )
    by_gateway = {i.props["gateway"]: i.props["distance"] for i in s["/ip/route"].items}

    assert by_gateway["10.255.0.0"] == 1
    assert by_gateway["10.255.0.2"] == 2


def test_cheaper_uplink_wins_within_the_same_tag() -> None:
    s = sections_of(
        view(
            [policy()],
            mpls=[
                path("expensive", ["10.255.0.4"], cost=9.0),
                path("cheap", ["10.255.0.0"], cost=1.0),
            ],
        )
    )
    distances = {i.props["gateway"]: i.props["distance"] for i in s["/ip/route"].items}
    assert distances["10.255.0.0"] < distances["10.255.0.4"]


def test_fallback_any_adds_a_last_resort_route() -> None:
    s = sections_of(view([policy(fallback="any")], mpls=[path("wan1", ["10.255.0.0"])]))
    fallback = [i for i in s["/ip/route"].items if i.props["gateway"] == "main"]

    assert len(fallback) == 1
    assert fallback[0].props["distance"] == 250


def test_fallback_drop_leaves_no_escape_route() -> None:
    s = sections_of(view([policy(fallback="drop")], mpls=[path("wan1", ["10.255.0.0"])]))
    assert all(i.props["gateway"] != "main" for i in s["/ip/route"].items)


# -- the failure modes worth guarding --------------------------------------


def test_a_policy_with_no_matching_uplink_here_renders_nothing() -> None:
    """Marking traffic into an empty table blackholes it. Leaving it on the main
    table is worse for the policy and much better for the site."""
    s = sections_of(view([policy(prefer_tags=["satellite"])], mpls=[path("wan1", ["10.255.0.0"])]))

    assert s["/ip/firewall/mangle"].items == []
    assert s["/routing/table"].items == []


def test_an_uplink_with_no_tunnels_is_not_a_path() -> None:
    """A WAN carrying the right tag but no links leads nowhere."""
    s = sections_of(view([policy()], mpls=[path("wan1", [])]))
    assert s["/ip/firewall/mangle"].items == []


def test_a_disabled_policy_renders_nothing() -> None:
    s = sections_of(view([policy(enabled=False)], mpls=[path("wan1", ["10.255.0.0"])]))
    assert s["/ip/firewall/mangle"].items == []


def test_no_policies_still_emits_empty_sections() -> None:
    """A deleted policy must have its rules swept off the device, which only
    happens if a section still covers the menu."""
    s = sections_of(view([]))

    assert set(s) >= {"/ip/firewall/mangle", "/routing/table", "/ip/route"}
    assert all(sec.items == [] for sec in s.values())


def test_mangle_is_position_sensitive_and_ordered_by_priority() -> None:
    """RouterOS evaluates the chain top to bottom and the first match wins, so
    the order is the semantics."""
    high = policy(name="critical", priority=10, prefer_tags=["mpls"])
    low = policy(name="bulk", priority=900, prefer_tags=["mpls"])
    s = sections_of(view([low, high], mpls=[path("wan1", ["10.255.0.0"])]))

    assert s["/ip/firewall/mangle"].ordered is True
    marks = [i.props["new-routing-mark"] for i in s["/ip/firewall/mangle"].items]
    assert marks == ["sdwan-critical", "sdwan-bulk"]


def test_a_port_match_always_carries_a_protocol() -> None:
    """RouterOS rejects dst-port without protocol."""
    s = sections_of(view([policy(dst_ports="443")], mpls=[path("wan1", ["10.255.0.0"])]))
    props = s["/ip/firewall/mangle"].items[0].props

    assert props["dst-port"] == "443"
    assert props["protocol"] == "tcp"


def test_explicit_protocol_is_not_overridden() -> None:
    s = sections_of(
        view([policy(dst_ports="5060", protocol="udp")], mpls=[path("wan1", ["10.255.0.0"])])
    )
    assert s["/ip/firewall/mangle"].items[0].props["protocol"] == "udp"


# -- SLA --------------------------------------------------------------------


def test_netwatch_carries_the_profile_thresholds() -> None:
    sla = SlaProfile(
        name="voice",
        loss_percent=2,
        latency_ms=150,
        jitter_ms=30,
        probe_interval_seconds=5,
        probe_count=20,
        recovery_seconds=30,
    )
    p = policy(sla_profile_id="sla-1")
    p.sla_profile = sla

    s = sections_of(view([p], mpls=[path("wan1", ["10.255.0.0"])]))
    props = s["/tool/netwatch"].items[0].props

    assert props["thr-loss-percent"] == 2
    assert props["thr-latency"] == "150ms"
    assert props["thr-jitter"] == "30ms"
    assert props["interval"] == "5s"
    assert props["packet-count"] == 20


def test_a_policy_without_a_profile_falls_back_to_sane_defaults() -> None:
    s = sections_of(view([policy()], mpls=[path("wan1", ["10.255.0.0"])]))
    props = s["/tool/netwatch"].items[0].props

    assert props["thr-loss-percent"] == 20
    assert props["interval"] == "10s"


def test_breaching_the_sla_demotes_rather_than_removes() -> None:
    """The route must stay in the table so the path can be re-preferred the
    moment it recovers."""
    p = policy(prefer_tags=["mpls", "broadband"])
    s = sections_of(
        view(
            [p],
            mpls=[path("wan1", ["10.255.0.0"])],
            broadband=[path("wan2", ["10.255.0.2"])],
        )
    )
    primary = next(i for i in s["/tool/netwatch"].items if i.props["host"] == "10.255.0.0")

    assert f"distance={1 + SLA_PENALTY}" in primary.props["down-script"]
    assert "distance=1" in primary.props["up-script"]
    # Demoted below the backup (2) but still above the "any" fallback (250).
    assert 2 < 1 + SLA_PENALTY < 250


def test_the_demotion_script_is_absolute_not_cumulative() -> None:
    """An earlier version added a penalty to the current distance, so two down
    events compounded and the path never recovered its preference."""
    s = sections_of(view([policy()], mpls=[path("wan1", ["10.255.0.0"])]))
    script = s["/tool/netwatch"].items[0].props["down-script"]

    assert "+" not in script
    assert "/ip/route/get" not in script


def test_scripts_are_a_single_line() -> None:
    """RouterOS stores scripts verbatim; a multi-line value round-trips with
    whitespace changes and diffs dirty forever."""
    s = sections_of(view([policy()], mpls=[path("wan1", ["10.255.0.0"])]))
    for item in s["/tool/netwatch"].items:
        assert "\n" not in item.props["down-script"]
        assert "\n" not in item.props["up-script"]


# -- app groups -------------------------------------------------------------


def test_an_app_group_contributes_prefixes_ports_and_dscp() -> None:
    p = policy(app_group_id="ag-1")
    p.app_group = AppGroup(
        name="teams",
        prefixes=["52.112.0.0/14"],
        ports=[3478, 3479],
        protocol="udp",
        dscp=46,
    )

    s = sections_of(view([p], mpls=[path("wan1", ["10.255.0.0"])]))
    mangle = s["/ip/firewall/mangle"].items[0].props
    lists = {i.props["address"] for i in s["/ip/firewall/address-list"].items}

    assert "52.112.0.0/14" in lists
    assert mangle["protocol"] == "udp"
    assert mangle["dst-port"] == "3478,3479"
    assert mangle["dscp"] == 46


def test_an_explicit_match_overrides_the_app_group() -> None:
    p = policy(app_group_id="ag-1", dscp=26, dst_ports="8443")
    p.app_group = AppGroup(name="teams", prefixes=[], ports=[3478], dscp=46)

    mangle = sections_of(view([p], mpls=[path("wan1", ["10.255.0.0"])]))[
        "/ip/firewall/mangle"
    ].items[0].props

    assert mangle["dscp"] == 26
    assert mangle["dst-port"] == "8443"


# -- naming -----------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["voice", "Business Critical", "a-really-long-policy-name-that-goes-on"]
)
def test_routing_marks_fit_routeros(name: str) -> None:
    s = sections_of(view([policy(name=name)], mpls=[path("wan1", ["10.255.0.0"])]))
    mark = s["/ip/firewall/mangle"].items[0].props["new-routing-mark"]

    assert len(mark) <= 31
    assert " " not in mark


def test_everything_rendered_is_ownership_tagged() -> None:
    for sec in render_policies(view([policy()], mpls=[path("wan1", ["10.255.0.0"])])):
        assert sec.owner_tag.startswith("sdwan:policy")
        for item in sec.items:
            assert item.tag.startswith("sdwan:policy")
