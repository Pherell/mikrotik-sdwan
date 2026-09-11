"""Overlay transport abstraction.

A transport knows how to turn one link -- a tunnel between two WAN uplinks --
into device configuration. Everything above this layer decides *which* links
should exist; the transport decides what they look like on the wire.

Transports never see SQLAlchemy models. They are handed a ``LinkView``, a plain
snapshot of both endpoints, which keeps them pure and makes them testable
without a database.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from app.drivers.base import ConfigSection

Side = Literal["a", "b"]


class TransportError(ValueError):
    """The requested overlay cannot be built for this pair of endpoints."""


@dataclass(slots=True, frozen=True)
class Endpoint:
    """One end of a link, flattened from Site + Wan."""

    site_name: str
    wan_name: str
    interface: str
    tunnel_ip: str                 # address on the tunnel, from the fabric pool
    public_ip: str | None = None
    nat_behind: bool = False
    loopback_ip: str | None = None
    ros_major: int = 7

    @property
    def dial_out_only(self) -> bool:
        """Cannot accept an inbound tunnel: no public address, or behind NAT."""
        return self.public_ip is None or self.nat_behind


@dataclass(slots=True, frozen=True)
class FabricView:
    name: str
    asn: int = 65000
    mtu: int = 1400
    params: dict[str, object] = field(default_factory=dict)

    def param(self, key: str, default: object = None) -> object:
        return self.params.get(key, default)


@dataclass(slots=True, frozen=True)
class LinkView:
    """One tunnel, seen from ``local``."""

    slug: str
    fabric: FabricView
    local: Endpoint
    remote: Endpoint
    # True when this side dials. Exactly one side of a link initiates; the
    # other listens. A NAT'd endpoint is always the initiator.
    initiator: bool
    secrets: dict[str, str] = field(default_factory=dict)

    @property
    def tag(self) -> str:
        from app.render.engine import owner_tag

        return owner_tag("fabric", self.fabric.name, self.slug)

    def iface_name(self, prefix: str) -> str:
        """RouterOS interface names are capped; keep them short and stable."""
        return iface_name(prefix, self.slug)

    @property
    def subnet_cidr(self) -> str:
        """The link's /31, derived from either endpoint.

        WireGuard needs it for allowed-address: only the overlay crosses the
        peer, never a default route.
        """
        from ipaddress import ip_interface

        return str(ip_interface(f"{self.local.tunnel_ip}/31").network)


@runtime_checkable
class TransportDriver(Protocol):
    name: str
    supported_ros: set[int]
    # Whether at least one side must be publicly reachable for the tunnel to
    # come up at all.
    requires_reachable_responder: bool
    # Whether the far side can discover a peer's address instead of being told
    # it. WireGuard learns it from the handshake, so a roaming or NAT'd peer
    # works. A GRE/IPIP/EoIP/VXLAN interface is configured with a fixed
    # remote-address and has nowhere to learn one, so *both* ends need a
    # reachable address -- see validate_pair.
    learns_peer_address: bool
    supports_dynamic_mesh: bool
    # Every RouterOS menu this transport can write to. The reconciler renders an
    # empty section for each one even when a site has no links, so rows left
    # behind by a deleted link are still seen and removed.
    owned_paths: tuple[str, ...]

    def allocate(self) -> dict[str, str]:
        """Generate per-link secret material. Stored encrypted, never logged."""
        ...

    def render(self, link: LinkView) -> list[ConfigSection]:
        """Sections for ``link.local``. Called once per side."""
        ...

    def defaults(self) -> dict[str, object]:
        """This transport's parameter defaults, for the UI to draw against.

        On the protocol rather than read from each module's DEFAULT_PARAMS by
        name, so a transport that has none simply says so.
        """
        ...

    def interface_name(self, slug: str) -> str:
        """The interface this transport creates for a link with ``slug``.

        Takes the slug rather than the link because that is all the name has
        ever depended on, and because diagnostics needs to look up a rendered
        interface on a live device without rebuilding the whole link view to
        learn a string.
        """
        ...


# -- shared helpers ---------------------------------------------------------


def iface_name(prefix: str, slug: str) -> str:
    """RouterOS caps interface names at 32 characters. Truncate the same way
    everywhere, or the reconciler creates a second interface next to the one
    it meant to update."""
    return f"{prefix}-{slug}"[:31]



def validate_pair(a: Endpoint, b: Endpoint, transport: TransportDriver) -> None:
    """Reject a link that could never establish, before anything is rendered.

    Failing here produces a clear message in the fabric designer instead of two
    routers retrying an impossible negotiation forever.
    """
    if a.dial_out_only and b.dial_out_only and transport.requires_reachable_responder:
        raise TransportError(
            f"Neither {a.site_name}/{a.wan_name} nor {b.site_name}/{b.wan_name} is "
            "publicly reachable, so neither can accept the tunnel. Link these sites "
            "through a hub instead."
        )

    # One reachable end is enough only for a transport that can *learn* the
    # other's address. Everything else writes a fixed remote-address into the
    # tunnel interface, and there is nothing to write for an endpoint with no
    # reachable address: the far side renders a tunnel with an empty remote and
    # silently never comes up.
    if transport.requires_reachable_responder and not getattr(
        transport, "learns_peer_address", False
    ):
        for near, far in ((a, b), (b, a)):
            if near.dial_out_only:
                raise TransportError(
                    f"{near.site_name}/{near.wan_name} has no reachable address, and "
                    f"the {transport.name} transport needs one at both ends: "
                    f"{far.site_name}'s tunnel interface is configured with a fixed "
                    "remote address and cannot learn it. Give this uplink a reachable "
                    "public address, or use the wireguard transport, which learns a "
                    "roaming peer's address from its handshake."
                )

    for endpoint in (a, b):
        if endpoint.ros_major not in transport.supported_ros:
            raise TransportError(
                f"{endpoint.site_name} runs RouterOS {endpoint.ros_major}, which does "
                f"not support the {transport.name} transport "
                f"(needs {sorted(transport.supported_ros)})."
            )


def choose_initiator(a: Endpoint, b: Endpoint) -> Side:
    """Which side dials.

    A NAT'd endpoint must initiate, because nothing can reach it. When both are
    reachable the choice is arbitrary but must be *stable*, or the two sides
    disagree on every re-render and flap the tunnel -- so fall back to sorting
    by name.
    """
    if a.dial_out_only and not b.dial_out_only:
        return "a"
    if b.dial_out_only and not a.dial_out_only:
        return "b"
    return "a" if (a.site_name, a.wan_name) <= (b.site_name, b.wan_name) else "b"


def generate_psk(length: int = 48) -> str:
    """A pre-shared key with enough entropy to make offline cracking pointless."""
    return secrets.token_urlsafe(length)


# -- registry ---------------------------------------------------------------

_REGISTRY: dict[str, TransportDriver] = {}


def register(driver: TransportDriver) -> TransportDriver:
    _REGISTRY[driver.name] = driver
    return driver


def get_transport(name: str) -> TransportDriver:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise TransportError(
            f"Unknown transport {name!r}. Available: {sorted(_REGISTRY)}"
        ) from None


def available() -> list[str]:
    return sorted(_REGISTRY)
