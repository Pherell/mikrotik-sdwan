"""Job and plan shapes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import JobKind, JobState


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
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class ApplyRequest(BaseModel):
    # Refuse to push unless the operator confirms. Restoring a backup reboots
    # the router, so this is not a click-through.
    confirm: bool = False
    dry_run: bool = False
