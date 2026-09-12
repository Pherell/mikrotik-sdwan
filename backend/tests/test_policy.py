"""Policy rendering: mangle marks, routing tables, and SLA-driven failover."""

from __future__ import annotations

import pytest

from app.models.policy import AppGroup, Policy, SdwanGroup, SlaProfile
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


def group(uplinks: list[str], sla: SlaProfile | None = None) -> SdwanGroup:
    return SdwanGroup(
        id="grp-" + "-".join(uplinks),
        name="-".join(uplinks),
        members=[{"uplink": u, "weight": 1} for u in uplinks],
        strategy="failover",
        sla_profile=sla,
        tenant_id="default",
    )


def policy(**kw) -> Policy:
    """A rule pointing at a group.

    `prefer` and `sla_profile` are accepted for readability and turned into the
    group the renderer actually reads, so the tests describe intent rather than
    the storage shape.
    """
    prefer = kw.pop("prefer", ["mpls"])
    sla = kw.pop("sla_profile", None)
    defaults = dict(
        id=f"pol-{kw.get('name', 'p')}",
        name="voice",
        priority=100,
        enabled=True,
        sdwan_group=kw.pop("sdwan_group", None) or group(prefer, sla),
        src_prefixes=[],
        dst_prefixes=[],
        site_ids=[],
        fallback="any",
        tenant_id="default",
    )
    return Policy(**{**defaults, **kw})


def view(policies: list[Policy], **paths: list[PathOption]) -> SitePolicyView:
    return SitePolicyView(site_name="branch-1", policies=policies, paths_by_tag=paths)


def view_with_underlay(
    policies: list[Policy], underlay: list[str], **paths: list[PathOption]
) -> SitePolicyView:
    return SitePolicyView(
        site_name="branch-1",
        policies=policies,
        paths_by_tag=paths,
        underlay_addresses=underlay,
    )


def sections_of(v: SitePolicyView) -> dict:
    return {s.path: s for s in render_policies(v)}


# -- the underlay guard -----------------------------------------------------
#
# What these name: marking in the output chain put the router's own packets
# under the policy. A policy matching a supernet -- 10.0.0.0/8 is the one an
# operator reaches for -- covers the peer WAN addresses the tunnels are built
# on, so the encapsulated traffic was marked into a table whose only route is
# that same tunnel. The tunnel then cannot recover on its own: its session
# drops, check-gateway removes the route, and a strict table drops everything
# left over -- including the handshake that would have rebuilt it.


def test_the_peer_underlay_is_accepted_before_anything_marks_it() -> None:
    p = policy(dst_prefixes=["10.0.0.0/8"])
    s = sections_of(
        view_with_underlay([p], ["203.0.113.7"], mpls=[path("wan1", ["10.255.0.0"])])
    )

    mangle = s["/ip/firewall/mangle"].items
    guard = mangle[0].props
    assert guard["chain"] == "output"
    assert guard["action"] == "accept"
    assert guard["dst-address-list"] == "sdwan-branch-1-infra"
    # Positional: a guard below the rule it guards protects nothing.
    assert all(i.props["action"] == "mark-routing" for i in mangle[1:])

    addresses = {
        i.props["address"]
        for i in s["/ip/firewall/address-list"].items
        if i.props["list"] == "sdwan-branch-1-infra"
    }
    assert addresses == {"203.0.113.7"}


def test_a_site_with_no_tunnels_yet_renders_no_guard() -> None:
    """Nothing to protect, and an empty address-list match would accept
    everything the policy was meant to steer."""
    s = sections_of(view([policy()], mpls=[path("wan1", ["10.255.0.0"])]))

    mangle = s["/ip/firewall/mangle"].items
    assert all(i.props["action"] == "mark-routing" for i in mangle)
    assert not [
        i for i in s["/ip/firewall/address-list"].items if i.props["list"].endswith("-infra")
    ]


def test_the_overlay_next_hop_needs_no_guard() -> None:
    """It is the gateway the policy route already points at, and it resolves
    through the connected route on the tunnel interface -- so a marked packet
    addressed to it leaves by the interface it was leaving by anyway. Listing
    it would only add a rule that never changes an outcome."""
    s = sections_of(
        view_with_underlay([policy()], ["203.0.113.7"], mpls=[path("wan1", ["10.255.0.0"])])
    )

    addresses = {
        i.props["address"]
        for i in s["/ip/firewall/address-list"].items
        if i.props["list"].endswith("-infra")
    }
    assert "10.255.0.0" not in addresses


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
        # fallback="any" (the default) renders a lookup rule for the main-table
        # fallback -- ROS 7 has no gateway=main route.
        "/routing/rule",
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
    p = policy(prefer=["mpls", "broadband"])
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


