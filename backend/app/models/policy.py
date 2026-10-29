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

    # -- anti-flap (plan A3) -------------------------------------------------
    # Consecutive failed probe tests required before a soft (SLA) demotion,
    # Fortinet-style fail xN. Enforced on-device: the netwatch down-script
    # waits (fail_count - 1) probe intervals and demotes only if its entry
    # is still down, which is the time-domain form of "N tests in a row
    # failed" -- netwatch raises no event for the tests after the first.
    # 1 restores the pre-A3 behaviour of demoting on the first failed test.
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    # Consecutive passed tests required before a demoted path is restored,
    # the restore xN half of the pattern. The restore delay is the longer of
    # recovery_seconds and (restore_count - 1) probe intervals, so either
    # knob can be the binding one; see app.render.policy.
    restore_count: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    # Failback hysteresis: a recovered path must beat its own thresholds by
    # these margins before traffic returns, not merely duck back under them
    # (demote above T, restore below T - margin). Compared against the
    # profile's own thresholds, not the live active path's numbers -- a
    # netwatch entry can only read its own test results. See render.policy.
    failback_latency_margin_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10
    )
    failback_jitter_margin_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5
    )

    # -- probe shape (plan A5) ------------------------------------------------
    # What the netwatch entries measure. icmp judges loss/latency/jitter
    # against the thresholds above; the other types judge a real service
    # transaction instead (TCP connect, HTTP GET, DNS answer), which is
    # closer to application experience and survives ICMP being filtered or
    # deprioritised -- but carries no thr-* stats, so for them the on-device
    # verdict is up/down only and the thresholds above do not apply.
    probe_type: Mapped[str] = mapped_column(String(16), nullable=False, default="icmp")
    # Service port for tcp-conn / http-get probes. Required in practice for
    # tcp-conn (the schema enforces it there); http-get falls back to 80.
    probe_port: Mapped[int | None] = mapped_column(Integer)
    # DNS probes resolve a name: RouterOS rejects an address as the host of
    # a type=dns entry ("must use dns name when type is dns", verified on
    # the CHR lab), so a dns profile names what to resolve and that name --
    # not the hop -- becomes the entry's host. The schema requires it for
    # probe_type == "dns"; meaningless for the other types.
    probe_dns_name: Mapped[str | None] = mapped_column(String(255))
    # Extra probe targets judged together with the path's next hop: one
    # netwatch entry each, and a soft demotion needs every entry in the set
    # down at once (target agreement), so one dead anycast target cannot
    # demote a healthy path. Empty means the hop alone is probed, as before.
    probe_targets: Mapped[list | None] = mapped_column(JSONCol, default=list)

    # How far back the central evaluator (app.telemetry.evaluator) looks when
    # classifying this profile's paths. Longer windows judge steadier and
    # react slower; the on-device thresholds above are unaffected -- this
    # only sizes the compliance record's window.
    eval_window_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

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
