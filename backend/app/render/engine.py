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
    "interface": 20,      # loopbacks, bridges
    "crypto": 30,         # ipsec profiles, proposals, peers, identities
    "tunnel": 40,         # gre / ipip / wireguard interfaces
    "address": 50,        # ip addresses on those interfaces
    "routing": 60,        # bgp templates and connections, static routes
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
