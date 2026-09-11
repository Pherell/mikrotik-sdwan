"""Answer "why is this not working" without opening an SSH session.

Two halves. Ping and traceroute *cause* something to happen on the device --
the only actions in the read-only half of this product, and safe because
RouterOS ping changes no configuration and leaves nothing behind. Tunnel
health causes nothing: it reads menus the passthrough already allows and
joins them to what the controller intended.

The joining is the point. ``/ip/ipsec/active-peers`` says a peer is
established; it does not say which tunnel that is. The controller knows,
because it named both ends.
"""

from __future__ import annotations

import logging
import re
from ipaddress import ip_address, ip_network
from typing import Any

from app.drivers.base import DeviceDriver, DriverError
from app.models.fabric import Link
from app.models.site import Site, Wan
from app.render.firewall import required_ports, transit_rules
from app.schemas.diagnostics import (
    PingProbe,
    PingRequest,
    PingResult,
    TraceHop,
    TracerouteRequest,
    TracerouteResult,
    TunnelHealth,
)
from app.transports import TransportError, get_transport

log = logging.getLogger(__name__)

# Transports whose tunnels appear in /ip/ipsec/active-peers. Everything else
# either does not encrypt (GRE, IPIP, EoIP, VXLAN) or encrypts by its own
# means (WireGuard), and reporting "no security association" for those is not
# a warning -- it is a wrong answer to a question that was never asked.
_IPSEC_TRANSPORTS = frozenset({"ipsec_gre", "ipsec_policy"})

# What RouterOS calls the interfaces that carry an overlay. Used to spot a
# tunnel whose own endpoint is routed through another tunnel.
_TUNNEL_TYPES = frozenset({"gre", "ipip", "wireguard", "eoip", "vxlan", "ipsec"})

# RouterOS reports times as concatenated units: "11ms391us", "1s200ms", "4ms".
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(us|ms|s|m|h|d)")
_UNIT_MS = {
    "us": 0.001,
    "ms": 1.0,
    "s": 1000.0,
    "m": 60_000.0,
    "h": 3_600_000.0,
    "d": 86_400_000.0,
}


