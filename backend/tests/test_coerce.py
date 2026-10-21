"""The RouterOS string-typing normalization.

If these break, every diff in the system produces false positives.
"""

from __future__ import annotations

import pytest

from app.drivers.coerce import (
    canonical,
    canonical_compare,
    canonical_row,
    coerce,
    coerce_row,
    encode_payload,
    parse_duration_ns,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("false", False),
        ("1400", 1400),
        ("-1", -1),
        ("0", 0),
        ("", ""),
        ("ether1", "ether1"),
        ("7.14.3", "7.14.3"),
    ],
)
def test_coerce_scalars(raw: str, expected: object) -> None:
    assert coerce(raw) == expected
    assert type(coerce(raw)) is type(expected)


def test_yes_and_no_are_not_booleans() -> None:
    """RouterOS booleans are true/false. "no" is an enum member -- it is the
    first value of generate-policy (no | port-override | port-strict), and
    coercing it to False makes that property diff dirty forever."""
    assert coerce("no") == "no"
    assert coerce("yes") == "yes"
    assert canonical(coerce("no")) == "no"


def test_coerce_leaves_identity_props_alone() -> None:
    # A version is not a float and an address is not a number.
    assert coerce("7.14", "version") == "7.14"
    assert coerce("10", "name") == "10"
    assert coerce("*1", ".id") == "*1"


def test_coerce_keeps_significant_leading_zero() -> None:
    assert coerce("0012") == "0012"


def test_canonical_round_trips_through_coerce() -> None:
    """The property that makes diffing safe: for any device string, encoding the
    coerced value reproduces the original."""
    for raw in ["true", "false", "1400", "0", "-7", "ether1", "203.0.113.1", ""]:
        assert canonical(coerce(raw)) == raw


def test_canonical_normalizes_python_types() -> None:
    assert canonical(True) == "true"
    assert canonical(False) == "false"
    assert canonical(1400) == "1400"
    assert canonical(None) == ""
    assert canonical(["aes-256-cbc", "sha256"]) == "aes-256-cbc,sha256"


def test_intent_and_device_compare_equal_after_canonicalization() -> None:
    """This is the whole point: native intent and stringy device state must
    produce identical comparison keys."""
    intent = {"mtu": 1400, "passive": True, "name": "gre-hub1", "disabled": False}
    from_device = {"mtu": "1400", "passive": "true", "name": "gre-hub1", "disabled": "false"}

    assert canonical_row(intent) == canonical_row(coerce_row(from_device))


def test_encode_payload_drops_none_and_stringifies() -> None:
    assert encode_payload({"mtu": 1400, "passive": True, "comment": None}) == {
        "mtu": "1400",
        "passive": "true",
    }


# -- duration folding (canonical_compare) ------------------------------------
#
# RouterOS re-renders durations in compound form. These pairs are the same
# quantity spelled two ways; the differ must see one value, or the
# reconciler re-pushes it forever.

@pytest.mark.parametrize(
    ("intent", "device", "prop"),
    [
        ("90s", "1m30s", "interval"),
        (90, "1m30s", "interval"),
        ("90", "1m30s", "interval"),
        ("8h", "28800s", "lifetime"),
        ("120s", "2m", "dpd-interval"),
        ("30s", "00:00:30", "hold-time"),
        ("150ms", "150ms", "thr-avg"),
        ("1m,3", "60s,3", "keepalive"),
        ("10s,3", "10s,3", "keepalive"),
    ],
)
def test_canonical_compare_folds_duration_spellings(intent, device, prop) -> None:
    assert canonical_compare(intent, prop) == canonical_compare(device, prop)


@pytest.mark.parametrize(
    ("intent", "device", "prop"),
    [
        ("90s", "91s", "interval"),          # genuinely different durations
        ("10s,3", "10s,4", "keepalive"),     # retry count still compares raw
        ("25M", "25000000", "max-limit"),    # not a duration prop: no folding
        ("auto", "auto2", "interval"),      # unparseable: plain string compare
    ],
)
def test_canonical_compare_keeps_real_differences(intent, device, prop) -> None:
    assert canonical_compare(intent, prop) != canonical_compare(device, prop)


def test_parse_duration_ns_forms() -> None:
    assert parse_duration_ns("1m30s") == 90_000_000_000
    assert parse_duration_ns("150ms") == 150_000_000
    assert parse_duration_ns("1w") == 604_800_000_000_000
    assert parse_duration_ns("00:00:30") == 30_000_000_000
    assert parse_duration_ns(True) is None
    assert parse_duration_ns("ether1") is None
    assert parse_duration_ns("") is None


def test_encode_payload_keeps_the_authored_duration_form() -> None:
    """Folding is compare-only: a write must carry intent's spelling."""
    assert encode_payload({"interval": "90s"}) == {"interval": "90s"}
