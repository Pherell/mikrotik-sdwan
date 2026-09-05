"""Dynamic spoke-to-spoke tunnels.

Under ``hub_spoke_dynamic`` a spoke pair starts out hub-transited. When enough
traffic flows between them, a direct tunnel is built and BGP local-preference
pulls traffic onto it; when it goes idle, the tunnel is torn down again.

Two guards matter more than the heuristic:

* A pair where neither side can accept an inbound tunnel is never a candidate.
  They must stay hub-transited, and ``validate_pair`` says so.
* A tunnel is never torn down before ``min_lifetime``. Without that, traffic
  that sits just under the threshold builds and destroys a tunnel repeatedly,
  and each rebuild costs an IKE negotiation and a BGP convergence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.fabric.allocate import allocate_link_subnet, endpoints_of
from app.fabric.expand import WanRef, link_slug
from app.models.enums import SiteRole
from app.models.fabric import Fabric, Link
from app.transports.base import TransportError, choose_initiator, validate_pair

# Defaults are conservative: a tunnel is cheap to keep and expensive to flap.
DEFAULT_THRESHOLD_BYTES = 50 * 1024 * 1024   # 50 MB over the sample window
DEFAULT_IDLE_SECONDS = 30 * 60
DEFAULT_MIN_LIFETIME_SECONDS = 15 * 60


@dataclass(slots=True)
class PairTraffic:
    """Observed hub-transited traffic between two spoke WANs."""

    a_wan_id: str
    b_wan_id: str
    bytes_seen: int
    last_active: datetime


@dataclass(slots=True)
class MeshDecision:
    build: list[tuple[WanRef, WanRef]]
    tear_down: list[Link]
    skipped: list[tuple[str, str, str]]

    @property
    def summary(self) -> dict[str, int]:
        return {
            "build": len(self.build),
            "tear_down": len(self.tear_down),
            "skipped": len(self.skipped),
        }


def decide(
    fabric: Fabric,
    wans: list[WanRef],
    existing_dynamic: list[Link],
    traffic: list[PairTraffic],
    transport,
    *,
    now: datetime | None = None,
) -> MeshDecision:
    """What should be built and what should be torn down, right now."""
    now = now or datetime.now(UTC)
    params = fabric.transport_params or {}
    threshold = int(params.get("mesh_threshold_bytes", DEFAULT_THRESHOLD_BYTES))
    idle_after = timedelta(seconds=int(params.get("mesh_idle_seconds", DEFAULT_IDLE_SECONDS)))
    min_lifetime = timedelta(
        seconds=int(params.get("mesh_min_lifetime_seconds", DEFAULT_MIN_LIFETIME_SECONDS))
    )

    decision = MeshDecision(build=[], tear_down=[], skipped=[])
    by_id = {ref.wan.id: ref for ref in wans}
    have = {frozenset({link.a_wan_id, link.b_wan_id}) for link in existing_dynamic}

    for observation in traffic:
        pair = frozenset({observation.a_wan_id, observation.b_wan_id})
        if pair in have or observation.bytes_seen < threshold:
            continue

        a = by_id.get(observation.a_wan_id)
        b = by_id.get(observation.b_wan_id)
        if a is None or b is None:
            continue
        # Hub links are permanent; this only ever adds spoke-to-spoke.
        if SiteRole.hub in (a.role, b.role):
            continue

        try:
            validate_pair(a.endpoint("0.0.0.0"), b.endpoint("0.0.0.0"), transport)
        except TransportError as exc:
            decision.skipped.append(
                (f"{a.site_name}/{a.wan.name}", f"{b.site_name}/{b.wan.name}", str(exc))
            )
            continue
        decision.build.append((a, b))

    activity = {
        frozenset({t.a_wan_id, t.b_wan_id}): t.last_active for t in traffic
    }
    for link in existing_dynamic:
        pair = frozenset({link.a_wan_id, link.b_wan_id})
        age = now - _aware(link.created_at)
        if age < min_lifetime:
            # Too young to judge. Tearing down here is how a pair that sits near
            # the threshold ends up flapping.
            continue
        last = activity.get(pair)
        if last is None or (now - _aware(last)) > idle_after:
            decision.tear_down.append(link)

    return decision


def build_links(
    fabric: Fabric, pairs: list[tuple[WanRef, WanRef]], taken_subnets: list[str], transport
) -> list[Link]:
    """Address and key the tunnels ``decide`` asked for."""
    created: list[Link] = []
    allocated = list(taken_subnets)

    for a, b in pairs:
        subnet = allocate_link_subnet(fabric.ip_pool, allocated)
        allocated.append(str(subnet))
        a_ip, b_ip = endpoints_of(subnet)
        created.append(
            Link(
                fabric_id=fabric.id,
                a_wan_id=a.wan.id,
                b_wan_id=b.wan.id,
                slug=link_slug(a, b),
                subnet=str(subnet),
                a_tunnel_ip=a_ip,
                b_tunnel_ip=b_ip,
                initiator=choose_initiator(a.endpoint(a_ip), b.endpoint(b_ip)),
                dynamic=True,
                enabled=True,
                state="pending",
            )
        )
    return created


def _aware(value: datetime | None) -> datetime:
    """Treat a naive timestamp as UTC.

    SQLite hands back naive datetimes even for a timezone-aware column, and
    comparing one to an aware ``now`` raises rather than returning a wrong
    answer -- which would take down the mesh worker on the first sweep.
    """
    if value is None:
        return datetime.now(UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)
