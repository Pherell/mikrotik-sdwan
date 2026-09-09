"""M7 telemetry: the poller writes what netwatch and system/resource already
measure, and the series endpoint reads it back.

RouterOS was already collecting loss/RTT/jitter for every tunnel to drive SLA
based failover. The controller wrote those probes and never read them back --
this is that read, and these are the tests that it actually happens and that
one tenant cannot read another's.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.drivers.ros7_rest import Ros7RestDriver
from app.main import create_app
from app.models import Base, Fabric, Link, Site, User, Wan
from app.models.enums import Role, SiteStatus, Topology, Transport
from app.models.telemetry import Sample
from app.security import hash_password
from app.telemetry.poller import (
    CPU_PERCENT,
    FREE_MEMORY_BYTES,
    LOSS_PERCENT,
    RTT_AVG_MS,
    RTT_JITTER_MS,
    poll_all,
    poll_site,
)
from tests.fakeros.server import FakeRouterOS

NETWATCH = {
    "tool/netwatch": [
        {
            "host": "10.255.0.1",
            "status": "up",
            "loss-percent": "3",
            "rtt-avg": "8ms120us",
            "rtt-min": "5ms",
            "rtt-max": "12ms",
            "rtt-jitter": "2ms500us",
        }
    ],
    "system/resource": [{"cpu-load": "17", "free-memory": "104857600"}],
}


def _site_and_link() -> tuple[Site, Site, Fabric, Link]:
    near = Site(id="site-a", name="branch", mgmt_host="10.0.0.2", username="admin", tenant_id="t")
    far = Site(id="site-b", name="hq", mgmt_host="10.0.0.3", username="admin", tenant_id="t")
    near_wan = Wan(id="wan-a", site_id="site-a", name="wan1", interface="ether1")
    far_wan = Wan(id="wan-b", site_id="site-b", name="wan1", interface="ether1")
    fabric = Fabric(
        id="fab-1", name="core", tenant_id="t", transport=Transport.ipsec_gre,
        topology=Topology.hub_spoke, asn=65000, mtu=1400,
    )
    link = Link(
        id="link-1", fabric_id="fab-1", a_wan_id="wan-a", b_wan_id="wan-b",
        slug="branch-hq", a_tunnel_ip="10.255.0.0", b_tunnel_ip="10.255.0.1",
        subnet="10.255.0.0/31", enabled=True, state="applied",
    )
    near.wans = [near_wan]
    far.wans = [far_wan]
    return near, far, fabric, link


# -- poll_site, against a fake device ----------------------------------------


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


async def test_a_poll_writes_a_sample_for_every_link_metric_present(
    db: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    near, far, fabric, link = _site_and_link()
    async with db() as s:
        s.add_all([near, far, fabric, link])
        await s.commit()

    fake = FakeRouterOS(password="secret", menus={**NETWATCH})

    @asynccontextmanager
    async def fake_open_driver(site, _box=None):
        d = Ros7RestDriver(
            site.mgmt_host, "admin", "secret",
            transport=httpx.ASGITransport(app=fake.app),
        )
        await d.connect()
        try:
            yield d
        finally:
            await d.close()

    monkeypatch.setattr("app.telemetry.poller.open_driver", fake_open_driver)

    async with db() as s:
        site = await s.get(Site, "site-a")
        samples = await poll_site(s, site)
        await s.commit()

    by_metric = {sample.metric: sample.value for sample in samples}
    assert by_metric[LOSS_PERCENT] == 3.0
    assert by_metric[RTT_AVG_MS] == 8.12
    assert by_metric[RTT_JITTER_MS] == 2.5
    assert by_metric[CPU_PERCENT] == 17.0
    assert by_metric[FREE_MEMORY_BYTES] == 104857600.0
    # Every link sample carries the link it belongs to; the two site-level
    # ones (cpu, free memory) carry none.
    link_samples = [s for s in samples if s.metric != CPU_PERCENT and s.metric != FREE_MEMORY_BYTES]
    assert all(s.link_id == "link-1" for s in link_samples)
    site_samples = [s for s in samples if s.metric in (CPU_PERCENT, FREE_MEMORY_BYTES)]
    assert all(s.link_id is None for s in site_samples)


async def test_a_probe_with_no_response_writes_no_sample_for_that_link(
    db: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    """A netwatch row for a host nobody is probing must not be invented."""
    near, far, fabric, link = _site_and_link()
    async with db() as s:
        s.add_all([near, far, fabric, link])
        await s.commit()

    fake = FakeRouterOS(
        password="secret",
        menus={"tool/netwatch": [], "system/resource": [{"cpu-load": "5"}]},
    )

    @asynccontextmanager
    async def fake_open_driver(site, _box=None):
        d = Ros7RestDriver(
            site.mgmt_host, "admin", "secret",
            transport=httpx.ASGITransport(app=fake.app),
        )
        await d.connect()
        try:
            yield d
        finally:
            await d.close()

    monkeypatch.setattr("app.telemetry.poller.open_driver", fake_open_driver)

    async with db() as s:
        site = await s.get(Site, "site-a")
        samples = await poll_site(s, site)

    metrics = {sample.metric for sample in samples}
    assert LOSS_PERCENT not in metrics
    assert CPU_PERCENT in metrics


async def test_a_device_that_cannot_be_reached_writes_nothing_and_does_not_raise(
    db: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    near, far, fabric, link = _site_and_link()
    async with db() as s:
        s.add_all([near, far, fabric, link])
        await s.commit()

    from app.drivers.base import DeviceUnreachable

    @asynccontextmanager
    async def fake_open_driver(site, _box=None):
        raise DeviceUnreachable("no route to host")
        yield  # pragma: no cover - unreachable, satisfies the generator shape

    monkeypatch.setattr("app.telemetry.poller.open_driver", fake_open_driver)

    async with db() as s:
        site = await s.get(Site, "site-a")
        samples = await poll_site(s, site)

    assert samples == []


async def test_poll_all_skips_a_site_that_has_never_been_applied(
    db: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    """Nothing to probe on a site with no rendered config yet -- 'unprovisioned'
    is not a device the poller should even try to reach."""
    near, far, fabric, link = _site_and_link()
    near.status = SiteStatus.unprovisioned
    async with db() as s:
        s.add_all([near, far, fabric, link])
        await s.commit()

    called = False

    @asynccontextmanager
    async def fake_open_driver(site, _box=None):
        nonlocal called
        called = True
        raise AssertionError("should not be called")
        yield  # pragma: no cover

    monkeypatch.setattr("app.telemetry.poller.open_driver", fake_open_driver)

    async with db() as s:
        written = await poll_all(s)

    assert written == 0
    assert called is False


# -- GET /links/{id}/series ---------------------------------------------------


@pytest.fixture
async def api() -> AsyncIterator[tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_session] = _session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1"
    ) as client:
        yield client, maker
    await engine.dispose()


async def _token(client: httpx.AsyncClient, email: str) -> str:
    resp = await client.post(
        "/auth/login", json={"email": email, "password": "correct-horse"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def test_the_series_endpoint_returns_points_in_range(
    api: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, maker = api
    near, far, fabric, link = _site_and_link()
    now = datetime.now(UTC)
    async with maker() as s:
        s.add_all(
            [
                near,
                far,
                fabric,
                link,
                User(email="op@example.com", role=Role.operator,
                     password_hash=hash_password("correct-horse"), tenant_id="t"),
                Sample(tenant_id="t", site_id="site-a", link_id="link-1",
                       metric=LOSS_PERCENT, value=1.0, collected_at=now - timedelta(minutes=30)),
                Sample(tenant_id="t", site_id="site-a", link_id="link-1",
                       metric=LOSS_PERCENT, value=45.0, collected_at=now - timedelta(minutes=5)),
                # Outside the default 1h window.
                Sample(tenant_id="t", site_id="site-a", link_id="link-1",
                       metric=LOSS_PERCENT, value=99.0, collected_at=now - timedelta(hours=3)),
                # A different metric on the same link: must not leak in.
                Sample(tenant_id="t", site_id="site-a", link_id="link-1",
                       metric=RTT_AVG_MS, value=8.0, collected_at=now - timedelta(minutes=5)),
            ]
        )
        await s.commit()

    token = await _token(client, "op@example.com")
    resp = await client.get(
        "/links/link-1/series",
        headers={"Authorization": f"Bearer {token}"},
        params={"metric": "loss_percent"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["metric"] == "loss_percent"
    values = [p["value"] for p in body["points"]]
    assert values == [1.0, 45.0]  # ascending, in range, right metric only


async def test_an_unknown_metric_is_rejected() -> None:
    """400, not a silently-empty series -- a typo in the metric name should
    not read as 'nothing happened on this link'."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_session] = _session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1"
    ) as client:
        async with maker() as s:
            s.add(User(email="op@example.com", role=Role.operator,
                       password_hash=hash_password("correct-horse")))
            await s.commit()
        token = await _token(client, "op@example.com")
        resp = await client.get(
            "/links/link-1/series",
            headers={"Authorization": f"Bearer {token}"},
            params={"metric": "packets_eaten_by_gremlins"},
        )
    await engine.dispose()
    assert resp.status_code == 400


async def test_a_link_in_another_tenant_is_not_found(
    api: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, maker = api
    near, far, fabric, link = _site_and_link()  # tenant "t"
    async with maker() as s:
        s.add_all(
            [
                near, far, fabric, link,
                User(email="op-b@example.com", role=Role.operator,
                     password_hash=hash_password("correct-horse"), tenant_id="other"),
            ]
        )
        await s.commit()

    token = await _token(client, "op-b@example.com")
    resp = await client.get(
        "/links/link-1/series",
        headers={"Authorization": f"Bearer {token}"},
        params={"metric": "loss_percent"},
    )
    assert resp.status_code == 404
