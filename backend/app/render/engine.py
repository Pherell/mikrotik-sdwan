"""Intent to ConfigSections.

Renderers are pure: model in, sections out, no device access. That keeps them
trivially testable with golden fixtures and means a plan can be produced without
touching a router.

``ORDER`` fixes the sequence in which sections are applied. Additions run low to
high; removals run high to low (see ``Plan.ops``). Getting this wrong shows up as
a device rejecting a route whose interface does not exist yet, or refusing to
delete an interface a route still points at.
"""

from __future__ import annotations

from typing import Final

from app.drivers.base import OWNER_PREFIX, ConfigSection

ORDER: Final[dict[str, int]] = {
    "address_list": 10,   # prefix groups other rules reference
    # A policy's routing table must exist before anything names it: real
    # RouterOS rejects new-routing-mark=<table> (mangle) and routing-table=
    # <table> (route) for a table it has never heard of. It must beat not just
    # those menus but the *earliest* order any of them can reach -- and the
    # mark-routing mangle shares /ip/firewall/mangle with the fabric MSS clamp
    # (tunnel, 40), so merge_sections pulls the whole mangle menu down to 40.
    # Hence 15, before that. FakeRouterOS did not enforce the reference, so
    # this only ever failed on real hardware.
    "routing_table": 15,  # /routing/table entries a later mark/route references
    # A BGP connection names a template that must exist first, or ROS rejects
    # it ("input does not match any value of template"). Ordering this is
    # subtler than it looks: services.fabric._cleanup_sections emits an empty
    # /routing/bgp/template AND /routing/bgp/connection at ORDER["tunnel"] (40),
    # and merge_sections takes the *minimum* order per path -- so a template
    # rendered at, say, 58 collapses to 40, ties the connection (also 40), and
    # the (order, path) tie-break then runs alphabetically: "connection" before
    # "template", exactly backwards. It must therefore sort below 40 to win.
    # Same shape as routing_table above; only real hardware caught it.
    "bgp_instance": 14,   # /routing/bgp/instance (ROS 7.20+) -- named by a connection
    "bgp_template": 16,   # /routing/bgp/template -- referenced by a connection
    "interface": 20,      # loopbacks, bridges
    # ipsec has intra-block dependencies that real RouterOS enforces by
    # reference: an identity names a peer, a policy names a peer and a
    # proposal, a peer names a profile. They must be applied in that order or
    # the referring row is rejected ("input does not match any value of
    # peer"). A single shared order let merge_sections' (order, path) tie-break
    # decide, which is alphabetical -- identity before peer -- exactly wrong.
    # FakeRouterOS did not validate the reference, so only real hardware caught
    # it. These stay within the old crypto slot (30-34, before tunnel=40).
    "crypto": 30,         # generic crypto, for a transport that has no ordering
    "crypto_profile": 30,  # phase-1 profile -- no dependency
    "crypto_proposal": 31,  # phase-2 proposal -- no dependency
    "crypto_peer": 32,     # names a profile
    "crypto_identity": 33,  # names a peer -- peer must exist first
    "crypto_policy": 34,   # names a peer and a proposal -- both must exist first
    "tunnel": 40,         # gre / ipip / wireguard interfaces
    "address": 50,        # ip addresses on those interfaces
    "routing": 60,        # bgp connections, static routes
    # Host routes pinning each tunnel's far endpoint to the underlay. Shares
    # /ip/route with the policy routes, which point at tunnel interfaces, so it
    # must not sort before the tunnels exist -- merge_sections takes the
    # minimum order per path, and anything below 40 would drag the whole menu
    # ahead of the interfaces those other routes name.
    "underlay_route": 60,
    "routing_rule": 62,   # /routing/rule -- references a table, so after 15
    "firewall": 70,       # mangle marks, nat
    "policy": 80,         # routing rules and tables
    "monitoring": 90,     # netwatch probes
}


def owner_tag(*parts: str) -> str:
    """Build an ownership comment: owner_tag("core", "hub1") -> sdwan:core:hub1.

    Every managed row carries one of these. The reconciler only ever touches
    rows whose comment starts with the section's tag, which is what lets the
    controller share a device with hand-written configuration.
    """
    cleaned = [p.strip(":").replace(" ", "-") for p in parts if p]
    return OWNER_PREFIX + ":".join(cleaned)


def section(
    path: str,
    kind: str,
    *,
    owner: str,
    key: tuple[str, ...] = (),
    **kwargs: object,
) -> ConfigSection:
    """Construct a section with its apply order looked up from ``kind``."""
    if kind not in ORDER:
        raise ValueError(f"unknown section kind {kind!r}; add it to render.engine.ORDER")
    return ConfigSection(
        path=path,
        owner_tag=owner,
        key=key,
        order=ORDER[kind],
        **kwargs,  # type: ignore[arg-type]
    )
