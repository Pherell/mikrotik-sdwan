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

**Local breakout.** A group member is either ``via=overlay`` (the default: the
route's gateway is a tunnel's far end, so traffic rides the fabric to the hub)
or ``via=direct``: the route's gateway is the WAN's own next hop and traffic
leaves for the internet right here, NATed by the uplink's masquerade rule.
That is what SaaS traffic wants -- hairpinning Microsoft 365 through a hub
adds a continent of latency for nothing. Nothing else about the machinery
changes: a direct path is one more route in the same policy table, ordered by
the same distance, demoted by the same netwatch scripts, so "direct on fibre,
then overlay via the hub" is an ordinary failover group and a direct member is
an ordinary PCC bucket.

What *does* differ is health. Pinging the WAN gateway only proves the CPE is
alive, so a direct path is probed at an internet address instead (see
``DEFAULT_PROBE_TARGETS``). RouterOS netwatch cannot choose a routing table,
so the probe is forced out the right WAN by a /32 host route for the target
in ``main`` -- which is why the target must be unique per WAN, and why it is
also kept out of the output-chain marks by the infra guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

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
    # Tunnel addresses reachable over this uplink, in fabric order. Overlay
    # steering points at these, not at the WAN gateway, so traffic stays
    # encrypted. For a direct path _paths_for replaces them with the single
    # breakout next hop (see direct_hop).
    next_hops: list[str]
    # The fields below are set by _paths_for on the copy it chooses, from the
    # group member that selected the uplink. services.fabric.policy_view builds
    # every option as overlay; the same WAN can then be chosen once per mode.
    via: str = "overlay"
    weight: int = 1
    # The internet address netwatch probes for a direct path. None on overlay.
    probe_target: str | None = None

    @property
    def direct_hop(self) -> str:
        """Where a breakout route on this uplink points.

        The WAN's gateway address when it has one. An interface-gateway uplink
        -- PPPoE, LTE, anything point-to-point -- has no gateway address
        (``Wan.gateway`` is None), and RouterOS accepts the interface name
        itself as a route gateway there (``gateway=pppoe-out1``). That is also
        the right answer on such a link: the far end can change on every
        reconnect, and the interface cannot.
        """
        return self.gateway or self.interface

    @property
    def path_id(self) -> str:
        """Distinguishes the two ways one WAN can be a path.

        Overlay keeps the bare WAN name, so every tag and comment rendered
        before breakout existed is unchanged and an upgrade rewrites no row.
        """
        return self.wan_name if self.via == "overlay" else f"{self.wan_name}:direct"


# Default internet probe targets for direct paths, handed out one per WAN in
# WAN-name order (so adding a *policy* never moves a WAN's target; adding a
# WAN that sorts earlier can). Each is pinned out its WAN by a /32 in main --
# see _probe_routes -- so they are deliberately well-known anycast resolvers
# that answer ICMP from everywhere. The flip side, documented because it
# matters: from main, the router reaches that address *only* via that WAN, so
# a site whose clients use one of these as a DNS server will see it follow
# the pin. Set GroupMember.probe_target to something else if that matters.
DEFAULT_PROBE_TARGETS: tuple[str, ...] = (
    "1.1.1.1",
    "8.8.8.8",
    "9.9.9.9",
    "1.0.0.1",
    "8.8.4.4",
    "149.112.112.112",
    "208.67.222.222",
    "208.67.220.220",
)


@dataclass(slots=True)
class SitePolicyView:
    site_name: str
    policies: list[Policy]
    # wan tag -> the uplinks at this site carrying it.
    paths_by_tag: dict[str, list[PathOption]]
    # Addresses this router must always reach natively, never through a policy
    # table: the far end of every tunnel it builds. See _infra_rule.
    underlay_addresses: list[str] = field(default_factory=list)
    # This site's own LAN segments. Traffic *to* them must resolve in main --
    # see _lan_rules for why a policy table cannot reach them.
    lan_prefixes: list[str] = field(default_factory=list)


