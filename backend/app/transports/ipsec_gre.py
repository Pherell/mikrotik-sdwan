"""Route-based IPsec on RouterOS: GRE carried inside a transport-mode SA.

RouterOS has **no VTI**. "Route-based IPsec" here means a GRE tunnel whose
endpoints are the two public addresses, plus an IPsec policy that encrypts
``protocol=gre`` between exactly those addresses. Routing then decides what
enters the GRE interface, and BGP can run over it.

Two modes:

``full`` (default)
    Explicit ``/ip/ipsec`` profile, proposal, peer, identity and policy. Verbose,
    but the only way to control IKEv2, PFS, and certificate auth.

``simple``
    A single ``/interface/gre`` with ``ipsec-secret=``, which makes RouterOS
    generate the peer and policy itself. Fewer moving parts, no control over the
    crypto. Offered, not defaulted.

A NAT'd endpoint is always the initiator and never ``passive``; the reachable
side listens. See ``choose_initiator`` in ``transports.base``.
"""

from __future__ import annotations

from app.drivers.base import ConfigItem, ConfigSection
from app.render.engine import section
from app.transports.base import LinkView, generate_psk, register

# Defaults chosen for modern hardware. Overridable per fabric through
# transport_params so an old RB can fall back to CBC.
DEFAULT_PARAMS: dict[str, object] = {
    "mode": "full",
    "enc_algorithm": "aes-256-gcm",
    "auth_algorithm": "sha256",
    "dh_group": "ecp256",
    "pfs_group": "ecp256",
    "lifetime": "8h",
    "exchange_mode": "ike2",
    "dpd_interval": "8s",
    "dpd_maximum_failures": 4,
}


