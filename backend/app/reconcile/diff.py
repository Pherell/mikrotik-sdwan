"""Three-way diff between intent, last-known-applied, and live device state.

Two invariants hold everything together:

**Ownership.** Only rows whose ``comment`` starts with the section's
``owner_tag`` are visible to the reconciler. Hand-built configuration on the
same device is neither read as drift nor removed as unmanaged.

**Canonical comparison.** Intent and device state are both pushed through
``coerce.canonical`` before comparing, so ``mtu=1400`` and ``"1400"`` are the
same value. Without this every apply would diff dirty forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.drivers.base import ConfigItem, ConfigOp, ConfigSection, OpKind
from app.drivers.coerce import canonical
from app.drivers.redact import redact_props

# Never compared: RouterOS bookkeeping and read-only state that is not intent.
_ALWAYS_IGNORED = frozenset(
    {
        ".id",
        ".nextid",
        "dynamic",
        "invalid",
        "running",
        "inactive",
        "actual-mtu",
        "bytes",
        "packets",
        "rx-byte",
        "tx-byte",
        "last-seen",
        "uptime",
    }
)


@dataclass(slots=True)
class FieldChange:
    prop: str
    before: str
    after: str


@dataclass(slots=True)
class ItemDiff:
    kind: OpKind
    identity: tuple[Any, ...]
    tag: str
    props: dict[str, Any] = field(default_factory=dict)
    item_id: str | None = None
    place_before: str | None = None
    changes: list[FieldChange] = field(default_factory=list)

    def render(self, path: str) -> str:
        """One human-readable line per change, secrets already masked."""
        ident = ",".join(str(i) for i in self.identity)
        match self.kind:
            case OpKind.add:
                shown = " ".join(
                    f"{k}={v}" for k, v in sorted(redact_props(self.props).items())
                )
                return f"+ {path} {ident}  {shown}"
            case OpKind.remove:
                return f"- {path} {ident}"
            case OpKind.set:
                shown = ", ".join(
                    f"{c.prop}: {_mask(c.prop, c.before)} -> {_mask(c.prop, c.after)}"
                    for c in self.changes
                )
                return f"~ {path} {ident}  {shown}"
            case OpKind.move:
                where = f"before {self.place_before}" if self.place_before else "to the end"
                return f"> {path} {ident}  move {where}"


@dataclass(slots=True)
class SectionDiff:
    path: str
    owner_tag: str
    items: list[ItemDiff] = field(default_factory=list)
    order: int = 50
    # False for a menu that rejects a comment (e.g. /ip/ipsec/profile): the op
    # must not carry one, or the driver re-injects it and the write is refused.
    comment_capable: bool = True

    @property
    def empty(self) -> bool:
        return not self.items

    def ops(self) -> list[ConfigOp]:
        """Mutations for this section, adds and updates before removals.

        Removing last matters when a rename is expressed as add+remove: the new
        row must exist before the old one goes away, or the device spends a
        window with neither.
        """
        creates = [
            i for i in self.items if i.kind in (OpKind.add, OpKind.set)
        ]
        moves = [i for i in self.items if i.kind is OpKind.move]
        deletes = [i for i in self.items if i.kind is OpKind.remove]
        return [self._op(i) for i in creates + moves + deletes]

    def _op(self, item: ItemDiff) -> ConfigOp:
        return ConfigOp(
            kind=item.kind,
            path=self.path,
            props=item.props,
            item_id=item.item_id,
            comment=item.tag if self.comment_capable else "",
            place_before=item.place_before,
        )

    def render(self) -> list[str]:
        return [i.render(self.path) for i in self.items]


def diff_section(section: ConfigSection, live_rows: list[dict[str, Any]]) -> SectionDiff:
    """Compare one rendered section against what the device currently holds."""
    result = SectionDiff(
        path=section.path,
        owner_tag=section.owner_tag,
        order=section.order,
        comment_capable=section.comment_capable,
    )
    ignored = _ALWAYS_IGNORED | set(section.ignore)

    managed = [row for row in live_rows if section.owns(row)]
    current: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in managed:
        current[_row_identity(row, section)] = row

    desired: dict[tuple[Any, ...], ConfigItem] = {
        item.identity(section.key): item for item in section.items
    }

    for identity, item in desired.items():
        live = current.get(identity)
        if live is None:
            props = dict(item.props)
            if section.comment_capable:
                props["comment"] = item.tag or section.owner_tag
            result.items.append(
                ItemDiff(kind=OpKind.add, identity=identity, tag=item.tag, props=props)
            )
            continue

        changes = _compare(item, live, ignored, section.write_once, section.comment_capable)
        if changes:
            result.items.append(
                ItemDiff(
                    kind=OpKind.set,
                    identity=identity,
                    tag=item.tag,
                    item_id=str(live.get(".id", "")),
                    # Take the value from the change, not from item.props: a
                    # retagged row's correction lives in `comment`, which is
                    # section metadata rather than a rendered property.
                    props={c.prop: c.after for c in changes},
                    changes=changes,
                )
            )

    for identity, row in current.items():
        if identity in desired:
            continue
        result.items.append(
            ItemDiff(
                kind=OpKind.remove,
                identity=identity,
                tag=str(row.get("comment", "")),
                item_id=str(row.get(".id", "")),
            )
        )

    _order(section, live_rows, desired, result)
    return result


def _matches(row: dict[str, Any], predicate: dict[str, Any]) -> bool:
    return all(canonical(row.get(k)) == canonical(v) for k, v in predicate.items())


def _order(
    section: ConfigSection,
    live_rows: list[dict[str, Any]],
    desired: dict[tuple[Any, ...], ConfigItem],
    result: SectionDiff,
) -> None:
    """Emit moves when owned rows sit in the wrong place.

    Only for menus that declare ``before``. RouterOS evaluates firewall chains
    top to bottom, so an accept rule appended after the operator's masquerade
    never matches -- and a property-only diff reads perfectly clean while the
    configuration does nothing.

    New rows are handled by ``place_before`` on the add. This deals with rows
    that already exist in the wrong order, which an add cannot fix.
    """
    if section.before is None:
        return

    anchor_id: str | None = None
    anchor_at = len(live_rows)
    for index, row in enumerate(live_rows):
        if not section.owns(row) and _matches(row, section.before):
            anchor_id = str(row.get(".id", "")) or None
            anchor_at = index
            break

    # Give every add the same destination, so a fresh section lands in order in
    # one pass rather than needing a second apply to sort itself out.
    for item in result.items:
        if item.kind is OpKind.add:
            item.place_before = anchor_id

    owned = [
        (_row_identity(row, section), row, index)
        for index, row in enumerate(live_rows)
        if section.owns(row)
    ]
    surviving = [(ident, row, at) for ident, row, at in owned if ident in desired]
    if not surviving:
        return

    want = [ident for ident in desired if any(i == ident for i, _, _ in surviving)]
    have = [ident for ident, _, _ in surviving]
    below_anchor = any(at > anchor_at for _, _, at in surviving)

    if have == want and not below_anchor:
        return

    # Re-seat every surviving row in section order against the same anchor.
    # Moving them one at a time to the same destination leaves them in that
    # order, and doing all of them keeps this idempotent rather than depending
    # on which single row happened to be out of place.
    by_identity = {ident: row for ident, row, _ in surviving}
    for ident in want:
        row = by_identity[ident]
        result.items.append(
            ItemDiff(
                kind=OpKind.move,
                identity=ident,
                tag=str(row.get("comment", "")),
                item_id=str(row.get(".id", "")),
                place_before=anchor_id,
            )
        )


def _compare(
    item: ConfigItem,
    live: dict[str, Any],
    ignored: set[str],
    write_once: tuple[str, ...],
    comment_capable: bool = True,
) -> list[FieldChange]:
    """Which managed properties differ, comparing canonically."""
    changes: list[FieldChange] = []
    for prop, value in item.props.items():
        if prop in ignored or prop in write_once:
            continue
        want = canonical(value)
        got = canonical(live.get(prop))
        if want != got:
            changes.append(FieldChange(prop=prop, before=got, after=want))

    # The ownership comment is intent too: a retagged row must be corrected.
    # Skip it where the menu has no comment field (RouterOS would reject the
    # write, and such a section is owned by name, not comment).
    if item.tag and comment_capable:
        got = canonical(live.get("comment"))
        if got != item.tag:
            changes.append(FieldChange(prop="comment", before=got, after=item.tag))

    return changes


def _row_identity(row: dict[str, Any], section: ConfigSection) -> tuple[Any, ...]:
    if not section.key:
        return (str(row.get("comment", "")),)
    return tuple(canonical(row.get(k)) for k in section.key)


def _mask(prop: str, value: str) -> str:
    from app.drivers.redact import SECRET_PROPS
    from app.security import mask

    return mask(value) if prop in SECRET_PROPS else value
