"""Topology to links.

The operator never writes a tunnel. They declare a fabric's topology and its
members; this walks every enabled WAN on every member and produces the link set
the topology calls for.

Expansion is *convergent*, not generative: it is handed the links that already
exist and returns what should change. Existing links keep their addresses and
their keys, so re-expanding a fabric does not renumber a live overlay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from app.fabric.allocate import allocate_link_subnet, endpoints_of
from app.models.enums import SiteRole, Topology
from app.models.fabric import Fabric, FabricMember, Link
from app.models.site import Wan
from app.transports.base import Endpoint, TransportError, choose_initiator, validate_pair


@dataclass(slots=True)
class WanRef:
    """A member's uplink, with everything expansion and rendering need."""

    site_id: str
    site_name: str
    role: SiteRole
    wan: Wan
    ros_major: int = 7
    loopback_ip: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.site_name, self.wan.name)

    def endpoint(self, tunnel_ip: str) -> Endpoint:
        return Endpoint(
            site_name=self.site_name,
            wan_name=self.wan.name,
            interface=self.wan.interface,
            tunnel_ip=tunnel_ip,
            public_ip=self.wan.public_ip,
            nat_behind=self.wan.nat_behind,
            loopback_ip=self.loopback_ip,
            ros_major=self.ros_major,
        )


@dataclass(slots=True)
class ExpansionResult:
    created: list[Link] = field(default_factory=list)
    kept: list[Link] = field(default_factory=list)
    removed: list[Link] = field(default_factory=list)
    # Pairs that cannot be linked, with the reason. Surfaced in the designer
    # rather than raised, so one impossible pair does not block the fabric.
    skipped: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        return {
            "created": len(self.created),
            "kept": len(self.kept),
            "removed": len(self.removed),
            "skipped": len(self.skipped),
        }


def members_to_wans(members: list[FabricMember]) -> list[WanRef]:
    """Flatten enabled members into their enabled uplinks."""
    refs: list[WanRef] = []
    for member in members:
        if not member.enabled:
            continue
        site = member.site
        caps = site.capabilities or {}
        for wan in site.wans:
            if not wan.enabled:
                continue
            refs.append(
                WanRef(
                    site_id=site.id,
                    site_name=site.name,
                    role=member.role_override or site.role,
                    wan=wan,
                    ros_major=int(caps.get("ros_major") or 7),
                    loopback_ip=member.loopback_ip or site.loopback_ip,
                )
            )
    return refs


def wanted_pairs(
    wans: list[WanRef], topology: Topology
) -> list[tuple[WanRef, WanRef]]:
    """Which WAN pairs should have a tunnel under this topology.

    Sorted so expansion is deterministic: the same fabric always produces the
    same link order, and therefore the same address allocation.
    """
    ordered = sorted(wans, key=lambda r: r.key)
    pairs: list[tuple[WanRef, WanRef]] = []

    for a, b in combinations(ordered, 2):
        if a.site_id == b.site_id:
            continue  # never tunnel a site to itself
        match topology:
            case Topology.full_mesh:
                pairs.append((a, b))
            case Topology.hub_spoke | Topology.hub_spoke_dynamic:
                # Hub-to-hub and hub-to-spoke are permanent. Spoke-to-spoke is
                # built on demand under hub_spoke_dynamic, not up front.
                #
                # Compare by value, not identity: a role loaded from the
                # database is a plain str, and `is SiteRole.hub` is False for
                # it even though `== SiteRole.hub` is True.
                if SiteRole.hub in (a.role, b.role):
                    pairs.append((a, b))
    return pairs


def expand(
    fabric: Fabric,
    members: list[FabricMember],
    existing: list[Link],
    transport,
) -> ExpansionResult:
    """Reconcile the link set against the topology.

    ``existing`` links are matched by the pair of WAN ids they join, in either
    order, so a link survives even if expansion visits its endpoints the other
    way round.
    """
    result = ExpansionResult()
    wans = members_to_wans(members)
    by_wan_id = {ref.wan.id: ref for ref in wans}

    index: dict[frozenset[str], Link] = {
        frozenset({link.a_wan_id, link.b_wan_id}): link for link in existing
    }
    taken_subnets = [link.subnet for link in existing]
    wanted: set[frozenset[str]] = set()

    for a, b in wanted_pairs(wans, fabric.topology):
        pair = frozenset({a.wan.id, b.wan.id})
        wanted.add(pair)

        if (link := index.get(pair)) is not None:
            result.kept.append(link)
            continue

        try:
            # Validate against provisional endpoints; addressing is irrelevant
            # to whether the pair can ever establish.
            validate_pair(a.endpoint("0.0.0.0"), b.endpoint("0.0.0.0"), transport)
        except TransportError as exc:
            wanted.discard(pair)
            result.skipped.append((f"{a.site_name}/{a.wan.name}",
                                   f"{b.site_name}/{b.wan.name}", str(exc)))
            continue

        subnet = allocate_link_subnet(fabric.ip_pool, taken_subnets)
        taken_subnets.append(str(subnet))
        a_ip, b_ip = endpoints_of(subnet)
        initiator = choose_initiator(a.endpoint(a_ip), b.endpoint(b_ip))

        result.created.append(
            Link(
                fabric_id=fabric.id,
                a_wan_id=a.wan.id,
                b_wan_id=b.wan.id,
                slug=link_slug(a, b),
                subnet=str(subnet),
                a_tunnel_ip=a_ip,
                b_tunnel_ip=b_ip,
                initiator=initiator,
                dynamic=False,
                enabled=True,
                state="pending",
            )
        )

    for pair, link in index.items():
        # A link whose endpoints left the fabric is also unwanted, even though
        # the topology never considered it.
        if pair not in wanted or not all(wid in by_wan_id for wid in pair):
            result.removed.append(link)

    return result


def link_slug(a: WanRef, b: WanRef) -> str:
    """A short, stable identifier used in interface names and ownership tags.

    RouterOS caps interface names at 31 characters and the slug is embedded in
    several of them, so each component is truncated hard.
    """
    def part(ref: WanRef) -> str:
        site = _clean(ref.site_name)[:8]
        wan = _clean(ref.wan.name)[:4]
        return f"{site}-{wan}"

    first, second = sorted((a, b), key=lambda r: r.key)
    return f"{part(first)}-{part(second)}"[:26]


def _clean(value: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in value.lower()).strip("-")
