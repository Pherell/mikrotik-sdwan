"""The SLA retention reaper: daily server-side deletion of old evaluation rows.

The user's decision (2026-10-23, "Pakai 90 hari"): keep sla_evaluations for
90 days; a daily reaper deletes rows whose bucket_start is older than the
window. Server-side only, zero device traffic. These tests follow the same
in-memory pattern as test_api_jobs.py's worker tests: patch
app.tasks.worker.SessionLocal at the test's own engine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.models import Base, Fabric, Link, Site, Wan
from app.models.base import utcnow
from app.models.enums import Topology, Transport
from app.models.telemetry import SlaEvaluation


@pytest.fixture
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(db: async_sessionmaker[AsyncSession]) -> None:
    near = Site(
        id="site-a", name="branch", mgmt_host="10.2", username="admin",
        tenant_id="t",
    )
    fabric = Fabric(
        id="fab-1", name="core", tenant_id="t", transport=Transport.ipsec_gre,
        topology=Topology.hub_spoke, asn=65000, mtu=1400,
    )
    link = Link(
        id="link-1", fabric_id="fab-1", a_wan_id="wan-a", b_wan_id="wan-b",
        slug="branch-hq", a_tunnel_ip="10.255.1",
        b_tunnel_ip="10.255.2", subnet="10.255.0/31",
        enabled=True, state="applied",
    )
    async with db() as s:
        s.add_all(
            [
                near,
                Wan(id="wan-a", site_id="site-a", name="wan1", interface="ether1"),
                fabric,
                link,
            ]
        )
        await s.commit()


def _evaluation(age: timedelta) -> SlaEvaluation:
    start = utcnow() - age
    return SlaEvaluation(
        site_id="site-a",
        link_id="link-1",
        tenant_id="t",
        bucket_start=start,
        window_start=start - timedelta(minutes=5),
        window_end=start,
        window_minutes=5,
        classification="in_sla",
        evaluated_at=start,
    )


async def _remaining_ids(db: async_sessionmaker[AsyncSession]) -> set[str]:
    async with db() as s:
        rows = await s.scalars(select(SlaEvaluation))
        return {r.id for r in rows}


async def test_the_reaper_deletes_only_rows_older_than_retention(
    db, monkeypatch
) -> None:
    from app.tasks.worker import reap_sla_evaluations

    await _seed(db)
    old = _evaluation(timedelta(days=91))
    young = _evaluation(timedelta(days=89))
    fresh = _evaluation(timedelta(hours=1))
    async with db() as s:
        s.add_all([old, young, fresh])
        await s.commit()

    monkeypatch.setattr("app.tasks.worker.SessionLocal", db)
    result = await reap_sla_evaluations({})

    assert result == {"deleted": 1}
    remaining = await _remaining_ids(db)
    assert remaining == {young.id, fresh.id}


async def test_the_reaper_honours_a_custom_retention_setting(
    db, monkeypatch
) -> None:
    from app.tasks.worker import reap_sla_evaluations

    await _seed(db)
    old = _evaluation(timedelta(days=40))
    young = _evaluation(timedelta(days=20))
    async with db() as s:
        s.add_all([old, young])
        await s.commit()

    monkeypatch.setattr("app.tasks.worker.SessionLocal", db)
    monkeypatch.setattr(
        "app.tasks.worker.get_settings", lambda: Settings(sla_retention_days=30)
    )
    result = await reap_sla_evaluations({})

    assert result == {"deleted": 1}
    assert await _remaining_ids(db) == {young.id}


async def test_the_reaper_is_quiet_when_nothing_is_old(db, monkeypatch) -> None:
    from app.tasks.worker import reap_sla_evaluations

    await _seed(db)
    async with db() as s:
        s.add(_evaluation(timedelta(hours=2)))
        await s.commit()

    monkeypatch.setattr("app.tasks.worker.SessionLocal", db)
    result = await reap_sla_evaluations({})

    assert result == {"deleted": 0}
    assert len(await _remaining_ids(db)) == 1


def test_retention_defaults_to_ninety_days() -> None:
    assert Settings().sla_retention_days == 90


def test_the_reaper_is_registered_as_a_daily_cron() -> None:
    from app.tasks.worker import WorkerSettings, reap_sla_evaluations

    assert reap_sla_evaluations in WorkerSettings.functions
    crons = {c.coroutine.__name__: c for c in WorkerSettings.cron_jobs}
    assert crons["reap_sla_evaluations"].hour == 3
    assert crons["reap_sla_evaluations"].minute == 23
    assert crons["reap_sla_evaluations"].run_at_startup is False
