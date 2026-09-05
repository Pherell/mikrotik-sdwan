"""Type normalization for the RouterOS REST API.

RouterOS encodes *every* JSON value as a string -- numbers and booleans included.
Left alone this produces a permanent false-positive diff: intent says
``mtu=1400`` and ``passive=True``, the device reports ``"1400"`` and ``"true"``,
and the reconciler pushes the same config forever.

Two functions solve it, and they are the only place in the codebase allowed to
reason about RouterOS value encoding:

``coerce``      device string -> native Python, for API responses and the UI.
``canonical``   any value -> the exact string RouterOS would store, for diffing
                and for building request payloads.

The differ compares ``canonical(intent)`` against ``canonical(device)``, so the
two sides can never disagree merely about representation.
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


def encode_payload(props: dict[str, Any]) -> dict[str, str]:
    """Build a REST request body. RouterOS accepts strings for everything."""
    return {k: canonical(v) for k, v in props.items() if v is not None}
