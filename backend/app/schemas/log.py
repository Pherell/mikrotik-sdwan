"""Three different things people call "the log".

The word covers the controller's audit trail, an apply's job log, and the
router's own `/log`, which answer three different questions and belong to
three different owners. Keeping them separate in the schema is the cheapest
way to keep them separate on the screen.

Job logs already have a schema in `app.schemas.job`; the other two are here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.time import UtcDatetime


class AuditRead(BaseModel):
    """One thing somebody did to the controller.

    `detail` is whatever the endpoint recorded. It is deliberately untyped:
    the value of an audit trail is that it keeps what happened, not what a
    schema anticipated. Secrets never reach it -- the writers pass names and
    counts, never credentials.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: UtcDatetime
    actor_id: str | None = None
    actor_email: str | None = None
    action: str
    object_type: str | None = None
    object_id: str | None = None
    detail: dict[str, Any] | None = None
    source_ip: str | None = None


class DeviceLogEntry(BaseModel):
    """One line of RouterOS's own log.

    RouterOS keeps this in memory by default, so it is short, it resets on
    reboot, and it is the only place that says *why* the device did something
    -- an IPsec proposal mismatch appears here and nowhere else.
    """

    time: str | None = None
    # Comma-separated on the device ("ipsec,error"), split here because the
    # UI filters on them and splitting in three components is three bugs.
    topics: list[str] = []
    message: str
    # True when any topic is an error/critical/warning topic, so the UI does
    # not have to know RouterOS's topic vocabulary.
    severity: str = "info"


class ConsoleRequest(BaseModel):
    """One RouterOS command, as typed."""

    command: str = Field(min_length=1, max_length=512)


class ConsoleResponse(BaseModel):
    command: str
    # What the console actually ran, after parsing. Shown back because a
    # console that will not say what it ran is asking to be trusted for no
    # reason -- and because "/ip route" silently becoming "/ip/route/print"
    # should be visible rather than surprising.
    resolved: str
    rows: list[dict[str, Any]] = []
    # Set when the device answered with a failure. Distinct from a 4xx, which
    # means the controller refused before the device was asked.
    error: str | None = None
