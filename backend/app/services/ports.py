"""What the front panel of a device looks like.

A read-only view, assembled from menus the driver already reads. It exists
because "which cable is my second uplink in" is a question people answer by
walking to the rack, and because a port that is administratively up but not
running is invisible in a form full of text fields.

Nothing here writes. Port *configuration* -- bridge membership, VLANs, PoE --
is switch management, which is a different product with a different blast
radius: a mistaken bridge change locks you out far more reliably than a
mistaken route does. If that is ever wanted it needs the same ownership tagging
and dead-man rollback the rest of the reconciler has, and it should be a
deliberate decision rather than a side effect of drawing ports.
"""

from __future__ import annotations

from app.drivers.base import DeviceDriver
from app.models.site import Site
from app.schemas.ports import PortRead

# Interface types RouterOS reports that are physical front-panel ports.
_PHYSICAL = frozenset({"ether", "wlan", "sfp", "sfp-plus", "qsfp", "wifi"})

# Types the controller creates itself, or that are logical by nature.
_TUNNEL = frozenset({"gre", "gre6", "ipip", "ipip6", "wireguard", "eoip", "vxlan", "vpls"})


def _int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _classify(
    name: str,
    kind: str,
    *,
    wan_interfaces: set[str],
    bridge_ports: set[str],
    has_address: bool,
) -> str:
    """The one piece of judgement in this module.

    Ordered so the most specific answer wins: a WAN that also happens to be a
    bridge port is still a WAN, because that is the fact the operator cares
    about.
    """
    if name in wan_interfaces:
        return "wan"
    if kind in _TUNNEL:
        return "tunnel"
    if kind == "bridge":
        return "bridge"
    if name in bridge_ports:
        return "lan"
    if kind in _PHYSICAL and has_address:
        return "lan"
    if kind in _PHYSICAL:
        return "unused"
    return "other"


async def _safe_read(driver: DeviceDriver, path: str) -> list[dict]:
    """A menu that is absent is not an error.

    /interface/ethernet does not exist on a device with no ethernet ports, and
    a CHR has no /interface/bridge/port until someone makes a bridge. Neither
    should cost the whole panel.
    """
    try:
        return await driver.read(path)
    except Exception:
        return []


async def read_ports(driver: DeviceDriver, site: Site) -> list[PortRead]:
    interfaces = await driver.read("/interface")
    ethernet = await _safe_read(driver, "/interface/ethernet")
    addresses = await _safe_read(driver, "/ip/address")
    bridge_ports = await _safe_read(driver, "/interface/bridge/port")

    # site.wans is loaded by the caller; this module does no IO of its own.
    wan_by_interface = {w.interface: w for w in site.wans}
    in_bridge = {
        str(p.get("interface", "")): str(p.get("bridge", "")) for p in bridge_ports
    }
    speed_by_name = {
        str(e.get("name", "")): (e.get("speed") or e.get("rate") or None) for e in ethernet
    }
    default_name_by_name = {
        str(e.get("name", "")): e.get("default-name") for e in ethernet
    }

    addresses_by_interface: dict[str, list[str]] = {}
    for row in addresses:
        iface = str(row.get("interface", ""))
        address = str(row.get("address", ""))
        if iface and address:
            addresses_by_interface.setdefault(iface, []).append(address)

    ports: list[PortRead] = []
    for row in interfaces:
        name = str(row.get("name", ""))
        if not name:
            continue
        kind = str(row.get("type", "") or "")
        comment = row.get("comment") or None
        wan = wan_by_interface.get(name)

        ports.append(
            PortRead(
                name=name,
                type=kind,
                running=bool(row.get("running")),
                disabled=bool(row.get("disabled")),
                comment=comment,
                mac=row.get("mac-address") or None,
                mtu=_int(row.get("mtu")),
                speed=(str(speed_by_name[name]) or None) if name in speed_by_name else None,
                default_name=(
                    str(default_name_by_name.get(name) or "") or None
                    if name in default_name_by_name
                    else None
                ),
                addresses=addresses_by_interface.get(name, []),
                rx_bytes=_int(row.get("rx-byte")),
                tx_bytes=_int(row.get("tx-byte")),
                bridge=in_bridge.get(name) or None,
                role=_classify(
                    name,
                    kind,
                    wan_interfaces=set(wan_by_interface),
                    bridge_ports=set(in_bridge),
                    has_address=bool(addresses_by_interface.get(name)),
                ),
                wan_name=wan.name if wan else None,
                wan_enabled=wan.enabled if wan else None,
                # Anything the reconciler owns carries this tag. Showing it
                # means nobody has to guess whether a change here will be
                # reverted on the next apply.
                managed=bool(comment and str(comment).startswith("sdwan:")),
            )
        )

    # Physical ports first and in device order, then bridges, then tunnels --
    # which is roughly how someone looking at the box reads it.
    order = {"wan": 0, "lan": 1, "unused": 2, "bridge": 3, "tunnel": 4, "other": 5}
    return sorted(ports, key=lambda p: (order.get(p.role, 9), p.name))
