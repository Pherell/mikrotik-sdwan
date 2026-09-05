"""Render steering policies to RouterOS.

The mechanism, in the order the packet meets it:

1. ``/ip/firewall/address-list`` -- prefix groups a rule can match by name.
2. ``/ip/firewall/mangle`` in ``prerouting`` -- match, then
   ``action=mark-routing`` with a mark naming the chosen path.
3. ``/routing/table`` -- one table per mark, with ``fib`` so it installs.
4. ``/ip/route`` -- inside each table, the preferred tunnels ordered by
   ``distance``, each with ``check-gateway=ping`` so a dead next hop drops out.
5. ``/tool/netwatch`` -- probes with the SLA's thresholds; its scripts raise the
   distance of a path that breaches them, which moves traffic without tearing
   anything down.

Mangle rules are positional: RouterOS evaluates the chain top to bottom and the
first match wins. Policies are therefore rendered in ``priority`` order and the
section is marked ``ordered`` so the reconciler preserves it.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.drivers.base import ConfigItem, ConfigSection
from app.models.policy import Policy, SlaProfile
from app.render.engine import owner_tag, section

# How far a path is demoted when it breaches its SLA. Large enough to fall
# below every other preference, small enough to stay above the "any" fallback
# at distance 250, so a fully degraded site still forwards.
SLA_PENALTY = 100

DEFAULT_SLA = SlaProfile(
    name="default",
    loss_percent=20,
    latency_ms=300,
    probe_interval_seconds=10,
    probe_count=10,
    recovery_seconds=60,
)


@dataclass(slots=True)
class PathOption:
    """One uplink a policy may steer onto, at one site."""

    wan_name: str
    interface: str
    gateway: str | None
    cost: float
    # Tunnel addresses reachable over this uplink, in fabric order. Steering
    # points at the overlay, not at the WAN gateway, so traffic stays encrypted.
    next_hops: list[str]


@dataclass(slots=True)
class SitePolicyView:
    site_name: str
    policies: list[Policy]
    # wan tag -> the uplinks at this site carrying it.
    paths_by_tag: dict[str, list[PathOption]]


def render_policies(view: SitePolicyView) -> list[ConfigSection]:
    if not view.policies:
        # Still emit the empty sections: a policy that was deleted must have its
        # rules swept off the device.
        return _sections(view, [], [], [], [], [])

    lists: list[ConfigItem] = []
    mangle: list[ConfigItem] = []
    tables: list[ConfigItem] = []
    routes: list[ConfigItem] = []
    probes: list[ConfigItem] = []

    seen_marks: set[str] = set()
    for policy in sorted(view.policies, key=lambda p: (p.priority, p.name)):
        if not policy.enabled:
            continue
        mark = _mark(policy)
        paths = _paths_for(policy, view)
        if not paths:
            # No uplink at this site carries any preferred tag. Rendering the
            # mangle rule anyway would blackhole the traffic into an empty
            # table, which is worse than leaving it on the main table.
            continue

        lists.extend(_address_lists(policy, view.site_name))
        mangle.append(_mangle_rule(policy, mark, view.site_name))

        if mark not in seen_marks:
            seen_marks.add(mark)
            tables.append(_routing_table(mark, view.site_name))
            routes.extend(_routes(policy, mark, paths, view.site_name))
            probes.extend(_probes(policy, paths, view.site_name))

    return _sections(view, lists, mangle, tables, routes, probes)


# -- pieces -----------------------------------------------------------------


def _mark(policy: Policy) -> str:
    return f"sdwan-{_slug(policy.name)}"[:31]


def _paths_for(policy: Policy, view: SitePolicyView) -> list[PathOption]:
    """Preferred uplinks present at this site, in the policy's tag order."""
    chosen: list[PathOption] = []
    seen: set[str] = set()
    for tag in policy.prefer_tags or []:
        for path in sorted(view.paths_by_tag.get(tag, []), key=lambda p: p.cost):
            if path.wan_name not in seen and path.next_hops:
                seen.add(path.wan_name)
                chosen.append(path)
    return chosen


def _address_lists(policy: Policy, site_name: str) -> list[ConfigItem]:
    tag = owner_tag("policy", policy.name, "list")
    items: list[ConfigItem] = []
    for kind, prefixes in (
        ("src", policy.src_prefixes or []),
        ("dst", policy.dst_prefixes or []),
    ):
        for prefix in prefixes:
            items.append(
                ConfigItem(
                    props={"list": _list_name(policy, kind), "address": prefix}, tag=tag
                )
            )
    if policy.app_group is not None:
        for prefix in policy.app_group.prefixes or []:
            items.append(
                ConfigItem(
                    props={"list": _list_name(policy, "app"), "address": prefix}, tag=tag
                )
            )
    return items


def _mangle_rule(policy: Policy, mark: str, site_name: str) -> ConfigItem:
    props: dict[str, object] = {
        "chain": "prerouting",
        "action": "mark-routing",
        "new-routing-mark": mark,
        # Marking every packet of a flow costs more than marking the first and
        # letting the connection tracker carry the rest, but it is correct when
        # a path changes mid-flow, which is the whole point of SLA steering.
        "passthrough": False,
        "comment": owner_tag("policy", policy.name),
    }
    if policy.src_prefixes:
        props["src-address-list"] = _list_name(policy, "src")
    if policy.dst_prefixes:
        props["dst-address-list"] = _list_name(policy, "dst")
    if policy.app_group is not None and (policy.app_group.prefixes or []):
        props["dst-address-list"] = _list_name(policy, "app")

    protocol = policy.protocol or (policy.app_group.protocol if policy.app_group else None)
    if protocol:
        props["protocol"] = protocol
    ports = policy.dst_ports or _ports_of(policy)
    if ports:
        # RouterOS rejects a port match without a protocol.
        props.setdefault("protocol", "tcp")
        props["dst-port"] = ports
    dscp = policy.dscp if policy.dscp is not None else _dscp_of(policy)
    if dscp is not None:
        props["dscp"] = dscp

    return ConfigItem(props=props, tag=owner_tag("policy", policy.name))