def parse_duration_ms(value: Any) -> float | None:
    """A RouterOS duration in milliseconds.

    Returns None rather than 0 for anything unparseable, because a probe that
    timed out and a probe that answered in under a microsecond are opposite
    answers and the UI has to draw them differently.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    matches = _DURATION_RE.findall(text)
    if matches:
        return round(sum(float(n) * _UNIT_MS[u] for n, u in matches), 3)
    try:
        # Some builds report a bare number of milliseconds.
        return round(float(text), 3)
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def _rows(raw: Any) -> list[dict[str, Any]]:
    """Whatever the driver returned, as a list of rows."""
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [row for row in raw if isinstance(row, dict)]
    return []


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _truthy(value: Any) -> bool:
    return _text(value).lower() in {"true", "yes", "1"}


# -- actions ----------------------------------------------------------------


async def run_ping(driver: DeviceDriver, request: PingRequest) -> PingResult:
    """Ping from the device and summarise every probe.

    The summary is computed from the probe rows rather than read from the
    device's own cumulative fields. Those appear only on the last row, and the
    last row goes missing exactly when it matters most -- when the run was cut
    short.
    """
    params: dict[str, Any] = {"address": request.target, "count": request.count}
    if request.interface:
        params["interface"] = request.interface
    if request.source:
        params["src-address"] = request.source

    raw = await driver.run("/ping", params)

    probes: list[PingProbe] = []
    times: list[float] = []
    for row in _rows(raw):
        time_ms = parse_duration_ms(row.get("time"))
        if time_ms is not None:
            times.append(time_ms)
        probes.append(
            PingProbe(
                seq=_int(row.get("seq")),
                host=_text(row.get("host")) or None,
                ttl=_int(row.get("ttl")),
                size=_int(row.get("size")),
                time_ms=time_ms,
                status=_text(row.get("status")) or None,
            )
        )

    sent = len(probes)
    received = len(times)
    return PingResult(
        target=request.target,
        interface=request.interface,
        sent=sent,
        received=received,
        # A run that produced no rows at all is total loss, not a zero divide.
        loss_percent=round(100.0 * (sent - received) / sent, 1) if sent else 100.0,
        min_ms=min(times) if times else None,
        avg_ms=round(sum(times) / len(times), 3) if times else None,
        max_ms=max(times) if times else None,
        probes=probes,
    )


async def run_traceroute(
    driver: DeviceDriver, request: TracerouteRequest
) -> TracerouteResult:
    """Trace from the device, bounded by wall clock.

    RouterOS traceroute runs until it is stopped, so ``duration`` is not a
    nicety -- without it this call never returns.
    """
    params: dict[str, Any] = {
        "address": request.target,
        "duration": request.seconds,
        "count": 1,
    }
    if request.interface:
        params["interface"] = request.interface

    raw = await driver.run("/tool/traceroute", params)

    hops: list[TraceHop] = []
    for position, row in sorted(_traceroute_rounds(_rows(raw)).items()):
        hops.append(
            TraceHop(
                hop=_int(row.get("hop")) or position,
                address=_text(row.get("address")) or None,
                loss_percent=_float(row.get("loss")),
                sent=_int(row.get("sent")),
                last_ms=parse_duration_ms(row.get("last")),
                avg_ms=parse_duration_ms(row.get("avg")),
                best_ms=parse_duration_ms(row.get("best")),
                worst_ms=parse_duration_ms(row.get("worst")),
                status=_text(row.get("status")) or None,
            )
        )
    return TracerouteResult(target=request.target, hops=hops)


def _traceroute_rounds(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Collapse RouterOS's repeated probe rounds into one row per hop.

    RouterOS does not return a trace, it returns a *stream*: one row per hop,
    re-emitted every round until the duration runs out. Numbering rows in
    arrival order therefore invents hops -- an 11-hop trace probed twice reads
    as 22 hops, with hop 12 showing the first router again. ``sent`` is the
    round counter (it is cumulative per hop), so a change in it marks the start
    of the next round and the hop number is the position within that round.

    The stats RouterOS reports are cumulative, so where a hop appears in
    several rounds the last one wins -- it carries the fullest sample.
    """
    by_hop: dict[int, dict[str, Any]] = {}
    position = 0
    previous_sent: int | None = None

    for row in rows:
        sent = _int(row.get("sent"))
        if previous_sent is not None and sent != previous_sent:
            position = 0  # a new round began
        previous_sent = sent
        position += 1
        by_hop[position] = row

    return by_hop


# -- tunnel health ----------------------------------------------------------


async def _read(driver: DeviceDriver, path: str) -> list[dict[str, Any]]:
    """Read a menu, treating absence as emptiness.

    A device without the ipsec package has no ``/ip/ipsec/active-peers``, and
    that is a fact about the device rather than a failure of the diagnostic.
    """
    try:
        return _rows(await driver.read(path))
    except DriverError as exc:
        log.debug("diagnostics: %s unreadable: %s", path, exc)
        return []


