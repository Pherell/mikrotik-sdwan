"""Overlay transports.

Importing this package registers every built-in driver, so ``get_transport``
can resolve a name straight from a Fabric row.
"""

from app.transports import (
    ipsec_gre,  # noqa: F401 -- registration side effect
    l2,  # noqa: F401
    plain,  # noqa: F401
    wireguard,  # noqa: F401
)
from app.transports.base import (
    Endpoint,
    FabricView,
    LinkView,
    TransportDriver,
    TransportError,
    available,
    choose_initiator,
    get_transport,
    validate_pair,
)

__all__ = [
    "Endpoint",
    "FabricView",
    "LinkView",
    "TransportDriver",
    "TransportError",
    "available",
    "choose_initiator",
    "get_transport",
    "validate_pair",
]
