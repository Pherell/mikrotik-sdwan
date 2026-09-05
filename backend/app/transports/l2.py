"""Layer-2 stretch: VXLAN and EoIP.

These extend one broadcast domain across sites, which is occasionally necessary
(a clustered appliance, a legacy application that assumes a flat LAN) and always
a liability: a broadcast storm at one site becomes a storm at every site, and
the WAN carries traffic that routing would have kept local.

Neither encrypts on its own. Both are configured here to run over an
``ipsec_gre`` parent link -- the L2 tunnel addresses the overlay /31, so its
payload inherits the parent's IPsec SA rather than crossing the internet in the
clear.
"""

from __future__ import annotations

from app.drivers.base import ConfigItem, ConfigSection
from app.render.engine import section
from app.transports.base import LinkView, register

DEFAULT_PARAMS: dict[str, object] = {
    # The bridge each stretched segment lands on. The operator is expected to
    # put local ports into it; the controller only manages the tunnel.
    "bridge": "sdwan-l2",
    "vni": 1000,
    "vxlan_port": 8472,
}


class _L2Stretch:
    name: str
    menu: str
    prefix: str
    supported_ros: set[int]
    requires_reachable_responder = True
    supports_dynamic_mesh = False
    encrypted = False
    # Runs on top of this transport rather than directly on the underlay.
    parent_transport = "ipsec_gre"

    @property
    def owned_paths(self) -> tuple[str, ...]:
        return (self.menu, "/interface/bridge", "/interface/bridge/port")

    def allocate(self) -> dict[str, str]:
        return {}

    def _bridge(self, link: LinkView, params: dict) -> ConfigSection:
        tag = f"{link.tag}:l2-bridge"
        name = str(params["bridge"])
        return section(
            "/interface/bridge",
            "interface",
            owner=tag,
            key=("name",),
            items=[
                ConfigItem(
                    props={"name": name, "protocol-mode": "rstp"},
                    tag=tag,
                )
            ],
        )

    def _port(self, link: LinkView, params: dict, iface: str) -> ConfigSection:
        tag = f"{link.tag}:l2-port"
        return section(
            "/interface/bridge/port",
            "interface",
            owner=tag,
            key=("bridge", "interface"),
            items=[
                ConfigItem(
                    props={
                        "bridge": params["bridge"],
                        "interface": iface,
                        # A stretched segment is exactly where a loop turns into
                        # an outage at every site at once. Leave STP on.
                        "horizon": "none",
                    },
                    tag=tag,
                )
            ],
        )


class VxlanTransport(_L2Stretch):
    """VXLAN. RouterOS 7 only; there is no VXLAN in 6."""

    name = "vxlan"
    menu = "/interface/vxlan"
    prefix = "vxlan"
    supported_ros = {7}

    def render(self, link: LinkView) -> list[ConfigSection]:
        params = {**DEFAULT_PARAMS, **link.fabric.params}
        iface = link.iface_name("vxl")
        tag = f"{link.tag}:vxlan"

        vxlan = section(
            self.menu,
            "tunnel",
            owner=tag,
            key=("name",),
            items=[
                ConfigItem(
                    props={
                        "name": iface,
                        "vni": params["vni"],
                        "port": params["vxlan_port"],
                        # Bind to the overlay address so the payload rides the
                        # parent IPsec SA instead of the bare internet.
                        "local-address": link.local.tunnel_ip,
                        "mtu": int(link.fabric.mtu) - 50,  # VXLAN header overhead
                    },
                    tag=tag,
                )
            ],
        )
        peer_tag = f"{link.tag}:vxlan-peer"
        peers = section(
            "/interface/vxlan/vteps",
            "tunnel",
            owner=peer_tag,
            key=("interface", "remote-ip"),
            items=[
                ConfigItem(
                    props={"interface": iface, "remote-ip": link.remote.tunnel_ip},
                    tag=peer_tag,
                )
            ],
        )
        return [vxlan, peers, self._bridge(link, params), self._port(link, params, iface)]

    @property
    def owned_paths(self) -> tuple[str, ...]:
        return (
            self.menu,
            "/interface/vxlan/vteps",
            "/interface/bridge",
            "/interface/bridge/port",
        )


class EoipTransport(_L2Stretch):
    """EoIP. MikroTik-proprietary, but available all the way back to RouterOS 6."""

    name = "eoip"
    menu = "/interface/eoip"
    prefix = "eoip"
    supported_ros = {6, 7}

    def render(self, link: LinkView) -> list[ConfigSection]:
        params = {**DEFAULT_PARAMS, **link.fabric.params}
        iface = link.iface_name("eoip")
        tag = f"{link.tag}:eoip"

        eoip = section(
            self.menu,
            "tunnel",
            owner=tag,
            key=("name",),
            items=[
                ConfigItem(
                    props={
                        "name": iface,
                        "local-address": link.local.tunnel_ip,
                        "remote-address": link.remote.tunnel_ip,
                        # Both ends must agree, and it must be unique per pair.
                        "tunnel-id": params["vni"],
                        "mtu": int(link.fabric.mtu) - 42,
                        "keepalive": "10s,3",
                    },
                    tag=tag,
                )
            ],
        )
        return [eoip, self._bridge(link, params), self._port(link, params, iface)]


register(VxlanTransport())  # type: ignore[arg-type]
register(EoipTransport())  # type: ignore[arg-type]