def render_policies(view: SitePolicyView) -> list[ConfigSection]:
    if not view.policies:
        # Still emit the empty sections: a policy that was deleted must have its
        # rules swept off the device.
        return _sections(view, [], [], [], [], [], [])

    lists: list[ConfigItem] = []
    mangle: list[ConfigItem] = []
    tables: list[ConfigItem] = []
    routes: list[ConfigItem] = []
    probes: list[ConfigItem] = []
    rules: list[ConfigItem] = []

    # Guard addresses are computed now but emitted after the loop (see below),
    # because they belong only where steering was actually rendered. The peer
    # underlay must stay out of every policy table: ``output`` is where the
    # router's own encapsulated tunnel packets appear, and a policy matching a
    # supernet like 10.0.0.0/8 covers the peer WAN address the tunnel is built
    # on. Marked into a table whose only route is that same tunnel, it loops --
    # the session drops, check-gateway takes the route out, and a strict table
    # drops the handshake that would have rebuilt it.
    infra = _infra_addresses(view)
    lan = _lan_addresses(view)
    default_probes = _default_probe_targets(view)
    # Every direct path actually rendered, across policies: their probe
    # targets need a host route each, and a place in the infra guard.
    direct_paths: list[PathOption] = []

    seen_marks: set[str] = set()
    for policy in sorted(view.policies, key=lambda p: (p.priority, p.name)):
        if not policy.enabled:
            continue
        mark = _mark(policy)
        paths = _paths_for(policy, view, default_probes)
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
            mangle.extend(_mangle_rules(policy, mark, view.site_name))

        if mark not in seen_marks:
            seen_marks.add(mark)
            direct_paths.extend(p for p in paths if p.via == "direct")
            if balanced:
                tables.extend(_balanced_tables(mark, paths, view.site_name))
                routes.extend(_balanced_routes(policy, mark, paths, view.site_name))
                probes.extend(_probes(policy, paths, view.site_name, mark=mark))
                rules.extend(
                    _fallback_rules(
                        policy, [_bucket_mark(mark, i) for i in range(len(paths))]
                    )
                )
            else:
                tables.append(_routing_table(mark, view.site_name))
                routes.extend(_routes(policy, mark, paths, view.site_name))
                probes.extend(_probes(policy, paths, view.site_name, table=mark))
                rules.extend(_fallback_rules(policy, [mark]))

    # Direct paths are probed at an internet address pinned out their WAN by a
    # host route in main (see _probe_routes). Those addresses join the infra
    # guard: the probe is the router's own traffic, so without the guard the
    # output copy of a policy whose match covers it -- 0.0.0.0/0 breakout, say
    # -- would mark netwatch's ping into the policy table, and the probe would
    # measure whichever path that table currently prefers instead of the WAN
    # it is supposed to be judging. The peer underlay stays in the same guard,
    # unchanged: a direct table's default route is no more a way to reach a
    # tunnel's far end than an overlay one is.
    if direct_paths:
        routes.extend(_probe_routes(direct_paths, view.site_name))
        infra = sorted(set(infra) | {p.probe_target for p in direct_paths if p.probe_target})

    # Guards go first, and only when steering was actually rendered. mangle is
    # positional, so an accept below the rule it guards protects nothing; and a
    # guard emitted with no steering would leave a stray rule where the "renders
    # nothing" sweep expects an empty section, so a deleted policy would never
    # be swept off the device. _local_bypass_rule keeps traffic destined to one
    # of this router's own addresses on the main table; _infra_rule does the
    # same for the peer underlay addresses.
    if mangle:
        guards: list[ConfigItem] = [_local_bypass_rule(view)]
        if lan:
            lists.extend(_lan_list(view, lan))
            guards.extend(_lan_rules(view))
        if infra:
            lists.extend(_infra_list(view, infra))
            guards.append(_infra_rule(view))
        mangle = guards + mangle

    return _sections(view, lists, mangle, tables, routes, probes, rules)


