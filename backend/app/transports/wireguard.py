"""WireGuard transport. RouterOS 7 only.

Simpler and cheaper than IPsec: one interface per site rather than per link, one
keypair per site, and peers hung off it. That shape is why this driver differs
from the others -- ``render`` still runs per link, but the interface it emits is
shared, so every link on a site must agree on it.

Keys are generated controller-side. Only the public half is ever rendered to the
far end, and the private half is written once and never diffed: RouterOS returns
it, but comparing it on every run is a pointless secret round-trip.
"""

from __future__ import annotations

import base64
import hashlib
import secrets as _secrets

from app.drivers.base import ConfigItem, ConfigSection
from app.render.engine import section
from app.transports.base import LinkView, register

DEFAULT_PARAMS: dict[str, object] = {
    # A NAT'd peer must keep its mapping alive or the far side can never reach
    # it. 25s is the WireGuard convention: under the shortest common NAT
    # timeout, cheap enough to run forever.
    "persistent_keepalive": "25s",
    "listen_port": 13231,
}


def generate_keypair() -> tuple[str, str]:
    """A Curve25519 keypair in the base64 form RouterOS expects.

    Implemented here rather than pulled from a crypto library because the clamp
    and the scalar multiplication are the whole of it, and it keeps key
    generation on the controller with no extra dependency.
    """
    private = bytearray(_secrets.token_bytes(32))
    # Curve25519 clamping, per RFC 7748.
    private[0] &= 248
    private[31] &= 127
    private[31] |= 64
    public = _x25519_base(bytes(private))
    return base64.b64encode(bytes(private)).decode(), base64.b64encode(public).decode()


def _x25519_base(private: bytes) -> bytes:
    """Scalar multiplication of the Curve25519 base point (u = 9)."""
    p = 2**255 - 19
    a24 = 121665
    k = int.from_bytes(private, "little")
    u = 9

    x1, x2, z2, x3, z3, swap = u, 1, 0, u, 1, 0
    for t in range(254, -1, -1):
        kt = (k >> t) & 1
        swap ^= kt
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = kt

        a = (x2 + z2) % p
        aa = a * a % p
        b = (x2 - z2) % p
        bb = b * b % p
        e = (aa - bb) % p
        c = (x3 + z3) % p
        d = (x3 - z3) % p
        da = d * a % p
        cb = c * b % p
        x3 = pow(da + cb, 2, p)
        z3 = x1 * pow(da - cb, 2, p) % p
        x2 = aa * bb % p
        z2 = e * (aa + a24 * e) % p

    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    result = x2 * pow(z2, p - 2, p) % p
    return result.to_bytes(32, "little")


class WireGuardTransport:
    name = "wireguard"
    # RouterOS 6 has no WireGuard at all. validate_pair rejects a v6 site with a
    # clear message rather than letting the apply fail halfway.
    supported_ros = {7}
    requires_reachable_responder = True
    supports_dynamic_mesh = True
    owned_paths = ("/interface/wireguard", "/interface/wireguard/peers", "/ip/address")

    def allocate(self) -> dict[str, str]:
        """Per-link key material.

        A preshared key is generated alongside the peer keys: it is optional in
        WireGuard, costs nothing, and adds a symmetric layer that survives a
        future break in Curve25519.
        """
        a_private, a_public = generate_keypair()
        b_private, b_public = generate_keypair()
        return {
            "a_private": a_private,
            "a_public": a_public,
            "b_private": b_private,
            "b_public": b_public,
            "preshared": base64.b64encode(_secrets.token_bytes(32)).decode(),
        }

    def render(self, link: LinkView) -> list[ConfigSection]:
        params = {**DEFAULT_PARAMS, **link.fabric.params}
        # Side "a" of the stored keypair is the link's a_wan. This side is "a"
        # exactly when it initiates and the link's initiator is "a" -- which is
        # what LinkView.initiator already encodes relative to `local`.
        local_private, remote_public = self._key_pair_for(link)

        iface = self._interface_name(link)
        iface_tag = f"{link.tag}:wg"
        peer_tag = f"{link.tag}:wg-peer"
        address_tag = f"{link.tag}:address"

        interface = section(
            "/interface/wireguard",
            "tunnel",
            owner=iface_tag,
            key=("name",),
            # RouterOS returns the private key, but comparing it every run is a
            # secret round-trip that buys nothing.
            write_once=("private-key",),
            items=[
                ConfigItem(
                    props={
                        "name": iface,
                        "private-key": local_private,
                        "listen-port": params["listen_port"],
                        "mtu": link.fabric.mtu,
                    },
                    tag=iface_tag,
                )
            ],
        )

        peer_props: dict[str, object] = {
            "interface": iface,
            "public-key": remote_public,
            # Only the overlay /31 crosses this peer. A 0.0.0.0/0 allowed-ips
            # would make WireGuard's cryptokey routing swallow everything.
            "allowed-address": link.subnet_cidr,
            "preshared-key": link.secrets.get("preshared", ""),
        }
        if link.remote.public_ip:
            peer_props["endpoint-address"] = link.remote.public_ip
            peer_props["endpoint-port"] = params["listen_port"]
        if link.local.nat_behind or link.remote.nat_behind:
            peer_props["persistent-keepalive"] = params["persistent_keepalive"]

        peers = section(
            "/interface/wireguard/peers",
            "tunnel",
            owner=peer_tag,
            key=("interface", "public-key"),
            write_once=("preshared-key",),
            items=[ConfigItem(props=peer_props, tag=peer_tag)],
        )

        address = section(
            "/ip/address",
            "address",
            owner=address_tag,
            key=("address",),
            items=[
                ConfigItem(
                    props={"address": f"{link.local.tunnel_ip}/31", "interface": iface},
                    tag=address_tag,
                )
            ],
        )

        return [interface, peers, address]

    @staticmethod
    def _interface_name(link: LinkView) -> str:
        return link.iface_name("wg")

    @staticmethod
    def _key_pair_for(link: LinkView) -> tuple[str, str]:
        """(this side's private key, the far side's public key).

        Which stored half belongs to this side is decided by the endpoint names,
        not by who initiates -- initiator can change when a WAN gains a public
        address, and the keys must not move with it.
        """
        local_is_a = (link.local.site_name, link.local.wan_name) <= (
            link.remote.site_name,
            link.remote.wan_name,
        )
        if local_is_a:
            return link.secrets.get("a_private", ""), link.secrets.get("b_public", "")
        return link.secrets.get("b_private", ""), link.secrets.get("a_public", "")


def fingerprint(public_key: str) -> str:
    """Short, stable identifier for a public key, for the UI and logs."""
    return hashlib.sha256(public_key.encode()).hexdigest()[:12]


register(WireGuardTransport())  # type: ignore[arg-type]
