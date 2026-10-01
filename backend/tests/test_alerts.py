"""Outbound alerting and the Prometheus endpoint.

What has to be true, in the order an operator would notice it missing:

- a tunnel going down produces exactly one alert, however many polls see it
  down, and coming back produces exactly one more;
- a destination that is down, slow or rejecting never fails the poll or the
  apply that raised the alert, and never leaks its URL or bot token into a
  log line or a stored error;
- one tenant cannot see, edit, test or receive another tenant's alerts or
  channels;
- /metrics does not exist until a token is configured, refuses the wrong
  token, and speaks the exposition format Prometheus parses.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import get_session
from app.drivers.base import DeviceUnreachable
from app.drivers.ros7_rest import Ros7RestDriver
from app.main import create_app
from app.models import Alert, AlertState, Base, Job, NotificationChannel, Site, User
from app.models.enums import (
    AlertKind,
    AlertSeverity,
    ChannelType,
    JobKind,
    JobState,
    Role,
    SiteStatus,
)
from app.models.telemetry import Sample
from app.security import hash_password
from app.services import alerts
from app.telemetry.poller import CPU_PERCENT, LOSS_PERCENT, RTT_AVG_MS, poll_site
from tests.fakeros.server import FakeRouterOS
from tests.test_telemetry import _site_and_link

WEBHOOK_URL = "https://hooks.example.com/services/T000/B000/sup3rs3cr3tpath"
BOT_TOKEN = "123456789:AAH-sup3r_s3cr3t-bot-token-value-xyz"


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch) -> None:
    monkeypatch.setattr(alerts, "RETRY_BACKOFF_SECONDS", 0)


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


def _netwatch(status: str, loss: str = "0") -> dict:
    return {
        "tool/netwatch": [
            {"host": "10.255.0.1", "status": status, "loss-percent": loss, "rtt-avg": "8ms"}
        ],
        "system/resource": [{"cpu-load": "17", "free-memory": "1000"}],
    }


class _Device:
    """A fake router whose netwatch answer the test can change between polls,
    or switch off entirely to look unreachable."""

    def __init__(self) -> None:
        self.menus = _netwatch("up")
        self.reachable = True

    def open_driver(self):
        @asynccontextmanager
        async def _open(site, _box=None):
            if not self.reachable:
                raise DeviceUnreachable("no route to host")
            fake = FakeRouterOS(password="secret", menus=dict(self.menus))
            d = Ros7RestDriver(
                site.mgmt_host, "admin", "secret", transport=httpx.ASGITransport(app=fake.app)
            )
            await d.connect()
            try:
                yield d
            finally:
                await d.close()

        return _open


async def _seed_link(db) -> None:
    near, far, fabric, link = _site_and_link()
    near.status = SiteStatus.reachable
    async with db() as s:
        s.add_all([near, far, fabric, link])
        await s.commit()


async def _poll(db) -> None:
    async with db() as s:
        await poll_site(s, await s.get(Site, "site-a"))
        await s.commit()


async def _kinds(db) -> list[str]:
    async with db() as s:
        rows = await s.scalars(select(Alert).order_by(Alert.created_at))
        return [r.kind for r in rows]


# -- transitions and de-duplication ------------------------------------------


async def test_link_alerts_fire_on_transitions_only(db, monkeypatch) -> None:
    await _seed_link(db)
    device = _Device()
    monkeypatch.setattr("app.telemetry.poller.open_driver", device.open_driver())

    await _poll(db)  # first look, healthy: a baseline, not an alert
    assert await _kinds(db) == []

    device.menus = _netwatch("down", loss="100")
    for _ in range(3):  # down for three polls: one alert
        await _poll(db)
    assert await _kinds(db) == [AlertKind.link_down]

    device.menus = _netwatch("up")
    await _poll(db)
    await _poll(db)
    assert await _kinds(db) == [AlertKind.link_down, AlertKind.link_up]

    async with db() as s:
        down = await s.scalar(select(Alert).where(Alert.kind == AlertKind.link_down))
    assert down.severity == AlertSeverity.critical
    assert down.link_id == "link-1" and down.site_id == "site-a" and down.tenant_id == "t"
    assert down.delivery_state == "pending"


async def test_partial_loss_is_an_sla_breach_not_a_link_down(db, monkeypatch) -> None:
    await _seed_link(db)
    device = _Device()
    monkeypatch.setattr("app.telemetry.poller.open_driver", device.open_driver())

    await _poll(db)
    device.menus = _netwatch("down", loss="30")
    await _poll(db)
    device.menus = _netwatch("down", loss="100")  # breach worsens to outage
    await _poll(db)
    assert await _kinds(db) == [AlertKind.sla_breach, AlertKind.link_down]


async def test_a_link_already_down_on_first_sight_still_alerts(db, monkeypatch) -> None:
    await _seed_link(db)
    device = _Device()
    device.menus = _netwatch("down", loss="100")
    monkeypatch.setattr("app.telemetry.poller.open_driver", device.open_driver())
    await _poll(db)
    await _poll(db)
    assert await _kinds(db) == [AlertKind.link_down]


async def test_reachability_alerts_on_edges_and_poll_still_writes_samples(
    db, monkeypatch
) -> None:
    await _seed_link(db)
    device = _Device()
    monkeypatch.setattr("app.telemetry.poller.open_driver", device.open_driver())

    await _poll(db)
    device.reachable = False
    await _poll(db)
    await _poll(db)
    device.reachable = True
    await _poll(db)
    assert await _kinds(db) == [AlertKind.site_unreachable, AlertKind.site_reachable]

    async with db() as s:
        unreachable = await s.scalar(
            select(Alert).where(Alert.kind == AlertKind.site_unreachable)
        )
        samples = list(await s.scalars(select(Sample)))
    assert "no route to host" in unreachable.message
    assert {x.metric for x in samples} >= {CPU_PERCENT, LOSS_PERCENT, RTT_AVG_MS}


async def test_a_broken_alert_hook_never_fails_the_poll(db, monkeypatch) -> None:
    """The recording side, this time: an exception inside the hook itself."""
    await _seed_link(db)
    device = _Device()
    monkeypatch.setattr("app.telemetry.poller.open_driver", device.open_driver())

    async def boom(*_a, **_k):
        raise RuntimeError("alert store on fire")

    monkeypatch.setattr(alerts, "_transition", boom)
    async with db() as s:
        samples = await poll_site(s, await s.get(Site, "site-a"))
        await s.commit()
    assert samples, "the poll must still write its samples"


async def test_drift_alerts_once_per_episode(db) -> None:
    site = Site(id="s1", name="branch", mgmt_host="h", username="u", tenant_id="t")
    async with db() as s:
        s.add(site)
        await s.commit()
    counts = {"add": 2, "set": 1, "remove": 0}
    async with db() as s:
        site = await s.get(Site, "s1")
        await alerts.observe_drift(s, site, drifted=False, counts={})
        await alerts.observe_drift(s, site, drifted=True, counts=counts)
        await alerts.observe_drift(s, site, drifted=True, counts=counts)  # same drift
        await alerts.observe_drift(s, site, drifted=False, counts={})  # fixed
        await alerts.observe_drift(s, site, drifted=True, counts=counts)  # new episode
        await s.commit()
    assert await _kinds(db) == [AlertKind.drift_detected, AlertKind.drift_detected]


async def test_drift_check_hook_fires_from_check_site(db, monkeypatch) -> None:
    """The hook as wired into services.drift: one drifted check alerts, a
    second identical check (the next hourly sweep) does not."""
    from app.services import drift

    site = Site(id="s1", name="branch", mgmt_host="h", username="u", tenant_id="t",
                status=SiteStatus.reachable)
    async with db() as s:
        s.add(site)
        await s.commit()

    class _DriftedPlan:
        empty = False
        counts = {"add": 1, "set": 0, "remove": 0}

        def to_json(self) -> dict:
            return {}

        def render(self) -> str:
            return ""

    async def fake_render(_s, _site):
        return []

    async def fake_plan(_driver, _sections):
        return _DriftedPlan()

    @asynccontextmanager
    async def fake_open(_site, _box=None):
        yield object()

    monkeypatch.setattr(drift, "render_device", fake_render)
    monkeypatch.setattr(drift, "open_driver", fake_open)
    monkeypatch.setattr(drift, "build_plan", fake_plan)

    for _ in range(2):
        async with db() as s:
            await drift.check_site(s, await s.get(Site, "s1"))
            await s.commit()
    assert await _kinds(db) == [AlertKind.drift_detected]


async def test_apply_hook_fires_for_failed_and_rolled_back_only(db) -> None:
    site = Site(id="s1", name="branch", mgmt_host="h", username="u", tenant_id="t")
    async with db() as s:
        s.add(site)
        await s.commit()
    async with db() as s:
        site = await s.get(Site, "s1")
        for state in (JobState.succeeded, JobState.failed, JobState.rolled_back):
            job = Job(kind=JobKind.apply, state=state, site_id="s1", tenant_id="t",
                      error="boom" if state != JobState.succeeded else None)
            s.add(job)
            await s.flush()
            await alerts.observe_apply(s, site, job)
        await s.commit()
    assert await _kinds(db) == [AlertKind.apply_failed, AlertKind.apply_rolled_back]


async def test_apply_site_records_an_alert_when_the_device_is_unreachable(
    db, monkeypatch
) -> None:
    from app.services import reconcile

    site = Site(id="s1", name="branch", mgmt_host="h", username="u", tenant_id="t")
    async with db() as s:
        s.add(site)
        await s.commit()

    @asynccontextmanager
    async def unreachable(_site, _box=None):
        raise DeviceUnreachable("connect timeout")
        yield  # pragma: no cover

    monkeypatch.setattr(reconcile, "open_driver", unreachable)
    async with db() as s:
        site = await s.get(Site, "s1")
        job = reconcile.new_job(site, JobKind.apply, None)
        s.add(job)
        await s.flush()
        await reconcile.apply_site(s, site, job)
        await s.commit()
    assert job.state == JobState.failed
    assert await _kinds(db) == [AlertKind.apply_failed]


# -- delivery ----------------------------------------------------------------


async def _channel(s, *, tenant="t", name="hook", type_=ChannelType.webhook,
                   config=None, min_severity=AlertSeverity.info, enabled=True):
    config = config or {"url": WEBHOOK_URL}
    enc, hint = alerts.encode_config(type_, config)
    ch = NotificationChannel(tenant_id=tenant, name=name, type=type_, config_enc=enc,
                             target_hint=hint, min_severity=min_severity, enabled=enabled)
    s.add(ch)
    await s.flush()
    return ch


async def _alert(s, *, tenant="t", kind=AlertKind.link_down, created_at=None):
    a = await alerts.record(
        s, alerts.AlertEvent(kind=kind, tenant_id=tenant, message=f"{kind} happened")
    )
    if created_at is not None:
        a.created_at = created_at
    await s.flush()
    return a


async def test_webhook_payload_shape(db) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    async with db() as s:
        await _channel(s)
        alert = await _alert(s)
        await s.commit()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            counts = await alerts.deliver_pending(s, client=client)

    assert counts["sent"] == 1
    assert str(seen[0].url) == WEBHOOK_URL
    body = json.loads(seen[0].content)
    assert body["version"] == 1 and body["source"] == "mikrotik-sdwan"
    assert body["id"] == alert.id
    assert body["kind"] == "link_down" and body["severity"] == "critical"
    assert body["tenant_id"] == "t"
    async with db() as s:
        stored = await s.get(Alert, alert.id)
    assert stored.delivery_state == "sent" and stored.delivered_at is not None


async def test_telegram_delivery_uses_send_message(db) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    async with db() as s:
        await _channel(s, type_=ChannelType.telegram,
                       config={"bot_token": BOT_TOKEN, "chat_id": "-100123"})
        await _alert(s)
        await s.commit()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await alerts.deliver_pending(s, client=client)

    assert seen[0].url.path == f"/bot{BOT_TOKEN}/sendMessage"
    body = json.loads(seen[0].content)
    assert body["chat_id"] == "-100123"
    assert "link_down" in body["text"] and "[CRITICAL]" in body["text"]


async def test_each_alert_is_delivered_once(db) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    async with db() as s:
        await _channel(s)
        await _alert(s)
        await s.commit()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await alerts.deliver_pending(s, client=client)
            second = await alerts.deliver_pending(s, client=client)
    assert calls == 1
    assert not any(second.values())


async def test_min_severity_and_disabled_channels_are_respected(db) -> None:
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.host)
        return httpx.Response(200)

    async with db() as s:
        await _channel(s, name="crit-only", min_severity=AlertSeverity.critical,
                       config={"url": "https://crit.example.com/h"})
        await _channel(s, name="off", enabled=False, config={"url": "https://off.example.com/h"})
        info = await _alert(s, kind=AlertKind.link_up)
        await s.commit()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            counts = await alerts.deliver_pending(s, client=client)
    assert hits == []
    assert counts["skipped"] == 1
    async with db() as s:
        assert (await s.get(Alert, info.id)).delivery_state == "skipped"


async def test_alerts_only_go_to_their_own_tenants_channels(db) -> None:
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.host)
        return httpx.Response(200)

    async with db() as s:
        await _channel(s, tenant="a", name="a", config={"url": "https://a.example.com/h"})
        await _channel(s, tenant="b", name="b", config={"url": "https://b.example.com/h"})
        await _alert(s, tenant="a")
        await s.commit()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await alerts.deliver_pending(s, client=client)
    assert hits == ["a.example.com"]


async def test_stale_pending_alerts_expire_instead_of_sending(db) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("an expired alert must not be sent")

    async with db() as s:
        await _channel(s)
        old = await _alert(s, created_at=datetime.now(UTC) - timedelta(hours=5))
        await s.commit()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            counts = await alerts.deliver_pending(s, client=client)
    assert counts["expired"] == 1
    async with db() as s:
        assert (await s.get(Alert, old.id)).delivery_state == "expired"


async def test_transient_errors_are_retried_a_limited_number_of_times(db) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    async with db() as s:
        ch = await _channel(s)
        alert = await _alert(s)
        await s.commit()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            counts = await alerts.deliver_pending(s, client=client)
    assert calls == 1 + get_settings().alert_delivery_retries
    assert counts["failed"] == 1
    async with db() as s:
        stored = await s.get(Alert, alert.id)
        channel = await s.get(NotificationChannel, ch.id)
    assert stored.delivery_state == "failed" and "HTTP 503" in stored.delivery_error
    assert channel.last_status == "error" and channel.failure_count == 1


async def test_a_4xx_is_not_retried(db) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    async with db() as s:
        await _channel(s)
        await _alert(s)
        await s.commit()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await alerts.deliver_pending(s, client=client)
    assert calls == 1


async def test_one_failing_channel_does_not_stop_the_others(db) -> None:
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.host)
        if request.url.host == "broken.example.com":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200)

    async with db() as s:
        await _channel(s, name="broken", config={"url": "https://broken.example.com/x"})
        await _channel(s, name="fine", config={"url": "https://fine.example.com/x"})
        alert = await _alert(s)
        await s.commit()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            counts = await alerts.deliver_pending(s, client=client)
    assert "fine.example.com" in hits
    assert counts["partial"] == 1
    async with db() as s:
        assert (await s.get(Alert, alert.id)).delivery_state == "partial"


async def test_delivery_errors_and_logs_never_contain_the_secret(db, caplog) -> None:
    """httpx puts the full URL in its exception text. For a webhook the URL
    is the credential, so every place an error lands has to be scrubbed."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed to connect to {request.url}", request=request)

    caplog.set_level(logging.DEBUG)
    async with db() as s:
        hook = await _channel(s, name="hook")
        tg = await _channel(s, name="tg", type_=ChannelType.telegram,
                            config={"bot_token": BOT_TOKEN, "chat_id": "-100123"})
        alert = await _alert(s)
        await s.commit()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await alerts.deliver_pending(s, client=client)

    async with db() as s:
        stored = await s.get(Alert, alert.id)
        channels = [await s.get(NotificationChannel, c.id) for c in (hook, tg)]
    texts = [stored.delivery_error or "", caplog.text] + [c.last_error or "" for c in channels]
    for text in texts:
        assert "sup3rs3cr3tpath" not in text
        assert BOT_TOKEN not in text
        assert "sup3r_s3cr3t" not in text
    assert "ConnectError" in stored.delivery_error  # still says *why*


