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


@dataclass(slots=True)
class SectionDiff:
    path: str
    owner_tag: str
    items: list[ItemDiff] = field(default_factory=list)
    order: int = 50

    @property
    def empty(self) -> bool:
        return not self.items

    def ops(self) -> list[ConfigOp]:
        """Mutations for this section, adds and updates before removals.

        Removing last matters when a rename is expressed as add+remove: the new
        row must exist before the old one goes away, or the device spends a
        window with neither.
        """
        creates = [i for i in self.items if i.kind is not OpKind.remove]
        deletes = [i for i in self.items if i.kind is OpKind.remove]
        return [self._op(i) for i in creates + deletes]

    def _op(self, item: ItemDiff) -> ConfigOp:
        return ConfigOp(
            kind=item.kind,
            path=self.path,
            props=item.props,
            item_id=item.item_id,
            comment=item.tag,
        )

    def render(self) -> list[str]:
        return [i.render(self.path) for i in self.items]


def diff_section(section: ConfigSection, live_rows: list[dict[str, Any]]) -> SectionDiff:
    """Compare one rendered section against what the device currently holds."""
    result = SectionDiff(path=section.path, owner_tag=section.owner_tag, order=section.order)
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
            props["comment"] = item.tag or section.owner_tag
            result.items.append(
                ItemDiff(kind=OpKind.add, identity=identity, tag=item.tag, props=props)
            )
            continue

        changes = _compare(item, live, ignored, section.write_once)
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

    return result


def _compare(
    item: ConfigItem,
    live: dict[str, Any],
    ignored: set[str],
    write_once: tuple[str, ...],
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
    if item.tag:
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
