"""Sites and their WAN uplinks -- the two objects an operator actually authors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy import true as sa_true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.models.base import Base, Tenanted, Timestamps, UUIDPk
from app.models.enums import DeviceKind, SiteRole, SiteStatus

if TYPE_CHECKING:
    from app.models.fabric import FabricMember

# Postgres in production, SQLite in unit tests.
JSONCol = JSON().with_variant(JSONB(), "postgresql")


class Site(Base, UUIDPk, Timestamps, Tenanted):
    __tablename__ = "sites"

    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(String(64), index=True)
    role: Mapped[SiteRole] = mapped_column(String(16), nullable=False, default=SiteRole.spoke)

    # --- management access -------------------------------------------------
    mgmt_host: Mapped[str] = mapped_column(String(255), nullable=False)
    mgmt_port: Mapped[int | None] = mapped_column(Integer)  # None -> driver default
    device_kind: Mapped[DeviceKind] = mapped_column(
        String(16), nullable=False, default=DeviceKind.ros7
    )
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    password_enc: Mapped[str | None] = mapped_column(Text)
    ssh_key_enc: Mapped[str | None] = mapped_column(Text)
    verify_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Pinned device identity, learned on first contact. A mismatch afterwards
    # means either the device was rebuilt or someone is in the middle; the
    # driver refuses either way and an operator has to clear it deliberately.
    tls_fingerprint: Mapped[str | None] = mapped_column(String(95))
    ssh_host_key: Mapped[str | None] = mapped_column(Text)

    # --- discovered on probe ----------------------------------------------
    ros_version: Mapped[str | None] = mapped_column(String(32))
    board_name: Mapped[str | None] = mapped_column(String(128))
    architecture: Mapped[str | None] = mapped_column(String(32))
    identity: Mapped[str | None] = mapped_column(String(128))
    capabilities: Mapped[dict | None] = mapped_column(JSONCol)
    status: Mapped[SiteStatus] = mapped_column(
        String(24), nullable=False, default=SiteStatus.unprovisioned
    )
    last_seen_at: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)

    # --- routing identity --------------------------------------------------
    loopback_ip: Mapped[str | None] = mapped_column(String(64))
    local_prefixes: Mapped[list | None] = mapped_column(JSONCol, default=list)

    # --- operational -------------------------------------------------------
    rollback_timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    drift_action: Mapped[str] = mapped_column(String(16), nullable=False, default="alert")
    tags: Mapped[dict | None] = mapped_column(JSONCol, default=dict)

    wans: Mapped[list[Wan]] = relationship(
        back_populates="site", cascade="all, delete-orphan", lazy="selectin"
    )
    # selectin, like wans above. The delete endpoint reads this to refuse a
    # site that is still in a fabric, and a lazy load inside an async request
    # raises MissingGreenlet rather than quietly fetching -- so the guard blew
    # up before it could decide, and no device could ever be deleted.
    memberships: Mapped[list[FabricMember]] = relationship(
        back_populates="site", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_site_tenant_name"),)


class Wan(Base, UUIDPk, Timestamps):
    """One uplink on a site. Tunnels are built per WAN, not per site, so a
    dual-homed site participates in the fabric twice."""

    __tablename__ = "wans"

    site_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    interface: Mapped[str] = mapped_column(String(64), nullable=False)

    # public_ip is None when the uplink is dynamic or behind CGNAT. Such a WAN can
    # only ever be an IKE initiator -- see transports/base.py::validate_pair.
    public_ip: Mapped[str | None] = mapped_column(String(64))
    dynamic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    nat_behind: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    gateway: Mapped[str | None] = mapped_column(String(64))
    # Mask length of public_ip on this interface, so the controller can tell an
    # on-link peer from one reached through the gateway. Storing an address
    # with no mask is lossy: the two cases need different routes and there is
    # no way to distinguish them afterwards. Null when the uplink was entered
    # by hand rather than probed.
    prefix_len: Mapped[int | None] = mapped_column(Integer)
    provider: Mapped[str | None] = mapped_column(String(128))
    bandwidth_mbps: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Whether the controller should NAT traffic leaving on this uplink.
    #
    # Steering routes traffic out an interface the site's own firewall was
    # never written for, so without a masquerade rule matching it the packets
    # leave with a private source and die at the first upstream router. True
    # for an internet uplink; false for private transit -- MPLS, a partner
    # link -- where NAT would break the far end.
    masquerade: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_true()
    )
    tags: Mapped[dict | None] = mapped_column(JSONCol, default=dict)

    site: Mapped[Site] = relationship(back_populates="wans")

    __table_args__ = (UniqueConstraint("site_id", "name", name="uq_wan_site_name"),)

    @property
    def dial_out_only(self) -> bool:
        """True when this uplink cannot accept an inbound tunnel."""
        return self.public_ip is None or self.nat_behind