def test_redact_catches_token_shapes_it_was_not_told_about() -> None:
    assert BOT_TOKEN not in alerts.redact(f"POST /bot{BOT_TOKEN}/x failed", [])


async def test_the_destination_is_encrypted_at_rest(db) -> None:
    async with db() as s:
        ch = await _channel(s)
        await s.commit()
    assert "sup3rs3cr3tpath" not in ch.config_enc
    assert "sup3rs3cr3tpath" not in ch.target_hint
    assert alerts.decode_config(ch) == {"url": WEBHOOK_URL}


# -- API: channels and alerts, tenant isolation ------------------------------


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
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, maker
    await engine.dispose()


async def _as(client, maker, role: Role, email: str, tenant: str) -> dict[str, str]:
    async with maker() as s:
        s.add(User(email=email, role=role, password_hash=hash_password("correct-horse"),
                   tenant_id=tenant))
        await s.commit()
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_channel_crud_never_returns_the_secret(api) -> None:
    client, maker = api
    admin = await _as(client, maker, Role.admin, "admin@example.com", "t")

    created = await client.post(
        "/api/v1/alerts/channels", headers=admin,
        json={"name": "ops", "type": "webhook", "url": WEBHOOK_URL, "min_severity": "warning"},
    )
    assert created.status_code == 201, created.text
    assert "sup3rs3cr3tpath" not in created.text
    assert created.json()["target_hint"] == "https://hooks.example.com/***"
    cid = created.json()["id"]

    listed = await client.get("/api/v1/alerts/channels", headers=admin)
    assert [c["id"] for c in listed.json()] == [cid]
    assert "sup3rs3cr3tpath" not in listed.text

    patched = await client.patch(
        f"/api/v1/alerts/channels/{cid}", headers=admin, json={"enabled": False}
    )
    assert patched.status_code == 200 and patched.json()["enabled"] is False

    # Telegram fields on a webhook channel: refused, not silently stored.
    wrong = await client.patch(
        f"/api/v1/alerts/channels/{cid}", headers=admin,
        json={"bot_token": BOT_TOKEN, "chat_id": "1"},
    )
    assert wrong.status_code == 422

    dup = await client.post(
        "/api/v1/alerts/channels", headers=admin,
        json={"name": "ops", "type": "webhook", "url": WEBHOOK_URL},
    )
    assert dup.status_code == 409

    # Audit rows must not carry the destination either.
    async with maker() as s:
        from app.models.job import AuditEvent

        details = [json.dumps(a.detail) for a in await s.scalars(select(AuditEvent))]
    assert details and not any("sup3rs3cr3tpath" in d for d in details)

    assert (await client.delete(f"/api/v1/alerts/channels/{cid}", headers=admin)).status_code == 204
    assert (await client.get("/api/v1/alerts/channels", headers=admin)).json() == []


