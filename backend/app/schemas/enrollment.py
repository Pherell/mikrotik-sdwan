"""Enrollment token shapes."""

from __future__ import annotations

from ipaddress import ip_network

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import SiteRole
from app.schemas.time import UtcDatetime


class EnrollmentTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    site_name: str = Field(min_length=1, max_length=128)
    site_role: SiteRole = SiteRole.spoke
    local_prefixes: list[str] = Field(default_factory=list)
    # Join this fabric automatically once the device enrolls and probes
    # reachable. Omit to onboard the site alone, unattached to any overlay.
    fabric_id: str | None = None
    source_cidr: str | None = None
    expires_in_hours: int = Field(default=24, ge=1, le=24 * 30)

    @field_validator("local_prefixes")
    @classmethod
    def _check_prefixes(cls, v: list[str]) -> list[str]:
        for p in v:
            ip_network(p, strict=False)
        return v

    @field_validator("source_cidr")
    @classmethod
    def _check_cidr(cls, v: str | None) -> str | None:
        if v is not None:
            ip_network(v, strict=False)
        return v


class EnrollmentTokenRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    site_name: str
    site_role: SiteRole
    fabric_id: str | None
    source_cidr: str | None
    expires_at: UtcDatetime
    used_at: UtcDatetime | None
    used_from_ip: str | None
    revoked_at: UtcDatetime | None
    enrolled_site_id: str | None
    created_at: UtcDatetime


class EnrollmentTokenCreated(EnrollmentTokenRead):
    """The one response that carries the secret. Shown once, like an API
    token: the row afterward holds only what identifies it, never this."""

    # The line an operator pastes into WinBox or a serial console. Building
    # it here rather than in the UI means there is exactly one place that
    # knows the fetch syntax the bootstrap script actually needs.
    enroll_command: str