def test_fallback_any_adds_a_lookup_rule() -> None:
    # RouterOS 7 has no gateway=main route; "fall back to main" is a
    # /routing/rule with action=lookup (which falls through on a miss, unlike
    # lookup-only-in-table).
    s = sections_of(view([policy(fallback="any")], mpls=[path("wan1", ["10.255.0.0"])]))
    rules = s["/routing/rule"].items

    assert len(rules) == 1
    assert rules[0].props["action"] == "lookup"
    # The rule points its own mark's table at itself; the miss falls through.
    assert rules[0].props["routing-mark"] == rules[0].props["table"]
    assert all(i.props["gateway"] != "main" for i in s["/ip/route"].items)


def test_fallback_drop_leaves_no_escape_route() -> None:
    # drop = strict: no fallthrough rule, so a marked packet with no live route
    # is dropped rather than leaking to the main table.
    s = sections_of(view([policy(fallback="drop")], mpls=[path("wan1", ["10.255.0.0"])]))
    assert s["/routing/rule"].items == []
    assert all(i.props["gateway"] != "main" for i in s["/ip/route"].items)


# -- the failure modes worth guarding --------------------------------------


def test_a_policy_with_no_matching_uplink_here_renders_nothing() -> None:
    """Marking traffic into an empty table blackholes it. Leaving it on the main
    table is worse for the policy and much better for the site."""
    s = sections_of(view([policy(prefer=["satellite"])], mpls=[path("wan1", ["10.255.0.0"])]))

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
    high = policy(name="critical", priority=10, prefer=["mpls"])
    low = policy(name="bulk", priority=900, prefer=["mpls"])
    s = sections_of(view([low, high], mpls=[path("wan1", ["10.255.0.0"])]))

    assert s["/ip/firewall/mangle"].ordered is True
    items = s["/ip/firewall/mangle"].items
    marks = [i.props["new-routing-mark"] for i in items if i.props["chain"] == "prerouting"]
    assert marks == ["sdwan-critical", "sdwan-bulk"]
    # The output copies are ordered by the same priority, for the same reason:
    # first match wins there too.
    out = [i.props["new-routing-mark"] for i in items if i.props["chain"] == "output"]
    assert out == ["sdwan-critical", "sdwan-bulk"]


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
    # The SLA is a property of the path now, so it hangs off the group rather
    # than the rule -- which is the point: one health standard, many rules.
    p = policy(sla_profile=sla)

    s = sections_of(view([p], mpls=[path("wan1", ["10.255.0.0"])]))
    props = s["/tool/netwatch"].items[0].props

    assert props["thr-loss-percent"] == 2
    assert props["thr-avg"] == "150ms"  # ROS7 latency threshold is thr-avg
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
    p = policy(prefer=["mpls", "broadband"])
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


# -- load balancing ---------------------------------------------------------
#
# RouterOS has no weighted next hop. Spreading traffic means PCC: hash each
# connection into one of N buckets and act on the bucket. Weights are bucket
# counts, so 70/30 is "of ten buckets, seven go left".


def balanced(uplinks: list[str], weights: list[int], sla: SlaProfile | None = None):
    g = group(uplinks, sla)
    g.strategy = "load_balance"
    g.members = [{"uplink": u, "weight": w} for u, w in zip(uplinks, weights, strict=True)]
    return g


def test_equal_weights_split_the_buckets_evenly() -> None:
    p = policy(sdwan_group=balanced(["fibre", "lte"], [1, 1]))
    result = sections_of(
        view([p], fibre=[path("fibre", ["10.255.0.1"])], lte=[path("lte", ["10.255.1.1"])])
    )

    mangle = result["/ip/firewall/mangle"].items
    classifiers = [i for i in mangle if i.props.get("action") == "mark-connection"]

    assert len(classifiers) == 2
    assert [i.props["per-connection-classifier"] for i in classifiers] == [
        "both-addresses:2/0",
        "both-addresses:2/1",
    ]


