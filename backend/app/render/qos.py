"""Render per-policy QoS: packet marks plus an HTB queue tree per shaped uplink.

A policy may carry a ``qos_class`` (realtime / interactive / default / bulk).
That is a statement about *priority*, independent of *path*: steering decides
which uplink a flow takes (render.policy), QoS decides who goes first when that
uplink is full. They are kept in separate renderers because they are separate
mechanisms on RouterOS and share nothing but the match:

1. ``/ip/firewall/mangle`` chain=postrouting -- one ``mark-packet`` rule per
   QoS policy, matching exactly what the policy matches (render.policy's
   ``_match_props``, imported rather than copied, so the QoS rule can never
   classify a wider or narrower set of traffic than the steering rule). Then a
   catch-all that marks every still-unmarked packet ``sdwan-qos-default``.
2. ``/queue/tree`` -- per WAN that has ``bandwidth_mbps`` set: a parent queue
   on the WAN interface with ``max-limit`` = the link rate, and one leaf per
   class keyed on the packet mark, with an HTB priority and a ``limit-at``
   guarantee.

Why packet marks do not fight steering
--------------------------------------
Steering uses routing marks (and, for PCC/SNI, connection marks). A packet
mark is a third, independent field on the packet: setting it changes neither
the routing decision nor the connection's mark. And the QoS rules live in
``postrouting``, a chain the steering renderer never writes, so the LAN /
local / underlay ``accept`` guards (prerouting + output) cannot skip them --
an ``accept`` in mangle ends traversal of *that chain* only.

Why postrouting and not forward
-------------------------------
``forward`` only sees transit packets. Two kinds of traffic that load the WAN
never pass through it: the router's own traffic, and -- the big one -- the
*outer* packets of every tunnel, which the router originates when it
encapsulates steered traffic. Those meet ``output`` and ``postrouting``.
``postrouting`` sees both, after the routing decision, immediately before the
interface queue, which is exactly where a queue tree on the WAN picks its leaf.

Inner packets are marked here on their way into the tunnel interface. Whether
that packet mark survives encapsulation onto the outer packet is transport-
and RouterOS-version-dependent (Linux keeps skb->mark across GRE/IPIP/IPsec in
one namespace; WireGuard is not guaranteed to). Where it does not survive the
outer packet reaches the catch-all and is shaped as ``default`` -- never
dropped, never unshaped. **Needs on-device verification per transport.**

Why every class rule also says ``packet-mark=no-mark``
-------------------------------------------------------
``mark-packet`` with ``passthrough=yes`` keeps evaluating, so without the
guard a packet matching two QoS policies would end up with the *last* one's
class. Matching only unmarked packets makes the first (highest-priority)
policy win -- the same first-match semantics the steering rules have -- and
leaves passthrough on so operator rules further down still see the packet.
It also means a packet the operator's own mangle already marked is left
alone.

Unmarked traffic
----------------
A RouterOS queue tree leaf selects packets by ``packet-mark``; a packet that
matches no leaf under an interface parent is not queued by the tree at all,
so it neither respects nor counts against ``max-limit``. Left that way, the
unclassified bulk of the traffic would bypass the shaper and starve the
realtime class anyway. The standard fix is the catch-all rendered last in
the chain: every packet still at ``no-mark`` becomes ``sdwan-qos-default``.

Limitations, stated next to the code
------------------------------------
- **Upload only.** A queue tree parented on the WAN interface shapes egress:
  traffic *leaving* by that uplink. Download arrives already having crossed
  the bottleneck; shaping it needs ingress tricks (queueing on the LAN-facing
  interfaces or ``global`` with download-direction marks, or an IFB-style
  setup) and is not attempted here.
- **FastTrack bypasses all of this.** A fasttracked connection skips mangle
  and queues entirely. The controller never renders FastTrack, but an
  operator's default config usually has ``action=fasttrack-connection`` in
  ``/ip/firewall/filter``. ``fasttrack_warnings`` detects it so a plan can say
  so; it is surfaced, not fixed, because the filter chain belongs to the
  operator.
- ``bandwidth_mbps`` must be the *real* upload rate, a little under the line
  rate, or the queue never fills on the router and the modem's buffer does
  the queueing instead.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Final

from app.drivers.base import ConfigItem, ConfigSection
from app.models.policy import Policy
from app.render.engine import owner_tag, section
from app.render.policy import (
    _address_lists,
    _mark,
    _match_props,
    _slug,
    _sni_patterns,
    _suffixed,
)

# Classes in priority order. HTB priority: 1 is served first, 8 last.
QOS_CLASSES: Final[tuple[str, ...]] = ("realtime", "interactive", "default", "bulk")

PRIORITY: Final[dict[str, int]] = {
    "realtime": 1,
    "interactive": 3,
    "default": 5,
    "bulk": 8,
}

# Guaranteed share of the uplink (limit-at) per class, in percent. Sums to 100
# so the guarantees are all honourable at once. Every leaf may still borrow up
# to the full link rate (max-limit) when the others are idle -- the guarantee
# only matters under contention, which is the only time QoS does anything.
GUARANTEE_PERCENT: Final[dict[str, int]] = {
    "realtime": 30,
    "interactive": 30,
    "default": 25,
    "bulk": 15,
}

# Queue type per leaf. Built-in types only, so nothing has to be created in
# /queue/type first:
# - default-small (pfifo, 10 packets) for realtime/interactive: a short buffer
#   is the point -- a voice packet that waited 200ms is already useless.
# - default (pfifo, 50 packets) for default/bulk: TCP throughput needs some
#   buffer, and a 10-packet fifo at 100M drops bulk flows into slow start.
# Not pcq: a WAN queue tree sees packets after src-nat (and tunnel outer
# packets all share one source), so pcq's per-address fairness would collapse
# to one sub-queue and add nothing but CPU.
QUEUE_TYPE: Final[dict[str, str]] = {
    "realtime": "default-small",
    "interactive": "default-small",
    "default": "default",
    "bulk": "default",
}

# RouterOS caps a mark at 31 characters; "sdwan-qos-interactive" is 21.
def packet_mark(qos_class: str) -> str:
    return f"sdwan-qos-{qos_class}"


@dataclass(slots=True)
class QosUplink:
    """One uplink at the site; shaped only when ``bandwidth_mbps`` is set."""

    wan_name: str
    interface: str
    bandwidth_mbps: int | None = None


@dataclass(slots=True)
class SiteQosView:
    site_name: str
    policies: list[Policy]
    uplinks: list[QosUplink] = field(default_factory=list)


def render_qos(view: SiteQosView) -> list[ConfigSection]:
    """Mark + queue sections for one site.

    Renders *nothing* (but still the empty sections, so a removed class or a
    cleared bandwidth is swept off the device) unless at least one enabled
    policy has a QoS class AND at least one uplink has a bandwidth. Marks with
    no queue to consume them would only cost CPU on every packet; queues with
    no class marks would shape everything as one class, which is a plain rate
    limit nobody asked for.
    """
    classified = [
        p
        for p in sorted(view.policies, key=lambda p: (p.priority, p.name))
        if p.enabled and p.qos_class
    ]
    shaped = [u for u in view.uplinks if u.bandwidth_mbps and u.bandwidth_mbps > 0]
    if not classified or not shaped:
        return _sections([], [], [])

    lists: list[ConfigItem] = []
    mangle: list[ConfigItem] = []
    for policy in classified:
        # The steering renderer only writes a policy's address lists when the
        # policy has a usable path at this site. QoS applies regardless, so
        # emit the same rows here; merge_sections collapses byte-identical
        # duplicates, so a policy rendered by both yields one row.
        lists.extend(_address_lists(policy, view.site_name))
        mangle.append(_mark_rule(policy))
    mangle.append(_catch_all())

    queues: list[ConfigItem] = []
    for uplink in shaped:
        queues.extend(_queue_tree(uplink))
    return _sections(lists, mangle, queues)


def _mark_rule(policy: Policy) -> ConfigItem:
    """``mark-packet`` with exactly the policy's match.

    For an SNI policy the match lives in the steering renderer's
    connection mark (tls-host is only visible in the handshake), so this
    keys on that mark as well. It is only set when the policy's steering is
    rendered at this site; without it the rule matches nothing and the
    traffic falls to ``default`` -- degraded, not wrong.
    """
    tag = owner_tag("qos", policy.name)
    props: dict[str, Any] = _match_props(policy)
    if _sni_patterns(policy):
        props["connection-mark"] = _suffixed(_mark(policy), "-sni")
    props.update(
        {
            "chain": "postrouting",
            # First matching policy wins; see the module docstring.
            "packet-mark": "no-mark",
            "action": "mark-packet",
            "new-packet-mark": packet_mark(str(policy.qos_class)),
            "passthrough": True,
            "comment": tag,
        }
    )
    return ConfigItem(props=props, tag=tag)


def _catch_all() -> ConfigItem:
    """Everything unclassified is ``default``, so nothing bypasses the shaper."""
    tag = owner_tag("qos", "catch-all")
    return ConfigItem(
        props={
            "chain": "postrouting",
            "packet-mark": "no-mark",
            "action": "mark-packet",
            "new-packet-mark": packet_mark("default"),
            "passthrough": True,
            "comment": tag,
        },
        tag=tag,
    )


def queue_name(uplink: QosUplink, qos_class: str | None = None) -> str:
    base = f"sdwan-qos-{_slug(uplink.wan_name)}"
    return f"{base}-{qos_class}" if qos_class else base


def _queue_tree(uplink: QosUplink) -> list[ConfigItem]:
    """Parent on the interface, then one leaf per class.

    Parent first: a leaf names its parent queue, which must already exist.
    Removal runs in reverse (Plan.ops), so leaves go before the parent.

    Rates are written in bits per second as plain integers, the form RouterOS
    stores and returns over REST, so the diff compares like with like.
    Every class gets a leaf even if no policy uses it -- ``default`` always
    has traffic (the catch-all), and a stable set of rows means adding the
    first ``bulk`` policy changes the mangle, not the queue tree.
    """
    rate = int(uplink.bandwidth_mbps or 0) * 1_000_000
    parent = queue_name(uplink)
    items = [
        ConfigItem(
            props={
                "name": parent,
                "parent": uplink.interface,
                "max-limit": rate,
                "comment": owner_tag("qos", uplink.wan_name),
            },
            tag=owner_tag("qos", uplink.wan_name),
        )
    ]
    for qos_class in QOS_CLASSES:
        tag = owner_tag("qos", uplink.wan_name, qos_class)
        items.append(
            ConfigItem(
                props={
                    "name": queue_name(uplink, qos_class),
                    "parent": parent,
                    "packet-mark": packet_mark(qos_class),
                    "priority": PRIORITY[qos_class],
                    "limit-at": rate * GUARANTEE_PERCENT[qos_class] // 100,
                    "max-limit": rate,
                    "queue": QUEUE_TYPE[qos_class],
                    "comment": tag,
                },
                tag=tag,
            )
        )
    return items


def _sections(
    lists: list[ConfigItem], mangle: list[ConfigItem], queues: list[ConfigItem]
) -> list[ConfigSection]:
    scope = owner_tag("qos") + ":"
    return [
        # The address-list rows carry the *policy's* tags (they are the same
        # rows render.policy writes), so this section is scoped to cover
        # those; merge_sections widens it to sdwan: anyway.
        section(
            "/ip/firewall/address-list",
            "address_list",
            owner=owner_tag("policy") + ":",
            key=("list", "address"),
            items=lists,
        ),
        section(
            "/ip/firewall/mangle",
            "firewall",
            owner=scope,
            # Same key, ordered flag and ignore as render.policy's mangle:
            # merge_sections refuses a key mismatch, and ordered=True is what
            # keeps the catch-all below the class rules.
            key=("comment",),
            ordered=True,
            ignore=("disabled",),
            items=mangle,
        ),
        section(
            "/queue/tree",
            "qos",
            owner=scope,
            key=("name",),
            # Live counters/state RouterOS reports on every queue row.
            ignore=("bytes", "packets", "dropped", "rate", "packet-rate",
                    "queued-bytes", "queued-packets", "invalid"),
            items=queues,
        ),
    ]


# -- FastTrack detection ----------------------------------------------------


def fasttrack_warnings(filter_rows: Iterable[dict[str, Any]]) -> list[str]:
    """Warn about enabled FastTrack rules in /ip/firewall/filter.

    A fasttracked connection skips mangle and queues entirely, so QoS-marked
    traffic on it is neither classified nor shaped -- silently. The
    controller never writes FastTrack and does not remove the operator's
    rule; it says so instead. The usual fix is to exclude marked traffic
    (e.g. ``connection-mark=no-mark`` plus a QoS-specific connection mark) or
    disable FastTrack on the router.
    """
    warnings: list[str] = []
    for row in filter_rows:
        if str(row.get("action", "")) != "fasttrack-connection":
            continue
        disabled = row.get("disabled")
        if disabled is True or str(disabled).lower() == "true":
            continue
        where = row.get("comment") or row.get(".id") or "?"
        warnings.append(
            f"/ip/firewall/filter has an enabled fasttrack-connection rule ({where}); "
            "fasttracked connections bypass mangle and queues, so QoS will not "
            "classify or shape them. Disable FastTrack for QoS traffic."
        )
    return warnings
