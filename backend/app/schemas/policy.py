"""Policy, SLA profile, and app group shapes."""

from __future__ import annotations

import re
from ipaddress import ip_address, ip_network
from typing import Literal

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
    # How traffic leaves over this uplink.
    #
    # overlay (the default, and the only behaviour before this field existed):
    #   through the fabric tunnels this WAN carries, to the hub or peer --
    #   encrypted, and subject to whatever the far end does with it.
    # direct: local internet breakout. Straight out this WAN's own gateway and
    #   NATed by the uplink's masquerade rule, never touching the hub. This is
    #   what SaaS traffic (Microsoft 365, video calls) wants: hairpinning it
    #   through a hub only adds latency and burns the hub's bandwidth.
    #
    # A group may mix the two -- "direct on fibre first, then overlay via the
    # hub" is the usual shape -- and the same uplink may appear once per mode.
    # Stored inside the members JSON column, so this needs no migration; rows
    # written before it existed have no key and read back as overlay.
    via: Literal["overlay", "direct"] = "overlay"
    # The internet address netwatch probes to judge a *direct* path. Optional:
    # each WAN gets a default from app.render.policy.DEFAULT_PROBE_TARGETS.
    # It must be distinct per WAN at a site, because RouterOS netwatch cannot
    # pick a routing table: the probe is pinned out its WAN by a /32 host route
    # in main, and one address can only be pinned to one WAN. A clash is
    # refused at render time. Meaningless for overlay members, which probe the
    # tunnel's far end, so it is rejected there rather than silently ignored.
    probe_target: str | None = None

    @field_validator("probe_target")
    @classmethod
    def _probe_is_a_host(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            address = ip_address(v)
        except ValueError as exc:
            raise ValueError(f"{v!r} is not an IP address") from exc
        if address.version != 4:
            # Every policy route this renders is IPv4 (0.0.0.0/0); an IPv6
            # probe would be pinned by a route in a family nothing else uses.
            raise ValueError("probe_target must be an IPv4 address")
        if address.is_private or address.is_loopback or address.is_unspecified:
            # The point is to prove the *internet* is reachable out this WAN. A
            # private target proves only the CPE is up -- the weak check this
            # field exists to replace -- and pinning one could steal a LAN or
            # overlay address into main.
            raise ValueError("probe_target must be a public internet address")
        return str(address)

    @model_validator(mode="after")
    def _probe_only_for_direct(self) -> GroupMember:
        if self.probe_target is not None and self.via != "direct":
            raise ValueError(
                "probe_target only applies to via='direct'. An overlay member "
                "is probed at the tunnel's far end."
            )
        return self


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
        # Unique per (uplink, via), not per uplink: "fibre direct, then fibre
        # through the hub" is two different paths over one wire, and a
        # legitimate failover order. The same pair twice is still nonsense.
        names = [(m.uplink, m.via) for m in v]
        if len(names) != len(set(names)):
            raise ValueError("the same uplink cannot appear twice in a group")
        return v

    @field_validator("strategy")
    @classmethod
    def _strategy(cls, v: str) -> str:
        if v not in STRATEGIES:
            raise ValueError(f"strategy must be one of: {', '.join(sorted(STRATEGIES))}")
        return v

    # NOTE: business-rule cross-field checks (weights vs strategy, member
    # count vs strategy) live on SdwanGroupCreate / SdwanGroupUpdate, NOT
    # here. SdwanGroupRead inherits this base, and a validator that rejects
    # stored data turns a plain GET into a 500 -- the same reasoning that
    # moved PolicyCreate.sdwan_group_id out of PolicyBase. Keep this base
    # to structural checks only (uniqueness, length bounds).


class SdwanGroupCreate(SdwanGroupBase):
    @model_validator(mode="after")
    def _strategy_constraints(self) -> SdwanGroupCreate:
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

    @model_validator(mode="after")
    def _strategy_constraints(self) -> SdwanGroupUpdate:
        """Cross-field check only when both fields are present in this request."""
        strategy = self.strategy
        members = self.members
        if strategy is None or members is None:
            # Partial update: can't validate the combo without the stored value.
            # The constraint will have been enforced on the original create, so
            # single-field updates (change strategy alone, or members alone) are
            # safe to pass through here.
            return self
        if strategy == "failover" and any(m.weight != 1 for m in members):
            raise ValueError(
                "weights only apply to load_balance. Under failover the order "
                "of the uplinks is the preference; a weight here would be a "
                "number that does nothing."
            )
        if strategy == "load_balance" and len(members) < 2:
            raise ValueError(
                "load_balance needs at least two uplinks to spread across. "
                "With one, use failover."
            )
        return self


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
