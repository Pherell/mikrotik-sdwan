"""Fabric, membership, and link shapes."""

from __future__ import annotations

from datetime import datetime
from ipaddress import ip_network

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import SiteRole, Topology, Transport


def _valid_cidr(v: str) -> str:
    ip_network(v, strict=False)
    return v


class FabricBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    transport: Transport = Transport.ipsec_gre
    transport_params: dict = Field(default_factory=dict)
    topology: Topology = Topology.hub_spoke_dynamic
    ip_pool: str = "10.255.0.0/16"
    loopback_pool: str = "10.254.0.0/24"
    asn: int = Field(default=65000, ge=1, le=4294967295)
    mtu: int = Field(default=1400, ge=576, le=9000)
    enabled: bool = True

    _check_pool = field_validator("ip_pool")(_valid_cidr)
    _check_loopback_pool = field_validator("loopback_pool")(_valid_cidr)

    @field_validator("ip_pool")
    @classmethod
    def _pool_has_room(cls, v: str) -> str:
        # Every link consumes a /31; a pool that cannot hold one is a typo.
        if ip_network(v, strict=False).prefixlen > 30:
            raise ValueError("ip_pool must be /30 or larger to hold link /31s")
        return v


class FabricCreate(FabricBase):
    member_site_ids: list[str] = Field(default_factory=list)


class FabricUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    transport: Transport | None = None
    transport_params: dict | None = None
    topology: Topology | None = None
    ip_pool: str | None = None
    loopback_pool: str | None = None
    asn: int | None = None
    mtu: int | None = None
    enabled: bool | None = None


class MemberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    site_id: str
    site_name: str = ""
    role_override: SiteRole | None = None
    loopback_ip: str | None = None
    enabled: bool


class MemberCreate(BaseModel):
    site_id: str
    role_override: SiteRole | None = None


class LinkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    fabric_id: str
    slug: str
    a_wan_id: str
    b_wan_id: str
    a_tunnel_ip: str
    b_tunnel_ip: str
    subnet: str
    initiator: str
    dynamic: bool
    enabled: bool
    state: str
    last_error: str | None = None
    # Secrets are never exposed; only whether they exist.
    has_secrets: bool = False


class FabricRead(FabricBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    updated_at: datetime
    members: list[MemberRead] = Field(default_factory=list)
    link_count: int = 0
    pool_capacity: int = 0


class ExpansionRead(BaseModel):
    """What re-expanding the topology would do, or did."""

    created: int
    kept: int
    removed: int
    skipped: int
    # Pairs that cannot be linked, with the reason, so the designer can explain.
    problems: list[dict[str, str]] = Field(default_factory=list)
    affected_site_ids: list[str] = Field(default_factory=list)