async def tunnel_health(
    driver: DeviceDriver, site: Site, links: list[Link]
) -> list[TunnelHealth]:
    """Every link this site has an end of, joined to what the device reports."""
    interfaces = {
        _text(row.get("name")): row for row in await _read(driver, "/interface")
    }
    peers = await _read(driver, "/ip/ipsec/active-peers")
    sessions = await _read(driver, "/routing/bgp/session")
    netwatch = await _read(driver, "/tool/netwatch")
    # Read to explain a failure, not to report one: which interface traffic to
    # the far end actually leaves by, and which address each tunnel is sourced
    # from. See _diagnose.
    routes = await _read(driver, "/ip/route")
    addresses = await _read(driver, "/ip/address")
    configured_peers = await _read(driver, "/ip/ipsec/peer")
    # WireGuard reports a handshake on the peer, not the interface: an
    # interface with no peer traffic at all still says running=true.
    wg_interfaces = await _read(driver, "/interface/wireguard")
    wg_peers = await _read(driver, "/interface/wireguard/peers")

    out: list[TunnelHealth] = []
    for link in links:
        near_is_a = link.a_wan.site_id == site.id
        far = link.b_wan if near_is_a else link.a_wan
        far_tunnel_ip = link.b_tunnel_ip if near_is_a else link.a_tunnel_ip

        health = TunnelHealth(
            link_id=link.id,
            fabric_id=link.fabric_id,
            fabric_name=link.fabric.name,
            slug=link.slug,
            peer_site_id=far.site_id,
            peer_site_name=far.site.name if far.site else None,
            enabled=link.enabled,
            state=link.state,
            last_error=link.last_error,
            interface=_interface_for(link),
        )

        if health.interface is not None:
            row = interfaces.get(health.interface)
            # Absent is not the same as down: it means never applied.
            health.interface_running = _truthy(row.get("running")) if row else None

        if str(link.fabric.transport) in _IPSEC_TRANSPORTS:
            health.ipsec_established, health.ipsec_detail = _ipsec_for(peers, far)
        health.bgp_established, health.bgp_detail = _bgp_for(sessions, far_tunnel_ip)
        _apply_netwatch(health, netwatch, far_tunnel_ip)
        transport = str(link.fabric.transport)
        # The link's own port, not the fabric's base: every link listens on a
        # different one, and quoting the base would send the operator to open
        # a port this tunnel never uses.
        listen_port = str(
            link.listen_port or (link.fabric.transport_params or {}).get("listen_port") or ""
        )
        health.listen_port = link.listen_port
        health.diagnosis = _diagnose(
            health, far, routes, addresses, configured_peers, interfaces,
            transport, listen_port or None, wg_interfaces, wg_peers,
        )
        if health.diagnosis and "permitted all the way" in health.diagnosis:
            near_ip = _near_public_ip(link, site) or ""
            if near_ip and far.public_ip:
                health.transit_rules = transit_rules(
                    transport, near_ip, far.public_ip, listen_port or None
                )
        out.append(health)

    return out


def _near_public_ip(link: Link, site: Site) -> str | None:
    near = link.a_wan if link.a_wan.site_id == site.id else link.b_wan
    return near.public_ip


def _address_owner(addresses: list[dict[str, Any]], wanted: str) -> str | None:
    """Which interface holds this exact address."""
    for row in addresses:
        if row.get("disabled"):
            continue
        if _text(row.get("address")).split("/")[0] == wanted:
            return _text(row.get("interface")) or None
    return None


def _subnet_owner(addresses: list[dict[str, Any]], wanted: str) -> str | None:
    """Which interface's subnet contains this address."""
    try:
        target = ip_address(wanted)
    except ValueError:
        return None
    for row in addresses:
        if row.get("disabled"):
            continue
        try:
            net = ip_network(_text(row.get("address")), strict=False)
        except ValueError:
            continue
        if target in net:
            return _text(row.get("interface")) or None
    return None


def _egress(
    routes: list[dict[str, Any]], addresses: list[dict[str, Any]], target: str
) -> tuple[str | None, str | None]:
    """The interface an active route would send ``target`` out of.

    Most specific match wins, the same way the device picks one. RouterOS
    reports the resolved next hop as "10.0.0.1%ether2" when it has one, so the
    interface is usually there for the taking; when it is not, the gateway is
    either an interface name already or an address on a connected subnet.
    """
    try:
        ip = ip_address(target)
    except ValueError:
        return None, None

    best: tuple[int, str, str] | None = None
    for row in routes:
        if row.get("disabled") or not _truthy(row.get("active")):
            continue
        try:
            net = ip_network(_text(row.get("dst-address")), strict=False)
        except ValueError:
            continue
        if ip not in net:
            continue

        hop = _text(row.get("immediate-gw"))
        iface = hop.partition("%")[2] if "%" in hop else ""
        if not iface:
            gateway = _text(row.get("gateway"))
            iface = _subnet_owner(addresses, gateway) or ("" if _is_ipish(gateway) else gateway)
        if not iface:
            continue
        gateway = _text(row.get("gateway"))
        hop_ip = hop.partition("%")[0] if "%" in hop else (gateway if _is_ipish(gateway) else "")
        if best is None or net.prefixlen > best[0]:
            best = (net.prefixlen, iface, hop_ip)
    return (best[1], best[2] or None) if best else (None, None)


