"""Address allocation for a fabric.

Every link gets a /31 out of the fabric's tunnel pool; every member site gets a
/32 loopback out of the loopback pool. Allocation is *stable*: an existing
assignment is never moved, because renumbering a live tunnel drops it.

Links also get a UDP listen port, for the transports that listen on one. That
is allocation rather than a constant because the port is per *interface* and a
multi-homed site has one interface per link -- see ``allocate_listen_port``.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from ipaddress import IPv4Address, IPv4Network, ip_network


class PoolExhausted(ValueError):
    """The fabric's pool cannot cover the links or sites it now needs."""


def _pool(cidr: str) -> IPv4Network:
    net = ip_network(cidr, strict=False)
    if not isinstance(net, IPv4Network):
        raise ValueError("IPv6 overlay addressing is not supported yet")
    return net


def iter_link_subnets(cidr: str) -> Iterator[IPv4Network]:
    """Every /31 in the pool, in order.

    A /31 is a point-to-point link per RFC 3021: two usable addresses, no
    network or broadcast waste. RouterOS handles them correctly.
    """
    yield from _pool(cidr).subnets(new_prefix=31)


def allocate_link_subnet(cidr: str, taken: Iterable[str]) -> IPv4Network:
    """The lowest free /31, given the ones already handed out."""
    used = {str(ip_network(t, strict=False)) for t in taken}
    for candidate in iter_link_subnets(cidr):
        if str(candidate) not in used:
            return candidate
    raise PoolExhausted(
        f"Tunnel pool {cidr} has no free /31 left ({len(used)} allocated). "
        "Widen fabric.ip_pool."
    )


def endpoints_of(subnet: IPv4Network) -> tuple[str, str]:
    """The two addresses of a /31, low first."""
    hosts = list(subnet)
    if len(hosts) != 2:
        raise ValueError(f"{subnet} is not a /31")
    return str(hosts[0]), str(hosts[1])


def allocate_loopback(cidr: str, taken: Iterable[str]) -> str:
    """The lowest free host address in the loopback pool."""
    used = {str(t) for t in taken}
    net = _pool(cidr)
    # A loopback pool is addressed as individual /32s, so every address in the
    # range is usable -- including what would be network and broadcast on a LAN.
    candidates: Iterable[IPv4Address] = (
        net.hosts() if net.prefixlen < 31 else iter(net)
    )
    for address in candidates:
        if str(address) not in used:
            return str(address)
    raise PoolExhausted(
        f"Loopback pool {cidr} is full ({len(used)} allocated). "
        "Widen fabric.loopback_pool."
    )


# A listener has to live somewhere above the well-known range and below the
# ephemeral one, or it collides with whatever the device opens for itself.
_MAX_PORT = 65535


def allocate_listen_port(base: int, taken: Iterable[int]) -> int:
    """The lowest free UDP port at or above ``base``.

    WireGuard binds one UDP listener per interface, and the renderer emits one
    interface per *link* -- so a site with two uplinks asks for two listeners.
    RouterOS does not refuse the second one: it accepts the row and leaves it
    ``running=false``, silently, which is far worse than an error. Every link
    therefore gets its own port.

    Both ends of a link share the port number: they are different devices, so
    there is no collision, and it means one side's ``listen-port`` is the
    other's ``endpoint-port`` without either having to look the other up.
    """
    used = set(taken)
    for port in range(base, _MAX_PORT + 1):
        if port not in used:
            return port
    raise PoolExhausted(
        f"No free UDP listen port at or above {base} ({len(used)} allocated)."
    )


def capacity(cidr: str) -> int:
    """How many links a tunnel pool can carry. Shown in the fabric designer."""
    net = _pool(cidr)
    if net.prefixlen > 31:
        return 0
    return 2 ** (31 - net.prefixlen)
