"""Three different things people call "the log".

The word covers the controller's audit trail, an apply's job log, and the
router's own `/log`, which answer three different questions and belong to
three different owners. Keeping them separate in the schema is the cheapest
way to keep them separate on the screen.

Job logs already have a schema in `app.schemas.job`; the other two are here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AuditRead(BaseModel):
    """One thing somebody did to the controller.

    `detail` is whatever the endpoint recorded. It is deliberately untyped:
    the value of an audit trail is that it keeps what happened, not what a
    schema anticipated. Secrets never reach it -- the writers pass names and
    counts, never credentials.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
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