def test_weights_become_bucket_counts() -> None:
    """Three-to-one is three buckets to one, not a number RouterOS understands
    as a weight -- it has no such number."""
    p = policy(sdwan_group=balanced(["fibre", "lte"], [3, 1]))
    result = sections_of(
        view([p], fibre=[path("fibre", ["10.255.0.1"])], lte=[path("lte", ["10.255.1.1"])])
    )

    mangle = result["/ip/firewall/mangle"].items
    classifiers = [i for i in mangle if i.props.get("action") == "mark-connection"]

    assert len(classifiers) == 4
    assert all(
        i.props["per-connection-classifier"].startswith("both-addresses:4/")
        for i in classifiers
    )
    marks = [i.props["new-connection-mark"] for i in classifiers]
    assert marks.count(marks[0]) == 3  # the weight-3 member owns three buckets


def test_a_lopsided_weight_is_scaled_rather_than_rendering_a_hundred_rules() -> None:
    p = policy(sdwan_group=balanced(["fibre", "lte"], [100, 1]))
    result = sections_of(
        view([p], fibre=[path("fibre", ["10.255.0.1"])], lte=[path("lte", ["10.255.1.1"])])
    )

    mangle = result["/ip/firewall/mangle"].items
    classifiers = [i for i in mangle if i.props.get("action") == "mark-connection"]

    assert len(classifiers) <= 16
    marks = {i.props["new-connection-mark"] for i in classifiers}
    # Both members survive scaling. Rounding the small share to zero would
    # silently remove a link somebody deliberately listed.
    assert len(marks) == 2


def test_the_connection_is_marked_not_the_packet() -> None:
    """What stops a single TCP stream being split across two links mid-transfer."""
    p = policy(sdwan_group=balanced(["fibre", "lte"], [1, 1]))
    result = sections_of(
        view([p], fibre=[path("fibre", ["10.255.0.1"])], lte=[path("lte", ["10.255.1.1"])])
    )

    mangle = result["/ip/firewall/mangle"].items
    classifiers = [i for i in mangle if i.props.get("action") == "mark-connection"]
    routers = [i for i in mangle if i.props.get("action") == "mark-routing"]

    assert all(
        i.props["per-connection-classifier"].startswith("both-addresses:")
        for i in classifiers
    )
    # Classifiers must fall through so the later ones and the routing pass run.
    assert all(i.props["passthrough"] is True for i in classifiers)
    # Routing marks are terminal.
    assert all(i.props["passthrough"] is False for i in routers)
    # Every connection mark is turned into a routing mark.
    assert {i.props["connection-mark"] for i in routers} == {
        i.props["new-connection-mark"] for i in classifiers
    }


def test_classifiers_match_exactly_what_the_rule_matches() -> None:
    """A classifier matching wider traffic than its rule would balance packets
    the policy never claimed."""
    p = policy(
        sdwan_group=balanced(["fibre", "lte"], [1, 1]),
        dst_prefixes=["203.0.113.0/24"],
        protocol="udp",
        dst_ports="5060",
    )
    result = sections_of(
        view([p], fibre=[path("fibre", ["10.255.0.1"])], lte=[path("lte", ["10.255.1.1"])])
    )

    mangle = result["/ip/firewall/mangle"].items
    classifiers = [i for i in mangle if i.props.get("action") == "mark-connection"]

    for item in classifiers:
        assert item.props["protocol"] == "udp"
        assert item.props["dst-port"] == "5060"
        assert item.props["dst-address-list"].endswith("-dst")


def test_one_table_per_member_not_per_bucket() -> None:
    """A 7/3 split must not build ten identical tables."""
    p = policy(sdwan_group=balanced(["fibre", "lte"], [7, 3]))
    result = sections_of(
        view([p], fibre=[path("fibre", ["10.255.0.1"])], lte=[path("lte", ["10.255.1.1"])])
    )

    tables = result["/routing/table"].items
    assert len(tables) == 2


