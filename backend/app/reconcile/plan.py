"""Turn rendered sections plus live device state into an ordered change plan."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.drivers.base import ConfigOp, ConfigSection, DeviceDriver, DriverError, OpKind
from app.reconcile.diff import SectionDiff, diff_section


@dataclass(slots=True)
class Plan:
    """Everything an operator needs to decide whether to apply."""

    sections: list[SectionDiff] = field(default_factory=list)
    # Paths that could not be read. A section is skipped rather than treated as
    # empty, because an empty read would look like "remove everything".
    unreadable: dict[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return all(s.empty for s in self.sections)

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
        return "\n".join(lines) if lines else "(no changes)"

    def to_json(self) -> dict[str, Any]:
        """Storable on the Job row and displayable in the UI. Already redacted:
        ItemDiff.render masks secret properties."""
        return {
            "counts": self.counts,
            "empty": self.empty,
            "unreadable": self.unreadable,
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


async def build_plan(driver: DeviceDriver, sections: list[ConfigSection]) -> Plan:
    """Read the device once per section and diff intent against it."""
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
        plan.sections.append(diff_section(section, live))
    return plan
