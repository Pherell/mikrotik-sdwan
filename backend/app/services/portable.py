"""Export and import the whole intent model as YAML.

The point is that the controller's database is not the only place this
configuration can live. A fabric that round-trips through a file can be diffed
in a pull request, restored after a rebuild, and moved between installs.

Secrets are deliberately excluded. Device credentials and link keys are
environment-specific and belong in a secret store, not in a file people put in
git -- so an import re-keys links and asks for credentials rather than pretending
it can carry them.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fabric import Fabric, FabricMember
from app.models.policy import AppGroup, Policy, SlaProfile
from app.models.site import Site, Wan

SCHEMA_VERSION = 1


async def export_intent(session: AsyncSession, tenant_id: str = "default") -> dict[str, Any]:
    """Everything an operator authored, and nothing the controller derived.

    Links are omitted on purpose: they are a function of the topology and the
    members, so exporting them would let a file and its own fabric disagree.
    Re-expanding after an import rebuilds them.
    """
    sites = list(
        await session.scalars(
            select(Site).where(Site.tenant_id == tenant_id).order_by(Site.name)
        )
    )
    fabrics = list(
        await session.scalars(
            select(Fabric).where(Fabric.tenant_id == tenant_id).order_by(Fabric.name)
        )
    )
    members = list(await session.scalars(select(FabricMember)))
    site_names = {s.id: s.name for s in sites}
    fabric_names = {f.id: f.name for f in fabrics}

    return {
        "version": SCHEMA_VERSION,
        "sites": [
            {
                "name": s.name,
                "description": s.description,
                "region": s.region,
                "role": str(s.role),
                "mgmt_host": s.mgmt_host,
                "mgmt_port": s.mgmt_port,
                "device_kind": str(s.device_kind),
                "username": s.username,
                # No password or ssh key: see the module docstring.
                "loopback_ip": s.loopback_ip,
                "local_prefixes": list(s.local_prefixes or []),
                "rollback_timeout_seconds": s.rollback_timeout_seconds,
                "drift_action": s.drift_action,
                "tags": dict(s.tags or {}),
                "wans": [
                    {
                        "name": w.name,
                        "interface": w.interface,
                        "public_ip": w.public_ip,
                        "dynamic": w.dynamic,
                        "nat_behind": w.nat_behind,
                        "gateway": w.gateway,
                        "provider": w.provider,
                        "bandwidth_mbps": w.bandwidth_mbps,
                        "cost": w.cost,
                        "enabled": w.enabled,
                        "tags": dict(w.tags or {}),
                    }
                    for w in sorted(s.wans, key=lambda w: w.name)
                ],
            }
            for s in sites
        ],
        "fabrics": [
            {
                "name": f.name,
                "description": f.description,
                "transport": str(f.transport),
                "transport_params": dict(f.transport_params or {}),
                "topology": str(f.topology),
                "ip_pool": f.ip_pool,
                "loopback_pool": f.loopback_pool,
                "asn": f.asn,
                "mtu": f.mtu,
                "enabled": f.enabled,
                "members": sorted(
                    site_names[m.site_id]
                    for m in members
                    if m.fabric_id == f.id and m.site_id in site_names
                ),
            }
            for f in fabrics
        ],
        "sla_profiles": [
            {
                "name": p.name,
                "description": p.description,
                "loss_percent": p.loss_percent,
                "latency_ms": p.latency_ms,
                "jitter_ms": p.jitter_ms,
                "probe_interval_seconds": p.probe_interval_seconds,
                "probe_count": p.probe_count,
                "recovery_seconds": p.recovery_seconds,
            }
            for p in await session.scalars(
                select(SlaProfile)
                .where(SlaProfile.tenant_id == tenant_id)
                .order_by(SlaProfile.name)
            )
        ],
        "app_groups": [
            {
                "name": g.name,
                "description": g.description,
                "prefixes": list(g.prefixes or []),
                "ports": list(g.ports or []),
                "protocol": g.protocol,
                "dscp": g.dscp,
            }
            for g in await session.scalars(
                select(AppGroup)
                .where(AppGroup.tenant_id == tenant_id, AppGroup.builtin.is_(False))
                .order_by(AppGroup.name)
            )
        ],
        "policies": [
            {
                "name": p.name,
                "description": p.description,
                "priority": p.priority,
                "enabled": p.enabled,
                # Referenced by name so the file survives a rebuild that hands
                # out different ids.
                "fabric": fabric_names.get(p.fabric_id or ""),
                "sites": sorted(
                    site_names[sid] for sid in (p.site_ids or []) if sid in site_names
                ),
                "src_prefixes": list(p.src_prefixes or []),
                "dst_prefixes": list(p.dst_prefixes or []),
                "app_group": p.app_group.name if p.app_group else None,
                "protocol": p.protocol,
                "dst_ports": p.dst_ports,
                "dscp": p.dscp,
                "prefer_tags": list(p.prefer_tags or []),
                "sla_profile": p.sla_profile.name if p.sla_profile else None,
                "fallback": p.fallback,
            }
            for p in await session.scalars(
                select(Policy)
                .where(Policy.tenant_id == tenant_id)
                .order_by(Policy.priority, Policy.name)
            )
        ],
    }


class ImportError_(ValueError):
    """The document cannot be applied to this install."""


async def import_intent(
    session: AsyncSession,
    document: dict[str, Any],
    *,
    tenant_id: str = "default",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Create what is missing and update what exists, matching on name.

    Nothing is deleted. An import that removed whatever the file omitted would
    make a partial document destructive, and a partial document is the normal
    case when someone exports one fabric to share it.
    """
    version = document.get("version")
    if version != SCHEMA_VERSION:
        raise ImportError_(
            f"Document is schema version {version!r}; this controller reads "
            f"version {SCHEMA_VERSION}."
        )

    summary = {"sites": 0, "wans": 0, "fabrics": 0, "policies": 0, "sla_profiles": 0,
               "app_groups": 0, "created": [], "updated": [], "warnings": []}

    existing_sites = {
        s.name: s
        for s in await session.scalars(select(Site).where(Site.tenant_id == tenant_id))
    }

    for spec in document.get("sites", []):
        name = spec["name"]
        site = existing_sites.get(name)
        fields = {
            k: spec.get(k)
            for k in (
                "description", "region", "role", "mgmt_host", "mgmt_port",
                "device_kind", "username", "loopback_ip", "local_prefixes",
                "rollback_timeout_seconds", "drift_action", "tags",
            )
            if k in spec
        }
        if site is None:
            site = Site(name=name, tenant_id=tenant_id, **fields)
            # Seed the collection so it counts as loaded. Touching site.wans
            # after the flush would otherwise trigger a lazy load from async
            # context and raise MissingGreenlet.
            site.wans = []
            session.add(site)
            existing_sites[name] = site
            summary["created"].append(f"site/{name}")
            summary["warnings"].append(
                f"site/{name} has no credentials; set a password before probing it"
            )
        else:
            for key, value in fields.items():
                setattr(site, key, value)
            summary["updated"].append(f"site/{name}")
        summary["sites"] += 1

        if not dry_run:
            await session.flush()
            by_name = {w.name: w for w in site.wans}
            for wan_spec in spec.get("wans", []):
                wan = by_name.get(wan_spec["name"])
                if wan is None:
                    site.wans.append(Wan(**wan_spec))
                else:
                    for key, value in wan_spec.items():
                        setattr(wan, key, value)
                summary["wans"] += 1

    if dry_run:
        await session.rollback()
        return summary

    await session.flush()

    for spec in document.get("sla_profiles", []):
        await _upsert(session, SlaProfile, spec, tenant_id, summary, "sla_profiles")
    for spec in document.get("app_groups", []):
        await _upsert(session, AppGroup, spec, tenant_id, summary, "app_groups")

    await _import_fabrics(session, document, tenant_id, existing_sites, summary)
    await _import_policies(session, document, tenant_id, existing_sites, summary)
    await session.flush()
    return summary