# -- pieces -----------------------------------------------------------------


def _mark(policy: Policy) -> str:
    return f"sdwan-{_slug(policy.name)}"[:31]


def _members(policy: Policy) -> list[_Member]:
    """The group's members, in preference order.

    A rule with no group steers nothing. That is deliberate: the group is where
    "which uplinks, in what order" lives now, and a rule without one has not
    said where its traffic should go.

    Read defensively from the JSON column: rows written before ``via`` and
    ``probe_target`` existed lack the keys and mean overlay, and anything that
    is not a recognisable mode is treated as overlay too -- the schema refuses
    it on write, so here it can only be hand-edited data, and overlay is the
    mode that never sends traffic to the internet unencrypted.
    """
    group = policy.sdwan_group
    if group is None:
        return []
    members: list[_Member] = []
    for member in group.members or []:
        if not isinstance(member, dict) or not member.get("uplink"):
            continue
        via = "direct" if member.get("via") == "direct" else "overlay"
        probe = member.get("probe_target") if via == "direct" else None
        members.append(
            _Member(
                uplink=str(member["uplink"]),
                via=via,
                weight=max(1, int(member.get("weight", 1) or 1)),
                probe_target=str(probe) if probe else None,
            )
        )
    return members


@dataclass(slots=True, frozen=True)
class _Member:
    uplink: str
    via: str
    weight: int
    probe_target: str | None


def _sla(policy: Policy) -> SlaProfile:
    """The group's health standard, or the built-in default."""
    group = policy.sdwan_group
    return (group.sla_profile if group is not None else None) or DEFAULT_SLA


def _paths_for(
    policy: Policy, view: SitePolicyView, default_probes: dict[str, str] | None = None
) -> list[PathOption]:
    """Uplinks present at this site, in the group's order.

    Each chosen path is a *copy* stamped with the member's mode and weight, so
    one WAN can be chosen twice -- once direct, once overlay -- and the two
    stay distinct routes, buckets and probes.

    An overlay path needs a tunnel on that WAN (no tunnel, nowhere to send
    it). A direct path needs nothing but the WAN itself: its next hop is the
    WAN's gateway, or the interface when the uplink has no gateway address.
    """
    chosen: list[PathOption] = []
    seen: set[tuple[str, str]] = set()
    for member in _members(policy):
        for path in sorted(view.paths_by_tag.get(member.uplink, []), key=lambda p: p.cost):
            key = (path.wan_name, member.via)
            if key in seen:
                continue
            if member.via == "direct":
                target = member.probe_target or (default_probes or {}).get(path.wan_name)
                if target is None:
                    raise ValueError(
                        f"policy {policy.name!r}: no internet probe target left for "
                        f"direct uplink {path.wan_name!r} at {view.site_name!r} -- "
                        f"more than {len(DEFAULT_PROBE_TARGETS)} WANs; set the "
                        "group member's probe_target"
                    )
                seen.add(key)
                chosen.append(
                    replace(
                        path,
                        via="direct",
                        weight=member.weight,
                        next_hops=[path.direct_hop],
                        probe_target=target,
                    )
                )
            elif path.next_hops:
                seen.add(key)
                chosen.append(replace(path, via="overlay", weight=member.weight))
    return chosen


def _default_probe_targets(view: SitePolicyView) -> dict[str, str]:
    """WAN name -> the internet address its direct paths are probed at.

    Assigned once per site, over every WAN the view knows, in name order --
    not per policy -- so two policies breaking out of the same WAN share one
    target and one host route, and adding a policy never moves a target.
    Addresses an operator set explicitly on any member are taken out of the
    pool first, so a default can never collide with them.
    """
    claimed = {
        member.probe_target
        for policy in view.policies
        if policy.enabled
        for member in _members(policy)
        if member.probe_target
    }
    pool = [t for t in DEFAULT_PROBE_TARGETS if t not in claimed]
    wans = sorted({p.wan_name for options in view.paths_by_tag.values() for p in options})
    return dict(zip(wans, pool, strict=False))


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
    conn_mark = _suffixed(mark, "-sni")
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


