"""Position, for menus RouterOS evaluates top to bottom.

A property-only diff reads perfectly clean while the configuration does
nothing: an accept rule appended after the operator's masquerade never matches.
`ConfigOp.place_before` was plumbed through both drivers and set by nothing, so
every create was a bare append.

The anchor here is the operator's own rule, which the controller does not own
and must never touch. It only decides where its own rows sit relative to it.
"""

from __future__ import annotations

from typing import Any

from app.drivers.base import ConfigItem, ConfigSection, OpKind
from app.reconcile.diff import diff_section

MASQUERADE = {
    ".id": "*99",
    "chain": "srcnat",
    "action": "masquerade",
    "out-interface": "ether1",
    "comment": "hand written, not ours",
}


def section(items: list[str], *, before: dict[str, Any] | None = None) -> ConfigSection:
    return ConfigSection(
        path="/ip/firewall/nat",
        owner_tag="sdwan:fw:",
        key=("dst-address",),
        before=before,
        items=[
            ConfigItem(
                props={"chain": "srcnat", "action": "accept", "dst-address": dst},
                tag=f"sdwan:fw:{dst}",
            )
            for dst in items
        ],
    )


def owned(dst: str, item_id: str) -> dict[str, Any]:
    return {
        ".id": item_id,
        "chain": "srcnat",
        "action": "accept",
        "dst-address": dst,
        "comment": f"sdwan:fw:{dst}",
    }


BEFORE_MASQ = {"chain": "srcnat", "action": "masquerade"}


# -- adds ------------------------------------------------------------------


def test_a_new_row_is_created_before_the_anchor() -> None:
    """Without this the rule lands after masquerade and never fires."""
    result = diff_section(section(["203.0.113.1"], before=BEFORE_MASQ), [MASQUERADE])

    adds = [i for i in result.items if i.kind is OpKind.add]
    assert len(adds) == 1
    assert adds[0].place_before == "*99"
    assert result.ops()[0].place_before == "*99"


def test_without_an_anchor_a_new_row_is_appended() -> None:
    """No masquerade on the device: there is nothing to get above."""
    result = diff_section(section(["203.0.113.1"], before=BEFORE_MASQ), [])

    adds = [i for i in result.items if i.kind is OpKind.add]
    assert adds[0].place_before is None


def test_a_section_that_does_not_declare_before_is_untouched() -> None:
    """Every other menu in the app keeps its existing behaviour."""
    result = diff_section(section(["203.0.113.1"]), [MASQUERADE])

    assert [i.kind for i in result.items] == [OpKind.add]
    assert result.items[0].place_before is None


# -- moves -----------------------------------------------------------------


def test_an_existing_row_below_the_anchor_is_moved_up() -> None:
    """An add cannot fix this: the row already exists, in the wrong place."""
    live = [MASQUERADE, owned("203.0.113.1", "*1")]

    result = diff_section(section(["203.0.113.1"], before=BEFORE_MASQ), live)

    moves = [i for i in result.items if i.kind is OpKind.move]
    assert [m.item_id for m in moves] == ["*1"]
    assert moves[0].place_before == "*99"


def test_rows_in_the_wrong_relative_order_are_reseated() -> None:
    live = [owned("b", "*2"), owned("a", "*1"), MASQUERADE]

    result = diff_section(section(["a", "b"], before=BEFORE_MASQ), live)

    moves = [i for i in result.items if i.kind is OpKind.move]
    # Re-seated in section order against the same anchor, which leaves them in
    # that order once applied.
    assert [m.item_id for m in moves] == ["*1", "*2"]
    assert {m.place_before for m in moves} == {"*99"}


def test_correctly_placed_rows_produce_no_moves() -> None:
    """The whole reconciler depends on a second apply being a no-op."""
    live = [owned("a", "*1"), owned("b", "*2"), MASQUERADE]

    result = diff_section(section(["a", "b"], before=BEFORE_MASQ), live)

    assert result.empty, [i.render("/ip/firewall/nat") for i in result.items]


def test_foreign_rows_between_ours_do_not_count_as_disorder() -> None:
    """The controller owns some rows in a shared chain. A hand-written rule
    sitting between two of ours is not ours to move."""
    live = [
        owned("a", "*1"),
        {".id": "*5", "chain": "srcnat", "action": "accept", "comment": "theirs"},
        owned("b", "*2"),
        MASQUERADE,
    ]

    result = diff_section(section(["a", "b"], before=BEFORE_MASQ), live)

    assert result.empty


def test_the_anchor_row_is_never_touched() -> None:
    """It is not ours. Removing or editing it would be the exact failure the
    ownership model exists to prevent."""
    live = [MASQUERADE, owned("a", "*1")]

    result = diff_section(section(["a"], before=BEFORE_MASQ), live)

    assert all(i.item_id != "*99" for i in result.items)
    assert all(i.kind is not OpKind.remove for i in result.items)


def test_an_owned_row_is_not_mistaken_for_the_anchor() -> None:
    """If the controller ever manages a masquerade rule of its own, the anchor
    must still be the operator's -- otherwise it tries to sit before itself."""
    ours_masq = {
        ".id": "*7",
        "chain": "srcnat",
        "action": "masquerade",
        "comment": "sdwan:fw:masq",
    }
    live = [ours_masq, MASQUERADE, owned("a", "*1")]

    result = diff_section(section(["a"], before=BEFORE_MASQ), live)

    moves = [i for i in result.items if i.kind is OpKind.move]
    assert [m.place_before for m in moves] == ["*99"]


# -- op ordering within the section ----------------------------------------


def test_moves_run_after_creates_and_before_removals() -> None:
    """A freshly added row has to exist before it can be positioned, and
    nothing should be positioned relative to a row about to be deleted."""
    live = [MASQUERADE, owned("a", "*1"), owned("gone", "*3")]

    result = diff_section(section(["a", "b"], before=BEFORE_MASQ), live)
    kinds = [op.kind for op in result.ops()]

    assert OpKind.add in kinds and OpKind.move in kinds and OpKind.remove in kinds
    assert kinds.index(OpKind.add) < kinds.index(OpKind.move)
    assert kinds.index(OpKind.move) < kinds.index(OpKind.remove)


def test_a_move_renders_as_a_readable_line() -> None:
    live = [MASQUERADE, owned("a", "*1")]

    result = diff_section(section(["a"], before=BEFORE_MASQ), live)
    lines = result.render()

    assert any(line.startswith(">") and "move" in line for line in lines), lines