async def _upsert(
    session: AsyncSession, model, spec: dict, tenant_id: str, summary: dict, bucket: str
) -> object:
    existing = await session.scalar(
        select(model).where(model.tenant_id == tenant_id, model.name == spec["name"])
    )
    if existing is None:
        existing = model(tenant_id=tenant_id, **spec)
        session.add(existing)
        summary["created"].append(f"{bucket}/{spec['name']}")
    else:
        for key, value in spec.items():
            setattr(existing, key, value)
        summary["updated"].append(f"{bucket}/{spec['name']}")
    summary[bucket] += 1
    return existing


async def _import_fabrics(
    session: AsyncSession,
    document: dict,
    tenant_id: str,
    sites: dict[str, Site],
    summary: dict,
) -> None:
    for spec in document.get("fabrics", []):
        member_names = spec.pop("members", [])
        fabric = await _upsert(session, Fabric, spec, tenant_id, summary, "fabrics")
        await session.flush()

        current = {
            m.site_id
            for m in await session.scalars(
                select(FabricMember).where(FabricMember.fabric_id == fabric.id)
            )
        }
        for name in member_names:
            site = sites.get(name)
            if site is None:
                summary["warnings"].append(
                    f"fabric/{spec['name']} lists member {name!r}, which is not in "
                    "this document or this install"
                )
                continue
            if site.id not in current:
                session.add(FabricMember(fabric_id=fabric.id, site_id=site.id))
        summary["warnings"].append(
            f"fabric/{spec['name']} imported without links; run expand to rebuild them"
        )


async def _import_policies(
    session: AsyncSession,
    document: dict,
    tenant_id: str,
    sites: dict[str, Site],
    summary: dict,
) -> None:
    slas = {
        p.name: p.id
        for p in await session.scalars(
            select(SlaProfile).where(SlaProfile.tenant_id == tenant_id)
        )
    }
    groups = {
        g.name: g.id
        for g in await session.scalars(
            select(AppGroup).where(AppGroup.tenant_id == tenant_id)
        )
    }
    fabrics = {
        f.name: f.id
        for f in await session.scalars(select(Fabric).where(Fabric.tenant_id == tenant_id))
    }

    for spec in document.get("policies", []):
        data = dict(spec)
        data["sla_profile_id"] = slas.get(data.pop("sla_profile", None) or "")
        data["app_group_id"] = groups.get(data.pop("app_group", None) or "")
        data["fabric_id"] = fabrics.get(data.pop("fabric", None) or "")
        data["site_ids"] = [
            sites[n].id for n in data.pop("sites", []) if n in sites
        ]
        await _upsert(session, Policy, data, tenant_id, summary, "policies")
