"""The firewall rules that make an overlay actually pass traffic.

Three problems, none of which a property diff can see, because the rules simply
were not there:

**Steering left traffic unNATted.** A site masquerades on the uplink it was
built with. A policy that steers traffic to a second uplink sends it out an
interface no NAT rule matches, so it leaves with a private source address and
dies at the first upstream router. The plan applied cleanly and the traffic
disappeared.

**Tunnel traffic was masqueraded.** Packets to a peer's public address match the
same masquerade rule. IPsec in transport mode needs an exception above it.

**A default-drop input chain blocked establishment.** Nothing opened the
transport's ports and protocols from the peers, so on a hardened device the
tunnel never came up and the symptom was a timeout.

All of it depends on position -- see ``ConfigSection.before``. An accept rule
appended after the operator's masquerade never matches, and the diff reads
clean while the configuration does nothing.

Every row is ownership-tagged. A hand-written firewall is read to find the
anchor and otherwise never touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.drivers.base import ConfigItem, ConfigSection
from app.render.engine import ORDER, owner_tag

# Where the controller's rows must sit. Both are predicates on a row the
# operator wrote, not on anything the controller owns.
BEFORE_MASQUERADE = {"chain": "srcnat", "action": "masquerade"}
BEFORE_INPUT_DROP = {"chain": "input", "action": "drop"}

# What each transport needs open on the input chain, as (protocol, dst-port).
# A port of None means the protocol carries no ports.
_TRANSPORT_PROTOCOLS: dict[str, tuple[tuple[str, str | None], ...]] = {
    # IKE negotiates on 500, moves to 4500 when NAT is detected; ESP carries the
    # payload; GRE is what the IPsec policy actually protects.
    "ipsec_gre": (("udp", "500,4500"), ("ipsec-esp", None), ("gre", None)),
    "ipsec_policy": (("udp", "500,4500"), ("ipsec-esp", None)),
    "gre": (("gre", None),),
    "eoip": (("gre", None),),
    "ipip": (("ipencap", None),),
    "vxlan": (("udp", "8472"),),
}

_WIREGUARD_DEFAULT_PORT = "13231"


# How each protocol reads to a person who has to open it on a firewall they
# own. IP protocol numbers are included because that is what a non-RouterOS
# device will ask for.
_PROTOCOL_LABEL = {
    "udp": "UDP",
    "ipsec-esp": "IP protocol 50 (ESP)",
    "gre": "IP protocol 47 (GRE)",
    "ipencap": "IP protocol 4 (IPIP)",
}


def transport_protocols(
    transport: str, listen_port: str | None = None
) -> tuple[tuple[str, str | None], ...]:
    """The raw (protocol, dst-port) pairs this transport needs permitted."""
    if transport == "wireguard":
        return (("udp", listen_port or _WIREGUARD_DEFAULT_PORT),)
    return _TRANSPORT_PROTOCOLS.get(transport, ())


def transit_rules(
    transport: str, near: str, far: str, listen_port: str | None = None
) -> list[str]:
    """RouterOS commands for a router *between* two tunnel endpoints.

    The controller writes the input-chain accepts on the two devices it owns.
    A router in the path is somebody else's -- often literally, an upstream
    ISP box -- so the most the controller can do is hand over the exact rules
    rather than a description of them. Both directions, because a firewall in
    the middle sees both.
    """
    out = ["/ip/firewall/filter"]
    for protocol, ports in transport_protocols(transport, listen_port):
        port = f" dst-port={ports}" if ports else ""
        for src, dst in ((near, far), (far, near)):
            out.append(
                f"add chain=forward action=accept protocol={protocol}{port} "
                f"src-address={src} dst-address={dst} "
                f'comment="sdwan tunnel {near} <-> {far}"'
            )
    return out


def required_ports(transport: str, listen_port: str | None = None) -> list[str]:
    """What has to be permitted end to end for this transport to establish.

    The controller opens these on the two devices it manages. It cannot open
    them on anything *between* them, and that gap is where a tunnel quietly
    fails: every layer reports down, ping still works because ICMP was never
    the thing being blocked, and nothing logs a packet that never arrived.
    Saying it out loud in the UI is the cheapest fix available.
    """
    if transport == "wireguard":
        return [f"UDP {listen_port or _WIREGUARD_DEFAULT_PORT}"]
    out = []
    for protocol, ports in _TRANSPORT_PROTOCOLS.get(transport, ()):
        label = _PROTOCOL_LABEL.get(protocol, protocol)
        out.append(f"{label} {ports}" if ports else label)
    return out


@dataclass(slots=True)
class UplinkNat:
    """One uplink and whether traffic steered onto it should be NATted."""

    interface: str
    masquerade: bool


@dataclass(slots=True)
class FirewallView:
    site_name: str
    # Remote endpoint addresses this device builds tunnels to, keyed by the
    # transport carrying them. Only peers with a known address: a peer behind
    # CGNAT has none, dials out, and needs no inbound rule.
    peers_by_transport: dict[str, set[str]] = field(default_factory=dict)
    # Every uplink this site has. Steering can send traffic out any of them.
    uplinks: list[UplinkNat] = field(default_factory=list)
    # WireGuard listen ports in use, if any.
    wireguard_ports: set[str] = field(default_factory=set)


def render_firewall(view: FirewallView) -> list[ConfigSection]:
    """NAT and input rules for this site. Always returns both sections.

    Empty sections matter: they are how a peer that has gone away has its rules
    removed. A section that is not rendered leaves its rows orphaned on the
    device forever.
    """
    return [_nat(view), _filter(view)]


def _nat(view: FirewallView) -> ConfigSection:
    scope = owner_tag("firewall", view.site_name, "nat")
    items: list[ConfigItem] = []

    # Order within the section is the order on the device. Accepts first: a
    # masquerade rule below them must not catch tunnel traffic on its way out.
    for address in sorted({p for peers in view.peers_by_transport.values() for p in peers}):
        items.append(
            ConfigItem(
                props={
                    "chain": "srcnat",
                    "action": "accept",
                    "dst-address": address,
                },
                tag=f"{scope}:bypass:{address}",
            )
        )

    for uplink in view.uplinks:
        if not uplink.masquerade:
            continue
        items.append(
            ConfigItem(
                props={
                    "chain": "srcnat",
                    "action": "masquerade",
                    "out-interface": uplink.interface,
                },
                tag=f"{scope}:masq:{uplink.interface}",
            )
        )

    return ConfigSection(
        path="/ip/firewall/nat",
        items=items,
        owner_tag=scope,
        # Not the property tuple: two rules can share chain and action and
        # differ only by what they match, so the comment is the identity.
        key=(),
        before=BEFORE_MASQUERADE,
        order=ORDER["firewall"],
    )


def _filter(view: FirewallView) -> ConfigSection:
    scope = owner_tag("firewall", view.site_name, "input")
    items: list[ConfigItem] = []

    for transport in sorted(view.peers_by_transport):
        needs = list(_TRANSPORT_PROTOCOLS.get(transport, ()))
        if transport == "wireguard":
            ports = (
                ",".join(sorted(view.wireguard_ports, key=int))
                or _WIREGUARD_DEFAULT_PORT
            )
            needs = [("udp", ports)]
        for address in sorted(view.peers_by_transport[transport]):
            for protocol, port in needs:
                props: dict[str, object] = {
                    "chain": "input",
                    "action": "accept",
                    "protocol": protocol,
                    "src-address": address,
                }
                if port:
                    props["dst-port"] = port
                items.append(
                    ConfigItem(
                        props=props,
                        tag=f"{scope}:{transport}:{address}:{protocol}",
                    )
                )

    return ConfigSection(
        path="/ip/firewall/filter",
        items=items,
        owner_tag=scope,
        key=(),
        before=BEFORE_INPUT_DROP,
        order=ORDER["firewall"],
    )