def _is_ipish(value: str) -> bool:
    try:
        ip_address(value)
    except ValueError:
        return False
    return True


def _diagnose(
    health: TunnelHealth,
    far: Wan,
    routes: list[dict[str, Any]],
    addresses: list[dict[str, Any]],
    configured_peers: list[dict[str, Any]],
    interfaces: dict[str, dict[str, Any]],
    transport: str = "",
    listen_port: str | None = None,
    wg_interfaces: list[dict[str, Any]] | None = None,
    wg_peers: list[dict[str, Any]] | None = None,
) -> str | None:
    """Why this tunnel is not up, in the order worth checking.

    Every one of these was diagnosed by hand against a live pair of routers
    before it was written down here. Reporting "no security association" is
    true and useless; the operator needs to know which of the handful of causes
    it is, and each of them is visible in state the device already gave us.
    """
    if health.interface is None:
        return None

    if interfaces.get(health.interface) is None:
        return (
            "This tunnel has not been written to the device yet. Building a "
            "tunnel network only works it out -- open this device and Apply to "
            "push it."
        )

    # The endpoint is reached through a tunnel. Check this before anything
    # protocol-specific: it explains a tunnel that cannot re-establish no
    # matter which transport carries it, and every lower branch would report a
    # symptom of it instead. Only when the link is actually in trouble -- a
    # session that is up is working, whatever the route looks like.
    if far.public_ip and health.bgp_established is not True:
        egress, _hop = _egress(routes, addresses, far.public_ip)
        carrier = _text(interfaces.get(egress or "", {}).get("type"))
        if egress and carrier in _TUNNEL_TYPES:
            return (
                f"The route to {far.public_ip} -- this tunnel's own far end -- "
                f"leaves via {egress}, which is itself a tunnel. Its underlay "
                "runs through an overlay that depends on it, so once it drops "
                "it cannot come back. Usually a neighbour advertising its own "
                "uplink subnet into the fabric. The controller pins a host "
                "route to each endpoint to prevent this; this uplink has no "
                "gateway recorded, so it got none."
            )

    if transport == "wireguard":
        wireguard = _diagnose_wireguard(
            health, far, routes, addresses, interfaces,
            wg_interfaces or [], wg_peers or [], listen_port,
        )
        if wireguard is not None:
            return wireguard

    # Sourced from an address that is not on the path to the far end. IKE then
    # leaves one interface carrying another's source address, and is dropped
    # upstream as spoofed -- so nothing ever arrives and nothing is logged.
    if far.public_ip and health.ipsec_established is False:
        # By name, not by address. A dual-homed site pointing at a
        # single-homed one has two links to the *same* far address, so
        # matching on it picks whichever peer came first and reports one
        # tunnel's source for both. The name carries the link's slug, which is
        # what actually distinguishes them.
        wanted = f"peer-{health.slug}"[:31]
        peer = next(
            (p for p in configured_peers if _text(p.get("name")) == wanted),
            None,
        ) or next(
            (
                p
                for p in configured_peers
                if _text(p.get("address")).split("/")[0] == far.public_ip
            ),
            None,
        )
        local = _text(peer.get("local-address")) if peer else ""
        if local:
            sourced_from = _address_owner(addresses, local)
            egress, _hop = _egress(routes, addresses, far.public_ip)
            if sourced_from and egress and sourced_from != egress:
                return (
                    f"Sourced from {local} on {sourced_from}, but traffic to "
                    f"{far.public_ip} leaves via {egress}. IKE goes out one "
                    "interface carrying another's address and is dropped as "
                    f"spoofed. Give {sourced_from} its own route to the far end, "
                    "or build this tunnel on the uplink that carries the route."
                )
        egress, hop = _egress(routes, addresses, far.public_ip)
        needs = " and ".join(required_ports(transport, listen_port))
        where = (
            f" It leaves via {egress} toward {hop}, so that is the first device "
            "in the path that has to permit them."
            if egress and hop
            else ""
        )
        return (
            f"No IKE exchange has happened with {far.public_ip}. Nothing has "
            f"arrived, so check that {needs} are permitted all the way between "
            f"these two addresses.{where} A successful ping proves nothing about "
            "any of them -- ICMP is not what is being blocked."
        )

    if health.ipsec_established and health.interface_running is False:
        return (
            "Encryption is up but the tunnel interface is not. The IKE path "
            "works, so check IP protocol 47 (GRE) specifically is permitted "
            "between the two addresses."
        )

    if health.interface_running and health.bgp_established is False:
        return (
            "The tunnel is up but BGP has not established over it. Check both "
            "ends are in the same AS and that the far end has been applied."
        )

    return None


