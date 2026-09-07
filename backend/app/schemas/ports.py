"""The shape of one interface as the UI draws it."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# "candidate" is an interface the *device* is using as an uplink -- it holds a
# DHCP client or a default route -- that the controller has no Wan record for.
# Without it such a port reads as "LAN", which is what the panel knows rather
# than what is true.
PortRole = Literal[
    "wan", "candidate", "lan", "unused", "bridge", "tunnel", "other"
]


class PortRead(BaseModel):
    name: str
    type: str
    running: bool
    disabled: bool
    comment: str | None = None
    mac: str | None = None
    mtu: int | None = None
    speed: str | None = None
    default_name: str | None = None
    addresses: list[str] = []
    rx_bytes: int | None = None
    tx_bytes: int | None = None
    bridge: str | None = None

    role: PortRole
    # Set when this interface is one of the site's configured uplinks.
    wan_name: str | None = None
    wan_enabled: bool | None = None
    # Why the device looks like it is using this port as an uplink.
    dhcp_client: bool = False
    default_route: bool = False
    # True when the reconciler owns this interface, so an edit made by hand
    # here will be reverted on the next apply.
    managed: bool = False
