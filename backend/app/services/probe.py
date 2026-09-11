"""Touch a device, learn what it is, and guess its uplinks.

This backs the onboarding wizard. Everything here is read-only -- probing a
device must never change it.
"""

from __future__ import annotations

import logging
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
            wans, lan = await _suggest_wans(driver)
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
    """Infer uplinks from the routing table and DHCP clients.

    An interface is a WAN candidate when it carries a default route or runs a
    DHCP client. The operator confirms or edits the list in the wizard -- this
    is a starting point, not an authority.

    Those are *inclusion* signals only, which is how a LAN used to end up
    offered as an uplink: nothing here knew what a LAN looks like. Bridge
    membership and DHCP *servers* are the exclusion signal, and the interfaces
    they rule out are returned alongside so the wizard can say why rather than
    silently dropping them.
    """
    routes = await _safe_read(driver, "/ip/route")
    addresses = await _safe_read(driver, "/ip/address")
    dhcp = await _safe_read(driver, "/ip/dhcp-client")
    membership = await _bridge_membership(driver)
    bridges = await _bridge_names(driver)
    serving = await _dhcp_serving(driver)

    # interface -> gateway, from active default routes only.
    gateways: dict[str, str | None] = {}
    # interface -> the distance of its default route. The device has already
    # said which uplink it prefers; deriving cost from the order interfaces
    # happen to sort in would contradict it, and cost is what the controller
    # uses to order failover and to pick which uplink carries a tunnel's
    # underlay route.
    preference: dict[str, float] = {}
    for route in routes:
        if str(route.get("dst-address", "")) not in _DEFAULT_ROUTE:
            continue
        if route.get("disabled") or route.get("inactive"):
            continue
        iface = route.get("immediate-gw") or route.get("gateway") or ""
        gw, _, name = str(iface).partition("%")
        if not name and _is_ip(gw):
            # Gateway given as a bare address; match it to an interface subnet.
            name = _interface_for(gw, addresses) or ""
        if not name:
            continue
        gateways.setdefault(name, gw or None)
        distance = _as_float(route.get("distance"))
        if distance is not None:
            preference[name] = min(preference.get(name, distance), distance)

    for client in dhcp:
        if client.get("disabled"):
            continue
        iface = str(client.get("interface", ""))
        if iface:
            gateways.setdefault(iface, client.get("gateway"))

    # Separate before numbering, so dropping a LAN does not leave a gap in the
    # wan1/wan2 sequence or in the cost ladder derived from it. Ordered by the
    # device's own default-route distance, so wan1 is the uplink it actually
    # prefers; an interface with no default route sorts last rather than
    # jumping the queue on its name.
    candidates: list[tuple[str, str | None]] = []
    lan: list[InterfaceNote] = []
    ordered = sorted(gateways.items(), key=lambda kv: (preference.get(kv[0], _NO_ROUTE), kv[0]))
    for iface, gw in ordered:
        reason = _lan_reason(iface, membership, bridges, serving)
        if reason is None:
            candidates.append((iface, gw))
        else:
            lan.append(InterfaceNote(interface=iface, reason=reason))

    suggestions: list[WanCreate] = []
    for index, (iface, gw) in enumerate(candidates, start=1):
        found = _address_on(iface, addresses)
        addr, prefix_len = found if found else (None, None)
        is_dhcp = any(
            str(c.get("interface", "")) == iface and not c.get("disabled") for c in dhcp
        )
        private = addr is not None and is_unroutable(addr)
        suggestions.append(
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
                dynamic=is_dhcp,
                nat_behind=private,
                gateway=gw if gw and _is_ip(str(gw)) else None,
                cost=float(index),
            )
        )
    return suggestions, lan


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