def _diagnose_wireguard(
    health: TunnelHealth,
    far: Wan,
    routes: list[dict[str, Any]],
    addresses: list[dict[str, Any]],
    interfaces: dict[str, dict[str, Any]],
    wg_interfaces: list[dict[str, Any]],
    wg_peers: list[dict[str, Any]],
    listen_port: str | None,
) -> str | None:
    """WireGuard's own failure modes, which are not IPsec's.

    There is no security association to look at and no separate carrier
    interface, so the ipsec ladder above says nothing useful here. What
    WireGuard does expose is a handshake, on the peer rather than the
    interface -- an interface with no traffic whatsoever still reports
    running=true, so "running" is not evidence the tunnel works.
    """
    ours = next(
        (r for r in wg_interfaces if _text(r.get("name")) == health.interface), None
    )
    if ours is None:
        return None  # the generic "not applied" branch already covered this

    # A port is one listener. RouterOS accepts a second interface asking for a
    # port that is taken and simply never runs it -- no error at apply time,
    # nothing in the log. It is invisible unless something says it out loud.
    port = _text(ours.get("listen-port"))
    if not _truthy(ours.get("running")) and port:
        clashing = sorted(
            _text(r.get("name"))
            for r in wg_interfaces
            if _text(r.get("listen-port")) == port
            and _text(r.get("name")) != health.interface
        )
        if clashing:
            return (
                f"{health.interface} is not running because {', '.join(clashing)} "
                f"already holds UDP {port}. One WireGuard interface is one "
                "listener, so each tunnel needs its own port. Re-expand the "
                "tunnel network to hand this link a free one."
            )
        return (
            f"{health.interface} is configured but not running. Check the "
            "interface is enabled and that its private key was written."
        )

    peer = next(
        (p for p in wg_peers if _text(p.get("interface")) == health.interface), None
    )
    if peer is None:
        return (
            f"{health.interface} exists but has no peer. Apply this device "
            "again -- the interface landed and the peer did not."
        )

    if _handshaked(peer):
        return None  # the wire works; let the BGP branch below speak

    if not far.public_ip:
        return (
            "No handshake yet, and the far end has no address to dial. One of "
            "the two ends has to be reachable for the first packet; give the "
            "far uplink a public address, or wait for it to dial in."
        )

    egress, hop = _egress(routes, addresses, far.public_ip)
    where = (
        f" It leaves via {egress} toward {hop}, so that is the first device in "
        "the path that has to permit it."
        if egress and hop
        else ""
    )
    # WireGuard records where the last packet claimed to come from, handshake
    # or not. When that is not the address being dialled, something in the
    # path is rewriting the source -- which is worth saying, because the
    # reply is then arriving from somewhere the far end does not know it is.
    seen = _text(peer.get("current-endpoint-address"))
    roamed = (
        f" Replies are arriving from {seen} rather than {far.public_ip}, so "
        "something in the path is rewriting the address."
        if seen and seen != far.public_ip
        else ""
    )
    return (
        f"No WireGuard handshake with {far.public_ip}. Check that UDP "
        f"{listen_port or port} is permitted all the way between these two "
        f"addresses.{where}{roamed} A successful ping proves nothing about it "
        "-- ICMP is not what is being blocked."
    )


