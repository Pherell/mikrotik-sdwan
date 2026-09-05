"""Enumerations shared across models, schemas, and drivers."""

from enum import StrEnum


class Role(StrEnum):
    admin = "admin"
    operator = "operator"
    viewer = "viewer"


class SiteRole(StrEnum):
    hub = "hub"
    spoke = "spoke"


class DeviceKind(StrEnum):
    """Which driver talks to this device."""

    ros7 = "ros7"       # RouterOS 7.1+, REST
    ros6 = "ros6"       # RouterOS 6.x, SSH
    softhub = "softhub"  # strongSwan + FRR container


class SiteStatus(StrEnum):
    unprovisioned = "unprovisioned"
    reachable = "reachable"
    unreachable = "unreachable"
    drifted = "drifted"
    error = "error"


class Transport(StrEnum):
    ipsec_gre = "ipsec_gre"
    ipsec_policy = "ipsec_policy"
    wireguard = "wireguard"
    gre = "gre"
    ipip = "ipip"
    vxlan = "vxlan"
    eoip = "eoip"


class Topology(StrEnum):
    hub_spoke = "hub_spoke"
    hub_spoke_dynamic = "hub_spoke_dynamic"
    full_mesh = "full_mesh"


class JobKind(StrEnum):
    probe = "probe"
    plan = "plan"
    apply = "apply"
    rollback = "rollback"
    drift_check = "drift_check"


class JobState(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    rolled_back = "rolled_back"
    cancelled = "cancelled"
