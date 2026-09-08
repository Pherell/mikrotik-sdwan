"""What a diagnostic run asks for and what it answers.

Ping and traceroute are the two questions an operator asks before anything
else, and today they are answered by SSHing to the router. That is a shame
for a tool that already holds the credentials.

The target is validated here rather than at the driver, because the REST
driver and the SSH driver fail differently on a hostile value: one sends it
as JSON, the other builds a console line. A single strict rule in front of
both is the only version of this that stays true when a third driver arrives.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

# A hostname label, an IPv4 address, or an IPv6 address. Deliberately narrower
# than what RouterOS accepts: no spaces, no semicolons, no braces, nothing that
# could end a console command and begin another one.
_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:%-]{0,252}[A-Za-z0-9.]$|^[A-Za-z0-9]$")

# Interface and address names go to the device too, and carry the same risk.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")


def _valid_target(value: str) -> str:
    text = value.strip()
    if not _TARGET_RE.match(text):
        raise ValueError(
            "must be a hostname or IP address: letters, digits, dot, colon, "
            "hyphen and underscore only"
        )
    return text


def _valid_name(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if text == "":
        return None
    if not _NAME_RE.match(text):
        raise ValueError("not a valid interface or address")
    return text


DiagTarget = Annotated[str, AfterValidator(_valid_target)]
DiagName = Annotated[str | None, AfterValidator(_valid_name)]


class PingRequest(BaseModel):
    target: DiagTarget
    # Capped low on purpose. This runs inline on an API worker and holds a
    # connection to the device open for its whole duration.
    count: int = Field(default=4, ge=1, le=10)
    # Ping *out of* a specific uplink -- the only way to tell "the internet is
    # down" from "this one uplink is down".
    interface: DiagName = None
    source: DiagName = None


class PingProbe(BaseModel):
    seq: int | None = None
    host: str | None = None
    ttl: int | None = None
    size: int | None = None
    time_ms: float | None = None
    # "" when the probe answered; "timeout", "host unreachable" and friends
    # when it did not. Passed through rather than mapped: RouterOS's wording
    # is more precise than any category we would invent.
    status: str | None = None


class PingResult(BaseModel):
    target: str
    interface: str | None = None
    sent: int
    received: int
    loss_percent: float
    min_ms: float | None = None
    avg_ms: float | None = None
    max_ms: float | None = None
    probes: list[PingProbe] = []


class TracerouteRequest(BaseModel):
    target: DiagTarget
    # RouterOS traceroute runs until stopped, so the controller has to bound
    # it. Seconds of wall clock, not hops.
    seconds: int = Field(default=5, ge=1, le=20)
    interface: DiagName = None


class TraceHop(BaseModel):
    hop: int
    address: str | None = None
    loss_percent: float | None = None
    sent: int | None = None
    last_ms: float | None = None
    avg_ms: float | None = None
    best_ms: float | None = None
    worst_ms: float | None = None
    status: str | None = None


class TracerouteResult(BaseModel):
    target: str
    hops: list[TraceHop] = []


class TunnelHealth(BaseModel):
    """One link, seen from one end.

    Every field is nullable because every one of them can be legitimately
    unknown: a GRE fabric has no IPsec SA, a static-route fabric has no BGP
    session, and a device that has never applied has none of it.
    """

    link_id: str
    fabric_id: str
    fabric_name: str
    slug: str
    peer_site_id: str | None = None
    peer_site_name: str | None = None
    # What the controller intends.
    enabled: bool
    state: str
    last_error: str | None = None
    # What the device reports.
    interface: str | None = None
    interface_running: bool | None = None
    ipsec_established: bool | None = None
    ipsec_detail: str | None = None
    bgp_established: bool | None = None
    bgp_detail: str | None = None
    netwatch_status: str | None = None
    netwatch_loss_percent: float | None = None
    netwatch_latency_ms: float | None = None
    # Populated when the device could not be read at all, so the UI can say
    # "unknown" rather than draw every tunnel as down.
    error: str | None = None