async def test_channel_validation(api) -> None:
    client, maker = api
    admin = await _as(client, maker, Role.admin, "admin@example.com", "t")
    for body in (
        {"name": "x", "type": "webhook"},
        {"name": "x", "type": "webhook", "url": "ftp://nope"},
        {"name": "x", "type": "telegram", "bot_token": BOT_TOKEN},
        {"name": "x", "type": "telegram", "url": WEBHOOK_URL},
    ):
        resp = await client.post("/api/v1/alerts/channels", headers=admin, json=body)
        assert resp.status_code == 422, body


async def test_channels_are_admin_only(api) -> None:
    client, maker = api
    op = await _as(client, maker, Role.operator, "op@example.com", "t")
    assert (await client.get("/api/v1/alerts/channels", headers=op)).status_code == 403
    resp = await client.post(
        "/api/v1/alerts/channels", headers=op,
        json={"name": "x", "type": "webhook", "url": WEBHOOK_URL},
    )
    assert resp.status_code == 403


async def test_a_channel_is_invisible_to_another_tenant(api) -> None:
    client, maker = api
    a = await _as(client, maker, Role.admin, "admin-a@example.com", "tenant-a")
    b = await _as(client, maker, Role.admin, "admin-b@example.com", "tenant-b")
    created = await client.post(
        "/api/v1/alerts/channels", headers=a,
        json={"name": "ops", "type": "webhook", "url": WEBHOOK_URL},
    )
    cid = created.json()["id"]

    assert (
        await client.patch(f"/api/v1/alerts/channels/{cid}", headers=b, json={"enabled": False})
    ).status_code == 404
    assert (await client.post(f"/api/v1/alerts/channels/{cid}/test", headers=b)).status_code == 404
    assert (await client.delete(f"/api/v1/alerts/channels/{cid}", headers=b)).status_code == 404
    assert (await client.get("/api/v1/alerts/channels", headers=b)).json() == []
    # And still there for its owner.
    assert len((await client.get("/api/v1/alerts/channels", headers=a)).json()) == 1


