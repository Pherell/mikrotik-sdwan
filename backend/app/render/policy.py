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

Under ``load_balance`` steps 2-4 change shape. RouterOS has no weighted
next-hop selection, so spreading traffic means PCC: hash each connection into
one of N buckets with ``per-connection-classifier``, give each bucket a
connection mark, and route by it. Weights are bucket *counts* -- a member with
weight 3 owns three of the N buckets.

**The ceiling, stated here because it belongs next to the code:** this
distributes *connections*, not packets. One download never uses two links. That
is a property of RouterOS, and of Sophos's weighted round-robin too, but nobody
should choose load_balance expecting a single transfer to go faster.

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

# Total PCC buckets a group may use. Weights are scaled into this, so the
# mangle chain grows with the cap rather than with whatever numbers somebody
# typed: weights of 100 and 1 would otherwise render 101 classifier rules.
MAX_BUCKETS = 16

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
        balanced = _strategy(policy) == "load_balance" and len(paths) > 1
        sni_patterns = _sni_patterns(policy)

        if sni_patterns and balanced:
            # Rejected at the API layer when a policy is created or updated
            # (see api.v1.policies._check_sni_load_balance_conflict) -- SNI's
            # connection mark and PCC's classifier both want to own "the"
            # connection mark for this policy, and combining them is exactly
            # the overlap docs/model.md flags as needing its own design
            # rather than a bolt-on. If a group's strategy changes to
            # load_balance *after* a conflicting policy already exists, this
            # is the backstop: fail loudly rather than render silently wrong
            # mangle rules.
            raise ValueError(
                f"policy {policy.name!r} combines SNI matching with "
                "load_balance, which is not supported"
            )
        elif sni_patterns:
            # Same two-pass shape as PCC, for the same underlying reason: SNI
            # is only visible in the TLS handshake, so the first packets are
            # matched on tls-host and marked at the *connection* level, and
            # every later packet of that connection inherits the mark instead
            # of being re-inspected -- inspected, since only the handshake
            # carries it.
            mangle.extend(_sni_mangle_rules(policy, mark, sni_patterns))
        elif balanced:
            # PCC needs the connection marked before it can be routed, so the
            # single mark-routing rule becomes a classifier pass followed by a
            # routing pass. Both are appended in order; the section is
            # ``ordered`` so the reconciler keeps them that way.
            buckets = _buckets(policy, paths)
            mangle.extend(_pcc_rules(policy, mark, paths, buckets))
        else:
            mangle.append(_mangle_rule(policy, mark, view.site_name))

        if mark not in seen_marks:
            seen_marks.add(mark)
            if balanced:
                tables.extend(_balanced_tables(mark, paths, view.site_name))
                routes.extend(_balanced_routes(policy, mark, paths, view.site_name))
                probes.extend(_probes(policy, paths, view.site_name, mark=mark))
            else:
                tables.append(_routing_table(mark, view.site_name))
                routes.extend(_routes(policy, mark, paths, view.site_name))
                probes.extend(_probes(policy, paths, view.site_name))

    return _sections(view, lists, mangle, tables, routes, probes)


# -- pieces -----------------------------------------------------------------


def _mark(policy: Policy) -> str:
    return f"sdwan-{_slug(policy.name)}"[:31]


def _members(policy: Policy) -> list[str]:
    """The group's uplinks, in preference order.

    A rule with no group steers nothing. That is deliberate: the group is where
    "which uplinks, in what order" lives now, and a rule without one has not
    said where its traffic should go.
    """
    group = policy.sdwan_group
    if group is None:
        return []
    return [
        str(member["uplink"])
        for member in (group.members or [])
        if isinstance(member, dict) and member.get("uplink")
    ]


def _sla(policy: Policy) -> SlaProfile:
    """The group's health standard, or the built-in default."""
    group = policy.sdwan_group
    return (group.sla_profile if group is not None else None) or DEFAULT_SLA


