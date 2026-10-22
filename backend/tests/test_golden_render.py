"""Golden-file render tests: the renderers' entire output, pinned.

Unit tests assert the properties their author thought to check. A golden
file asserts *everything else as well* -- the script body nobody re-reads,
the section metadata nobody lists, the guard rule added for one incident
in 2026. Any render change, intended or not, shows up here as a diff a
reviewer can read line by line.

Regenerate after an intended render change, and review the result like
code -- the golden diff *is* the device-config diff:

    UPDATE_GOLDEN=1 pytest tests/test_golden_render.py

The fixtures are small but representative on purpose: two failover
policies sharing one group and SLA profile (the shared-probe shape), one
load-balance policy, one WireGuard fabric link, one baseline site.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.drivers.base import ConfigSection
from app.drivers.coerce import canonical
from app.models.enums import SiteRole
from app.models.policy import Policy, SdwanGroup, SlaProfile
from app.models.site import Site
from app.render.fabric import SiteFabricView, render_fabric
from app.render.policy import PathOption, SitePolicyView, render_policies
from app.render.site import render_site
from app.transports import get_transport
from app.transports.base import Endpoint, FabricView, LinkView

GOLDEN_DIR = Path(__file__).parent / "golden"


def _serialize(sections: list[ConfigSection]) -> str:
    """Deterministic JSON for a render: the full contract, nothing elided."""
    doc = []
    for section in sorted(sections, key=lambda s: (s.order, s.path)):
        doc.append(
            {
                "path": section.path,
                "owner_tag": section.owner_tag,
                "key": list(section.key),
                "order": section.order,
                "ordered": section.ordered,
                "comment_capable": section.comment_capable,
                "ignore": sorted(section.ignore),
                "write_once": sorted(section.write_once),
                "items": [
                    {
                        "tag": item.tag,
                        "enforce": list(item.enforce),
                        "props": {k: canonical(v) for k, v in item.props.items()},
                    }
                    for item in section.items
                ],
            }
        )
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def assert_golden(name: str, sections: list[ConfigSection]) -> None:
    path = GOLDEN_DIR / name
    rendered = _serialize(sections)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN_DIR.mkdir(exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        return
    assert path.exists(), (
        f"golden file {name} is missing; create it with UPDATE_GOLDEN=1 "
        "and review the output before committing"
    )
    expected = path.read_text(encoding="utf-8")
    assert rendered == expected, (
        f"render output changed vs golden {name}. If the change is "
        "intended, regenerate with UPDATE_GOLDEN=1 and review the diff -- "
        "it is exactly what will change on every device."
    )


# -- fixtures -----------------------------------------------------------------


def _path(name: str, hops: list[str]) -> PathOption:
    return PathOption(
        wan_name=name, interface=f"ether-{name}", gateway=None, cost=1.0, next_hops=hops
    )


def _group(
    name: str, members: list[dict], strategy: str, sla: SlaProfile | None = None
) -> SdwanGroup:
    return SdwanGroup(
        id=f"grp-{name}",
        name=name,
        members=members,
        strategy=strategy,
        sla_profile=sla,
        tenant_id="default",
    )


def _policy(name: str, priority: int, group: SdwanGroup, **kw) -> Policy:
    defaults = dict(
        id=f"pol-{name}",
        name=name,
        priority=priority,
        enabled=True,
        sdwan_group=group,
        src_prefixes=[],
        dst_prefixes=[],
        site_ids=[],
        fallback="any",
        tenant_id="default",
    )
    return Policy(**{**defaults, **kw})


def _policy_view() -> SitePolicyView:
    sla = SlaProfile(
        name="strict",
        loss_percent=5,
        latency_ms=150,
        jitter_ms=30,
        probe_interval_seconds=10,
        probe_count=10,
        recovery_seconds=60,
        tenant_id="default",
    )
    shared = _group(
        "mpls-inet",
        [{"uplink": "mpls", "weight": 1}, {"uplink": "inet", "weight": 1}],
        "failover",
        sla,
    )
    balanced = _group(
        "bulk",
        [{"uplink": "fibre", "weight": 3}, {"uplink": "lte", "weight": 1}],
        "load_balance",
    )
    return SitePolicyView(
        site_name="branch-1",
        policies=[
            _policy("voice", 100, shared),
            _policy("video", 90, shared),
            _policy("bulk", 50, balanced, dst_prefixes=["0.0.0.0/0"]),
        ],
        paths_by_tag={
            "mpls": [_path("mpls", ["10.255.0.1", "10.255.0.5"])],
            "inet": [_path("inet", ["10.255.1.1"])],
            "fibre": [_path("fibre", ["10.255.2.1"])],
            "lte": [_path("lte", ["10.255.3.1"])],
        },
        underlay_addresses=["203.0.113.1"],
        lan_prefixes=["192.168.10.0/24"],
    )


def _fabric_view() -> SiteFabricView:
    fabric = FabricView(name="core", asn=65001, mtu=1400)
    link = LinkView(
        slug="oslo-bergen",
        fabric=fabric,
        local=Endpoint(
            site_name="oslo",
            wan_name="wan1",
            interface="ether1",
            tunnel_ip="10.255.0.0",
            public_ip="203.0.113.1",
        ),
        remote=Endpoint(
            site_name="bergen",
            wan_name="wan1",
            interface="ether1",
            tunnel_ip="10.255.0.1",
            public_ip="203.0.113.2",
        ),
        initiator=True,
    )
    return SiteFabricView(
        fabric=fabric,
        site_name="oslo",
        role=SiteRole.spoke,
        loopback_ip="10.254.0.7",
        links=[link],
        local_prefixes=["192.168.10.0/24"],
    )


def _site() -> Site:
    return Site(
        name="branch-1",
        mgmt_host="203.0.113.10",
        username="admin",
        role=SiteRole.spoke,
        verify_tls=False,
        loopback_ip="10.254.0.7",
        local_prefixes=["192.168.10.0/24", "192.168.11.0/24"],
        tenant_id="default",
    )


# -- golden comparisons ---------------------------------------------------------


def test_golden_policy_render() -> None:
    assert_golden("policy_sections.json", render_policies(_policy_view()))


def test_golden_fabric_render() -> None:
    assert_golden(
        "fabric_sections.json", render_fabric(_fabric_view(), get_transport("wireguard"))
    )


def test_golden_site_render() -> None:
    assert_golden("site_sections.json", render_site(_site()))


# -- structural invariants -------------------------------------------------------
#
# Properties that must hold for *any* policy render, checked over the golden
# fixture: a mark without its table blackholes traffic, a route into a
# missing table does the same, and a route via a gateway that is not a
# tunnel next-hop was never the renderer's intent.


def test_policy_render_structure_is_closed() -> None:
    view = _policy_view()
    sections = {s.path: s for s in render_policies(view)}

    tables = {
        i.props["name"] for i in sections["/routing/table"].items if "name" in i.props
    }
    marks = {
        i.props["new-routing-mark"]
        for i in sections["/ip/firewall/mangle"].items
        if "new-routing-mark" in i.props
    }
    # Every steering mark has its fib table, and every rendered table is
    # reachable from some mark.
    assert marks == tables

    next_hops = {hop for paths in view.paths_by_tag.values() for p in paths for hop in p.next_hops}
    for route in sections["/ip/route"].items:
        assert route.props["routing-table"] in tables
        assert route.props["gateway"] in next_hops

    probe_hosts = {i.props["host"] for i in sections["/tool/netwatch"].items}
    assert probe_hosts <= next_hops


@pytest.mark.parametrize(
    "name", ["policy_sections.json", "fabric_sections.json", "site_sections.json"]
)
def test_golden_files_exist(name: str) -> None:
    assert (GOLDEN_DIR / name).exists()