async def test_alert_list_is_tenant_scoped_and_paginated(api) -> None:
    client, maker = api
    a = await _as(client, maker, Role.viewer, "viewer-a@example.com", "tenant-a")
    b = await _as(client, maker, Role.viewer, "viewer-b@example.com", "tenant-b")
    async with maker() as s:
        for _ in range(5):
            await _alert(s, tenant="tenant-a")
        await _alert(s, tenant="tenant-b", kind=AlertKind.drift_detected)
        await s.commit()

    page = await client.get("/api/v1/alerts", headers=a, params={"limit": 2, "offset": 0})
    assert page.status_code == 200, page.text
    body = page.json()
    assert body["total"] == 5 and len(body["items"]) == 2
    rest = await client.get("/api/v1/alerts", headers=a, params={"limit": 10, "offset": 2})
    assert len(rest.json()["items"]) == 3
    ids = {i["id"] for i in body["items"]} | {i["id"] for i in rest.json()["items"]}
    assert len(ids) == 5

    theirs = (await client.get("/api/v1/alerts", headers=b)).json()
    assert theirs["total"] == 1 and theirs["items"][0]["kind"] == "drift_detected"

    filtered = await client.get("/api/v1/alerts", headers=a, params={"kind": "drift_detected"})
    assert filtered.json()["total"] == 0


