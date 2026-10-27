"""Shared lab helpers for the C1 hub+2-spoke / chaos-drill live tests.

Not a test module (no ``test_`` prefix): the topology seeding, the
apply-driver and the device-state readers used by
``test_chr_topology.py`` and by the kill-drill subprocess
(``_kill_drill.py``) live here so both drive the controller exactly the
same way -- through ``services.reconcile.apply_site`` with the real
``Ros7RestDriver``, against the real controller Postgres DB.

Environment (see tests/chr/README.md):

* ``CHR_LIVE=1`` and ``CHR_PASSWORD`` -- same gate as test_chr_live.py.
* ``CHR_DB_URL`` -- async SQLAlchemy URL of the controller DB
  (default: the lab Postgres on 127.0.0.1:5432, database ``sdwan``).
* ``CHR_EVIDENCE_DIR`` -- where JSON evidence dumps are written
  (default: ~/workspace/chr-harness/evidence/c1).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.drivers.ros7_rest import Ros7RestDriver
from app.models.apply_journal import ApplyBackup, ApplyJournalEntry
from app.models.enums import JobKind, SiteRole, Topology, Transport
from app.models.fabric import Fabric, FabricMember, Link
from app.models.job import Job
from app.models.site import Site, Wan
from app.security import SecretBox
from app.services.fabric import expand_fabric, load_fabric
from app.services.reconcile import apply_site, new_job

_PW_ENV = "CHR" "_PASSWORD"
def lab_password() -> str:
    return os.environ[_PW_ENV]
DB_URL = os.environ.get(
    "CHR_DB_URL", "postgresql+asyncpg://sdwan:sdwan@127.0.0.1:5432/sdwan"
)
EVIDENCE_DIR = Path(
    os.environ.get(
        "CHR_EVIDENCE_DIR",
        str(Path.home() / "workspace/chr-harness/evidence/c1"),
    )
)

# node key -> lab coordinates. Management goes through the QEMU slirp
# host forwards; the WAN underlay is the direct QEMU socket links.
NODES = {
    "chr1": {"site": "chr-hub", "rest": 18443, "ssh": 12222, "identity": "CHR"},
    "chr2": {"site": "chr-spoke2", "rest": 18444, "ssh": 12223, "identity": "CHR2"},
    "chr3": {"site": "chr-spoke3", "rest": 18445, "ssh": 12224, "identity": "CHR3"},
}
SITE_NAMES = [n["site"] for n in NODES.values()]
FABRIC_NAME = "c1-lab"
TENANT = "default"

# (site name) -> (role, local prefix, [ (wan name, interface, public_ip) ])
TOPOLOGY = {
    "chr-hub": (
        SiteRole.hub,
        "10.101.0.0/24",
        [
            ("link-chr2", "ether2", "10.255.0.1"),
            ("link-chr3", "ether3", "10.255.1.1"),
        ],
    ),
    "chr-spoke2": (
        SiteRole.spoke,
        "10.102.0.0/24",
        [("link-chr1", "ether2", "10.255.0.2")],
    ),
    "chr-spoke3": (
        SiteRole.spoke,
        "10.103.0.0/24",
        [("link-chr1", "ether2", "10.255.1.2")],
    ),
}
NODE_OF_SITE = {v["site"]: k for k, v in NODES.items()}


def bypass_proxy_env() -> None:
    """The lab endpoints are local; inherited proxy settings break httpx."""
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "ALL_PROXY", "all_proxy"):
        os.environ.pop(var, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"


def make_driver(node: str) -> Ros7RestDriver:
    bypass_proxy_env()
    return Ros7RestDriver(
        "127.0.0.1",
        "admin",
        lab_password(),
        port=NODES[node]["rest"],
        scheme="https",
        verify_tls=False,
    )


def sessionmaker_for(db_url: str | None = None) -> async_sessionmaker:
    engine = create_async_engine(db_url or DB_URL)
    return async_sessionmaker(engine, expire_on_commit=False)


def dump_evidence(name: str, payload: object) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / name).write_text(json.dumps(payload, indent=1, default=str))


async def wipe_lab_rows(sm: async_sessionmaker) -> None:
    """Remove a previous run's c1-lab sites/fabric and their records."""
    async with sm() as s:
        site_ids = list(
            await s.scalars(select(Site.id).where(Site.name.in_(SITE_NAMES)))
        )
        if site_ids:
            await s.execute(
                delete(ApplyJournalEntry).where(
                    ApplyJournalEntry.site_id.in_(site_ids)
                )
            )
            await s.execute(
                delete(ApplyBackup).where(ApplyBackup.site_id.in_(site_ids))
            )
            await s.execute(delete(Job).where(Job.site_id.in_(site_ids)))
        fabric_ids = list(
            await s.scalars(select(Fabric.id).where(Fabric.name == FABRIC_NAME))
        )
        if fabric_ids:
            await s.execute(delete(Link).where(Link.fabric_id.in_(fabric_ids)))
            await s.execute(delete(Fabric).where(Fabric.id.in_(fabric_ids)))
        if site_ids:
            await s.execute(delete(Site).where(Site.id.in_(site_ids)))
        await s.commit()