def _paths_for(policy: Policy, view: SitePolicyView) -> list[PathOption]:
    """Uplinks present at this site, in the group's order."""
    chosen: list[PathOption] = []
    seen: set[str] = set()
    for uplink in _members(policy):
        for path in sorted(view.paths_by_tag.get(uplink, []), key=lambda p: p.cost):
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


def _match_props(policy: Policy) -> dict[str, object]:
    """What this policy matches, without saying what to do about it.

    Shared by the failover rule and by every PCC classifier, because a
    classifier that matched a wider set of traffic than the rule it belongs to
    would balance packets the policy never claimed.
    """
    props: dict[str, object] = {}
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
    return props


def _sni_patterns(policy: Policy) -> list[str]:
    return list(policy.app_group.sni_patterns or []) if policy.app_group else []


def _sni_mangle_rules(policy: Policy, mark: str, patterns: list[str]) -> list[ConfigItem]:
    """tls-host match -> connection mark -> routing mark, in that order.

    tls-host only ever matches the handshake packet, so the pass that reads
    it must mark the *connection* (passthrough=True: later classifiers still
    need to see the packet) and a second pass turns that connection mark into
    the routing mark every later packet actually needs. src/dst-address-list
    narrowing from _match_props still applies -- an operator can scope SNI
    matching to a destination range -- but protocol and port are forced to
    tcp/443 regardless of anything an app group's own protocol/ports say:
    SNI is a TLS-handshake property, not a policy-configurable one.
    """
    conn_mark = f"{mark}-sni"[:31]
    items: list[ConfigItem] = []
    for index, pattern in enumerate(patterns):
        props = _match_props(policy)
        props.update(
            {
                "chain": "prerouting",
                "protocol": "tcp",
                "dst-port": "443",
                "tls-host": pattern,
                "action": "mark-connection",
                "new-connection-mark": conn_mark,
                "passthrough": True,
                "comment": owner_tag("policy", policy.name, f"sni-{index}"),
            }
        )
        items.append(
            ConfigItem(props=props, tag=owner_tag("policy", policy.name, f"sni-{index}"))
        )

    items.append(
        ConfigItem(
            props={
                "chain": "prerouting",
                "connection-mark": conn_mark,
                "action": "mark-routing",
                "new-routing-mark": mark,
                "passthrough": False,
                # Same tag the plain mangle rule would carry: whichever match
                # mechanism a policy renders through, its mark-routing row is
                # identified the same way, so per-policy counters (M7) do not
                # need to know which path a policy took to find its own row.
                "comment": owner_tag("policy", policy.name),
            },
            tag=owner_tag("policy", policy.name),
        )
    )
    return items


def _mangle_rule(policy: Policy, mark: str, site_name: str) -> ConfigItem:
    props = _match_props(policy)
    props.update(
        {
            "chain": "prerouting",
            "action": "mark-routing",
            "new-routing-mark": mark,
            # Marking every packet of a flow costs more than marking the first
            # and letting the connection tracker carry the rest, but it is
            # correct when a path changes mid-flow, which is the whole point of
            # SLA steering.
            "passthrough": False,
            "comment": owner_tag("policy", policy.name),
        }
    )
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


# -- load balancing ---------------------------------------------------------
#
# RouterOS has no weighted next hop. What it has is PCC: hash a connection's
# addresses into one of N buckets, and act on the bucket. So "70/30 across two
# links" becomes "of 10 buckets, 7 go left and 3 go right", and the weights are
# bucket counts rather than anything the router understands as a weight.


def _strategy(policy: Policy) -> str:
    group = policy.sdwan_group
    return str(group.strategy) if group is not None else "failover"


