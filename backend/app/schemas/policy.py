"""Policy, SLA profile, and app group shapes."""

from __future__ import annotations

from datetime import datetime
from ipaddress import ip_network

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _prefixes(v: list[str] | None) -> list[str] | None:
    for p in v or []:
        ip_network(p, strict=False)
    return v


class SlaProfileBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    loss_percent: int = Field(default=20, ge=1, le=100)
    latency_ms: int = Field(default=300, ge=1, le=10000)
    jitter_ms: int | None = Field(default=None, ge=1, le=10000)
    probe_interval_seconds: int = Field(default=10, ge=1, le=3600)
    probe_count: int = Field(default=10, ge=1, le=100)
    recovery_seconds: int = Field(default=60, ge=0, le=3600)

    @field_validator("probe_interval_seconds")
    @classmethod
    def _not_too_twitchy(cls, v: int) -> int:
        # Sub-second probing on a WAN edge burns CPU and turns ordinary jitter
        # into a failover. Nothing below 1s is honest.
        if v < 1:
            raise ValueError("probe_interval_seconds must be at least 1")
        return v


class SlaProfileCreate(SlaProfileBase):
    pass


class SlaProfileRead(SlaProfileBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    # Roughly how long a breach takes to be noticed, so the UI can say it.
    detection_seconds: int = 0


class AppGroupBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    prefixes: list[str] = Field(default_factory=list)
    ports: list[int] = Field(default_factory=list)
    protocol: str | None = None
    dscp: int | None = Field(default=None, ge=0, le=63)

    _check_prefixes = field_validator("prefixes")(_prefixes)


class AppGroupCreate(AppGroupBase):
    pass


class AppGroupRead(AppGroupBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    builtin: bool


class PolicyBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    priority: int = Field(default=100, ge=0, le=10000)
    enabled: bool = True
    fabric_id: str | None = None
    site_ids: list[str] = Field(default_factory=list)

    src_prefixes: list[str] = Field(default_factory=list)
    dst_prefixes: list[str] = Field(default_factory=list)
    app_group_id: str | None = None
    protocol: str | None = None
    dst_ports: str | None = None
    dscp: int | None = Field(default=None, ge=0, le=63)

    prefer_tags: list[str] = Field(default_factory=list)
    sla_profile_id: str | None = None
    fallback: str = "any"

    _check_src = field_validator("src_prefixes")(_prefixes)
    _check_dst = field_validator("dst_prefixes")(_prefixes)

    @field_validator("fallback")
    @classmethod
    def _known_fallback(cls, v: str) -> str:
        if v not in {"any", "drop"}:
            raise ValueError("fallback must be 'any' or 'drop'")
        return v

    @field_validator("prefer_tags")
    @classmethod
    def _needs_a_preference(cls, v: list[str]) -> list[str]:
        # A policy with nothing to prefer marks traffic into an empty table.
        if not v:
            raise ValueError("prefer_tags must name at least one uplink tag or WAN name")
        return v


class PolicyCreate(PolicyBase):
    pass


class PolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    priority: int | None = None
    enabled: bool | None = None
    site_ids: list[str] | None = None
    src_prefixes: list[str] | None = None
    dst_prefixes: list[str] | None = None
    app_group_id: str | None = None
    protocol: str | None = None
    dst_ports: str | None = None
    dscp: int | None = None
    prefer_tags: list[str] | None = None
    sla_profile_id: str | None = None
    fallback: str | None = None


class PolicyRead(PolicyBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    updated_at: datetime