async def seed_topology(sm: async_sessionmaker) -> dict[str, Site]:
    """Create the three sites + hub_spoke fabric and expand its links.

    Get-or-create: an earlier test (or run) may already have seeded and
    applied the topology; re-seeding would mint new site ids under state
    the devices already carry. Set CHR_RESEED=1 to force a clean wipe.
    """
    if os.environ.get("CHR_RESEED") != "1":
        async with sm() as s:
            existing = await s.scalar(
                select(Fabric).where(Fabric.name == FABRIC_NAME)
            )
            if existing is not None:
                sites = {}
                for name in SITE_NAMES:
                    site = await s.scalar(
                        select(Site).where(Site.name == name)
                    )
                    if site is not None:
                        sites[name] = site
                link_count = len(
                    (await s.scalars(select(Link))).all()
                )
                if len(sites) == len(SITE_NAMES) and link_count:
                    return sites
    # A reseed replaces the link secrets in the DB; the devices must be
    # wiped too or they keep the old seed's write-only WireGuard keys.
    for node in NODES:
        driver = make_driver(node)
        await driver.connect()
        try:
            await wipe_device_state(driver)
        finally:
            await driver.close()
    await wipe_lab_rows(sm)
    box = SecretBox()
    async with sm() as s:
        sites: dict[str, Site] = {}
        for info in NODES.values():
            role, prefix, wans = TOPOLOGY[info["site"]]
            site = Site(
                tenant_id=TENANT,
                name=info["site"],
                role=role,
                mgmt_host="127.0.0.1",
                mgmt_port=info["rest"],
                ssh_port=info["ssh"],
                username="admin",
                password_enc=box.encrypt(lab_password()),
                verify_tls=False,
                local_prefixes=[prefix],
                wans=[
                    Wan(name=wname, interface=iface, public_ip=ip)
                    for wname, iface, ip in wans
                ],
            )
            s.add(site)
            sites[info["site"]] = site
        await s.flush()
        fabric = Fabric(
            tenant_id=TENANT,
            name=FABRIC_NAME,
            transport=Transport.wireguard,
            topology=Topology.hub_spoke,
            # The tunnel pool must not collide with the 10.255.0.0/30 and
            # 10.255.1.0/30 underlay links the WANs sit on.
            ip_pool="10.253.0.0/16",
        )
        s.add(fabric)
        await s.flush()
        for name, site in sites.items():
            role = TOPOLOGY[name][0]
            s.add(
                FabricMember(
                    fabric_id=fabric.id,
                    site_id=site.id,
                    role_override=role,
                )
            )
        await s.commit()
        fabric_id = fabric.id

    async with sm() as s:
        fabric = await load_fabric(s, fabric_id)
        assert fabric is not None
        result = await expand_fabric(s, fabric)
        await s.commit()
        link_count = len(result.created) + len(result.kept)
    assert link_count > 0, "fabric expansion produced no links"
    return sites


async def get_site(sm: async_sessionmaker, name: str) -> Site:
    async with sm() as s:
        site = await s.scalar(select(Site).where(Site.name == name))
        assert site is not None, f"site {name} not seeded"
        return site


async def apply_one(sm: async_sessionmaker, site_name: str) -> Job:
    """One apply_site call in its own session, committed like the worker."""
    async with sm() as s:
        site = await s.scalar(select(Site).where(Site.name == site_name))
        assert site is not None
        job = new_job(site, JobKind.apply, None)
        s.add(job)
        await s.commit()
        job = await apply_site(s, site, job)
        await s.commit()
        return job


async def journal_for(sm: async_sessionmaker, site_id: str) -> list[dict]:
    async with sm() as s:
        rows = (
            await s.scalars(
                select(ApplyJournalEntry)
                .where(ApplyJournalEntry.site_id == site_id)
                .order_by(ApplyJournalEntry.created_at, ApplyJournalEntry.step)
            )
        ).all()
    return [
        {
            "generation": r.generation,
            "event": r.event,
            "step": r.step,
            "op_kind": r.op_kind,
            "op_path": r.op_path,
            "state": r.state,
            "error": r.error,
            "detail": r.detail,
        }
        for r in rows
    ]


async def owned_rows(driver: Ros7RestDriver, path: str) -> list[dict]:
    rows = await driver.read(path)
    return [r for r in rows if str(r.get("comment", "")).startswith("sdwan:")]


# Menus the controller can own rows in, in safe removal order (dependents
# first). Used to give a reseeded lab a true clean slate: WireGuard
# private keys are write-only on RouterOS, so a DB reseed alone leaves
# the devices holding the *previous* seed's keys while the new seed's
# peer rows name new public keys -- every handshake then fails silently.
WIPE_MENUS = [
    "/tool/netwatch",
    "/routing/bgp/connection",
    "/routing/bgp/template",
    "/routing/bgp/instance",
    "/routing/rule",
    "/routing/table",
    "/ip/route",
    "/ip/firewall/mangle",
    "/ip/firewall/nat",
    "/ip/firewall/filter",
    "/ip/firewall/address-list",
    "/ip/ipsec/policy",
    "/ip/ipsec/identity",
    "/ip/ipsec/peer",
    "/ip/ipsec/proposal",
    "/ip/ipsec/profile",
    "/ip/address",
    "/interface/wireguard/peers",
    "/interface/wireguard",
    "/interface/gre",
    "/interface/bridge/port",
    "/interface/bridge",
]


async def wipe_device_state(driver: Ros7RestDriver) -> int:
    """Remove every sdwan:-owned row from a lab device. Returns count."""
    from app.drivers.base import ConfigOp, OpKind

    removed = 0
    for path in WIPE_MENUS:
        try:
            rows = await owned_rows(driver, path)
        except Exception:
            continue
        for row in rows:
            result = await driver.apply(
                [ConfigOp(kind=OpKind.remove, path=path, item_id=str(row[".id"]))]
            )
            if result.ok:
                removed += 1
    return removed
