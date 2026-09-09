"""Traffic steering policies and the SLA profiles they reference.

A policy says: traffic matching *this* should prefer *these* uplinks, and should
move off one when it stops meeting *that* SLA.
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Tenanted, Timestamps, UUIDPk
from app.models.site import JSONCol


class SlaProfile(Base, UUIDPk, Timestamps, Tenanted):
    """Thresholds that decide whether a path is usable.

    Defaults are tuned for a voice-tolerable path. Tightening them speeds up
    detection and increases both router load and the chance of flapping on a
    merely twitchy link -- hence the hold-down timers.
    """

    __tablename__ = "sla_profiles"

    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)

    loss_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    jitter_ms: Mapped[int | None] = mapped_column(Integer)

    # Netwatch sends this many probes per cycle. Detection takes roughly
    # interval x (probes to breach the threshold), so 10s x 10 is ~10-15s.
    probe_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    probe_count: Mapped[int] = mapped_column(Integer, nullable=False, default=10)

    # How long a path must stay good before traffic returns to it. Without this
    # a flapping link drags traffic back and forth.
    recovery_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_sla_tenant_name"),)


class AppGroup(Base, UUIDPk, Timestamps, Tenanted):
    """A named set of prefixes and ports standing in for an application.

    RouterOS has no usable L7 classifier, so "Teams" here means a prefix list
    and some ports, refreshed from a feed. The UI says so explicitly: this is
    prefix matching, not DPI, and it will miss traffic that moves to a new
    range before the feed catches up.
    """

    __tablename__ = "app_groups"

    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    prefixes: Mapped[list | None] = mapped_column(JSONCol, default=list)
    ports: Mapped[list | None] = mapped_column(JSONCol, default=list)
    protocol: Mapped[str | None] = mapped_column(String(16))
    dscp: Mapped[int | None] = mapped_column(Integer)
    # TLS SNI glob patterns ("*.teams.microsoft.com"), matched with RouterOS's
    # tls-host firewall matcher (6.41+). Real application identification, not
    # DPI -- but plaintext SNI only, so Encrypted Client Hello defeats it, and
    # prefix matching above remains the fallback. See app.render.policy for
    # why this needs a second mangle pass rather than a new match property.
    sni_patterns: Mapped[list | None] = mapped_column(JSONCol, default=list)
    builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_appgroup_tenant_name"),)


class SdwanGroup(Base, UUIDPk, Timestamps, Tenanted):
    """A named set of uplinks, in preference order, with a health standard.

    This is the half of a steering decision that is worth naming once and
    reusing: *which* uplinks, in what order, and how bad one has to get before
    traffic leaves it. Before this existed it was retyped into every rule, and
    "the SLA" was a thing you set per rule rather than a property of the path.

    Modelled on Sophos's SD-WAN profile, including the eight-member cap.
    """

    __tablename__ = "sdwan_groups"

    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)

    # [{"uplink": "fibre", "weight": 1}, ...] in preference order. "uplink" is
    # a WAN tag or a WAN name, exactly as prefer_tags was.
    members: Mapped[list | None] = mapped_column(JSONCol, default=list)

    # failover: first healthy member wins, weights ignored.
    # load_balance: not implemented -- see docs/plan-v3.md. Rejected on write
    # rather than silently rendered as failover, which would be a lie.
    strategy: Mapped[str] = mapped_column(String(16), nullable=False, default="failover")

    sla_profile_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sla_profiles.id", ondelete="SET NULL")
    )
    sla_profile: Mapped[SlaProfile | None] = relationship(lazy="selectin")

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_sdwan_group_tenant_name"),
    )


class Policy(Base, UUIDPk, Timestamps, Tenanted):
    """One steering rule.

    Rules are evaluated in ``priority`` order, lowest first, and render to
    mangle rules in that same order -- RouterOS firewall chains are positional,
    so the order is the semantics.
    """

    __tablename__ = "policies"

    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    fabric_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("fabrics.id", ondelete="CASCADE"), index=True
    )
    # Null means every site in the fabric. Otherwise a list of site ids.
    site_ids: Mapped[list | None] = mapped_column(JSONCol, default=list)

    # -- match --------------------------------------------------------------
    src_prefixes: Mapped[list | None] = mapped_column(JSONCol, default=list)
    dst_prefixes: Mapped[list | None] = mapped_column(JSONCol, default=list)
    app_group_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("app_groups.id", ondelete="SET NULL")
    )
    protocol: Mapped[str | None] = mapped_column(String(16))
    dst_ports: Mapped[str | None] = mapped_column(String(128))
    dscp: Mapped[int | None] = mapped_column(Integer)

    # -- action -------------------------------------------------------------
    # Which uplinks, in what order, and how healthy they must be. Named once as
    # a group and pointed at from here, so one path definition serves many
    # rules.
    sdwan_group_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sdwan_groups.id", ondelete="RESTRICT"), index=True
    )
    sdwan_group: Mapped[SdwanGroup | None] = relationship(lazy="selectin")

    # Superseded by sdwan_group. Retained so the migration that moved every
    # rule onto a group is reversible and nothing is silently destroyed; the
    # renderer no longer reads either.
    prefer_tags: Mapped[list | None] = mapped_column(JSONCol, default=list)
    sla_profile_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sla_profiles.id", ondelete="SET NULL")
    )
    # What to do when no preferred path meets the SLA.
    fallback: Mapped[str] = mapped_column(String(16), nullable=False, default="any")

    sla_profile: Mapped[SlaProfile | None] = relationship(lazy="selectin")
    app_group: Mapped[AppGroup | None] = relationship(lazy="selectin")

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_policy_tenant_name"),)
