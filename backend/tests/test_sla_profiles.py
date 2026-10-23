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
