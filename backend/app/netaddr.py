"""Address questions the controller keeps needing to ask.

Small and dependency-free on purpose: both the probe (which reads addresses off
a device) and the schemas (which validate the ones an operator types) need the
same answer, and neither can import the other.
"""

from __future__ import annotations

from ipaddress import ip_address, ip_network

# ``ip_address.is_private`` is deliberately not used: its membership changed
# across Python versions (3.12 folded the documentation ranges in), and it also
# covers space that says nothing about NAT. The question here is narrower --
# is this address unroutable on the public internet?
UNROUTABLE = tuple(
    ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",   # CGNAT
        "169.254.0.0/16",  # link-local
        "127.0.0.0/8",
        "0.0.0.0/8",
        "fc00::/7",
        "fe80::/10",
    )
)


def is_unroutable(value: str) -> bool:
    """Is this address unreachable from the public internet?"""
    try:
        addr = ip_address(value)
    except ValueError:
        return False
    return any(addr in net for net in UNROUTABLE if net.version == addr.version)


def private_uplink_note(public_ip: str | None, nat_behind: bool) -> str | None:
    """Why an uplink whose public address is not public deserves a second look.

    Not an error, and not something to correct. Private transit is real --
    MPLS, a partner link, a lab -- and the model already carries
    ``masquerade=False`` for exactly that case. But it is also what an uplink
    behind NAT looks like when someone types in whatever address the router
    showed them, and that mistake is expensive: the far end is told to dial an
    address the traffic never arrives from, so IKE matches no peer and is
    discarded without a log. Saying it once, where the value is entered, is the
    cheap half of catching it.

    Silent when the uplink is already marked as behind NAT, because then the
    operator has said the same thing the address says.
    """
    if nat_behind or not public_ip or not is_unroutable(public_ip):
        return None
    return (
        f"{public_ip} is not routable on the internet. That is correct for "
        "private transit -- MPLS, a partner link, a lab -- and wrong if this "
        "uplink is really behind NAT, in which case the far end cannot dial "
        "it and it should be marked as behind NAT instead."
    )
