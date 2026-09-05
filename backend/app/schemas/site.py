"""Request and response shapes for sites and WAN uplinks.

Credentials are write-only: they enter through Create/Update and never appear in
a response.
"""

from __future__ import annotations

from datetime import datetime
from ipaddress import ip_address, ip_network

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import DeviceKind, SiteRole, SiteStatus


def _valid_ip(v: str | None) -> str | None:
    if v in (None, ""):
        return None
    ip_address(v)  # raises ValueError, which Pydantic reports as a 422
    return v


class WanBase(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    interface: str = Field(min_length=1, max_length=64)
    public_ip: str | None = None
    dynamic: bool = False
    nat_behind: bool = False
    gateway: str | None = None
    provider: str | None = None
    bandwidth_mbps: int | None = Field(default=None, ge=1)
    cost: float = 1.0
    enabled: bool = True
    tags: dict[str, str] = Field(default_factory=dict)

    _check_public_ip = field_validator("public_ip")(_valid_ip)
    _check_gateway = field_validator("gateway")(_valid_ip)


class WanCreate(WanBase):
    pass


class WanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    interface: str | None = None
    public_ip: str | None = None
    dynamic: bool | None = None
    nat_behind: bool | None = None
    gateway: str | None = None
    provider: str | None = None
    bandwidth_mbps: int | None = None
    cost: float | None = None
    enabled: bool | None = None
    tags: dict[str, str] | None = None


class WanRead(WanBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    site_id: str
    dial_out_only: bool


class SiteBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    region: str | None = None
    role: SiteRole = SiteRole.spoke
    mgmt_host: str = Field(min_length=1, max_length=255)
    mgmt_port: int | None = Field(default=None, ge=1, le=65535)
    device_kind: DeviceKind = DeviceKind.ros7
    username: str = Field(min_length=1, max_length=128)
    verify_tls: bool = False
    loopback_ip: str | None = None
    local_prefixes: list[str] = Field(default_factory=list)
    rollback_timeout_seconds: int | None = Field(default=None, ge=30, le=3600)
    drift_action: str = "alert"
    tags: dict[str, str] = Field(default_factory=dict)

    _check_loopback = field_validator("loopback_ip")(_valid_ip)

    @field_validator("local_prefixes")
    @classmethod
    def _check_prefixes(cls, v: list[str]) -> list[str]:
        for p in v:
            ip_network(p, strict=False)
        return v

    @field_validator("drift_action")
    @classmethod
    def _check_drift(cls, v: str) -> str:
        if v not in {"alert", "auto-remediate"}:
            raise ValueError("drift_action must be 'alert' or 'auto-remediate'")
        return v


class SiteCreate(SiteBase):
    password: str | None = Field(default=None, repr=False)
    ssh_key: str | None = Field(default=None, repr=False)
    wans: list[WanCreate] = Field(default_factory=list)


class SiteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    region: str | None = None
    role: SiteRole | None = None
    mgmt_host: str | None = None
    mgmt_port: int | None = None
    device_kind: DeviceKind | None = None
    username: str | None = None
    password: str | None = Field(default=None, repr=False)
    ssh_key: str | None = Field(default=None, repr=False)
    verify_tls: bool | None = None
    loopback_ip: str | None = None
    local_prefixes: list[str] | None = None
    rollback_timeout_seconds: int | None = None
    drift_action: str | None = None
    tags: dict[str, str] | None = None


class SiteRead(SiteBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: SiteStatus
    ros_version: str | None = None
    board_name: str | None = None
    architecture: str | None = None
    identity: str | None = None
    capabilities: dict | None = None
    last_seen_at: str | None = None
    last_error: str | None = None
    has_credentials: bool = False
    created_at: datetime
    updated_at: datetime
    wans: list[WanRead] = Field(default_factory=list)


class ProbeResult(BaseModel):
    """What the onboarding wizard shows after touching a device."""

    reachable: bool
    error: str | None = None
    version: str | None = None
    board_name: str | None = None
    architecture: str | None = None
    identity: str | None = None
    ros_major: int | None = None
    has_wireguard: bool = False
    has_container: bool = False
    has_netwatch_thresholds: bool = False
    packages: list[str] = Field(default_factory=list)
    # Interfaces that look like uplinks, offered as WAN candidates in the wizard.
    suggested_wans: list[WanCreate] = Field(default_factory=list)
