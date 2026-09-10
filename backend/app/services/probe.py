"""Touch a device, learn what it is, and guess its uplinks.

This backs the onboarding wizard. Everything here is read-only -- probing a
device must never change it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from ipaddress import ip_address, ip_interface, ip_network

from app.drivers.base import DeviceDriver, DriverError
from app.drivers.factory import open_driver
from app.drivers.identity import IdentityMismatch
from app.models.enums import SiteStatus
from app.models.site import Site
from app.schemas.site import InterfaceNote, ProbeResult, WanCreate
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
    )


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
    for route in routes:
        if str(route.get("dst-address", "")) not in _DEFAULT_ROUTE:
            continue
        if route.get("disabled") or route.get("inactive"):
            continue
        iface = route.get("immediate-gw") or route.get("gateway") or ""
        gw, _, name = str(iface).partition("%")
        if name:
            gateways.setdefault(name, gw or None)
        elif _is_ip(gw):
            # Gateway given as a bare address; match it to an interface subnet.
            owner = _interface_for(gw, addresses)
            if owner:
                gateways.setdefault(owner, gw)

    for client in dhcp:
        if client.get("disabled"):
            continue
        iface = str(client.get("interface", ""))
        if iface:
            gateways.setdefault(iface, client.get("gateway"))

    # Separate before numbering, so dropping a LAN does not leave a gap in the
    # wan1/wan2 sequence or in the cost ladder derived from it.
    candidates: list[tuple[str, str | None]] = []
    lan: list[InterfaceNote] = []
    for iface, gw in sorted(gateways.items()):
        reason = _lan_reason(iface, membership, bridges, serving)
        if reason is None:
            candidates.append((iface, gw))
        else:
            lan.append(InterfaceNote(interface=iface, reason=reason))

    suggestions: list[WanCreate] = []
    for index, (iface, gw) in enumerate(candidates, start=1):
        addr = _address_on(iface, addresses)
        is_dhcp = any(
            str(c.get("interface", "")) == iface and not c.get("disabled") for c in dhcp
        )
        private = addr is not None and _is_private(addr)
        suggestions.append(
            WanCreate(
                name=f"wan{index}",
                interface=iface,
                # A private address on the uplink means the router sits behind
                # NAT and can only ever dial out.
                public_ip=None if (private or addr is None) else addr,
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


def _address_on(interface: str, addresses: list[dict]) -> str | None:
    for row in addresses:
        if str(row.get("interface", "")) == interface and not row.get("disabled"):
            raw = str(row.get("address", ""))
            if "/" in raw:
                try:
                    return str(ip_interface(raw).ip)
                except ValueError:
                    continue
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
# ``ip_address.is_private`` is deliberately not used: its membership changed
# across Python versions (3.12 folded the documentation ranges in), and it also
# covers space that says nothing about NAT. The question here is narrower --
# is this address unroutable on the public internet?
_UNROUTABLE = tuple(
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


def _is_private(value: str) -> bool:
    try:
        addr = ip_address(value)
    except ValueError:
        return False
    return any(addr in net for net in _UNROUTABLE if net.version == addr.version)
