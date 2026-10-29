"""PATCH /sla-profiles/{profile_id}: partial update of an SLA profile."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.main import create_app
from app.models import Base, User
from app.models.enums import Role
from app.models.policy import SlaProfile
from app.security import hash_password


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


def _profile(
    id_: str, tenant_id: str, name: str, **kwargs
) -> SlaProfile:
    defaults = dict(
        loss_percent=20, latency_ms=300, jitter_ms=None,
        probe_interval_seconds=10, probe_count=10,
        recovery_seconds=60, eval_window_minutes=30,
    )
    defaults.update(kwargs)
    return SlaProfile(id=id_, tenant_id=tenant_id, name=name, **defaults)


def _user(email: str, tenant_id: str = "t") -> User:
    return User(
        email=email, role=Role.operator,
        password_hash=hash_password("correct-horse"), tenant_id=tenant_id,
    )


async def _seed(maker: async_sessionmaker[AsyncSession]) -> None:
    async with maker() as s:
        s.add_all(
            [
                _user("op@example.com", tenant_id="t"),
                _profile("sla-1", "t", "voice"),
                _profile("sla-2", "t", "video"),
                _profile("sla-other", "other", "foreign"),
            ]
        )
        await s.commit()


async def test_patch_updates_thresholds_and_eval_window(
    api: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, maker = api
    await _seed(maker)
    token = await _token(client, "op@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.patch(
        "/sla-profiles/sla-1",
        json={
            "loss_percent": 5,
            "latency_ms": 123,
            "jitter_ms": 42,
            "eval_window_minutes": 15,
            "probe_interval_seconds": 5,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == "sla-1"
    assert body["loss_percent"] == 5
    assert body["latency_ms"] == 123
    assert body["jitter_ms"] == 42
    assert body["eval_window_minutes"] == 15
    assert body["probe_interval_seconds"] == 5
    # detection_seconds is still derived: probe_interval_seconds * 2
    assert body["detection_seconds"] == 10
    # Untouched fields keep their stored values.
    assert body["probe_count"] == 10
    assert body["name"] == "voice"


async def test_patch_other_tenant_profile_is_404(
    api: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, maker = api
    await _seed(maker)
    token = await _token(client, "op@example.com")
    resp = await client.patch(
        "/sla-profiles/sla-other",
        json={"loss_percent": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, resp.text


async def test_patch_rename_to_existing_name_is_409(
    api: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, maker = api
    await _seed(maker)
    token = await _token(client, "op@example.com")
    resp = await client.patch(
        "/sla-profiles/sla-1",
        json={"name": "video"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409, resp.text


async def test_create_and_patch_round_trip_the_a3a5_fields(
    api: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """The A3/A5 profile fields are stored, returned, and patchable --
    not just accepted and silently dropped."""
    client, maker = api
    await _seed(maker)
    token = await _token(client, "op@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/sla-profiles",
        json={
            "name": "critical",
            "probe_type": "tcp-conn",
            "probe_port": 8291,
            "probe_targets": ["1.1.1.1", "8.8.8.8"],
            "fail_count": 3,
            "restore_count": 2,
            "failback_latency_margin_ms": 25,
            "failback_jitter_margin_ms": 7,
        },
        headers=headers,
    )
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert body["probe_type"] == "tcp-conn"
    assert body["probe_port"] == 8291
    assert body["probe_targets"] == ["1.1.1.1", "8.8.8.8"]
    assert body["fail_count"] == 3
    assert body["restore_count"] == 2
    assert body["failback_latency_margin_ms"] == 25
    assert body["failback_jitter_margin_ms"] == 7

    resp = await client.patch(
        f"/sla-profiles/{body['id']}",
        json={"fail_count": 8, "probe_targets": []},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["fail_count"] == 8
    assert resp.json()["probe_targets"] == []


async def test_create_rejects_the_a3a5_misconfigurations(
    api: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, maker = api
    await _seed(maker)
    token = await _token(client, "op@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    for payload in (
        # tcp-conn without the port it is meant to connect to.
        {"name": "bad1", "probe_type": "tcp-conn"},
        # A failback margin that swallows the whole threshold.
        {"name": "bad2", "latency_ms": 100, "failback_latency_margin_ms": 100},
        # A target that is a name, not an address.
        {"name": "bad3", "probe_targets": ["dns.example.com"]},
        # A dns probe without the name it should resolve.
        {"name": "bad4", "probe_type": "dns"},
    ):
        resp = await client.post("/sla-profiles", json=payload, headers=headers)
        assert resp.status_code == 422, (payload, resp.text)
