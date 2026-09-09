"""Policy, SLA profile, and app group shapes."""

from __future__ import annotations

import re
from ipaddress import ip_network

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.time import UtcDatetime


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
    created_at: UtcDatetime
    # Roughly how long a breach takes to be noticed, so the UI can say it.
    detection_seconds: int = 0


# A tls-host pattern: hostname characters plus the single leading "*." a
# glob needs. Rejects anything that could break out of the RouterOS
# property it is rendered into -- the same reasoning PingRequest's target
# validator gives for a console command line.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_SNI_PATTERN = re.compile(rf"^(\*\.)?{_LABEL}(\.{_LABEL})*$")


class AppGroupBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    prefixes: list[str] = Field(default_factory=list)
    ports: list[int] = Field(default_factory=list)
    protocol: str | None = None
    dscp: int | None = Field(default=None, ge=0, le=63)
    sni_patterns: list[str] = Field(default_factory=list)

    _check_prefixes = field_validator("prefixes")(_prefixes)

    @field_validator("sni_patterns")
    @classmethod
    def _check_sni_patterns(cls, v: list[str]) -> list[str]:
        for pattern in v:
            if not _SNI_PATTERN.match(pattern):
                raise ValueError(
                    f"{pattern!r} is not a valid SNI pattern -- a hostname, "
                    "optionally with one leading '*.'"
                )
        return v


class AppGroupCreate(AppGroupBase):
    pass


class AppGroupRead(AppGroupBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    builtin: bool


MAX_MEMBERS = 8

# failover: the first healthy member wins; weights are meaningless.
# load_balance: connections are spread across members in proportion to their
# weights, via PCC. Connections, not packets -- see app.render.policy.
STRATEGIES = frozenset({"failover", "load_balance"})


class GroupMember(BaseModel):
    """One uplink's place in a group."""

    model_config = ConfigDict(extra="forbid")

    # A WAN tag or a WAN name. Tags let one group serve devices whose uplinks
    # are wired differently.
    uplink: str = Field(min_length=1, max_length=64)
    # Only meaningful under load_balance, where it is a share of the
    # connections rather than of the bandwidth. Rejected as misleading if
    # someone sets it to something other than 1 under failover, where the
    # order of the uplinks is the whole preference.
    weight: int = Field(default=1, ge=1, le=100)


class SdwanGroupBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    members: list[GroupMember] = Field(default_factory=list)
    strategy: str = "failover"
    sla_profile_id: str | None = None

    @field_validator("members")
    @classmethod
    def _members(cls, v: list[GroupMember]) -> list[GroupMember]:
        if not v:
            raise ValueError("a group needs at least one uplink")
        if len(v) > MAX_MEMBERS:
            raise ValueError(f"a group holds at most {MAX_MEMBERS} uplinks")
        names = [m.uplink for m in v]
        if len(names) != len(set(names)):
            raise ValueError("the same uplink cannot appear twice in a group")
        return v

    @field_validator("strategy")
    @classmethod
    def _strategy(cls, v: str) -> str:
        if v not in STRATEGIES:
            raise ValueError(f"strategy must be one of: {', '.join(sorted(STRATEGIES))}")
        return v

    @model_validator(mode="after")
    def _weights_do_nothing_under_failover(self) -> SdwanGroupBase:
        if self.strategy == "failover" and any(m.weight != 1 for m in self.members):
            raise ValueError(
                "weights only apply to load_balance. Under failover the order "
                "of the uplinks is the preference; a weight here would be a "
                "number that does nothing."
            )
        if self.strategy == "load_balance" and len(self.members) < 2:
            raise ValueError(
                "load_balance needs at least two uplinks to spread across. "
                "With one, use failover."
            )
        return self


class SdwanGroupCreate(SdwanGroupBase):
    pass


class SdwanGroupUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    members: list[GroupMember] | None = None
    strategy: str | None = None
    sla_profile_id: str | None = None

    @field_validator("members")
    @classmethod
    def _members(cls, v: list[GroupMember] | None) -> list[GroupMember] | None:
        return v if v is None else SdwanGroupBase._members(v)

    @field_validator("strategy")
    @classmethod
    def _strategy(cls, v: str | None) -> str | None:
        return v if v is None else SdwanGroupBase._strategy(v)


class SdwanGroupRead(SdwanGroupBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: UtcDatetime
    updated_at: UtcDatetime


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

    # Which uplinks and how healthy: named once as a group, pointed at here.
    sdwan_group_id: str | None = None
    fallback: str = "any"

    _check_src = field_validator("src_prefixes")(_prefixes)
    _check_dst = field_validator("dst_prefixes")(_prefixes)

    @field_validator("fallback")
    @classmethod
    def _known_fallback(cls, v: str) -> str:
        if v not in {"any", "drop"}:
            raise ValueError("fallback must be 'any' or 'drop'")
        return v




class PolicyCreate(PolicyBase):
    # Required, not merely validated: a field validator does not run when the
    # field is absent, so a rule posted without one sailed through. Making it
    # required also puts it in the OpenAPI schema as required, which is where
    # anyone integrating will look.
    #
    # Deliberately overridden here rather than on PolicyBase: PolicyRead
    # inherits that, and an output schema which rejects stored data turns a
    # plain GET into a 500.
    sdwan_group_id: str = Field(
        min_length=1,
        description=(
            "The SD-WAN group this rule uses: which uplinks the traffic should "
            "take, in what order, and how healthy they must be."
        ),
    )


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
    sdwan_group_id: str | None = None
    fallback: str | None = None


class PolicyRead(PolicyBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: UtcDatetime
    updated_at: UtcDatetime