def _handshaked(peer: dict[str, Any]) -> bool:
    """Has this peer ever completed a handshake?

    ``last-handshake`` and nothing else. The tempting shortcuts are both
    wrong, and were both observed wrong on a live 7.24.2 peer that had never
    completed one: ``rx`` was 148, because bytes arriving is not the same as a
    handshake finishing, and ``current-endpoint-address`` was populated,
    because WireGuard records where the last packet came from whether or not
    it authenticated. Trusting either reports a dead tunnel as healthy and
    sends the operator off to check BGP.
    """
    return bool(_text(peer.get("last-handshake")))


def _interface_for(link: Link) -> str | None:
    """The interface this link's transport creates on either end.

    ``str()`` rather than ``.value``: the column is a plain String, so a row
    read back from the database hands you the name as a str while a row still
    in the session hands you the enum. Both stringify to the registry key.
    """
    try:
        return get_transport(str(link.fabric.transport)).interface_name(link.slug)
    except TransportError:
        # A stored fabric can name a transport this build no longer has. Worth
        # reporting as "unknown"; not worth failing the whole page over.
        log.warning("no transport %r for link %s", link.fabric.transport, link.slug)
        return None


def _ipsec_for(
    peers: list[dict[str, Any]], far: Wan
) -> tuple[bool | None, str | None]:
    """Is there a live security association to the far end?

    Matched on the far side's public address, because that is the only field
    the controller and ``/ip/ipsec/active-peers`` agree on -- the peer *name*
    is ours, but active-peers reports the negotiated identity.
    """
    remote = far.public_ip
    if not remote:
        # A dial-out-only far end has no address to match on, so finding
        # nothing here is not evidence of anything.
        return None, None

    for row in peers:
        if _text(row.get("remote-address")).split("%")[0] != remote:
            continue
        state = _text(row.get("state"))
        established = state == "established" or _truthy(row.get("established"))
        uptime = _text(row.get("uptime"))
        detail = state or ("up" if established else "down")
        return established, f"{detail} for {uptime}" if uptime else detail

    return False, "no active peer for this address"


def _bgp_for(
    sessions: list[dict[str, Any]], far_tunnel_ip: str | None
) -> tuple[bool | None, str | None]:
    """Is iBGP up over the tunnel?

    Matched on the far end's tunnel address rather than the session name:
    RouterOS truncates names at 32 characters, and a truncated name is not a
    key.
    """
    if not far_tunnel_ip:
        return None, None

    for row in sessions:
        remote = _text(row.get("remote.address") or row.get("remote-address"))
        if remote.split("%")[0] != far_tunnel_ip:
            continue
        if not _truthy(row.get("established")):
            return False, _text(row.get("state")) or "not established"
        prefixes = row.get("prefix-count")
        if prefixes is None:
            return True, "established"
        return True, f"established, {_text(prefixes)} prefixes"

    return False, "no BGP session to this tunnel address"


def _apply_netwatch(
    health: TunnelHealth, netwatch: list[dict[str, Any]], far_tunnel_ip: str | None
) -> None:
    """Fold in whatever an SLA probe last measured for the far end."""
    if not far_tunnel_ip:
        return
    for row in netwatch:
        if _text(row.get("host")) != far_tunnel_ip:
            continue
        health.netwatch_status = _text(row.get("status")) or None
        health.netwatch_loss_percent = _float(row.get("loss-percent"))
        health.netwatch_latency_ms = parse_duration_ms(row.get("rtt-avg"))
        return