def _routing_table(mark: str, site_name: str) -> ConfigItem:
    tag = owner_tag("policy", mark, "table")
    return ConfigItem(
        # Without fib=yes the table exists but never installs a route, and
        # marked traffic silently falls through to main.
        props={"name": mark, "fib": True},
        tag=tag,
    )


def _routes(
    policy: Policy, mark: str, paths: list[PathOption], site_name: str
) -> list[ConfigItem]:
    tag = owner_tag("policy", policy.name, "route")
    items: list[ConfigItem] = []
    for index, path in enumerate(paths):
        for hop in path.next_hops:
            items.append(
                ConfigItem(
                    props={
                        "dst-address": "0.0.0.0/0",
                        "gateway": hop,
                        "routing-table": mark,
                        # Order of preference. Netwatch raises this by 100 when
                        # the path breaches its SLA, which demotes it below the
                        # next preference without removing it.
                        "distance": index + 1,
                        "check-gateway": "ping",
                        "comment": f"{tag}:{path.wan_name}",
                    },
                    tag=f"{tag}:{path.wan_name}",
                )
            )
    if policy.fallback == "any":
        # Last resort: fall back to whatever the main table would have done.
        items.append(
            ConfigItem(
                props={
                    "dst-address": "0.0.0.0/0",
                    "gateway": "main",
                    "routing-table": mark,
                    "distance": 250,
                    "comment": f"{tag}:fallback",
                },
                tag=f"{tag}:fallback",
            )
        )
    return items


def _probes(policy: Policy, paths: list[PathOption], site_name: str) -> list[ConfigItem]:
    """Netwatch entries carrying this policy's SLA thresholds."""
    sla = policy.sla_profile or DEFAULT_SLA
    tag = owner_tag("policy", policy.name, "sla")
    items: list[ConfigItem] = []
    for index, path in enumerate(paths):
        base = index + 1
        for hop in path.next_hops:
            props: dict[str, object] = {
                "host": hop,
                "type": "icmp",
                "interval": f"{sla.probe_interval_seconds}s",
                "packet-count": sla.probe_count,
                "thr-loss-percent": sla.loss_percent,
                "thr-latency": f"{sla.latency_ms}ms",
                "disabled": False,
                # Demote rather than delete: the route stays in the table so the
                # path can be re-preferred the moment it recovers.
                "down-script": _distance_script(hop, base + SLA_PENALTY),
                "up-script": _distance_script(hop, base),
                "comment": f"{tag}:{path.wan_name}",
            }
            if sla.jitter_ms:
                props["thr-jitter"] = f"{sla.jitter_ms}ms"
            items.append(ConfigItem(props=props, tag=f"{tag}:{path.wan_name}"))
    return items


def _distance_script(gateway: str, distance: int) -> str:
    """A RouterOS script that sets every route via ``gateway`` to ``distance``.

    Absolute, not relative. An earlier version added a penalty to the current
    distance, which compounds: two down events in a row demote the path twice
    and it never returns to its original preference. Both the healthy and the
    demoted values are known at render time, so just write them.

    Kept to one line: RouterOS stores scripts verbatim, and a multi-line value
    round-trips with whitespace changes that would diff dirty forever.
    """
    return (
        f':foreach r in=[/ip/route/find gateway="{gateway}"] '
        f"do={{/ip/route/set $r distance={distance}}}"
    )


def _sections(
    view: SitePolicyView,
    lists: list[ConfigItem],
    mangle: list[ConfigItem],
    tables: list[ConfigItem],
    routes: list[ConfigItem],
    probes: list[ConfigItem],
) -> list[ConfigSection]:
    scope = owner_tag("policy") + ":"
    return [
        section(
            "/ip/firewall/address-list",
            "address_list",
            owner=scope,
            key=("list", "address"),
            items=lists,
        ),
        section(
            "/routing/table",
            "policy",
            owner=scope,
            key=("name",),
            items=tables,
        ),
        section(
            "/ip/route",
            "policy",
            owner=scope,
            key=("dst-address", "gateway", "routing-table"),
            items=routes,
        ),
        section(
            "/ip/firewall/mangle",
            "firewall",
            owner=scope,
            key=("comment",),
            ordered=True,  # first match wins; position is the semantics
            items=mangle,
        ),
        section(
            "/tool/netwatch",
            "monitoring",
            owner=scope,
            key=("host",),
            ignore=("status", "since", "sent-count", "loss-count", "rtt-avg", "rtt-jitter"),
            items=probes,
        ),
    ]


# -- helpers ----------------------------------------------------------------


def _list_name(policy: Policy, kind: str) -> str:
    return f"sdwan-{_slug(policy.name)}-{kind}"[:63]


def _slug(value: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in value.lower()).strip("-")


def _ports_of(policy: Policy) -> str | None:
    if policy.app_group is None or not policy.app_group.ports:
        return None
    return ",".join(str(p) for p in policy.app_group.ports)


def _dscp_of(policy: Policy) -> int | None:
    return policy.app_group.dscp if policy.app_group else None
