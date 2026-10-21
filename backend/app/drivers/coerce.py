"""Type normalization for the RouterOS REST API.

RouterOS encodes *every* JSON value as a string -- numbers and booleans included.
Left alone this produces a permanent false-positive diff: intent says
``mtu=1400`` and ``passive=True``, the device reports ``"1400"`` and ``"true"``,
and the reconciler pushes the same config forever.

Three functions solve it, and they are the only place in the codebase allowed
to reason about RouterOS value encoding:

``coerce``      device string -> native Python, for API responses and the UI.
``canonical``   any value -> the exact string RouterOS would store; the payload
                encoder for writes.
``canonical_compare``
                any value -> comparison key for diffing. Starts from
                ``canonical`` and additionally folds unit *forms* of the same
                quantity together (durations: ``90s`` == ``1m30s`` == ``90``).

The differ compares ``canonical_compare(intent)`` against
``canonical_compare(device)``, so the two sides can never disagree merely
about representation. Unit folding is compare-only, deliberately: a write
must carry the authored form (``interval=90s``), not the folded key.
"""

from __future__ import annotations

import re
from typing import Any

# RouterOS booleans over REST are exactly "true" and "false".
#
# "yes" and "no" are deliberately NOT treated as booleans. They are members of
# several RouterOS enums -- most importantly ``generate-policy`` on an IPsec
# identity, whose values are no / port-override / port-strict. Coercing "no" to
# False and canonicalising it back to "false" makes that property differ from
# intent on every run, and the reconciler pushes it forever.
_TRUE = frozenset({"true"})
_FALSE = frozenset({"false"})
_INT_RE = re.compile(r"^-?\d+$")

# Properties that look numeric but must stay strings. Coercing these corrupts
# them: a leading zero is significant in a VLAN tag list, and "7.14" is a
# version, not a float.
_ALWAYS_STRING = frozenset(
    {
        "version",
        "name",
        "comment",
        "address",
        "network",
        "gateway",
        "src-address",
        "dst-address",
        "local-address",
        "remote-address",
        "interface",
        "routing-mark",
        "routing-table",
        ".id",
        ".nextid",
    }
)


def coerce(value: Any, prop: str = "") -> Any:
    """Device representation -> native Python."""
    if not isinstance(value, str):
        return value
    if prop in _ALWAYS_STRING:
        return value
    low = value.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    if _INT_RE.match(value):
        # Reject values with a meaningful leading zero.
        if len(value) > 1 and value.lstrip("-").startswith("0"):
            return value
        return int(value)
    return value


def coerce_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: coerce(v, k) for k, v in row.items()}


def canonical(value: Any) -> str:
    """Any value -> the exact string RouterOS stores.

    This is the comparison key for diffing and the payload encoder for writes.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        # RouterOS renders multi-value properties comma-separated. Order is
        # significant for some menus (proposals) and not for others (address
        # lists); preserve caller order rather than sorting.
        return ",".join(canonical(v) for v in value)
    return str(value)


def canonical_row(row: dict[str, Any], ignore: frozenset[str] = frozenset()) -> dict[str, str]:
    return {k: canonical(v) for k, v in row.items() if k not in ignore}


# -- durations ---------------------------------------------------------------
#
# RouterOS re-renders durations in its own compound form: 90 seconds comes
# back as "1m30s", 28800 seconds as "8h", a plain count of seconds as "90s".
# String comparison sees a difference that is not one, and the reconciler
# pushes the same value on every run -- the same phantom-drift class the
# scalar coercion above exists to kill, one level up. Duration-typed
# properties are therefore compared by total length, not by spelling.
#
# The property set is deliberately limited to durations this controller
# actually renders (netwatch thresholds, BGP timers, IPsec lifetimes, tunnel
# keepalives). Folding units for *every* property would be wrong: most
# numeric-looking strings are not quantities with interchangeable spellings.

_DURATION_PROPS = frozenset(
    {
        "interval",
        "timeout",
        "hold-time",
        "keepalive-time",
        "lifetime",
        "dpd-interval",
        "ra-lifetime",
        "preferred-lifetime",
        "thr-avg",
        "thr-max",
        "thr-jitter",
        "thr-stdev",
        "thr-http-time",
        "thr-tcp-conn-time",
    }
)

# "<duration>,<count>" -- the GRE/IPIP tunnel keepalive shape. Only the head
# is a duration; the tail is a retry count and compares as written.
_COMPOUND_DURATION_PROPS = frozenset({"keepalive"})

_UNIT_NS = {
    "w": 7 * 24 * 3600 * 1_000_000_000,
    "d": 24 * 3600 * 1_000_000_000,
    "h": 3600 * 1_000_000_000,
    "m": 60 * 1_000_000_000,
    "s": 1_000_000_000,
    "ms": 1_000_000,
    "us": 1_000,
    "ns": 1,
}
_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)(w|d|h|ms|m|s|us|ns)")
_CLOCK_RE = re.compile(r"^(\d+):([0-5]\d):([0-5]\d)(?:\.(\d{1,6}))?$")
_BARE_SECONDS_RE = re.compile(r"^\d+(?:\.\d+)?$")


def parse_duration_ns(value: Any) -> int | None:
    """Total nanoseconds of a RouterOS duration, or None if it is not one.

    Accepts the forms RouterOS writes and returns: bare numbers (seconds),
    compound token strings ("1m30s", "8h", "150ms", optionally spaced), and
    the clock form ("00:01:30"). Anything else returns None so the caller
    falls back to plain string comparison -- an unparseable value must never
    silently compare equal to another unparseable value.
    """
    if isinstance(value, bool):
        return None  # bool is an int subclass, but never a duration
    if isinstance(value, (int, float)):
        return int(round(value * 1_000_000_000))
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    clock = _CLOCK_RE.match(text)
    if clock:
        hours, minutes, seconds = (int(clock.group(i)) for i in (1, 2, 3))
        micros = int((clock.group(4) or "0").ljust(6, "0"))
        return ((hours * 3600 + minutes * 60 + seconds) * 1_000_000 + micros) * 1_000
    if _BARE_SECONDS_RE.match(text):
        return int(round(float(text) * 1_000_000_000))

    total = 0.0
    pos = 0
    matched = False
    while pos < len(text):
        if text[pos] == " ":
            pos += 1
            continue
        token = _TOKEN_RE.match(text, pos)
        if token is None:
            return None
        total += float(token.group(1)) * _UNIT_NS[token.group(2)]
        pos = token.end()
        matched = True
    return int(round(total)) if matched else None


def canonical_compare(value: Any, prop: str = "") -> str:
    """Comparison key for diffing: ``canonical`` plus unit-form folding.

    Only the properties named in _DURATION_PROPS (and the duration head of
    _COMPOUND_DURATION_PROPS) are folded, and only when the value parses as
    a duration on that side; each side folds independently, so "90s" of
    intent meets "1m30s" from the device at the same key. Everything else is
    exactly ``canonical(value)``.
    """
    if prop in _DURATION_PROPS:
        ns = parse_duration_ns(value)
        if ns is not None:
            return f"duration:{ns}"
    elif prop in _COMPOUND_DURATION_PROPS and isinstance(value, str) and "," in value:
        head, _, rest = value.partition(",")
        ns = parse_duration_ns(head)
        if ns is not None:
            return f"duration:{ns},{rest.strip()}"
    return canonical(value)


def encode_payload(props: dict[str, Any]) -> dict[str, str]:
    """Build a REST request body. RouterOS accepts strings for everything."""
    return {k: canonical(v) for k, v in props.items() if v is not None}
