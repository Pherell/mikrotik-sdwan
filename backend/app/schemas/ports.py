"""The shape of one interface as the UI draws it."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

PortRole = Literal["wan", "lan", "unused", "bridge", "tunnel", "other"]


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
    # True when the reconciler owns this interface, so an edit made by hand
    # here will be reverted on the next apply.
    managed: bool = False