class IpsecGreTransport:
    name = "ipsec_gre"
    supported_ros = {6, 7}
    requires_reachable_responder = True
    supports_dynamic_mesh = True
    owned_paths = (
        "/ip/ipsec/profile",
        "/ip/ipsec/proposal",
        "/ip/ipsec/peer",
        "/ip/ipsec/identity",
        "/ip/ipsec/policy",
        "/interface/gre",
        "/ip/address",
    )

    def allocate(self) -> dict[str, str]:
        return {"psk": generate_psk()}

    def render(self, link: LinkView) -> list[ConfigSection]:
        params = {**DEFAULT_PARAMS, **link.fabric.params}
        if params["mode"] == "simple":
            return self._render_simple(link, params)
        return self._render_full(link, params)

    # -- simple mode -------------------------------------------------------

    def _render_simple(self, link: LinkView, params: dict) -> list[ConfigSection]:
        """One GRE interface; RouterOS derives the IPsec peer and policy."""
        return [
            self._gre(link, params, ipsec_secret=link.secrets.get("psk")),
            self._tunnel_address(link),
        ]

    # -- full mode ---------------------------------------------------------

    def _render_full(self, link: LinkView, params: dict) -> list[ConfigSection]:
        return [
            self._profile(link, params),
            self._proposal(link, params),
            self._peer(link, params),
            self._identity(link),
            self._policy(link, params),
            self._gre(link, params),
            self._tunnel_address(link),
        ]

    def _profile(self, link: LinkView, params: dict) -> ConfigSection:
        tag = f"{link.tag}:profile"
        return section(
            "/ip/ipsec/profile",
            "crypto",
            owner=tag,
            key=("name",),
            items=[
                ConfigItem(
                    props={
                        "name": self._name(link, "prof"),
                        "hash-algorithm": params["auth_algorithm"],
                        "enc-algorithm": _phase1_enc(str(params["enc_algorithm"])),
                        "dh-group": params["dh_group"],
                        "lifetime": params["lifetime"],
                        "dpd-interval": params["dpd_interval"],
                        "dpd-maximum-failures": params["dpd_maximum_failures"],
                        "nat-traversal": True,
                    },
                    tag=tag,
                )
            ],
        )

    def _proposal(self, link: LinkView, params: dict) -> ConfigSection:
        tag = f"{link.tag}:proposal"
        props: dict[str, object] = {
            "name": self._name(link, "prop"),
            "enc-algorithms": params["enc_algorithm"],
            "pfs-group": params["pfs_group"],
            "lifetime": params["lifetime"],
        }
        # AEAD ciphers carry their own integrity; RouterOS rejects a separate
        # auth algorithm alongside GCM.
        if not _is_aead(str(params["enc_algorithm"])):
            props["auth-algorithms"] = params["auth_algorithm"]
        return section(
            "/ip/ipsec/proposal",
            "crypto",
            owner=tag,
            key=("name",),
            items=[ConfigItem(props=props, tag=tag)],
        )

    def _peer(self, link: LinkView, params: dict) -> ConfigSection:
        tag = f"{link.tag}:peer"
        props: dict[str, object] = {
            "name": self._name(link, "peer"),
            "profile": self._name(link, "prof"),
            "exchange-mode": params["exchange_mode"],
            # Only the listening side is passive. A NAT'd endpoint must dial.
            "passive": not link.initiator,
        }
        if link.initiator:
            # Dialling: we need somewhere to dial. The remote must be reachable,
            # which validate_pair has already guaranteed.
            props["address"] = link.remote.public_ip
        else:
            # Listening: accept from the remote's address when we know it, and
            # from anywhere when the remote is behind a dynamic NAT.
            props["address"] = (
                f"{link.remote.public_ip}/32" if link.remote.public_ip else "0.0.0.0/0"
            )
        if link.local.public_ip:
            props["local-address"] = link.local.public_ip
        return section(
            "/ip/ipsec/peer",
            "crypto",
            owner=tag,
            key=("name",),
            items=[ConfigItem(props=props, tag=tag)],
        )

    def _identity(self, link: LinkView) -> ConfigSection:
        tag = f"{link.tag}:identity"
        return section(
            "/ip/ipsec/identity",
            "crypto",
            owner=tag,
            key=("peer",),
            # RouterOS never returns the secret, so comparing it would diff
            # dirty on every run.
            write_once=("secret",),
            items=[
                ConfigItem(
                    props={
                        "peer": self._name(link, "peer"),
                        "auth-method": "pre-shared-key",
                        "secret": link.secrets.get("psk", ""),
                        "generate-policy": "no",
                    },
                    tag=tag,
                )
            ],
        )

    def _policy(self, link: LinkView, params: dict) -> ConfigSection:
        """Encrypt GRE between the two public addresses, and nothing else.

        ``level=unique`` keeps each link's SA distinct; without it, several
        links sharing a proposal collapse onto one SA and traffic lands on the
        wrong tunnel.
        """
        tag = f"{link.tag}:policy"
        items: list[ConfigItem] = []
        if link.local.public_ip and link.remote.public_ip:
            items.append(
                ConfigItem(
                    props={
                        "src-address": f"{link.local.public_ip}/32",
                        "dst-address": f"{link.remote.public_ip}/32",
                        "protocol": "gre",
                        "tunnel": False,          # transport mode
                        "action": "encrypt",
                        "level": "unique",
                        "peer": self._name(link, "peer"),
                        "proposal": self._name(link, "prop"),
                    },
                    tag=tag,
                )
            )
        return section(
            "/ip/ipsec/policy",
            "crypto",
            owner=tag,
            key=("src-address", "dst-address", "protocol"),
            items=items,
        )

    # -- shared ------------------------------------------------------------

    def _gre(
        self, link: LinkView, params: dict, ipsec_secret: str | None = None
    ) -> ConfigSection:
        tag = f"{link.tag}:gre"
        props: dict[str, object] = {
            "name": link.iface_name("gre"),
            "remote-address": link.remote.public_ip,
            "mtu": link.fabric.mtu,
            # Without keepalives a GRE interface stays "running" after the far
            # end vanishes, and routes keep pointing into a black hole.
            "keepalive": "10s,3",
            "allow-fast-path": False,
        }
        if link.local.public_ip:
            props["local-address"] = link.local.public_ip
        write_once: tuple[str, ...] = ()
        if ipsec_secret:
            props["ipsec-secret"] = ipsec_secret
            write_once = ("ipsec-secret",)
        return section(
            "/interface/gre",
            "tunnel",
            owner=tag,
            key=("name",),
            write_once=write_once,
            items=[ConfigItem(props=props, tag=tag)],
        )

    def _tunnel_address(self, link: LinkView) -> ConfigSection:
        tag = f"{link.tag}:address"
        return section(
            "/ip/address",
            "address",
            owner=tag,
            key=("address",),
            items=[
                ConfigItem(
                    props={
                        "address": f"{link.local.tunnel_ip}/31",
                        "interface": link.iface_name("gre"),
                    },
                    tag=tag,
                )
            ],
        )

    @staticmethod
    def _name(link: LinkView, prefix: str) -> str:
        return f"{prefix}-{link.slug}"[:31]


def _is_aead(algorithm: str) -> bool:
    return "gcm" in algorithm or "ccm" in algorithm


def _phase1_enc(algorithm: str) -> str:
    """IKE (phase 1) does not take AEAD names in RouterOS.

    A fabric configured for aes-256-gcm on phase 2 still negotiates phase 1 with
    plain aes-256-cbc, which is what RouterOS expects.
    """
    if _is_aead(algorithm):
        return "aes-256"
    return algorithm.replace("-cbc", "").replace("-ctr", "")


register(IpsecGreTransport())  # type: ignore[arg-type]
