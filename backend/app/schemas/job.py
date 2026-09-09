"""Job and plan shapes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import JobKind, JobState
from app.schemas.time import UtcDatetime


class PlanSection(BaseModel):
    path: str
    order: int
    lines: list[str]


class PlanRead(BaseModel):
    """A dry-run result. Secrets are already masked by the differ."""

    counts: dict[str, int]
    empty: bool
    unreadable: dict[str, str] = Field(default_factory=dict)
    sections: list[PlanSection] = Field(default_factory=list)
    text: str


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: JobKind
    state: JobState
    site_id: str | None
    fabric_id: str | None
    requested_by: str | None
    plan: dict[str, Any] | None
    diff: dict[str, Any] | None
    result: dict[str, Any] | None
    log: str | None
    error: str | None
    backup_name: str | None
    rollback_token: str | None
    attempts: int
    started_at: UtcDatetime | None
    finished_at: UtcDatetime | None
    created_at: UtcDatetime
    scheduled_for: UtcDatetime | None = None
    window_closes_at: UtcDatetime | None = None


class ApplyRequest(BaseModel):
    # Refuse to push unless the operator confirms. Restoring a backup reboots
    # the router, so this is not a click-through. Required whether the push
    # happens now or is queued for a window: scheduling *is* the approval,
    # so it needs the same confirmation an immediate apply does.
    confirm: bool = False
    dry_run: bool = False
    # Set to queue this apply for a maintenance window instead of pushing it
    # now. Omitted or in the past: applies immediately, exactly as before.
    scheduled_for: UtcDatetime | None = None
    # Optional. Past this instant the window has closed: the sweep marks the
    # job failed rather than push outside the hours it was approved for.
    # Meaningless without scheduled_for; rejected on its own by the endpoint.
    window_closes_at: UtcDatetime | None = None
