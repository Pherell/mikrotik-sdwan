"""Turn rendered sections plus live device state into an ordered change plan."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.drivers.base import ConfigOp, ConfigSection, DeviceDriver, DriverError, OpKind
from app.reconcile.diff import Collision, SectionDiff, diff_section


@dataclass(slots=True)
class Plan:
    """Everything an operator needs to decide whether to apply."""

    sections: list[SectionDiff] = field(default_factory=list)
    # Paths that could not be read. A section is skipped rather than treated as
    # empty, because an empty read would look like "remove everything".
    unreadable: dict[str, str] = field(default_factory=dict)
    # Non-blocking findings about config the controller does not own but which
    # changes what its own rows do -- see _shadow_warnings.
    warnings: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return all(s.empty for s in self.sections)

    @property
    def collisions(self) -> list[Collision]:
        return [c for s in self.sections for c in s.collisions]

    @property
    def blocked(self) -> list[Collision]:
        """Collisions not resolved by adoption. An apply with any is refused:
        the withheld adds would leave the fabric half-built."""
        return [c for c in self.collisions if not c.adopted]

    @property
    def counts(self) -> dict[str, int]:
        totals = {"add": 0, "set": 0, "remove": 0}
        for section in self.sections:
            for item in section.items:
                totals[item.kind.value] += 1
        return totals

    def ops(self) -> list[ConfigOp]:
        """Every mutation, in a safe order.

        Additions run in section order (address lists before crypto before
        tunnels before routing); removals run in reverse, so a route is torn
        down before the interface it points at.
        """
        ordered = sorted(self.sections, key=lambda s: s.order)
        creates: list[ConfigOp] = []
        deletes: list[ConfigOp] = []
        for section in ordered:
            for op in section.ops():
                (deletes if op.kind is OpKind.remove else creates).append(op)
        deletes.reverse()
        return creates + deletes

    def render(self) -> str:
        lines: list[str] = []
        for section in sorted(self.sections, key=lambda s: s.order):
            if section.empty:
                continue
            lines.extend(section.render())
        for path, error in self.unreadable.items():
            lines.append(f"! {path} could not be read: {error}")
        for collision in self.collisions:
            lines.append(collision.render())
        for warning in self.warnings:
            lines.append(f"? {warning}")
        return "\n".join(lines) if lines else "(no changes)"

    def to_json(self) -> dict[str, Any]:
        """Storable on the Job row and displayable in the UI. Already redacted:
        ItemDiff.render masks secret properties."""
        return {
            "counts": self.counts,
            "empty": self.empty,
            "unreadable": self.unreadable,
            "collisions": [
                {
                    "path": c.path,
                    "identity": [str(i) for i in c.identity],
                    "item_id": c.item_id,
                    "comment": c.comment,
                    "adopted": c.adopted,
                }
                for c in self.collisions
            ],
            "warnings": list(self.warnings),
            "sections": [
                {
                    "path": s.path,
                    "order": s.order,
                    "lines": s.render(),
                }
                for s in sorted(self.sections, key=lambda s: s.order)
                if not s.empty
            ],
            "text": self.render(),
        }


async def build_plan(
    driver: DeviceDriver, sections: list[ConfigSection], *, adopt: bool = False
) -> Plan:
    """Read the device once per section and diff intent against it.

    ``adopt`` takes over unmanaged rows that stand where intent wants to
    write; without it they are reported as collisions. See diff_section.
    """
    plan = Plan()
    for section in sections:
        try:
            live = await driver.read(section.path)
        except DriverError as exc:
            if not section.items:
                # A section with nothing to write exists only to clean up rows a
                # deleted link left behind. If the menu is absent -- an older
                # RouterOS, or a package that is not installed -- there is
                # nothing there to clean up, so skip it quietly instead of
                # blocking the apply.
                continue
            # Never fabricate an empty read for a menu we intend to write to. It
            # would diff as "delete every managed row in it".
            plan.unreadable[section.path] = str(exc)
            continue
        plan.sections.append(diff_section(section, live, adopt=adopt))
        plan.warnings.extend(_shadow_warnings(section, live))
    return plan


# Mangle actions that decide a packet's fate for steering. An unmanaged rule
# doing any of these above ours, with passthrough off, means our rule never
# sees the traffic it was written for.
_STEERING_ACTIONS = {"mark-routing", "mark-connection", "accept"}


def _shadow_warnings(section: ConfigSection, live: list[dict[str, Any]]) -> list[str]:
    """Unowned config that silently changes what owned config does.

    Not a block: the operator may have put it there on purpose. But a plan
    that diffs clean while someone's own rule is eating the traffic is exactly
    the case nobody can debug from the controller, so it is said out loud.
    """
    out: list[str] = []
    if section.path == "/ip/firewall/mangle" and section.items:
        first_owned = next(
            (i for i, row in enumerate(live) if section.owns(row)), len(live)
        )
        for row in live[:first_owned]:
            if section.owns(row) or row.get("disabled") in (True, "true"):
                continue
            if row.get("chain") not in ("prerouting", "output"):
                continue
            if row.get("action") not in _STEERING_ACTIONS:
                continue
            if row.get("passthrough") in (True, "true") and row.get("action") != "accept":
                continue
            out.append(
                f"/ip/firewall/mangle [{row.get('.id', '?')}] chain={row.get('chain')} "
                f"action={row.get('action')} sits above every sdwan rule and stops "
                "the chain: traffic it matches is never steered"
            )
    if section.path == "/interface/wireguard":
        ports = {
            str(i.props.get("listen-port"))
            for i in section.items
            if i.props.get("listen-port") is not None
        }
        for row in live:
            if section.owns(row):
                continue
            port = str(row.get("listen-port", ""))
            if port and port in ports:
                out.append(
                    f"/interface/wireguard {row.get('name', '?')} already listens on "
                    f"UDP {port}, which an sdwan tunnel needs"
                )
    return out
