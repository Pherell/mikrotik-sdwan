"""M9 at the HTTP layer: SNI pattern validation, and that a policy combining
SNI matching with load_balance is refused with a clear reason rather than
silently rendering the wrong mangle rules."""

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
from app.security import hash_password

ApiFixture = tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]]


@pytest.fixture
async def api() -> AsyncIterator[ApiFixture]:
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


async def _seed(maker, role: Role, email: str) -> User:
    async with maker() as s:
        user = User(email=email, role=role, password_hash=hash_password("correct-horse"))
        s.add(user)
        await s.commit()
        return user


async def _token(client: httpx.AsyncClient, email: str) -> str:
    resp = await client.post(
        "/auth/login", json={"email": email, "password": "correct-horse"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _headers(client, maker) -> dict[str, str]:
    await _seed(maker, Role.operator, "op@example.com")
    return _auth(await _token(client, "op@example.com"))


# -- pattern validation -------------------------------------------------------


@pytest.mark.parametrize(
    "pattern",
    [
        "*.teams.microsoft.com; /system reboot",
        "*.teams microsoft.com",
        "$(reboot)",
        "*..com",
        "",
        "a@b.com",
        "*.*.com",
    ],
)
async def test_a_hostile_or_malformed_sni_pattern_is_rejected(api, pattern: str) -> None:
    client, maker = api
    headers = await _headers(client, maker)
    resp = await client.post(
        "/app-groups", headers=headers,
        json={"name": "bad", "sni_patterns": [pattern]},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize(
    "pattern", ["*.teams.microsoft.com", "office.com", "a-b.example.co.uk"]
)
async def test_an_ordinary_sni_pattern_is_accepted(api, pattern: str) -> None:
    client, maker = api
    headers = await _headers(client, maker)
    resp = await client.post(
        "/app-groups", headers=headers,
        json={"name": f"good-{pattern[:4]}", "sni_patterns": [pattern]},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["sni_patterns"] == [pattern]


# -- the load_balance conflict ------------------------------------------------


async def _sni_app_group(client, headers) -> str:
    resp = await client.post(
        "/app-groups", headers=headers,
        json={"name": "teams", "sni_patterns": ["*.teams.microsoft.com"]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _sdwan_group(client, headers, strategy: str) -> str:
    body: dict = {"name": f"grp-{strategy}", "members": [{"uplink": "wan1"}]}
    if strategy == "load_balance":
        body["members"] = [
            {"uplink": "wan1", "weight": 1}, {"uplink": "wan2", "weight": 1}
        ]
        body["strategy"] = "load_balance"
    resp = await client.post("/sdwan-groups", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_creating_a_policy_with_sni_and_load_balance_is_refused(api) -> None:
    client, maker = api
    headers = await _headers(client, maker)
    app_group_id = await _sni_app_group(client, headers)
    lb_group_id = await _sdwan_group(client, headers, "load_balance")

    resp = await client.post(
        "/policies", headers=headers,
        json={"name": "voice", "app_group_id": app_group_id, "sdwan_group_id": lb_group_id},
    )
    assert resp.status_code == 400, resp.text
    assert "load_balance" in resp.text


async def test_creating_a_policy_with_sni_and_failover_succeeds(api) -> None:
    client, maker = api
    headers = await _headers(client, maker)
    app_group_id = await _sni_app_group(client, headers)
    failover_group_id = await _sdwan_group(client, headers, "failover")

    resp = await client.post(
        "/policies", headers=headers,
        json={
            "name": "voice", "app_group_id": app_group_id,
            "sdwan_group_id": failover_group_id,
        },
    )
    assert resp.status_code == 201, resp.text


async def test_updating_a_policy_into_the_conflict_is_also_refused(api) -> None:
    """The conflict is checked against the *effective* state after a partial
    PATCH, not just what a create body happened to contain."""
    client, maker = api
    headers = await _headers(client, maker)
    app_group_id = await _sni_app_group(client, headers)
    failover_group_id = await _sdwan_group(client, headers, "failover")
    lb_group_id = await _sdwan_group(client, headers, "load_balance")

    created = await client.post(
        "/policies", headers=headers,
        json={
            "name": "voice", "app_group_id": app_group_id,
            "sdwan_group_id": failover_group_id,
        },
    )
    assert created.status_code == 201, created.text
    policy_id = created.json()["id"]

    # Only sdwan_group_id changes; app_group_id is not repeated in the body.
    resp = await client.patch(
        f"/policies/{policy_id}", headers=headers,
        json={"sdwan_group_id": lb_group_id},
    )
    assert resp.status_code == 400, resp.text
    assert "load_balance" in resp.text