def test_each_table_prefers_its_own_member_and_falls_back_to_the_others() -> None:
    """Balancing without failover is worse than no balancing: the failure is
    partial and looks random."""
    p = policy(sdwan_group=balanced(["fibre", "lte"], [1, 1]))
    result = sections_of(
        view([p], fibre=[path("fibre", ["10.255.0.1"])], lte=[path("lte", ["10.255.1.1"])])
    )

    routes = result["/ip/route"].items
    by_table: dict[str, dict[str, int]] = {}
    for item in routes:
        if item.props["gateway"] == "main":
            continue
        by_table.setdefault(str(item.props["routing-table"]), {})[
            str(item.props["gateway"])
        ] = int(item.props["distance"])

    assert len(by_table) == 2
    tables = sorted(by_table)
    # In its own table the member is preferred; in the other it is the backup.
    assert by_table[tables[0]]["10.255.0.1"] == 1
    assert by_table[tables[0]]["10.255.1.1"] == 2
    assert by_table[tables[1]]["10.255.0.1"] == 2
    assert by_table[tables[1]]["10.255.1.1"] == 1
    assert all(
        i.props.get("check-gateway") == "ping"
        for i in routes
        if i.props["gateway"] != "main"
    )


def test_the_sla_script_demotes_per_table_not_globally() -> None:
    """One gateway sits at a different distance in every table, so a script
    that set one distance everywhere would flatten the balance into whichever
    path recovered last."""
    sla = SlaProfile(
        name="voice",
        loss_percent=2,
        latency_ms=120,
        probe_interval_seconds=5,
        probe_count=10,
        recovery_seconds=30,
    )
    p = policy(sdwan_group=balanced(["fibre", "lte"], [1, 1], sla))
    result = sections_of(
        view([p], fibre=[path("fibre", ["10.255.0.1"])], lte=[path("lte", ["10.255.1.1"])])
    )

    probes = result["/tool/netwatch"].items
    fibre = next(i for i in probes if i.props["host"] == "10.255.0.1")

    up = str(fibre.props["up-script"])
    down = str(fibre.props["down-script"])
    # Both tables are named, and each carries its own distance.
    assert up.count("routing-table=") == 2
    assert "distance=1}" in up and "distance=2}" in up
    assert f"distance={1 + SLA_PENALTY}}}" in down
    assert f"distance={2 + SLA_PENALTY}}}" in down


def test_failover_marks_traffic_passing_through_and_the_routers_own() -> None:
    """One match, rendered into both chains.

    prerouting is blind to anything the router originates, so a ping run from
    the device -- the first thing anybody does to check that steering works --
    used to ignore the policy entirely and go out the main table.
    """
    p = policy(prefer=["mpls", "lte"])
    result = sections_of(
        view([p], mpls=[path("mpls", ["10.255.0.1"])], lte=[path("lte", ["10.255.1.1"])])
    )

    mangle = result["/ip/firewall/mangle"].items
    assert [i.props["chain"] for i in mangle] == ["prerouting", "output"]
    assert {i.props["action"] for i in mangle} == {"mark-routing"}
    assert all("per-connection-classifier" not in i.props for i in mangle)
    # Same match in both, or the two chains would steer different traffic.
    assert mangle[0].props["new-routing-mark"] == mangle[1].props["new-routing-mark"]
    # The prerouting rule keeps the tag it has always had: an upgrade must not
    # rewrite a row that has not changed.
    assert mangle[0].tag == "sdwan:policy:voice"
    assert mangle[1].tag == "sdwan:policy:voice:output"
    assert len(result["/routing/table"].items) == 1

    probes = result["/tool/netwatch"].items
    # One clause, no routing-table filter: there is only one table.
    assert "routing-table=" not in str(probes[0].props["up-script"])
    assert str(probes[0].props["up-script"]).count(":foreach") == 1


def test_a_single_member_group_never_balances() -> None:
    """PCC across one path is pure overhead and an extra failure mode."""
    g = balanced(["fibre", "lte"], [1, 1])
    p = policy(sdwan_group=g)
    # Only one of the two uplinks exists at this site.
    result = sections_of(view([p], fibre=[path("fibre", ["10.255.0.1"])]))

    mangle = result["/ip/firewall/mangle"].items
    assert [i.props["chain"] for i in mangle] == ["prerouting", "output"]
    assert {i.props["action"] for i in mangle} == {"mark-routing"}
    assert all("per-connection-classifier" not in i.props for i in mangle)



# -- TLS SNI matching --------------------------------------------------------
#
# SNI is only visible in the TLS handshake, so a policy that matches on it
# renders two mangle passes instead of one: tls-host marks the *connection*,
# and a second rule turns that connection mark into the routing mark every
# later packet actually needs. See app.render.policy._sni_mangle_rules.


