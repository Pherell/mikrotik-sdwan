"""Plan A3+A5: anti-flap counters, failback hysteresis, probe shapes.

A3 gates the netwatch scripts themselves: demotion waits out a
consecutive-failure window before it acts, restore waits out the longer
of the hold-down and the restore count, and the restore additionally has
to beat the thresholds by the failback margins. A5 widens what a probe
*is*: a service type instead of icmp, and a set of targets that must
agree before a path is demoted.

The helpers are test_policy's, copied rather than imported -- the suite
keeps test modules self-contained.
"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from app.models.policy import Policy, SdwanGroup, SlaProfile
from app.render.policy import PathOption, SitePolicyView, render_policies
from app.schemas.policy import SlaProfileCreate, SlaProfileUpdate


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


def sections_of(v: SitePolicyView) -> dict:
    return {s.path: s for s in render_policies(v)}


def probes_of(v: SitePolicyView) -> list:
    return sections_of(v)["/tool/netwatch"].items


def sla(**kw) -> SlaProfile:
    defaults = dict(
        name="std",
        loss_percent=20,
        latency_ms=300,
        jitter_ms=None,
        probe_interval_seconds=10,
        probe_count=10,
        recovery_seconds=60,
    )
    return SlaProfile(**{**defaults, **kw})


# -- A3: the demote gate ------------------------------------------------------


def test_default_demotion_waits_out_the_failure_count() -> None:
    """fail x5 at a 10 s interval: the down-script waits 40 s and demotes
    only if its entry is still down -- the consecutive failures in between
    are what kept it down."""
    items = probes_of(view([policy(sla_profile=sla())], mpls=[path("wan1", ["10.255.0.0"])]))
    down = str(items[0].props["down-script"])

    assert down.startswith(":delay 40s; ")
    assert 'status] = "down"' in down
    assert "distance=101" in down


def test_fail_count_one_demotes_immediately_as_before() -> None:
    items = probes_of(
        view([policy(sla_profile=sla(fail_count=1))], mpls=[path("wan1", ["10.255.0.0"])])
    )
    down = str(items[0].props["down-script"])

    assert ":delay" not in down
    assert ":if" not in down
    assert down.startswith(":foreach")


def test_the_failure_window_scales_with_count_and_interval() -> None:
    items = probes_of(
        view(
            [policy(sla_profile=sla(fail_count=3, probe_interval_seconds=5))],
            mpls=[path("wan1", ["10.255.0.0"])],
        )
    )
    assert str(items[0].props["down-script"]).startswith(":delay 10s; ")


def test_profiles_differing_only_in_fail_count_get_separate_probes() -> None:
    """The counter lives in the rendered script, so it is part of the
    standard two profiles are shared by."""
    a = policy(name="voice", sdwan_group=group(["mpls"], sla(name="a", fail_count=5)))
    b = policy(name="video", sdwan_group=group(["mpls"], sla(name="b", fail_count=1)))
    items = probes_of(view([a, b], mpls=[path("wan1", ["10.255.0.0"])]))

    assert len(items) == 2
    assert {str(i.props["down-script"]).startswith(":delay") for i in items} == {True, False}


# -- A3: the restore gate -----------------------------------------------------


def test_restore_waits_the_longer_of_hold_down_and_restore_count() -> None:
    """recovery 10 s would release after 10 s; restore x5 at 10 s
    intervals needs 40 -- the count binds."""
    items = probes_of(
        view(
            [policy(sla_profile=sla(recovery_seconds=10, restore_count=5))],
            mpls=[path("wan1", ["10.255.0.0"])],
        )
    )
    assert str(items[0].props["up-script"]).startswith(":delay 40s; ")


def test_restore_requires_beating_the_threshold_by_the_margin() -> None:
    """Demote above 300 ms, restore only at 290 or below (and 25 for
    jitter): the band between is where flapping used to live."""
    items = probes_of(
        view(
            [policy(sla_profile=sla(jitter_ms=30))],
            mpls=[path("wan1", ["10.255.0.0"])],
        )
    )
    up = str(items[0].props["up-script"])

    assert "rtt-avg] <= 00:00:00.290" in up
    assert "rtt-jitter] <= 00:00:00.025" in up


def test_zero_margins_restore_at_the_threshold_itself() -> None:
    items = probes_of(
        view(
            [
                policy(
                    sla_profile=sla(
                        failback_latency_margin_ms=0, failback_jitter_margin_ms=0
                    )
                )
            ],
            mpls=[path("wan1", ["10.255.0.0"])],
        )
    )
    up = str(items[0].props["up-script"])

    assert "rtt-avg" not in up
    assert "rtt-jitter" not in up


def test_a_margin_wider_than_the_threshold_clamps_instead_of_deadlocking() -> None:
    """The schema rejects margin >= threshold, but a hand-built profile
    must not render a restore condition nothing can satisfy."""
    items = probes_of(
        view(
            [policy(sla_profile=sla(latency_ms=100, failback_latency_margin_ms=150))],
            mpls=[path("wan1", ["10.255.0.0"])],
        )
    )
    assert "rtt-avg] <= 00:00:00.001" in str(items[0].props["up-script"])


# -- A5: probe types ------------------------------------------------------------


def test_tcp_conn_probes_a_service_port_not_icmp_thresholds() -> None:
    items = probes_of(
        view(
            [policy(sla_profile=sla(probe_type="tcp-conn", probe_port=8291))],
            mpls=[path("wan1", ["10.255.0.0"])],
        )
    )
    props = items[0].props

    assert props["type"] == "tcp-conn"
    assert props["port"] == 8291
    # icmp-only machinery stays off a type that cannot honour it.
    assert "thr-avg" not in props
    assert "packet-count" not in props
    # And its restore has no RTT to hold a margin against.
    assert "rtt-avg" not in str(props["up-script"])
    # The anti-flap gates are type-independent.
    assert str(props["down-script"]).startswith(":delay 40s; ")


def test_http_get_defaults_to_port_80_and_dns_needs_no_port() -> None:
    http = probes_of(
        view(
            [policy(sla_profile=sla(probe_type="http-get"))],
            mpls=[path("wan1", ["10.255.0.0"])],
        )
    )[0].props
    assert http["type"] == "http-get"
    assert http["port"] == 80

    dns = probes_of(
        view(
            [policy(sla_profile=sla(probe_type="dns", probe_dns_name="one.one.one.one"))],
            mpls=[path("wan1", ["10.255.0.0"])],
        )
    )[0].props
    assert dns["type"] == "dns"
    assert "port" not in dns


def test_dns_probes_resolve_the_name_not_the_hop() -> None:
    """RouterOS rejects an address as a type=dns host (verified on the CHR
    lab: "must use dns name when type is dns"), so the entry's host is the
    profile's name for every hop -- and two hops must not collide on the
    (host, comment) entry key just because they resolve the same name."""
    items = probes_of(
        view(
            [
                policy(
                    sla_profile=sla(
                        probe_type="dns",
                        probe_dns_name="one.one.one.one",
                        probe_targets=["1.1.1.1"],  # ignored for dns
                    )
                )
            ],
            mpls=[path("wan1", ["10.255.0.0", "10.255.0.2"])],
        )
    )

    assert len(items) == 2
    assert {i.props["host"] for i in items} == {"one.one.one.one"}
    assert len({i.props["comment"] for i in items}) == 2
    # The scripts still judge the hops: each demotes its own gateway.
    downs = {str(i.props["down-script"]) for i in items}
    assert any('gateway="10.255.0.0"' in d for d in downs)
    assert any('gateway="10.255.0.2"' in d for d in downs)


# -- A5: target agreement -------------------------------------------------------


def test_extra_targets_render_one_entry_each_with_distinct_comments() -> None:
    items = probes_of(
        view(
            [policy(sla_profile=sla(probe_targets=["1.1.1.1", "8.8.8.8"]))],
            mpls=[path("wan1", ["10.255.0.0"])],
        )
    )

    assert {i.props["host"] for i in items} == {"10.255.0.0", "1.1.1.1", "8.8.8.8"}
    comments = [i.props["comment"] for i in items]
    assert len(set(comments)) == 3
    # Multi-target entries carry a (hop, target) suffix on the shared base.
    assert all(c.count(":") >= 4 for c in comments)
    assert len({c.rsplit(":", 1)[0] for c in comments}) == 1


def test_a_single_target_standard_keeps_the_legacy_comment() -> None:
    items = probes_of(view([policy(sla_profile=sla())], mpls=[path("wan1", ["10.255.0.0"])]))
    assert re.fullmatch(r"sdwan:policy:sla:wan1:[0-9a-f]{8}", str(items[0].props["comment"]))


def test_demotion_needs_every_target_down() -> None:
    """One dead anycast target must not demote a healthy path: each
    entry's down-script re-reads *all* entries in its set and acts only
    when every one of them reads down."""
    items = probes_of(
        view(
            [policy(sla_profile=sla(probe_targets=["1.1.1.1"], fail_count=1))],
            mpls=[path("wan1", ["10.255.0.0"])],
        )
    )
    assert len(items) == 2
    comments = {str(i.props["comment"]) for i in items}
    for item in items:
        down = str(item.props["down-script"])
        # No failure window (fail_count=1), but the agreement gate applies
        # at once: both entries' statuses are consulted.
        assert ":delay" not in down
        assert down.count('status] = "down"') == 2
        for comment in comments:
            assert f'comment="{comment}"' in down
        assert "\n" not in down


def test_two_hops_sharing_a_target_do_not_collide() -> None:
    """The same extra target judged for two hops of one WAN: the section
    keys netwatch by (host, comment), so the two 1.1.1.1 entries must not
    share a comment."""
    items = probes_of(
        view(
            [policy(sla_profile=sla(probe_targets=["1.1.1.1"]))],
            mpls=[path("wan1", ["10.255.0.0", "10.255.0.2"])],
        )
    )
    keys = [(i.props["host"], i.props["comment"]) for i in items]

    assert len(items) == 4
    assert len(set(keys)) == 4


def test_a_target_equal_to_the_hop_is_not_probed_twice() -> None:
    items = probes_of(
        view(
            [policy(sla_profile=sla(probe_targets=["10.255.0.0", "1.1.1.1"]))],
            mpls=[path("wan1", ["10.255.0.0"])],
        )
    )
    assert {i.props["host"] for i in items} == {"10.255.0.0", "1.1.1.1"}
    assert len(items) == 2


def test_sharing_still_dedupes_a_multi_target_standard() -> None:
    """Two policies over one group share the whole entry set -- A5 must
    not resurrect the per-policy probe duplication A4 removed."""
    shared = group(["mpls"], sla(probe_targets=["1.1.1.1"]))
    a = policy(name="voice", sdwan_group=shared)
    b = policy(name="video", sdwan_group=shared)
    items = probes_of(view([a, b], mpls=[path("wan1", ["10.255.0.0"])]))

    assert len(items) == 2
    down = str(items[0].props["down-script"])
    for mark in ("sdwan-voice", "sdwan-video"):
        assert f'routing-table="{mark}"' in down


# -- schema ---------------------------------------------------------------------


def test_schema_rejects_a_margin_that_swallows_the_threshold() -> None:
    with pytest.raises(ValidationError):
        SlaProfileCreate(name="x", latency_ms=100, failback_latency_margin_ms=100)
    with pytest.raises(ValidationError):
        SlaProfileCreate(name="x", jitter_ms=30, failback_jitter_margin_ms=30)


def test_schema_requires_a_port_for_tcp_conn_only() -> None:
    with pytest.raises(ValidationError):
        SlaProfileCreate(name="x", probe_type="tcp-conn")
    ok = SlaProfileCreate(name="x", probe_type="tcp-conn", probe_port=443)
    assert ok.probe_port == 443
    # http-get has a sane default port, so it may omit one.
    assert SlaProfileCreate(name="x", probe_type="http-get").probe_port is None


def test_schema_validates_probe_targets() -> None:
    with pytest.raises(ValidationError):
        SlaProfileCreate(name="x", probe_targets=["dns.example.com"])
    with pytest.raises(ValidationError):
        SlaProfileCreate(
            name="x",
            probe_targets=["1.1.1.1", "8.8.8.8", "9.9.9.9", "8.8.4.4", "1.0.0.1"],
        )
    deduped = SlaProfileCreate(name="x", probe_targets=["1.1.1.1", "1.1.1.1"])
    assert deduped.probe_targets == ["1.1.1.1"]


def test_schema_rejects_an_unknown_probe_type() -> None:
    with pytest.raises(ValidationError):
        SlaProfileCreate(name="x", probe_type="snmp")


def test_schema_requires_a_real_name_for_dns() -> None:
    with pytest.raises(ValidationError):
        SlaProfileCreate(name="x", probe_type="dns")
    with pytest.raises(ValidationError):
        # An address is not a name -- the device rejects it too.
        SlaProfileCreate(name="x", probe_type="dns", probe_dns_name="1.1.1.1")
    ok = SlaProfileCreate(name="x", probe_type="dns", probe_dns_name="one.one.one.one")
    assert ok.probe_dns_name == "one.one.one.one"


def test_update_judges_cross_field_only_when_both_sides_ride_along() -> None:
    # Margin alone cannot be judged without the stored threshold.
    assert SlaProfileUpdate(failback_latency_margin_ms=500).failback_latency_margin_ms == 500
    with pytest.raises(ValidationError):
        SlaProfileUpdate(latency_ms=100, failback_latency_margin_ms=100)
    with pytest.raises(ValidationError):
        SlaProfileUpdate(probe_type="tcp-conn")
