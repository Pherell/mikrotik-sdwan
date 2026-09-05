"""Unencrypted point-to-point transports: GRE and IPIP.

For underlays that are already private -- an MPLS L3VPN, a dark fibre pair, a
carrier Ethernet circuit -- where paying for encryption twice buys nothing.

Neither offers confidentiality. The fabric designer says so plainly rather than
letting an operator pick one by accident.
"""

from __future__ import annotations

from app.drivers.base import ConfigItem, ConfigSection
from app.render.engine import section
from app.transports.base import LinkView, register


class _PointToPoint:
    """Shared body for the tunnel types that differ only in their menu."""

    name: str
    menu: str
    prefix: str
    supported_ros = {6, 7}
    requires_reachable_responder = True
    supports_dynamic_mesh = False
    encrypted = False

    @property
    def owned_paths(self) -> tuple[str, ...]:
        return (self.menu, "/ip/address")

    def allocate(self) -> dict[str, str]:
        return {}  # nothing to key

    def render(self, link: LinkView) -> list[ConfigSection]:
        tag = f"{link.tag}:{self.prefix}"
        iface = link.iface_name(self.prefix)
        props: dict[str, object] = {
            "name": iface,
            "remote-address": link.remote.public_ip,
            "mtu": link.fabric.mtu,
            # Without keepalives the interface stays "running" after the far end
            # vanishes, and routes keep pointing into a black hole.
            "keepalive": "10s,3",
        }
        if link.local.public_ip:
            props["local-address"] = link.local.public_ip
        if self.menu == "/interface/gre":
            props["allow-fast-path"] = False

        address_tag = f"{link.tag}:address"
        return [
            section(
                self.menu,
                "tunnel",
                owner=tag,
                key=("name",),
                items=[ConfigItem(props=props, tag=tag)],
            ),
            section(
                "/ip/address",
                "address",
                owner=address_tag,
                key=("address",),
                items=[
                    ConfigItem(
                        props={
                            "address": f"{link.local.tunnel_ip}/31",
                            "interface": iface,
                        },
                        tag=address_tag,
                    )
                ],
            ),
        ]


class GreTransport(_PointToPoint):
    name = "gre"
    menu = "/interface/gre"
    prefix = "gre"


class IpipTransport(_PointToPoint):
    """Lower overhead than GRE (no 4-byte header) but IPv4 payloads only."""

    name = "ipip"
    menu = "/interface/ipip"
    prefix = "ipip"


register(GreTransport())  # type: ignore[arg-type]
register(IpipTransport())  # type: ignore[arg-type]