def sni_group(patterns: list[str], **kw) -> AppGroup:
    return AppGroup(
        id="app-" + "-".join(patterns).replace("*", "").replace(".", "-"),
        name=kw.pop("name", "teams"),
        tenant_id="default",
        sni_patterns=patterns,
        prefixes=kw.pop("prefixes", []),
        ports=kw.pop("ports", []),
    )


def test_an_sni_policy_renders_a_connection_mark_then_a_routing_mark() -> None:
    p = policy(app_group=sni_group(["*.teams.microsoft.com"]))
    mangle = sections_of(view([p], mpls=[path("wan1", ["10.255.0.0"])]))[
        "/ip/firewall/mangle"
    ].items

    assert len(mangle) == 2
    conn, routing = mangle

    assert conn.props["action"] == "mark-connection"
    assert conn.props["tls-host"] == "*.teams.microsoft.com"
    assert conn.props["protocol"] == "tcp"
    assert conn.props["dst-port"] == "443"
    assert conn.props["passthrough"] is True

    assert routing.props["action"] == "mark-routing"
    assert routing.props["connection-mark"] == conn.props["new-connection-mark"]
    assert routing.props["new-routing-mark"] == "sdwan-voice"
    assert routing.props["passthrough"] is False


def test_every_sni_pattern_gets_its_own_connection_mark_rule_but_one_routing_rule() -> None:
    p = policy(app_group=sni_group(["*.teams.microsoft.com", "*.office.com"]))
    mangle = sections_of(view([p], mpls=[path("wan1", ["10.255.0.0"])]))[
        "/ip/firewall/mangle"
    ].items

    conn_rules = [i for i in mangle if i.props["action"] == "mark-connection"]
    routing_rules = [i for i in mangle if i.props["action"] == "mark-routing"]
    assert {r.props["tls-host"] for r in conn_rules} == {
        "*.teams.microsoft.com", "*.office.com",
    }
    assert len(routing_rules) == 1
    assert {r.props["new-connection-mark"] for r in conn_rules} == {
        routing_rules[0].props["connection-mark"]
    }


def test_the_sni_routing_rule_carries_the_same_tag_a_plain_rule_would() -> None:
    """Per-policy telemetry (M7) matches mangle rows by comment; that lookup
    must not need to know which match mechanism a policy took."""
    plain = sections_of(
        view([policy()], mpls=[path("wan1", ["10.255.0.0"])])
    )["/ip/firewall/mangle"].items[0]
    sni = sections_of(
        view([policy(app_group=sni_group(["*.teams.microsoft.com"]))],
             mpls=[path("wan1", ["10.255.0.0"])])
    )["/ip/firewall/mangle"].items[-1]

    assert plain.props["comment"] == sni.props["comment"]
    assert plain.tag == sni.tag


def test_sni_narrows_by_destination_prefix_when_both_are_set() -> None:
    p = policy(
        app_group=sni_group(["*.teams.microsoft.com"]),
        dst_prefixes=["10.9.0.0/24"],
    )
    mangle = sections_of(view([p], mpls=[path("wan1", ["10.255.0.0"])]))[
        "/ip/firewall/mangle"
    ].items
    conn = mangle[0]
    assert conn.props["tls-host"] == "*.teams.microsoft.com"
    assert "dst-address-list" in conn.props


def test_a_policy_with_no_sni_patterns_renders_the_plain_rules() -> None:
    """An app group without patterns adds no tls-host pass."""
    p = policy(app_group=sni_group([], name="plain-group", prefixes=["10.9.0.0/24"]))
    mangle = sections_of(view([p], mpls=[path("wan1", ["10.255.0.0"])]))[
        "/ip/firewall/mangle"
    ].items
    assert [i.props["chain"] for i in mangle] == ["prerouting", "output"]
    assert {i.props["action"] for i in mangle} == {"mark-routing"}
    assert all("tls-host" not in i.props for i in mangle)


def test_sni_combined_with_load_balance_is_refused_by_the_renderer() -> None:
    """Rejected earlier at the API layer (see test_api.py-style validation in
    api.v1.policies); this is the backstop for a group whose strategy changed
    to load_balance after a conflicting policy already existed."""
    p = policy(
        app_group=sni_group(["*.teams.microsoft.com"]),
        sdwan_group=balanced(["fibre", "lte"], [1, 1]),
    )
    with pytest.raises(ValueError, match="load_balance"):
        render_policies(
            view(
                [p],
                fibre=[path("fibre", ["10.255.0.1"])],
                lte=[path("lte", ["10.255.1.1"])],
            )
        )