def _mangle_rules(policy: Policy, mark: str, site_name: str) -> list[ConfigItem]:
    """Mark in ``prerouting`` for traffic passing through, ``output`` for the
    router's own.

    prerouting never sees a packet the router originates, so a policy that only
    marked there steered its clients' traffic while the router's own -- a ping
    or a probe run from the device, the thing an operator reaches for first when
    checking whether steering works -- quietly kept using the main table.

    Both rules share one match, so both are equally capable of matching
    infrastructure traffic. _infra_rule, rendered above every rule here, is what
    keeps the output copy from swallowing the tunnels this policy rides on.
    """
    items: list[ConfigItem] = []
    base_props = _match_props(policy)
    for chain in ("prerouting", "output"):
        props = dict(base_props)
        # Marking every packet of a flow costs more than marking the first and
        # letting the connection tracker carry the rest, but it is correct when
        # a path changes mid-flow, which is the whole point of SLA steering.
        props.update(
            {
                "chain": chain,
                "action": "mark-routing",
                "new-routing-mark": mark,
                "passthrough": False,
                "comment": owner_tag("policy", policy.name, _chain_suffix(chain)),
            }
        )
        items.append(
            ConfigItem(
                props=props,
                tag=owner_tag("policy", policy.name, _chain_suffix(chain)),
            )
        )
    return items


def _chain_suffix(chain: str) -> str:
    """The prerouting rule keeps its original tag, so an upgrade rewrites no row."""
    return "" if chain == "prerouting" else chain


# -- keeping the underlay out of the policy tables ---------------------------


def _infra_list_name(view: SitePolicyView) -> str:
    return f"sdwan-{_slug(view.site_name)}-infra"[:63]


def _infra_addresses(view: SitePolicyView) -> list[str]:
    """The peer WAN addresses that must resolve in ``main``, never in a table.

    Only the underlay. A tunnel's *overlay* next hop needs no guard: it is the
    gateway the policy route already points at, and RouterOS resolves that
    through the connected route on the tunnel interface, so a marked packet
    addressed to it leaves by the interface it was going to leave by anyway.
    An underlay address is the opposite case -- the only route to it is the
    /32 the fabric pins to a physical gateway, and that pin lives in main. A
    policy table holds a default route through the tunnel and nothing else, so
    a marked packet bound for the peer's WAN address is handed to the very
    tunnel it is carrying.
    """
    return sorted(a for a in set(view.underlay_addresses) if a)


def _infra_list(view: SitePolicyView, addresses: list[str]) -> list[ConfigItem]:
    name = _infra_list_name(view)
    tag = owner_tag("policy", view.site_name, "infra")
    return [
        ConfigItem(
            props={"list": name, "address": address},
            tag=f"{tag}:{address}",
        )
        for address in addresses
    ]


def _infra_rule(view: SitePolicyView) -> ConfigItem:
    """``accept`` in mangle stops chain traversal, it does not drop the packet.

    So this leaves infrastructure traffic entirely unmarked and the main table
    routes it, which is the only table holding the host routes that reach it.
    """
    tag = owner_tag("policy", view.site_name, "infra", "rule")
    return ConfigItem(
        props={
            "chain": "output",
            "action": "accept",
            "dst-address-list": _infra_list_name(view),
            "comment": tag,
        },
        tag=tag,
    )


def _lan_list_name(view: SitePolicyView) -> str:
    return f"sdwan-{_slug(view.site_name)}-lan"[:63]


def _lan_addresses(view: SitePolicyView) -> list[str]:
    return sorted(a for a in set(view.lan_prefixes) if a)


