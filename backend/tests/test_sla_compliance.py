"""Fase 2 SLA compliance aggregation endpoint.

GET /sla-compliance rolls persisted SlaEvaluation rows up into totals,
per-link and per-site compliance, without any device/driver calls.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.main import create_app
from app.models import Base, Fabric, Link, Site, User, Wan
from app.models.enums import Role, Topology, Transport
from app.models.telemetry import SlaEvaluation
from app.security import hash_password


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


def _site_and_link_other() -> tuple[Site, Site, Fabric, Link]:
    near = Site(
        id="site-c", name="branch2", mgmt_host="10.0.0.4", username="admin", tenant_id="other"
    )
    far = Site(id="site-d", name="hq2", mgmt_host="10.0.0.5", username="admin", tenant_id="other")
    near_wan = Wan(id="wan-c", site_id="site-c", name="wan1", interface="ether1")
    far_wan = Wan(id="wan-d", site_id="site-d", name="wan1", interface="ether1")
    fabric = Fabric(
        id="fab-2", name="core2", tenant_id="other", transport=Transport.ipsec_gre,
        topology=Topology.hub_spoke, asn=65001, mtu=1400,
    )
    link = Link(
        id="link-2", fabric_id="fab-2", a_wan_id="wan-c", b_wan_id="wan-d",
        slug="branch2-hq2", a_tunnel_ip="10.255.0.2", b_tunnel_ip="10.255.0.3",
        subnet="10.255.0.2/31", enabled=True, state="applied",
    )
    near.wans = [near_wan]
    far.wans = [far_wan]
    return near, far, fabric, link


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


def _evaluation(
    bucket: datetime,
    classification: str,
    previous_class: str | None = None,
    *,
    tenant_id: str = "t",
    site_id: str = "site-a",
    link_id: str = "link-1",
) -> SlaEvaluation:
    return SlaEvaluation(
        tenant_id=tenant_id, site_id=site_id, link_id=link_id,
        bucket_start=bucket, window_start=bucket - timedelta(minutes=30),
        window_end=bucket, window_minutes=30, loss_percent=1.0,
        latency_ms=42.0, jitter_ms=None, classification=classification,
        previous_class=previous_class, evaluated_at=bucket,
    )


def _user(email: str, tenant_id: str = "t") -> User:
    return User(
        email=email, role=Role.operator,
        password_hash=hash_password("correct-horse"), tenant_id=tenant_id,
    )


async def test_aggregation_maths(
    api: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, maker = api
    near, far, fabric, link = _site_and_link()
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    # in_sla, degraded (prev in_sla), down (prev degraded - continuation),
    # in_sla, down (prev in_sla - new episode)
    evals = [
        _evaluation(now - timedelta(minutes=50), "in_sla", None),
        _evaluation(now - timedelta(minutes=40), "degraded", "in_sla"),
        _evaluation(now - timedelta(minutes=30), "down", "degraded"),
        _evaluation(now - timedelta(minutes=20), "in_sla", "down"),
        _evaluation(now - timedelta(minutes=10), "down", "in_sla"),
    ]
    async with maker() as s:
        s.add_all([near, far, fabric, link, _user("op@example.com"), *evals])
        await s.commit()

    token = await _token(client, "op@example.com")
    resp = await client.get(
        "/sla-compliance", params={"window": "7d"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["window"] == "7d"
    assert body["window_hours"] == 168
    totals = body["totals"]
    assert totals["buckets"] == 5
    assert totals["counts"] == {"in_sla": 2, "degraded": 1, "down": 2}
    assert totals["shares"]["in_sla"] == pytest.approx(0.4)
    assert totals["shares"]["degraded"] == pytest.approx(0.2)
    assert totals["shares"]["down"] == pytest.approx(0.4)
    assert totals["violation_buckets"] == 3
    assert totals["violation_episodes"] == 2
    assert totals["links_total"] == 1
    assert totals["links_with_data"] == 1
    assert totals["paths_with_data"] == 1

    assert len(body["links"]) == 1
    link_body = body["links"][0]
    assert link_body["link_id"] == "link-1"
    assert link_body["slug"] == "branch-hq"
    assert link_body["fabric_id"] == "fab-1"
    assert link_body["fabric_name"] == "core"
    assert link_body["a_site_name"] == "branch"
    assert link_body["a_wan_name"] == "wan1"
    assert link_body["b_site_name"] == "hq"
    assert link_body["b_wan_name"] == "wan1"
    assert link_body["has_data"] is True
    assert link_body["buckets"] == 5
    assert link_body["counts"] == {"in_sla": 2, "degraded": 1, "down": 2}
    assert link_body["shares"]["in_sla"] == pytest.approx(0.4)
    assert link_body["violation_episodes"] == 2
    assert link_body["current_class"] == "down"
    # totals match the single-link rollup
    assert totals["buckets"] == link_body["buckets"]
    assert totals["counts"] == link_body["counts"]
    assert totals["violation_episodes"] == link_body["violation_episodes"]


async def test_empty_case(
    api: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, maker = api
    near, far, fabric, link = _site_and_link()
    async with maker() as s:
        s.add_all([near, far, fabric, link, _user("op@example.com")])
        await s.commit()

    token = await _token(client, "op@example.com")
    resp = await client.get(
        "/sla-compliance", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["totals"]["buckets"] == 0
    assert body["totals"]["counts"] == {"in_sla": 0, "degraded": 0, "down": 0}
    # Shares are null, not 0 -- never fabricate compliance from no data.
    assert body["totals"]["shares"] == {"in_sla": None, "degraded": None, "down": None}
    assert body["totals"]["violation_buckets"] == 0
    assert body["totals"]["violation_episodes"] == 0
    assert body["totals"]["links_total"] == 1
    assert body["totals"]["links_with_data"] == 0
    assert body["totals"]["paths_with_data"] == 0
    assert len(body["links"]) == 1
    link_body = body["links"][0]
    assert link_body["has_data"] is False
    assert link_body["buckets"] == 0
    assert link_body["counts"] == {"in_sla": 0, "degraded": 0, "down": 0}
    assert link_body["shares"] == {"in_sla": None, "degraded": None, "down": None}
    assert link_body["violation_episodes"] == 0
    assert link_body["current_class"] is None
    assert link_body["last_bucket_start"] is None


async def test_tenant_isolation(
    api: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, maker = api
    near, far, fabric, link = _site_and_link()
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    async with maker() as s:
        s.add_all(
            [
                near, far, fabric, link,
                _user("op@example.com", tenant_id="t"),
                _user("other@example.com", tenant_id="other"),
                _evaluation(now - timedelta(minutes=20), "in_sla", None),
                _evaluation(now - timedelta(minutes=10), "down", "in_sla"),
            ]
        )
        await s.commit()

    # A user of tenant "other" (which has no links yet) sees nothing.
    other_token = await _token(client, "other@example.com")
    resp = await client.get(
        "/sla-compliance", headers={"Authorization": f"Bearer {other_token}"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["totals"]["buckets"] == 0
    assert body["links"] == []
    assert body["totals"]["links_total"] == 0

    # Seed a second tenant's link + rows; the first tenant must exclude them.
    near2, far2, fabric2, link2 = _site_and_link_other()
    async with maker() as s:
        s.add_all(
            [
                near2, far2, fabric2, link2,
                _evaluation(
                    now - timedelta(minutes=15), "down", None,
                    tenant_id="other", site_id="site-c", link_id="link-2",
                ),
                _evaluation(
                    now - timedelta(minutes=5), "down", "down",
                    tenant_id="other", site_id="site-c", link_id="link-2",
                ),
                _evaluation(
                    now - timedelta(minutes=1), "degraded", "down",
                    tenant_id="other", site_id="site-c", link_id="link-2",
                ),
            ]
        )
        await s.commit()

    token = await _token(client, "op@example.com")
    resp = await client.get(
        "/sla-compliance", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["totals"]["buckets"] == 2
    assert body["totals"]["counts"] == {"in_sla": 1, "degraded": 0, "down": 1}
    assert body["totals"]["links_total"] == 1
    assert [entry["link_id"] for entry in body["links"]] == ["link-1"]


async def test_window_validation(
    api: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, maker = api
    near, far, fabric, link = _site_and_link()
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    async with maker() as s:
        s.add_all(
            [
                near, far, fabric, link,
                _user("op@example.com"),
                _evaluation(now - timedelta(days=3), "in_sla", None),
                _evaluation(now - timedelta(hours=1), "degraded", "in_sla"),
            ]
        )
        await s.commit()

    token = await _token(client, "op@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get(
        "/sla-compliance", params={"window": "90d"}, headers=headers
    )
    assert resp.status_code == 422, resp.text

    resp = await client.get(
        "/sla-compliance", params={"window": "24h"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["window"] == "24h"
    assert body["window_hours"] == 24
    assert body["totals"]["buckets"] == 1
    assert body["totals"]["counts"] == {"in_sla": 0, "degraded": 1, "down": 0}

    resp = await client.get(
        "/sla-compliance", params={"window": "7d"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["totals"]["buckets"] == 2
    assert body["totals"]["counts"] == {"in_sla": 1, "degraded": 1, "down": 0}
