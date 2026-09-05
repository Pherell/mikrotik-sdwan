"""Plan and apply through the HTTP surface, against a fake RouterOS device."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.drivers.ros7_rest import Ros7RestDriver
from app.main import create_app
from app.models import Base, Job, User
from app.models.enums import JobState, Role
from app.security import hash_password
from tests.fakeros.server import FakeRouterOS


@pytest.fixture
def ros() -> FakeRouterOS:
    return FakeRouterOS(
        password="secret",
        menus={
            "interface/bridge": [],
            "ip/address": [],
            "ip/firewall/address-list": [],
            "system/scheduler": [],
        },
    )


@pytest.fixture
async def api(ros: FakeRouterOS, monkeypatch) -> AsyncIterator[tuple]:
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

    # Point every driver the service layer opens at the fake device.
    @asynccontextmanager
    async def fake_open_driver(_site, _box=None):
        d = Ros7RestDriver(
            "fake", "admin", "secret", transport=httpx.ASGITransport(app=ros.app)
        )
        await d.connect()
        try:
            yield d
        finally:
            await d.close()

    monkeypatch.setattr("app.services.reconcile.open_driver", fake_open_driver)

    app = create_app()
    app.dependency_overrides[get_session] = _session
    async with maker() as s:
        s.add(
            User(
                email="op@example.com",
                role=Role.operator,
                password_hash=hash_password("correct-horse"),
            )
        )
        s.add(
            User(
                email="viewer@example.com",
                role=Role.viewer,
                password_hash=hash_password("correct-horse"),
            )
        )
        await s.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1"
    ) as client:
        yield client, maker, ros
    await engine.dispose()


async def _auth(client: httpx.AsyncClient, email: str = "op@example.com") -> dict[str, str]:
    resp = await client.post(
        "/auth/login", json={"email": email, "password": "correct-horse"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _make_site(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    resp = await client.post(
        "/sites",
        headers=headers,
        json={
            "name": "branch-1",
            "mgmt_host": "203.0.113.10",
            "username": "admin",
            "password": "secret",
            "loopback_ip": "10.254.0.7",
            "local_prefixes": ["192.168.10.0/24"],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# -- plan -------------------------------------------------------------------


async def test_plan_reports_changes_without_touching_the_device(api) -> None:
    client, _, ros = api
    headers = await _auth(client)
    site_id = await _make_site(client, headers)

    resp = await client.post(f"/sites/{site_id}/plan", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["empty"] is False
    assert body["counts"]["add"] == 3  # bridge + loopback address + 1 prefix
    assert "+ /interface/bridge" in body["text"]
    # Nothing was written.
    assert ros.rows("interface/bridge") == []


async def test_viewer_may_plan(api) -> None:
    client, _, _ = api
    op_headers = await _auth(client)
    site_id = await _make_site(client, op_headers)

    viewer = await _auth(client, "viewer@example.com")
    assert (await client.post(f"/sites/{site_id}/plan", headers=viewer)).status_code == 200


async def test_viewer_may_not_apply(api) -> None:
    client, _, _ = api
    op_headers = await _auth(client)
    site_id = await _make_site(client, op_headers)

    viewer = await _auth(client, "viewer@example.com")
    resp = await client.post(
        f"/sites/{site_id}/apply", headers=viewer, json={"confirm": True}
    )
    assert resp.status_code == 403


# -- apply ------------------------------------------------------------------


async def test_apply_requires_explicit_confirmation(api) -> None:
    client, _, ros = api
    headers = await _auth(client)
    site_id = await _make_site(client, headers)

    resp = await client.post(f"/sites/{site_id}/apply", headers=headers, json={})

    assert resp.status_code == 400
    assert "reboots" in resp.json()["detail"]
    assert ros.rows("interface/bridge") == []


async def test_apply_pushes_and_records_a_job(api) -> None:
    client, _, ros = api
    headers = await _auth(client)
    site_id = await _make_site(client, headers)

    resp = await client.post(
        f"/sites/{site_id}/apply", headers=headers, json={"confirm": True}
    )

    assert resp.status_code == 200, resp.text
    job = resp.json()
    assert job["state"] == JobState.succeeded
    assert job["result"]["applied"] == 3
    assert job["backup_name"]
    assert job["rollback_token"] is None  # disarmed on success
    assert job["log"]

    assert [r["name"] for r in ros.rows("interface/bridge")] == ["lo-sdwan"]
    assert ros.rows("system/scheduler") == []


async def test_second_apply_is_a_no_op(api) -> None:
    """End-to-end idempotency: the controller must not push forever."""
    client, _, _ = api
    headers = await _auth(client)
    site_id = await _make_site(client, headers)

    first = await client.post(
        f"/sites/{site_id}/apply", headers=headers, json={"confirm": True}
    )
    assert first.json()["result"]["applied"] == 3

    second = await client.post(
        f"/sites/{site_id}/apply", headers=headers, json={"confirm": True}
    )
    body = second.json()
    assert body["state"] == JobState.succeeded
    assert body["result"]["applied"] == 0
    assert body["result"]["message"] == "no changes"


async def test_dry_run_records_a_plan_but_pushes_nothing(api) -> None:
    client, _, ros = api
    headers = await _auth(client)
    site_id = await _make_site(client, headers)

    resp = await client.post(
        f"/sites/{site_id}/apply", headers=headers, json={"dry_run": True}
    )

    job = resp.json()
    assert job["state"] == JobState.succeeded
    assert job["result"]["applied"] == 0
    assert job["result"]["dry_run"] is True
    assert job["plan"]["counts"]["add"] == 3
    assert ros.rows("interface/bridge") == []


async def test_apply_refuses_when_a_managed_menu_cannot_be_read(api) -> None:
    """An unreadable menu must not be applied as 'delete everything in it'."""
    client, _, ros = api
    headers = await _auth(client)
    site_id = await _make_site(client, headers)
    del ros.menus["ip/firewall/address-list"]

    resp = await client.post(
        f"/sites/{site_id}/apply", headers=headers, json={"confirm": True}
    )

    job = resp.json()
    assert job["state"] == JobState.failed
    assert "could not read" in job["error"]
    assert ros.rows("interface/bridge") == []


async def test_jobs_are_listed_newest_first(api) -> None:
    client, maker, _ = api
    headers = await _auth(client)
    site_id = await _make_site(client, headers)

    await client.post(f"/sites/{site_id}/apply", headers=headers, json={"dry_run": True})
    await client.post(f"/sites/{site_id}/apply", headers=headers, json={"confirm": True})

    resp = await client.get("/jobs", headers=headers, params={"site_id": site_id})
    jobs = resp.json()

    assert len(jobs) == 2
    assert jobs[0]["created_at"] >= jobs[1]["created_at"]

    async with maker() as s:
        stored = list(await s.scalars(select(Job)))
    assert len(stored) == 2


async def test_job_detail_carries_the_diff_text(api) -> None:
    client, _, _ = api
    headers = await _auth(client)
    site_id = await _make_site(client, headers)

    created = await client.post(
        f"/sites/{site_id}/apply", headers=headers, json={"confirm": True}
    )
    job_id = created.json()["id"]

    resp = await client.get(f"/jobs/{job_id}", headers=headers)

    assert resp.status_code == 200
    assert "+ /ip/address" in resp.json()["diff"]["text"]


async def test_device_password_never_appears_in_a_job_record(api) -> None:
    client, maker, _ = api
    headers = await _auth(client)
    resp = await client.post(
        "/sites",
        headers=headers,
        json={
            "name": "branch-2",
            "mgmt_host": "203.0.113.11",
            "username": "admin",
            "password": "zebra-canyon-91-device-pw",
            "loopback_ip": "10.254.0.8",
        },
    )
    site_id = resp.json()["id"]

    applied = await client.post(
        f"/sites/{site_id}/apply", headers=headers, json={"confirm": True}
    )
    assert applied.status_code == 200

    assert "zebra-canyon-91-device-pw" not in applied.text
    # Nor anywhere in what was persisted for the job.
    async with maker() as s:
        job = (await s.scalars(select(Job))).one()
    stored = f"{job.plan}{job.diff}{job.result}{job.log}{job.error}"
    assert "zebra-canyon-91-device-pw" not in stored


# -- stale rollback recovery ------------------------------------------------


@pytest.fixture
def patched_jobs_driver(ros: FakeRouterOS, monkeypatch):
    """The rollback endpoints open their own driver, outside the service layer."""

    @asynccontextmanager
    async def fake(_site, _box=None):
        d = Ros7RestDriver(
            "fake", "admin", "secret", transport=httpx.ASGITransport(app=ros.app)
        )
        await d.connect()
        try:
            yield d
        finally:
            await d.close()

    monkeypatch.setattr("app.api.v1.jobs.open_driver", fake)
    return ros


async def test_lists_a_rollback_left_armed_by_a_crashed_controller(
    api, patched_jobs_driver
) -> None:
    client, _, ros = api
    headers = await _auth(client)
    site_id = await _make_site(client, headers)

    ros.rows("system/scheduler").append(
        {
            ".id": "*7",
            "name": "sdwan-rollback-job-orphan",
            "interval": "120s",
            "on-event": '/system/backup/load name=sdwan-pre-job-orphan password=""',
        }
    )

    resp = await client.get(f"/sites/{site_id}/rollbacks", headers=headers)

    assert resp.status_code == 200
    assert [r["name"] for r in resp.json()] == ["sdwan-rollback-job-orphan"]


async def test_clearing_a_rollback_disarms_it(api, patched_jobs_driver) -> None:
    client, _, ros = api
    headers = await _auth(client)
    site_id = await _make_site(client, headers)
    ros.rows("system/scheduler").append(
        {".id": "*7", "name": "sdwan-rollback-job-orphan", "interval": "120s"}
    )

    resp = await client.delete(
        f"/sites/{site_id}/rollbacks/sdwan-rollback-job-orphan", headers=headers
    )

    assert resp.status_code == 204
    assert ros.rows("system/scheduler") == []


async def test_clear_refuses_a_scheduler_the_controller_does_not_own(
    api, patched_jobs_driver
) -> None:
    """This endpoint must never become 'delete any scheduler entry'."""
    client, _, ros = api
    headers = await _auth(client)
    site_id = await _make_site(client, headers)
    ros.rows("system/scheduler").append(
        {".id": "*7", "name": "nightly-backup", "interval": "1d"}
    )

    resp = await client.delete(
        f"/sites/{site_id}/rollbacks/nightly-backup", headers=headers
    )

    assert resp.status_code == 400
    assert len(ros.rows("system/scheduler")) == 1