def _lan_list(view: SitePolicyView, prefixes: list[str]) -> list[ConfigItem]:
    name = _lan_list_name(view)
    tag = owner_tag("policy", view.site_name, "lan")
    return [
        ConfigItem(props={"list": name, "address": prefix}, tag=f"{tag}:{prefix}")
        for prefix in prefixes
    ]


def _lan_rules(view: SitePolicyView) -> list[ConfigItem]:
    """Accept, before any mark, everything addressed to this site's own LAN.

    A policy table holds one thing: a default route into the overlay. It has
    none of main's connected routes, so a packet marked into it while headed
    for a local segment is sent into a tunnel. Two kinds of traffic were
    getting caught that way:

    - inter-VLAN traffic whose source a policy matches (``src=LAN`` with a
      broad or empty destination), and
    - *replies* coming back from the overlay. The PCC routing pass matches on
      ``connection-mark`` alone, and a connection's mark is on its replies too,
      so the return leg of every balanced flow was routed straight back into
      the bucket table's tunnel instead of to the client.

    Operators were working around it with an extra high-priority policy per LAN
    segment; this guard makes that unnecessary. ``dst-address-type=local``
    (_local_bypass_rule) does not cover it: it matches the router's own
    addresses, not the subnets behind them.

    Rendered in output too, so the router's own traffic to its LAN is not
    captured by the output copy of a failover rule.
    """
    tag = owner_tag("policy", view.site_name, "lan_dst", "rule")
    items: list[ConfigItem] = []
    for chain in ("prerouting", "output"):
        chain_tag = tag if chain == "prerouting" else f"{tag}:output"
        items.append(
            ConfigItem(
                props={
                    "chain": chain,
                    "action": "accept",
                    "dst-address-list": _lan_list_name(view),
                    "comment": chain_tag,
                },
                tag=chain_tag,
            )
        )
    return items


def _local_bypass_rule(view: SitePolicyView) -> ConfigItem:
    """Accept, in prerouting, anything destined to one of this router's own
    addresses -- its LAN gateway, a local service.

    ``accept`` in mangle stops chain traversal without dropping, so the packet
    is left unmarked and resolves in the main table. Without it a broad policy
    prefix would mark the router's own management traffic into a policy table.
    """
    tag = owner_tag("policy", view.site_name, "local_dst", "rule")
    return ConfigItem(
        props={
            "chain": "prerouting",
            "action": "accept",
            "dst-address-type": "local",
            "comment": tag,
        },
        tag=tag,
    )


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
            props: dict[str, object] = {
                "dst-address": "0.0.0.0/0",
                "gateway": hop,
                "routing-table": mark,
                # Order of preference. Netwatch raises this by 100 when
                # the path breaches its SLA, which demotes it below the
                # next preference without removing it. A direct path is
                # ranked exactly like an overlay one: that is what lets
                # "direct first, then via the hub" be a plain failover.
                "distance": index + 1,
                "comment": f"{tag}:{path.path_id}",
            }
            props.update(_check_gateway(path))
            items.append(ConfigItem(props=props, tag=f"{tag}:{path.path_id}"))
    # The "any" fallback is a /routing/rule (action=lookup), not a route:
    # RouterOS 7 has no gateway-is-a-table route. See _fallback_rules.
    return items


def _check_gateway(path: PathOption) -> dict[str, object]:
    """``check-gateway=ping``, where a ping can mean something.

    Every overlay hop and every direct hop with a gateway address gets it, so
    a dead next hop drops out of the table within seconds, before netwatch's
    SLA window has even filled. A direct path through an interface gateway
    (PPPoE, LTE) gets none: there is no address to ping, and the route already
    goes inactive by itself when the interface goes down. Upstream failure
    beyond either kind of next hop is what the internet probe is for.
    """
    if path.via == "direct" and path.gateway is None:
        return {}
    return {"check-gateway": "ping"}