async def test_the_test_endpoint_reports_without_leaking(api, monkeypatch) -> None:
    client, maker = api
    admin = await _as(client, maker, Role.admin, "admin@example.com", "t")
    created = await client.post(
        "/api/v1/alerts/channels", headers=admin,
        json={"name": "ops", "type": "webhook", "url": WEBHOOK_URL, "enabled": False},
    )
    cid = created.json()["id"]

    outcome = {"status": 200}
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if outcome["status"] is None:
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)
        return httpx.Response(outcome["status"])

    monkeypatch.setattr(
        alerts, "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    ok = await client.post(f"/api/v1/alerts/channels/{cid}/test", headers=admin)
    assert ok.status_code == 200 and ok.json() == {"ok": True, "error": None}
    assert seen[0]["kind"] == "test"

    outcome["status"] = None
    bad = await client.post(f"/api/v1/alerts/channels/{cid}/test", headers=admin)
    assert bad.status_code == 200 and bad.json()["ok"] is False
    assert "sup3rs3cr3tpath" not in bad.text

    listed = (await client.get("/api/v1/alerts/channels", headers=admin)).json()
    assert listed[0]["last_status"] == "error"
    assert "sup3rs3cr3tpath" not in json.dumps(listed)
    # Testing never creates an alert row.
    async with maker() as s:
        assert list(await s.scalars(select(Alert))) == []


# -- /metrics ----------------------------------------------------------------


@pytest.fixture
def metrics_token(monkeypatch):
    def _set(value: str | None):
        if value is None:
            monkeypatch.delenv("SDWAN_METRICS_TOKEN", raising=False)
        else:
            monkeypatch.setenv("SDWAN_METRICS_TOKEN", value)
        get_settings.cache_clear()

    yield _set
    monkeypatch.delenv("SDWAN_METRICS_TOKEN", raising=False)
    get_settings.cache_clear()


async def test_metrics_is_404_without_a_configured_token(api, metrics_token) -> None:
    client, _ = api
    metrics_token(None)
    assert (await client.get("/metrics")).status_code == 404
    assert (
        await client.get("/metrics", headers={"Authorization": "Bearer anything"})
    ).status_code == 404


async def test_metrics_refuses_a_wrong_or_missing_token(api, metrics_token) -> None:
    client, _ = api
    metrics_token("scrape-me-please")
    assert (await client.get("/metrics")).status_code == 401
    assert (
        await client.get("/metrics", headers={"Authorization": "Bearer nope"})
    ).status_code == 401
    assert (
        await client.get("/metrics", headers={"Authorization": "Basic scrape-me-please"})
    ).status_code == 401


async def test_metrics_exposition(api, metrics_token, monkeypatch) -> None:
    client, maker = api
    metrics_token("scrape-me-please")

    near, far, fabric, link = _site_and_link()
    near.status = SiteStatus.drifted
    far.status = SiteStatus.unreachable
    now = datetime.now(UTC)
    async with maker() as s:
        s.add_all([near, far, fabric, link])
        await s.flush()
        s.add_all(
            [
                Sample(tenant_id="t", site_id="site-a", link_id="link-1",
                       metric=RTT_AVG_MS, value=99.0, collected_at=now - timedelta(minutes=2)),
                Sample(tenant_id="t", site_id="site-a", link_id="link-1",
                       metric=RTT_AVG_MS, value=8.5, collected_at=now),
                Sample(tenant_id="t", site_id="site-a", link_id="link-1",
                       metric=LOSS_PERCENT, value=3.0, collected_at=now),
                Sample(tenant_id="t", site_id="site-a", link_id=None,
                       metric=CPU_PERCENT, value=17.0, collected_at=now),
                # Stale: must not be exported as if it were current.
                Sample(tenant_id="t", site_id="site-b", link_id=None,
                       metric=CPU_PERCENT, value=88.0, collected_at=now - timedelta(days=1)),
                AlertState(tenant_id="t", subject="link:link-1@site-a", site_id="site-a",
                           state="down"),
                Job(kind=JobKind.apply, state=JobState.succeeded, tenant_id="t"),
                Job(kind=JobKind.apply, state=JobState.succeeded, tenant_id="t"),
                Job(kind=JobKind.apply, state=JobState.rolled_back, tenant_id="t"),
            ]
        )
        await s.commit()

    resp = await client.get("/metrics", headers={"Authorization": "Bearer scrape-me-please"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain; version=0.0.4")
    text = resp.text
    assert text.endswith("\n")

    site_a = 'tenant="t",site="branch",site_id="site-a"'
    assert f'sdwan_link_rtt_ms{{{site_a},link="branch-hq",link_id="link-1"}} 8.5' in text
    assert f'sdwan_link_loss_percent{{{site_a},link="branch-hq",link_id="link-1"}} 3.0' in text
    assert f'sdwan_link_up{{{site_a},link="branch-hq",link_id="link-1"}} 0.0' in text
    assert f"sdwan_device_cpu_percent{{{site_a}}} 17.0" in text
    assert "88.0" not in text
    assert f"sdwan_site_drift{{{site_a}}} 1.0" in text
    assert 'sdwan_site_reachable{tenant="t",site="hq",site_id="site-b"} 0.0' in text
    assert 'sdwan_job_total{state="succeeded"} 2.0' in text
    assert 'sdwan_job_total{state="rolled_back"} 1.0' in text

    # Format: every sample line belongs to a family declared just before it,
    # and each family's samples are contiguous.
    declared: list[str] = []
    current = None
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            name, kind = line.split()[2:4]
            assert kind == "gauge"
            assert name not in declared, f"{name} declared twice"
            declared.append(name)
            current = name
        elif line.startswith("# HELP"):
            continue
        else:
            metric = line.split("{")[0].split(" ")[0]
            assert metric == current, f"{metric} outside its family block"
            float(line.rsplit(" ", 1)[1])
    for name in ("sdwan_link_up", "sdwan_link_rtt_ms", "sdwan_link_loss_percent",
                 "sdwan_link_jitter_ms", "sdwan_site_reachable", "sdwan_site_drift",
                 "sdwan_device_cpu_percent", "sdwan_job_total"):
        assert name in declared


def test_label_values_are_escaped() -> None:
    from app.api.v1.metrics import _labels

    assert _labels(site='a"b\\c\nd') == '{site="a\\"b\\\\c\\nd"}'