def _weights(policy: Policy, paths: list[PathOption]) -> list[int]:
    """One weight per path, in path order.

    Paths are resolved from group members by tag, and a tag can match more than
    one uplink at a site, so this cannot be a straight zip: each path carries
    its own weight from whichever member selected it.
    """
    group = policy.sdwan_group
    by_uplink: dict[str, int] = {}
    for member in (group.members or []) if group is not None else []:
        if isinstance(member, dict) and member.get("uplink"):
            by_uplink[str(member["uplink"])] = int(member.get("weight", 1) or 1)
    return [max(1, by_uplink.get(path.wan_name, 1)) for path in paths]


def _buckets(policy: Policy, paths: list[PathOption]) -> list[int]:
    """Which path each bucket belongs to.

    Returns a list of path indexes, one entry per bucket. Scaled to at most
    MAX_BUCKETS: weights of 100 and 1 would otherwise render 101 classifier
    rules, and the ratio survives scaling while the rule count does not.

    Every path gets at least one bucket. A member with weight 1 next to a
    member with weight 100 rounds to zero otherwise, which silently removes a
    link somebody deliberately listed.
    """
    weights = _weights(policy, paths)
    total = sum(weights)
    if total <= MAX_BUCKETS:
        counts = weights
    else:
        counts = [max(1, round(w * MAX_BUCKETS / total)) for w in weights]
        # Rounding up every small share can overshoot the cap. Trim from the
        # largest, which is the one that can spare it.
        while sum(counts) > MAX_BUCKETS:
            biggest = counts.index(max(counts))
            if counts[biggest] == 1:
                break  # every member is down to one bucket; the cap yields
            counts[biggest] -= 1

    buckets: list[int] = []
    for index, count in enumerate(counts):
        buckets.extend([index] * count)
    return buckets


def _pcc_rules(
    policy: Policy, mark: str, paths: list[PathOption], buckets: list[int]
) -> list[ConfigItem]:
    """Classify into buckets, then route by the bucket.

    Two passes, in this order, because they depend on each other: the first
    marks the *connection* so every later packet of it takes the same path
    without being re-hashed, and the second turns that into a routing mark.
    Marking the connection rather than the packet is what stops a single TCP
    stream being split across two links mid-transfer.
    """
    items: list[ConfigItem] = []
    total = len(buckets)

    for position, path_index in enumerate(buckets):
        props = _match_props(policy)
        props.update(
            {
                "chain": "prerouting",
                "action": "mark-connection",
                "new-connection-mark": _conn_mark(mark, path_index),
                # both-addresses so a client's connections to different servers
                # spread out. src-address alone pins each client to one link,
                # which is the opposite of what a load balancer is for.
                "per-connection-classifier": f"both-addresses:{total}/{position}",
                # Must continue: the routing pass below is a separate rule, and
                # the remaining classifiers still need to see unmatched
                # connections.
                "passthrough": True,
                "comment": owner_tag("policy", policy.name, f"pcc-{position}"),
            }
        )
        items.append(
            ConfigItem(props=props, tag=owner_tag("policy", policy.name, f"pcc-{position}"))
        )

    for path_index in sorted(set(buckets)):
        items.append(
            ConfigItem(
                props={
                    "chain": "prerouting",
                    "action": "mark-routing",
                    "connection-mark": _conn_mark(mark, path_index),
                    "new-routing-mark": _bucket_mark(mark, path_index),
                    "passthrough": False,
                    "comment": owner_tag("policy", policy.name, f"route-{path_index}"),
                },
                tag=owner_tag("policy", policy.name, f"route-{path_index}"),
            )
        )
    return items


def _conn_mark(mark: str, index: int) -> str:
    return f"{mark}-c{index}"[:31]


def _bucket_mark(mark: str, index: int) -> str:
    return f"{mark}-{index}"[:31]


def _balanced_tables(mark: str, paths: list[PathOption], site_name: str) -> list[ConfigItem]:
    """One routing table per member, not per bucket.

    Buckets that share a member share its table. Otherwise a 7/3 split would
    build ten identical tables.
    """
    tag = owner_tag("policy", mark, "table")
    return [
        ConfigItem(props={"name": _bucket_mark(mark, index), "fib": True}, tag=tag)
        for index in range(len(paths))
    ]