def _probe_routes(paths: list[PathOption], site_name: str) -> list[ConfigItem]:
    """Pin each direct path's probe target out its own WAN, in ``main``.

    RouterOS netwatch has no routing-table or interface option: its ICMP
    probe is routed by main like any other router-originated packet. Left
    alone it would leave by main's default route -- whichever WAN that is --
    and a "fibre" probe could be answered over LTE. A /32 for the target via
    the WAN's own next hop is the only way to make the probe judge the WAN it
    is named after.

    Consequences that come with that, stated rather than hidden:

    - One address can only be pinned to one WAN, so targets are unique per
      WAN (DEFAULT_PROBE_TARGETS hands them out one each); two WANs claiming
      one target is refused here rather than rendered as ECMP.
    - The pin applies to *all* main-table traffic to that address, not just
      netwatch's -- pick a target the site's clients do not depend on.
    - When the WAN's next hop itself dies the pin goes inactive and the probe
      can leak out another WAN and still answer. That is the case
      check-gateway (or the interface going down) already handles on the
      policy route itself; the probe exists for failures *beyond* the next
      hop, where the pin stays active and the probe fails as it should.
    """
    owner: dict[str, PathOption] = {}
    for path in paths:
        target = path.probe_target
        if target is None:
            continue
        other = owner.get(target)
        if other is not None and other.wan_name != path.wan_name:
            raise ValueError(
                f"probe target {target} is claimed by both {other.wan_name!r} and "
                f"{path.wan_name!r} at {site_name!r}; netwatch cannot choose a "
                "routing table, so each WAN needs its own target"
            )
        owner.setdefault(target, path)

    tag = owner_tag("policy", site_name, "probe")
    items: list[ConfigItem] = []
    for target in sorted(owner):
        path = owner[target]
        items.append(
            ConfigItem(
                props={
                    "dst-address": f"{target}/32",
                    "gateway": path.direct_hop,
                    "routing-table": "main",
                    "distance": 1,
                    "comment": f"{tag}:{path.wan_name}:{target}",
                },
                tag=f"{tag}:{path.wan_name}:{target}",
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
    its own weight from whichever member selected it (stamped by _paths_for).
    Looking it up by WAN name instead silently gave weight 1 to every path a
    member chose by *tag*, and cannot tell fibre-direct from fibre-overlay.
    """
    return [max(1, path.weight) for path in paths]


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
                # Classify a connection once, on its first packet. Without
                # these the classifier re-hashed every packet of every flow,
                # replies included, so anything that toggled a classifier
                # (or another rule marking first) re-pinned live connections
                # to a different bucket mid-flow -- they broke, and it showed
                # up as the group flapping.
                "connection-state": "new",
                "connection-mark": "no-mark",
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
                # Asserted, not left to runtime: an earlier version had the
                # SLA scripts disable classifiers, and a device upgraded while
                # a link was down would otherwise keep that bucket off forever.
                "disabled": False,
                "comment": owner_tag("policy", policy.name, f"pcc-{position}"),
            }
        )
        items.append(
            ConfigItem(
                props=props,
                tag=owner_tag("policy", policy.name, f"pcc-{position}"),
                enforce=("disabled",),
            )
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
    return _suffixed(mark, f"-c{index}")


def _bucket_mark(mark: str, index: int) -> str:
    return _suffixed(mark, f"-{index}")


# RouterOS caps a routing or connection mark at 31 characters.
MARK_MAX = 31


def _suffixed(base: str, suffix: str) -> str:
    """Trim the base to make room for the suffix, never the other way round.

    Truncating after appending silently collapses everything the suffix was
    distinguishing. A policy name long enough to push _mark to the cap left
    every bucket with the same mark, so the classifier spread connections
    across buckets that all named one connection mark and one table -- a
    load_balance group that renders, applies, and behaves like a single link.
    """
    return f"{base[: MARK_MAX - len(suffix)]}{suffix}"


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
                props: dict[str, object] = {
                    "dst-address": "0.0.0.0/0",
                    "gateway": hop,
                    "routing-table": table,
                    "distance": distance,
                    "comment": f"{tag}:{table}:{path.path_id}",
                }
                props.update(_check_gateway(path))
                items.append(ConfigItem(props=props, tag=f"{tag}:{table}:{path.path_id}"))
        # "any" fallback for this bucket table is a /routing/rule, not a route
        # (RouterOS 7 has no gateway=main). See _fallback_rules.
    return items


def _fallback_rules(policy: Policy, table_names: list[str]) -> list[ConfigItem]:
    """The "any" fallback, as a /routing/rule per table.

    RouterOS 7 has no route whose gateway is another table, so "if this table
    has no live route, use the main table" is expressed as a routing rule with
    ``action=lookup`` -- which, unlike ``lookup-only-in-table``, falls through
    to the next rule (ultimately the main table) on a miss. Without a rule a
    routing-mark is looked up only in its own table and dropped on a miss, so
    a non-"any" policy needs none: strict is the default.
    """
    if policy.fallback != "any":
        return []
    tag = owner_tag("policy", policy.name, "rule")
    return [
        ConfigItem(
            props={
                "routing-mark": table,
                "action": "lookup",  # not lookup-only-in-table: allow fallthrough
                "table": table,
                "comment": f"{tag}:{table}",
            },
            tag=f"{tag}:{table}",
        )
        for table in table_names
    ]


def _probes(
    policy: Policy,
    paths: list[PathOption],
    site_name: str,
    *,
    mark: str | None = None,
    table: str | None = None,
) -> list[ConfigItem]:
    """Netwatch entries carrying the group's SLA thresholds.

    ``mark`` switches this to the load-balanced shape. There, one gateway sits
    at a different distance in every table -- preferred in its own, a fallback
    in the others -- so a script that set one distance everywhere would flatten
    the balance into "everything via whichever path recovered last".

    ``table`` is the failover policy's single table. Overlay scripts keep
    their historic table-less shape (one table per policy, and rewriting every
    deployed script for no behavioural gain is churn), but a *direct* path's
    script must name it: its gateway is the WAN's own next hop, which main
    also uses -- for the probe pin, and for the fabric's underlay pins -- so
    a bare ``find gateway=`` would re-distance those too and quietly reorder
    the tunnels' own underlay.

    A direct path is probed at its internet target, not at the next hop it
    routes through (see _probe_routes for how the probe is kept on its WAN);
    the scripts still act on routes by the next hop, exactly as for overlay.

    Load balancing is health-aware through the routes alone. Every bucket table
    already holds the other members at distance 2, so demoting a breaching
    member moves its buckets onto the survivors -- still inside the overlay.
    An earlier version also disabled the member's PCC classifiers, which did
    the opposite of what it meant to: an unclassified connection carries no
    routing mark, so it left by the *main* table, out the raw WAN, NATed. Then
    the classifier came back on and re-hashed those live flows into the
    tunnel, breaking them. That round trip is a large part of what looked like
    the group flapping.
    """
    sla = _sla(policy)
    tag = owner_tag("policy", policy.name, "sla")
    items: list[ConfigItem] = []
    for index, path in enumerate(paths):
        if mark is None:
            scope = table if path.via == "direct" else None
            healthy = [(scope, index + 1)]
        else:
            # Distance 1 in its own table, 2 in the rest -- exactly what
            # _balanced_routes wrote.
            healthy = [
                (_bucket_mark(mark, t), 1 if t == index else 2)
                for t in range(len(paths))
            ]
        demoted = [(t, distance + SLA_PENALTY) for t, distance in healthy]
        comment = f"{tag}:{path.path_id}"

        for hop in path.next_hops:
            host = path.probe_target if path.via == "direct" and path.probe_target else hop
            props: dict[str, object] = {
                "host": host,
                "type": "icmp",
                "interval": f"{sla.probe_interval_seconds}s",
                "packet-count": sla.probe_count,
                "thr-loss-percent": sla.loss_percent,
                # ROS 7 netwatch has no "thr-latency"; the latency fail
                # threshold is thr-avg (fail above this average RTT). thr-max
                # exists too, for peak RTT; avg is the SLA metric we mean.
                "thr-avg": f"{sla.latency_ms}ms",
                "disabled": False,
                # Demote rather than delete: the route stays in the table so the
                # path can be re-preferred once it has recovered.
                "down-script": _health_script(hop, demoted),
                "up-script": _health_script(
                    hop,
                    healthy,
                    hold_down=sla.recovery_seconds,
                    probe=(host, comment),
                ),
                "comment": comment,
            }
            if sla.jitter_ms:
                props["thr-jitter"] = f"{sla.jitter_ms}ms"
            items.append(ConfigItem(props=props, tag=comment))
    return items


def _health_script(
    gateway: str,
    targets: list[tuple[str | None, int]],
    *,
    hold_down: int = 0,
    probe: tuple[str, str] | None = None,
) -> str:
    """RouterOS script setting route distances.

    ``targets`` is (routing table, distance) pairs; a table of None means every
    table, which is the failover case where there is only one.

    ``hold_down`` is the SLA profile's ``recovery_seconds``, used on the
    up-script only. Down is acted on at once; up must *stay* up that long
    before the path is re-preferred. Without it a link that breaches on
    latency recovers the moment traffic leaves it, takes the traffic back,
    breaches again -- and under load_balance, where a member's latency is a
    function of the load PCC puts on it, that loop never settles. The script
    waits, then re-reads its own netwatch entry (``probe`` = host, comment)
    and restores only if it is still up; a link that dropped again during the
    wait stays demoted, and its next up event starts a fresh hold-down.

    Distances are absolute, not relative. An earlier version added a penalty to
    the current distance, which compounds: two down events in a row demote the
    path twice and it never returns to its original preference. Both the healthy
    and the demoted values are known at render time, so just write them.

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
    body = "; ".join(clauses)
    if hold_down <= 0 or probe is None:
        return body
    host, comment = probe
    return (
        f":delay {hold_down}s; "
        f':if ([/tool/netwatch/get [/tool/netwatch/find host="{host}" comment="{comment}"] status]'
        f' = "up") do={{{body}}}'
    )


def _sections(
    view: SitePolicyView,
    lists: list[ConfigItem],
    mangle: list[ConfigItem],
    tables: list[ConfigItem],
    routes: list[ConfigItem],
    probes: list[ConfigItem],
    rules: list[ConfigItem],
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
            # ROS returns fib as an empty string even when it was set true, so
            # comparing it diffs dirty on every run. It is set once on create.
            ignore=("fib",),
            items=tables,
        ),
        section(
            "/ip/route",
            "policy",
            owner=scope,
            key=("dst-address", "gateway", "routing-table"),
            # distance is netwatch's at runtime: its scripts raise a breaching
            # path by SLA_PENALTY and restore it on recovery. The reconciler
            # sets the baseline on create and must not re-assert it, or every
            # apply would briefly un-demote a path netwatch had correctly
            # dropped. (Verified on hardware: a down tunnel sat at 101, and
            # re-planning wanted it back at 1.)
            ignore=("distance",),
            items=routes,
        ),
        section(
            "/ip/firewall/mangle",
            "firewall",
            owner=scope,
            key=("comment",),
            ordered=True,  # first match wins; position is the semantics
            # Nothing toggles mangle rows at runtime any more (see _probes).
            # Kept ignored so rows an operator disabled by hand are not
            # silently re-enabled; PCC classifiers enforce it per row instead,
            # so a bucket left disabled by the old scripts comes back.
            ignore=("disabled",),
            items=mangle,
        ),
        section(
            "/tool/netwatch",
            "monitoring",
            owner=scope,
            key=("host", "comment"),
            ignore=("status", "since", "sent-count", "loss-count", "rtt-avg", "rtt-jitter"),
            items=probes,
        ),
        section(
            "/routing/rule",
            "routing_rule",
            owner=scope,
            key=("comment",),
            items=rules,
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
