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

from ipaddress import ip_network

from app.drivers.base import ConfigItem, ConfigSection
from app.render.engine import owner_tag, section
from app.transports.base import LinkView, iface_name, register

DEFAULT_PARAMS: dict[str, object] = {
    # The bridge each stretched segment lands on. The operator is expected to
    # put local ports into it; the controller only manages the tunnel.
    "bridge": "sdwan-l2",
    "vxlan_port": 8472,
}


def _segment_id(link: LinkView, modulus: int) -> int:
    """A per-link identifier both ends agree on, derived from the tunnel /31.

    A device with more than one L2 tunnel needs a distinct id on each -- EoIP
    rejects a duplicate tunnel-id outright, and two VXLANs sharing a VNI on one
    bridge merge segments that should stay apart. The old code gave every link
    the same 1000. The /31 network is identical at both ends of a link and
    unique between links, so it seeds an id that matches across the tunnel
    without being allocated."""
    net = int(ip_network(link.subnet_cidr, strict=False).network_address)
    return net % modulus + 1


class _L2Stretch:
    name: str
    menu: str
    prefix: str
    supported_ros: set[int]
    requires_reachable_responder = True
    learns_peer_address = False
    supports_dynamic_mesh = False
    # Nothing here listens on a port of its own choosing.
    listen_port_base = None
    encrypted = False
    # Runs on top of this transport rather than directly on the underlay.
    parent_transport = "ipsec_gre"

    @property
    def owned_paths(self) -> tuple[str, ...]:
        return (self.menu, "/interface/bridge", "/interface/bridge/port")

    def allocate(self) -> dict[str, str]:
        return {}

    def _bridge(self, link: LinkView, params: dict) -> ConfigSection:
        # One bridge per device, not per link. Every L2 tunnel on this site
        # lands on the same sdwan-l2 bridge, so the row is identical from each
        # link -- it must carry an identical owner tag too, or two links claim
        # one row under different owners and merge_sections fails the apply.
        # Scope it to the fabric and site rather than the link.
        tag = owner_tag("fabric", link.fabric.name, link.local.site_name, "l2-bridge")
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
            # After the tunnel, not with the other interfaces: a port names the
            # tunnel interface, which does not exist until ORDER["tunnel"]. At
            # "interface" (20) the port applied first and RouterOS rejected it
            # with "invalid value for argument interface".
            "l2_port",
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

    def defaults(self) -> dict[str, object]:
        return dict(DEFAULT_PARAMS)

    def interface_name(self, slug: str) -> str:
        # "vxl", not the class prefix: the rendered name predates it and
        # renaming an interface on a live device tears the tunnel down.
        return iface_name("vxl", slug)

    def render(self, link: LinkView) -> list[ConfigSection]:
        params = {**DEFAULT_PARAMS, **link.fabric.params}
        iface = self.interface_name(link.slug)
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
                        # 24-bit VNI, unique per link so two on one bridge do
                        # not merge into one segment.
                        "vni": _segment_id(link, 0xFFFFFE),
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

    def defaults(self) -> dict[str, object]:
        return dict(DEFAULT_PARAMS)

    def interface_name(self, slug: str) -> str:
        return iface_name("eoip", slug)

    def render(self, link: LinkView) -> list[ConfigSection]:
        params = {**DEFAULT_PARAMS, **link.fabric.params}
        iface = self.interface_name(link.slug)
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
                        # 16-bit, unique per link: EoIP rejects a device's
                        # second interface with a duplicate tunnel-id. Both ends
                        # derive the same value from the shared /31.
                        "tunnel-id": _segment_id(link, 0xFFFE),
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