def _balanced_routes(
    policy: Policy, mark: str, paths: list[PathOption], site_name: str
) -> list[ConfigItem]:
    """Each table prefers its own member and falls back to the others.

    The fallback is what makes this survivable. Without it a member going down
    blackholes every connection hashed to it -- balancing without failover is
    worse than no balancing, because the failure is partial and looks random.
    """
    tag = owner_tag("policy", policy.name, "route")
    items: list[ConfigItem] = []
    for table_index in range(len(paths)):
        table = _bucket_mark(mark, table_index)
        for path_index, path in enumerate(paths):
            distance = 1 if path_index == table_index else 2
            for hop in path.next_hops:
                items.append(
                    ConfigItem(
                        props={
                            "dst-address": "0.0.0.0/0",
                            "gateway": hop,
                            "routing-table": table,
                            "distance": distance,
                            "check-gateway": "ping",
                            "comment": f"{tag}:{table}:{path.wan_name}",
                        },
                        tag=f"{tag}:{table}:{path.wan_name}",
                    )
                )
        if policy.fallback == "any":
            items.append(
                ConfigItem(
                    props={
                        "dst-address": "0.0.0.0/0",
                        "gateway": "main",
                        "routing-table": table,
                        "distance": 250,
                        "comment": f"{tag}:{table}:fallback",
                    },
                    tag=f"{tag}:{table}:fallback",
                )
            )
    return items


def _probes(
    policy: Policy,
    paths: list[PathOption],
    site_name: str,
    *,
    mark: str | None = None,
) -> list[ConfigItem]:
    """Netwatch entries carrying the group's SLA thresholds.

    ``mark`` switches this to the load-balanced shape. There, one gateway sits
    at a different distance in every table -- preferred in its own, a fallback
    in the others -- so a script that set one distance everywhere would flatten
    the balance into "everything via whichever path recovered last".
    """
    sla = _sla(policy)
    tag = owner_tag("policy", policy.name, "sla")
    items: list[ConfigItem] = []
    for index, path in enumerate(paths):
        if mark is None:
            healthy = [(None, index + 1)]
        else:
            # Distance 1 in its own table, 2 in the rest -- exactly what
            # _balanced_routes wrote.
            healthy = [
                (_bucket_mark(mark, table), 1 if table == index else 2)
                for table in range(len(paths))
            ]
        demoted = [(table, distance + SLA_PENALTY) for table, distance in healthy]

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
                "down-script": _distance_script(hop, demoted),
                "up-script": _distance_script(hop, healthy),
                "comment": f"{tag}:{path.wan_name}",
            }
            if sla.jitter_ms:
                props["thr-jitter"] = f"{sla.jitter_ms}ms"
            items.append(ConfigItem(props=props, tag=f"{tag}:{path.wan_name}"))
    return items


def _distance_script(gateway: str, targets: list[tuple[str | None, int]]) -> str:
    """A RouterOS script setting this gateway's routes to given distances.

    ``targets`` is (routing table, distance) pairs; a table of None means every
    table, which is the failover case where there is only one.

    Absolute, not relative. An earlier version added a penalty to the current
    distance, which compounds: two down events in a row demote the path twice
    and it never returns to its original preference. Both the healthy and the
    demoted values are known at render time, so just write them.

    Kept to one line: RouterOS stores scripts verbatim, and a multi-line value
    round-trips with whitespace changes that would diff dirty forever.
    """
    clauses = []
    for table, distance in targets:
        where = f'gateway="{gateway}"'
        if table is not None:
            where += f' routing-table="{table}"'
        clauses.append(
            f":foreach r in=[/ip/route/find {where}] "
            f"do={{/ip/route/set $r distance={distance}}}"
        )
    return "; ".join(clauses)


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
            "routing_table",  # before the mangle (70) and routes (80) using it
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
