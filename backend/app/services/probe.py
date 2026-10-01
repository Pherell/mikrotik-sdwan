"""Touch a device, learn what it is, and guess its uplinks.

This backs the onboarding wizard. Everything here is read-only -- probing a
device must never change it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from ipaddress import ip_address, ip_interface

from sqlalchemy import inspect

from app.drivers.base import DeviceDriver, DriverError
from app.drivers.factory import open_driver
from app.drivers.identity import IdentityMismatch
from app.models.enums import SiteStatus
from app.models.site import Site, Wan
from app.netaddr import is_unroutable
from app.schemas.site import InterfaceNote, ProbeResult, UplinkConflict, WanCreate
from app.security import SecretBox

log = logging.getLogger(__name__)

_DEFAULT_ROUTE = {"0.0.0.0/0", "::/0"}


async def probe_site(site: Site, box: SecretBox | None = None) -> ProbeResult:
    """Connect to a site and report what the wizard needs to show."""
    try:
        async with open_driver(site, box) as driver:
            caps = await driver.capabilities()
            found = await detect_uplinks(driver)
            wans, lan = found.wans, found.lan
    except IdentityMismatch as exc:
        # Not a reachability problem: something answered, and it was not the
        # device this site is pinned to.
        return ProbeResult(reachable=False, error=str(exc))
    except DriverError as exc:
        return ProbeResult(reachable=False, error=str(exc))
    except Exception as exc:  # pragma: no cover - unexpected transport failure
        log.exception("probe of %s failed", site.mgmt_host)
        return ProbeResult(reachable=False, error=f"{type(exc).__name__}: {exc}")

    return ProbeResult(
        reachable=True,
        version=caps.version,
        board_name=caps.board_name,
        architecture=caps.architecture,
        identity=caps.identity,
        ros_major=caps.ros_major,
        has_wireguard=caps.has_wireguard,
        has_container=caps.has_container,
        has_netwatch_thresholds=caps.has_netwatch_thresholds,
        packages=caps.packages,
        suggested_wans=wans,
        lan_interfaces=lan,
        uplink_notes=found.notes,
        uplink_conflicts=compare_uplinks(site, wans),
    )


def compare_uplinks(site: Site, observed: list[WanCreate]) -> list[UplinkConflict]:
    """Stored uplink facts the device contradicts.

    Only reachability is checked, because only reachability changes what the
    controller builds. An uplink wrongly marked reachable pins the far end's
    IPsec peer to an address it never sees: IKE arrives, matches no peer, and
    is discarded, so both routers look correctly configured and nothing
    establishes. That is precisely what happened here -- three uplinks entered
    by hand claimed public addresses the devices had never had.

    Reported, never corrected. The device's view is evidence, not authority:
    an uplink can be reachable on an address the router cannot see on itself,
    which is the whole point of a port forward.
    """
    seen = {w.interface: w for w in observed}
    out: list[UplinkConflict] = []

    for wan in _loaded_wans(site):
        if not wan.enabled:
            continue
        device = seen.get(wan.interface)
        if device is None:
            continue

        if wan.public_ip and device.nat_behind and not wan.nat_behind:
            out.append(
                UplinkConflict(
                    wan_id=wan.id,
                    wan_name=wan.name,
                    interface=wan.interface,
                    field="nat_behind",
                    stored="reachable from outside",
                    observed=(
                        f"{device.public_ip} is not routable"
                        if device.public_ip
                        else "the address on that interface is not routable"
                    ),
                    why=(
                        "The far end will be told to dial this address. If the "
                        "traffic is translated on the way, it arrives from a "
                        "different one, matches no IPsec peer, and is dropped "
                        "without a log. WireGuard survives it by learning the "
                        "real address from the handshake; nothing else does."
                    ),
                )
            )
            continue

        if wan.public_ip and device.public_ip and wan.public_ip != device.public_ip:
            out.append(
                UplinkConflict(
                    wan_id=wan.id,
                    wan_name=wan.name,
                    interface=wan.interface,
                    field="public_ip",
                    stored=wan.public_ip,
                    observed=device.public_ip,
                    why=(
                        "Tunnels to this site are built to the stored address. "
                        "If the device no longer holds it, every tunnel to it "
                        "dials somewhere that will not answer."
                    ),
                )
            )

    return out


def _loaded_wans(site: Site) -> list[Wan]:
    """The site's uplinks, but only if the caller already loaded them.

    ``Site.wans`` is selectin-loaded on the query paths that go through the
    API, and *not* loaded on the enrollment path, which builds a Site that has
    never been read back. Touching the relationship there is a lazy load in a
    thread with no greenlet to run the IO, which fails as MissingGreenlet --
    a probe crashing on enrollment because it tried to compare uplinks that do
    not exist yet. Asking first is better than a try/except that would also
    swallow real database errors.
    """
    if "wans" in inspect(site).unloaded:
        return []
    return list(site.wans)


def apply_probe(site: Site, result: ProbeResult) -> None:
    """Fold a probe result back onto the Site row."""
    if not result.reachable:
        site.status = SiteStatus.unreachable
        site.last_error = result.error
        return

    site.status = SiteStatus.reachable
    site.last_error = None
    site.last_seen_at = datetime.now(UTC).isoformat()
    site.ros_version = result.version
    site.board_name = result.board_name
    site.architecture = result.architecture
    site.identity = result.identity
    site.capabilities = {
        "ros_major": result.ros_major,
        "has_wireguard": result.has_wireguard,
        "has_container": result.has_container,
        "has_netwatch_thresholds": result.has_netwatch_thresholds,
        "packages": result.packages,
    }


async def _suggest_wans(
    driver: DeviceDriver,
) -> tuple[list[WanCreate], list[InterfaceNote]]:
    """Infer uplinks from the device. See detect_uplinks for how.

    Kept as the two-value shape the wizard and enrollment have always used;
    the notes about down or excluded interfaces ride on detect_uplinks.
    """
    found = await detect_uplinks(driver)
    return found.wans, found.lan


@dataclass
class UplinkDetection:
    """Everything one detection pass learned.

    ``wans`` are the candidates, numbered and costed exactly as the wizard
    has always offered them. ``down`` names the candidates whose default route
    is disabled/inactive or whose interface is not running: they are *kept*,
    because an uplink that is down at the moment of the probe is still an
    uplink, and dropping it made a re-detection pass report a perfectly good
    WAN as vanished every time its ISP had an outage. ``notes`` explains both
    the down ones and the interfaces deliberately left out (tunnels, the
    controller's own sdwan-* interfaces), for the same reason ``lan`` exists:
    a suggestion that silently disappears is worse than one that explains
    itself.
    """

    wans: list[WanCreate] = field(default_factory=list)
    lan: list[InterfaceNote] = field(default_factory=list)
    notes: list[InterfaceNote] = field(default_factory=list)
    down: set[str] = field(default_factory=set)


# /interface "type" values that are never an uplink in their own right. Both
# the RouterOS spellings (gre-tunnel, wg, ...) and the menu names (gre,
# wireguard, ...) are listed: the type column uses the former, and a device
# that only lists the menus is read through the latter.
_TUNNEL_TYPES = frozenset(
    {
        "loopback",
        "gre", "gre-tunnel", "gre6-tunnel", "gre6",
        "ipip", "ipip-tunnel", "ipipv6-tunnel", "ipipv6",
        "wg", "wireguard",
        "eoip", "eoip-tunnel", "eoipv6-tunnel", "eoipv6",
        "vxlan",
    }
)

# Uplinks whose address is handed out by the far end rather than configured:
# the same uplink comes back with a different address after a reconnect, so a
# stored address is a fact with a shelf life, not configuration. PPP-family
# client interfaces (PPPoE, 3G ppp, SSTP/L2TP/OVPN/PPTP dial-outs) and LTE.
_DYNAMIC_TYPES = frozenset(
    {"pppoe-out", "lte", "ppp-out", "sstp-out", "l2tp-out", "ovpn-out", "pptp-out"}
)


def _excluded_reason(name: str, kind: str | None) -> str | None:
    """Why this interface can never be an uplink, or None.

    A default route through a GRE or WireGuard interface is overlay, not
    underlay: offering it as a WAN would have the controller build tunnels
    over its own tunnels. The controller's own interfaces are named sdwan-*,
    so they are excluded by name even on a device whose /interface is not
    readable and gives no type to go on.
    """
    lowered = name.lower()
    if lowered.startswith("sdwan"):
        return "created by the controller (sdwan-*), so it is overlay, not an uplink"
    if lowered == "lo" or kind == "loopback":
        return "a loopback interface, not an uplink"
    if kind in _TUNNEL_TYPES:
        return f"a {kind} tunnel interface: overlay, not an uplink"
    return None


async def detect_uplinks(driver: DeviceDriver) -> UplinkDetection:
    """Infer uplinks from routes, DHCP/PPPoE/LTE clients and interface state.

    An interface is a WAN candidate when it carries a default route -- active
    or not -- or runs a DHCP client, a PPPoE client that installs a default
    route, or is an LTE modem. The operator confirms or edits the list in the
    wizard; this is a starting point, not an authority.

    Those are *inclusion* signals only, which is how a LAN used to end up
    offered as an uplink: nothing here knew what a LAN looks like. Bridge
    membership and DHCP *servers* are the exclusion signal, and the interfaces
    they rule out are returned alongside so the wizard can say why rather than
    silently dropping them. Tunnels and the controller's own interfaces are
    excluded outright (see _excluded_reason).

    PPPoE/LTE/SSTP default routes name an *interface* as their gateway
    (gateway=pppoe-out1, immediate-gw empty or "%pppoe-out1"): there is no
    next-hop address on a point-to-point link. That used to lose the uplink
    entirely, because only an address could be matched to an interface. Now
    the interface name is the candidate, and the gateway stays None -- which
    is the truth, and what the underlay renderer already handles.

    Every extra menu goes through _safe_read: a device without the lte
    package, or a user without read access to /interface, still gets the
    detection it got before.
    """
    routes = await _safe_read(driver, "/ip/route")
    addresses = await _safe_read(driver, "/ip/address")
    dhcp = await _safe_read(driver, "/ip/dhcp-client")
    pppoe = await _safe_read(driver, "/interface/pppoe-client")
    lte = await _safe_read(driver, "/interface/lte")
    interfaces = await _safe_read(driver, "/interface")
    membership = await _bridge_membership(driver)
    bridges = await _bridge_names(driver)
    serving = await _dhcp_serving(driver)

    kinds: dict[str, str] = {}
    not_running: set[str] = set()
    for row in interfaces:
        name = str(row.get("name", ""))
        if not name:
            continue
        if row.get("type"):
            kinds[name] = str(row.get("type"))
        # Absent "running" means the device did not say; only an explicit
        # false (or a disabled interface) counts as down.
        if row.get("disabled") or row.get("running") is False:
            not_running.add(name)
    for row in pppoe:
        if row.get("name"):
            kinds.setdefault(str(row["name"]), "pppoe-out")
    for row in lte:
        if row.get("name"):
            kinds.setdefault(str(row["name"]), "lte")

    # interface -> gateway address (None for an interface-named gateway).
    gateways: dict[str, str | None] = {}
    # interface -> the distance of its default route. The device has already
    # said which uplink it prefers; deriving cost from the order interfaces
    # happen to sort in would contradict it, and cost is what the controller
    # uses to order failover and to pick which uplink carries a tunnel's
    # underlay route.
    preference: dict[str, float] = {}
    # Interfaces that have at least one usable (enabled, active) default
    # route, so a disabled backup route alongside a live one does not mark
    # the uplink down.
    live_route: set[str] = set()
    route_only_down: set[str] = set()
    for route in routes:
        if str(route.get("dst-address", "")) not in _DEFAULT_ROUTE:
            continue
        name, gw = _route_interface(route, addresses)
        if not name:
            continue
        # Disabled and inactive default routes are still candidates: a backup
        # uplink whose route is down right now is exactly the one an outage
        # makes matter, and losing it from detection made re-detection call
        # it vanished.
        if route.get("disabled") or route.get("inactive"):
            route_only_down.add(name)
        else:
            live_route.add(name)
        if gw is not None or name not in gateways:
            # Prefer a real next-hop address over an interface-only route.
            if gateways.get(name) is None:
                gateways[name] = gw
        distance = _as_float(route.get("distance"))
        if distance is not None:
            preference[name] = min(preference.get(name, distance), distance)

    dhcp_ifaces: set[str] = set()
    for client in dhcp:
        if client.get("disabled"):
            continue
        iface = str(client.get("interface", ""))
        if iface:
            dhcp_ifaces.add(iface)
            if gateways.get(iface) is None:
                gateways[iface] = client.get("gateway") or None

    # A PPPoE client that installs a default route is an uplink even while
    # the session is down -- at which point its dynamic route is gone and
    # nothing else on the device mentions it. Same for an LTE modem.
    for row in pppoe:
        name = str(row.get("name", ""))
        if not name or row.get("disabled"):
            continue
        if row.get("add-default-route") is False:
            continue
        gateways.setdefault(name, None)
        if row.get("running") is False:
            not_running.add(name)
    for row in lte:
        name = str(row.get("name", ""))
        if not name or row.get("disabled"):
            continue
        gateways.setdefault(name, None)
        if row.get("running") is False:
            not_running.add(name)

    found = UplinkDetection()

    # Separate before numbering, so dropping a LAN does not leave a gap in the
    # wan1/wan2 sequence or in the cost ladder derived from it. Ordered by the
    # device's own default-route distance, so wan1 is the uplink it actually
    # prefers; an interface with no default route sorts last rather than
    # jumping the queue on its name.
    candidates: list[tuple[str, str | None]] = []
    ordered = sorted(gateways.items(), key=lambda kv: (preference.get(kv[0], _NO_ROUTE), kv[0]))
    for iface, gw in ordered:
        excluded = _excluded_reason(iface, kinds.get(iface))
        if excluded is not None:
            found.notes.append(InterfaceNote(interface=iface, reason=excluded))
            continue
        reason = _lan_reason(iface, membership, bridges, serving)
        if reason is not None:
            found.lan.append(InterfaceNote(interface=iface, reason=reason))
            continue
        candidates.append((iface, gw))
        why_down = None
        if iface in not_running:
            why_down = "interface is not running"
        elif iface in route_only_down and iface not in live_route:
            why_down = "its default route is disabled or inactive"
        if why_down:
            found.down.add(iface)
            found.notes.append(
                InterfaceNote(
                    interface=iface,
                    reason=f"kept as an uplink, but down right now: {why_down}",
                )
            )

    for index, (iface, gw) in enumerate(candidates, start=1):
        # For PPPoE (and any PPP link) the address on the interface itself is
        # the WAN address -- a /32 whose "network" is the far end.
        addr_found = _address_on(iface, addresses)
        addr, prefix_len = addr_found if addr_found else (None, None)
        dynamic = iface in dhcp_ifaces or kinds.get(iface) in _DYNAMIC_TYPES
        private = addr is not None and is_unroutable(addr)
        found.wans.append(
            WanCreate(
                name=f"wan{index}",
                interface=iface,
                # A private address on the uplink means the router sits behind
                # NAT and can only ever dial out.
                public_ip=None if (private or addr is None) else addr,
                # Kept even when the address is not usable as a public one: the
                # mask describes the segment, which is what the underlay route
                # needs, and that is true whether or not the address routes.
                prefix_len=prefix_len,
                dynamic=dynamic,
                nat_behind=private,
                gateway=gw if gw and _is_ip(str(gw)) else None,
                cost=float(index),
            )
        )
    return found


def _route_interface(
    route: dict, addresses: list[dict]
) -> tuple[str | None, str | None]:
    """(interface, gateway address) for one default route.

    Three shapes reach here: ``10.0.0.1%ether1`` (address and interface),
    ``10.0.0.1`` (address only -- matched to an interface subnet) and
    ``pppoe-out1`` / ``%pppoe-out1`` (interface only, the point-to-point
    case). ``immediate-gw`` is preferred when it says something, but an
    inactive route often has it empty, so ``gateway`` is the fallback.
    """
    for raw in (route.get("immediate-gw"), route.get("gateway")):
        text = str(raw or "").strip()
        if not text:
            continue
        gw, _, name = text.partition("%")
        if name:
            return name, gw if gw and _is_ip(gw) else None
        if _is_ip(gw):
            name = _interface_for(gw, addresses) or ""
            if name:
                return name, gw
            continue
        # Not an address: an interface name as the gateway. Accepted whether
        # or not /interface listed it -- that menu may be unreadable -- but
        # never the routing-table name "main", which a default route can name
        # as a lookup target rather than a next hop.
        if gw != "main":
            return gw, None
    return None, None


async def _bridge_membership(driver: DeviceDriver) -> dict[str, str]:
    """Port interface -> the bridge it is a member of."""
    return {
        str(row.get("interface", "")): str(row.get("bridge", ""))
        for row in await _safe_read(driver, "/interface/bridge/port")
        if row.get("interface") and row.get("bridge") and not row.get("disabled")
    }


async def _bridge_names(driver: DeviceDriver) -> set[str]:
    return {
        str(row.get("name", ""))
        for row in await _safe_read(driver, "/interface/bridge")
        if row.get("name")
    }


async def _dhcp_serving(driver: DeviceDriver) -> set[str]:
    """Interfaces with an enabled DHCP server handing out addresses."""
    return {
        str(row.get("interface", ""))
        for row in await _safe_read(driver, "/ip/dhcp-server")
        if row.get("interface") and not row.get("disabled")
    }


def _lan_reason(
    interface: str,
    membership: dict[str, str],
    bridges: set[str],
    serving: set[str],
) -> str | None:
    """Why this interface is a LAN rather than an uplink, or None.

    Both signals are required, not either alone: a bridge can legitimately
    carry the uplink, and a DHCP server can sit on a routed sub-interface, so
    either on its own would throw away real WANs. Together they describe a
    switched segment this router hands addresses out on, which is a LAN.
    """
    bridge = membership.get(interface)
    if bridge is None and interface not in bridges:
        return None
    if not (interface in serving or (bridge is not None and bridge in serving)):
        return None
    where = f"bridged into {bridge}" if bridge else "a bridge"
    return (
        f"{where} and running a DHCP server, so it looks like a LAN "
        "rather than an uplink"
    )


async def _safe_read(driver: DeviceDriver, path: str) -> list[dict]:
    try:
        return await driver.read(path)
    except DriverError:
        return []


# Sorts after any real route distance, which RouterOS caps at 255.
_NO_ROUTE = 1e6


def _as_float(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _address_on(interface: str, addresses: list[dict]) -> tuple[str, int] | None:
    """The interface's address and its mask length.

    The mask is kept, not discarded: it is the only thing that says whether a
    tunnel's far endpoint is on this segment or out through the gateway, and
    those two need different routes. Recovering it later means going back to
    the device.
    """
    for row in addresses:
        if str(row.get("interface", "")) == interface and not row.get("disabled"):
            raw = str(row.get("address", ""))
            if "/" in raw:
                try:
                    parsed = ip_interface(raw)
                except ValueError:
                    continue
                return str(parsed.ip), parsed.network.prefixlen
    return None


def _interface_for(gateway: str, addresses: list[dict]) -> str | None:
    try:
        gw = ip_address(gateway)
    except ValueError:
        return None
    for row in addresses:
        raw = str(row.get("address", ""))
        if "/" not in raw:
            continue
        try:
            if gw in ip_interface(raw).network:
                return str(row.get("interface", "")) or None
        except ValueError:
            continue
    return None


def _is_ip(value: str) -> bool:
    try:
        ip_address(value)
    except ValueError:
        return False
    return True


# Ranges from which a router cannot be reached by an inbound tunnel.

