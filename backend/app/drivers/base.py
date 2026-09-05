"""Device access abstraction.

Everything above this layer speaks in declarative ConfigSections. Only the
drivers know about REST payloads, CLI syntax, or RouterOS version quirks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class OpKind(StrEnum):
    add = "add"
    set = "set"
    remove = "remove"


# Every row the controller manages carries this prefix in its comment. Rows
# without it were put there by a human and are never touched.
OWNER_PREFIX = "sdwan:"


@dataclass(slots=True)
class ConfigItem:
    """One row in a RouterOS menu, e.g. a single /ip/ipsec/peer."""

    props: dict[str, Any]
    # Full ownership comment written to the device, e.g.
    # "sdwan:core:hub1-wan1-spoke2-wan1". Also the diff identity when the
    # section declares no key columns.
    tag: str = ""

    def identity(self, key: tuple[str, ...]) -> tuple[Any, ...]:
        from app.drivers.coerce import canonical

        if not key:
            return (self.tag,)
        return tuple(canonical(self.props.get(k)) for k in key)


@dataclass(slots=True)
class ConfigSection:
    """A RouterOS menu path plus the rows the controller owns within it.

    ``key`` names the properties that identify a row for diffing. RouterOS
    ``.id`` values are not stable across reboots, so the reconciler matches on
    the ownership comment plus these columns instead.
    """

    path: str                       # e.g. "/ip/ipsec/peer"
    items: list[ConfigItem] = field(default_factory=list)
    # Ownership scope for this section. Device rows whose comment starts with
    # this string are considered controller-managed; everything else in the menu
    # is invisible to the reconciler. This is what makes it safe to point the
    # controller at a router that already has hand-built config.
    owner_tag: str = OWNER_PREFIX
    key: tuple[str, ...] = ()
    # Properties set once at creation and never diffed afterwards: generated
    # keys and PSKs, which the device either will not read back or returns in a
    # form that can never match what was written.
    write_once: tuple[str, ...] = ()
    # Device-populated properties that are not intent and must be ignored when
    # comparing, e.g. counters and dynamic state flags.
    ignore: tuple[str, ...] = ()
    ordered: bool = False           # firewall/mangle care about position
    order: int = 50                 # apply order; see app.render.engine.ORDER

    def owns(self, row: dict[str, Any]) -> bool:
        return str(row.get("comment", "")).startswith(self.owner_tag)


@dataclass(slots=True)
class ConfigOp:
    """A single mutation to send to a device."""

    kind: OpKind
    path: str
    props: dict[str, Any] = field(default_factory=dict)
    item_id: str | None = None      # RouterOS .id, resolved just before apply
    comment: str = ""
    place_before: str | None = None

    def redacted(self) -> ConfigOp:
        from app.drivers.redact import redact_props

        return ConfigOp(
            kind=self.kind,
            path=self.path,
            props=redact_props(self.props),
            item_id=self.item_id,
            comment=self.comment,
            place_before=self.place_before,
        )


@dataclass(slots=True)
class ApplyResult:
    ok: bool
    applied: int = 0
    failed_op: ConfigOp | None = None
    error: str | None = None
    log: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DeviceCaps:
    """What this device can actually do.

    The planner consults this before rendering so an unsupported feature fails
    at validation with a clear message rather than halfway through an apply.
    """

    ros_major: int
    version: str = ""
    board_name: str = ""
    architecture: str = ""
    identity: str = ""
    has_rest: bool = False
    has_wireguard: bool = False
    has_container: bool = False
    has_v7_bgp: bool = False
    has_netwatch_thresholds: bool = False
    packages: list[str] = field(default_factory=list)

    def supports_transport(self, transport: str) -> bool:
        if transport == "wireguard":
            return self.has_wireguard
        if transport in {"vxlan"}:
            return self.ros_major >= 7
        return True


class DriverError(RuntimeError):
    """Transport-level failure talking to a device."""


class DeviceUnreachable(DriverError):
    pass


class DeviceAuthError(DriverError):
    pass


@runtime_checkable
class DeviceDriver(Protocol):
    """The contract every device backend implements."""

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def capabilities(self) -> DeviceCaps: ...

    async def read(self, path: str, query: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Return rows from a menu, with values coerced to native Python types."""
        ...

    async def apply(self, ops: list[ConfigOp]) -> ApplyResult:
        """Execute mutations in order, stopping at the first failure."""
        ...

    async def run(self, command: str, params: dict[str, Any] | None = None) -> Any:
        """Execute a console command."""
        ...

    async def backup(self, name: str) -> None:
        """Take an on-device configuration backup under ``name``."""
        ...
