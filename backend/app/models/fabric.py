"""Fabrics, membership, and the derived Link objects.

A Link is never authored by hand. The expander walks every enabled WAN on every
member site and produces the tunnels the chosen topology calls for.
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Tenanted, Timestamps, UUIDPk
from app.models.enums import SiteRole, Topology, Transport
from app.models.site import JSONCol, Site, Wan


class Fabric(Base, UUIDPk, Timestamps, Tenanted):
    __tablename__ = "fabrics"

    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)

    transport: Mapped[Transport] = mapped_column(
        String(24), nullable=False, default=Transport.ipsec_gre
    )
    transport_params: Mapped[dict | None] = mapped_column(JSONCol, default=dict)
    topology: Mapped[Topology] = mapped_column(
        String(24), nullable=False, default=Topology.hub_spoke_dynamic
    )

    # Tunnel addressing. Every link consumes one /31 out of this pool.
    ip_pool: Mapped[str] = mapped_column(String(64), nullable=False, default="10.255.0.0/16")
    loopback_pool: Mapped[str] = mapped_column(String(64), nullable=False, default="10.254.0.0/24")

    asn: Mapped[int] = mapped_column(Integer, nullable=False, default=65000)
    mtu: Mapped[int] = mapped_column(Integer, nullable=False, default=1400)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    members: Mapped[list[FabricMember]] = relationship(
        back_populates="fabric", cascade="all, delete-orphan", lazy="selectin"
    )
    links: Mapped[list[Link]] = relationship(
        back_populates="fabric", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_fabric_tenant_name"),)


class FabricMember(Base, UUIDPk, Timestamps):
    __tablename__ = "fabric_members"

    fabric_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("fabrics.id", ondelete="CASCADE"), nullable=False, index=True
    )
    site_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Overrides Site.role for this fabric only -- a site can be a hub in one
    # fabric and a spoke in another.
    role_override: Mapped[SiteRole | None] = mapped_column(String(16))
    loopback_ip: Mapped[str | None] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    fabric: Mapped[Fabric] = relationship(back_populates="members")
    site: Mapped[Site] = relationship(back_populates="memberships", lazy="selectin")

    __table_args__ = (
        UniqueConstraint("fabric_id", "site_id", name="uq_member_fabric_site"),
    )


class Link(Base, UUIDPk, Timestamps):
    """A single tunnel between WAN A and WAN B. Derived, addressed, and keyed by
    the controller."""

    __tablename__ = "links"

    fabric_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("fabrics.id", ondelete="CASCADE"), nullable=False, index=True
    )
    a_wan_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("wans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    b_wan_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("wans.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Stable short name used for interface names and the sdwan: ownership tag.
    slug: Mapped[str] = mapped_column(String(48), nullable=False, index=True)

    a_tunnel_ip: Mapped[str] = mapped_column(String(64), nullable=False)
    b_tunnel_ip: Mapped[str] = mapped_column(String(64), nullable=False)
    subnet: Mapped[str] = mapped_column(String(64), nullable=False)  # the /31

    # Which side dials. Set by the transport driver from NAT/public-IP facts.
    initiator: Mapped[str] = mapped_column(String(1), nullable=False, default="a")

    # Populated on demand for dynamic mesh links; permanent links are pinned.
    dynamic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    secrets_enc: Mapped[str | None] = mapped_column(Text)  # JSON blob, Fernet-encrypted
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    last_error: Mapped[str | None] = mapped_column(Text)

    fabric: Mapped[Fabric] = relationship(back_populates="links")
    a_wan: Mapped[Wan] = relationship(foreign_keys=[a_wan_id], lazy="selectin")
    b_wan: Mapped[Wan] = relationship(foreign_keys=[b_wan_id], lazy="selectin")

    __table_args__ = (
        UniqueConstraint("fabric_id", "a_wan_id", "b_wan_id", name="uq_link_fabric_pair"),
    )
