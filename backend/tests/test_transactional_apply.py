"""B1+C3+C4: transactional apply, the apply journal, quarantine, preimages.

Three layers, matching the code:

* ``safe_apply`` against a stub driver and a fake SafeModeSession --
  chunking, per-chunk commit, abort semantics, and the legacy fallback
  when safe mode cannot be held.
* ``apply_site`` against an in-memory DB with the driver and the safe-mode
  factory patched at the service's namespace -- journal rows, the scoped
  preimage, generation counting, quarantine refusal and quarantine on a
  partial failure.
* The quarantine API -- set, release, tenant scoping, role checks.

The fakes follow test_reconcile.py's pattern of standing in at the driver
boundary; nothing here opens a socket.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.drivers.base import (
    ApplyResult,
    ConfigItem,
    ConfigOp,
    ConfigSection,
    DriverError,
    OpKind,
)
from app.drivers.factory import build_safe_mode_session
from app.drivers.safemode import SafeModeUnavailable, SshSafeModeSession
from app.main import create_app
from app.models import Base, User
from app.models.apply_journal import ApplyBackup, ApplyJournalEntry
from app.models.enums import DeviceKind, JobKind, JobState, Role
from app.models.job import AuditEvent
from app.models.site import Site
from app.reconcile.apply import safe_apply
from app.security import hash_password, mask
from app.services.reconcile import apply_site, new_job

# -- fakes -------------------------------------------------------------------


class FakeDriver:
    """A DeviceDriver stub with knobs for the failure modes apply cares about.

    ``fail_at`` is a *global* op index across all apply() calls, matching how
    a device experiences a push: ops before it landed, it did not.
    """

    def __init__(
        self,
        *,
        fail_at: int | None = None,
        fail_nth: tuple[str, int] | None = None,
        reachable: bool = True,
        live: dict[str, list[dict]] | None = None,
    ) -> None:
        self.fail_at = fail_at
        # Fail the Nth op (1-based) addressed to one path, ignoring the
        # scaffolding ops other paths carry -- the legacy flow's own
        # arm/disarm ops are /system/scheduler ops, and a test aiming at
        # "the second address add fails" should not have to count them.
        self.fail_nth = fail_nth
        self.reachable = reachable
        self.live = live or {}
        self.applied_ops: list[ConfigOp] = []
        self.apply_sizes: list[int] = []
        self.backups: list[str] = []
        self._counter = 0
        self._path_counts: dict[str, int] = {}

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def backup(self, name: str) -> None:
        self.backups.append(name)

    async def read(self, path, query=None, props=None):  # noqa: ANN001, ANN202
        if path == "/system/resource":
            return [{"uptime": "1w2d"}] if self.reachable else []
        if path == "/system/scheduler":
            name = (query or {}).get("name", "")
            return [{".id": "*99", "name": name}] if name else []
        if path == "/file":
            return []
        return [dict(r) for r in self.live.get(path, [])]

    async def apply(self, ops: list[ConfigOp]) -> ApplyResult:
        self.apply_sizes.append(len(ops))
        result = ApplyResult(ok=True)
        for op in ops:
            self._path_counts[op.path] = self._path_counts.get(op.path, 0) + 1
            nth_hit = self.fail_nth is not None and self.fail_nth == (
                op.path,
                self._path_counts[op.path],
            )
            if nth_hit or (self.fail_at is not None and self._counter == self.fail_at):
                result.ok = False
                result.failed_op = op
                result.error = "device said no"
                result.log.append(f"FAIL {op.kind} {op.path}")
                return result
            self._counter += 1
            result.applied += 1
            self.applied_ops.append(op)
            result.log.append(f"ok   {op.kind} {op.path}")
        return result


class FakeSafeMode:
    """A SafeModeSession with counters, per the protocol in drivers.safemode."""

    def __init__(self, *, fail_enter: bool = False) -> None:
        self.fail_enter = fail_enter
        self.enters = 0
        self.commits = 0
        self.aborts = 0
        self.closes = 0

    async def enter(self) -> None:
        if self.fail_enter:
            raise SafeModeUnavailable("held by another session")
        self.enters += 1

    async def commit(self) -> None:
        self.commits += 1

    async def abort(self) -> None:
        self.aborts += 1

    async def close(self) -> None:
        self.closes += 1


class Recorder:
    def __init__(self) -> None:
        self.steps: list[tuple[int, ConfigOp, str, str | None]] = []
        self.events: list[tuple[str, str]] = []

    async def step(self, index, op, state, error) -> None:  # noqa: ANN001
        self.steps.append((index, op, state, error))

    async def event(self, event, detail) -> None:  # noqa: ANN001
        self.events.append((event, detail))


def _ops(n: int) -> list[ConfigOp]:
    return [
        ConfigOp(kind=OpKind.add, path="/ip/address", props={"address": f"10.0.0.{i}/32"})
        for i in range(n)
    ]


# -- safe_apply: safe-mode flow ----------------------------------------------


async def test_safe_mode_chunks_of_90_and_commits_each() -> None:
    driver, sm, rec = FakeDriver(), FakeSafeMode(), Recorder()
    outcome = await safe_apply(
        driver,
        _ops(200),
        job_id="job-chunk001",
        safe_mode=sm,
        chunk_size=90,
        record_step=rec.step,
        record_event=rec.event,
    )
    assert outcome.ok
    assert outcome.safe_mode_used is True
    assert outcome.applied == 200
    assert driver.apply_sizes == [90, 90, 20]
    assert sm.enters == 3  # initial hold + one re-entry per later chunk
    assert sm.commits == 3
    assert sm.aborts == 0
    assert sm.closes == 1
    assert [e for e, _ in rec.events] == ["chunk_commit"] * 3
    assert len(rec.steps) == 200
    assert all(state == "applied" for _, _, state, _ in rec.steps)
    assert [i for i, *_ in rec.steps] == list(range(200))


async def test_safe_mode_chunk_failure_aborts_and_marks_partial() -> None:
    # Op 100 is chunk 2's eleventh op (chunk 1 = 0..89, chunk 2 = 90..179).
    driver, sm, rec = FakeDriver(fail_at=100), FakeSafeMode(), Recorder()
    outcome = await safe_apply(
        driver,
        _ops(200),
        job_id="job-chunk002",
        safe_mode=sm,
        chunk_size=90,
        record_step=rec.step,
        record_event=rec.event,
    )
    assert not outcome.ok
    assert outcome.partial is True  # chunk 1 committed before the failure
    assert outcome.quarantine_recommended is True
    assert outcome.rolled_back is True
    assert sm.commits == 1
    assert sm.aborts == 1
    states = {i: s for i, _, s, _ in rec.steps}
    assert all(states[i] == "applied" for i in range(100))
    assert states[100] == "failed"
    assert all(states[i] == "skipped" for i in range(101, 180))
    assert 180 not in states  # chunk 3 never attempted
    assert [e for e, _ in rec.events] == ["chunk_commit", "chunk_abort"]


async def test_safe_mode_first_chunk_failure_is_not_partial() -> None:
    driver, sm = FakeDriver(fail_at=5), FakeSafeMode()
    outcome = await safe_apply(
        driver, _ops(50), job_id="job-chunk003", safe_mode=sm, chunk_size=90
    )
    assert not outcome.ok
    assert outcome.partial is False  # nothing committed, chunk rolled back
    assert outcome.quarantine_recommended is True
    assert sm.commits == 0
    assert sm.aborts == 1


async def test_safe_mode_unreachable_rolls_back_via_session_close() -> None:
    driver, sm, rec = FakeDriver(reachable=False), FakeSafeMode(), Recorder()
    outcome = await safe_apply(
        driver,
        _ops(10),
        job_id="job-chunk004",
        safe_mode=sm,
        record_event=rec.event,
    )
    assert not outcome.ok
    assert sm.aborts == 1  # closing the session is the rollback trigger
    assert sm.commits == 0
    assert outcome.quarantine_recommended is True
    assert "safe mode" in (outcome.error or "").lower()
    assert [e for e, _ in rec.events] == ["chunk_abort"]


async def test_safe_mode_unavailable_falls_back_to_legacy() -> None:
    driver, rec = FakeDriver(), Recorder()
    sm = FakeSafeMode(fail_enter=True)
    outcome = await safe_apply(
        driver,
        _ops(5),
        job_id="job-fallback1",
        safe_mode=sm,
        record_step=rec.step,
    )
    assert outcome.ok
    assert outcome.safe_mode_used is False
    assert any("falling back to dead-man apply" in line for line in outcome.log)
    # The legacy scaffolding ran: a scheduler op was pushed to arm the
    # dead-man rollback, then disarmed again (rollback_armed back to False).
    assert any(op.path == "/system/scheduler" for op in driver.applied_ops)
    assert outcome.rollback_armed is False
    assert len(rec.steps) == 5
    assert all(state == "applied" for _, _, state, _ in rec.steps)


async def test_safe_apply_without_new_params_is_the_legacy_flow() -> None:
    driver = FakeDriver()
    outcome = await safe_apply(driver, _ops(3), job_id="plain000111")
    assert outcome.ok
    assert outcome.applied == 3
    assert outcome.safe_mode_used is False
    assert outcome.partial is False
    assert driver.backups == ["sdwan-pre-plain000"]


async def test_safe_apply_driver_error_during_push_is_journalled() -> None:
    class ExplodingDriver(FakeDriver):
        async def apply(self, ops: list[ConfigOp]) -> ApplyResult:
            # The dead-man scaffolding (scheduler ops) still works; the
            # push itself dies at the transport, as a reset connection does.
            if any(op.path == "/system/scheduler" for op in ops):
                return await super().apply(ops)
            raise DriverError("connection reset")

    rec = Recorder()
    outcome = await safe_apply(
        ExplodingDriver(), _ops(4), job_id="job-explode01", record_step=rec.step
    )
    assert not outcome.ok
    states = [s for _, _, s, _ in rec.steps]
    assert states == ["failed", "skipped", "skipped", "skipped"]


# -- factory -------------------------------------------------------------------


def _site(**overrides) -> Site:
    defaults = dict(
        name="branch-1",
        mgmt_host="203.0.113.10",
        username="admin",
        tenant_id="default",
    )
    return Site(**{**defaults, **overrides})


def test_build_safe_mode_session_by_kind() -> None:
    ros7 = build_safe_mode_session(_site(device_kind=DeviceKind.ros7))
    assert isinstance(ros7, SshSafeModeSession)
    assert ros7._port == 22
    custom = build_safe_mode_session(
        _site(device_kind=DeviceKind.softhub, ssh_port=2222)
    )
    assert isinstance(custom, SshSafeModeSession)
    assert custom._port == 2222
    assert build_safe_mode_session(_site(device_kind=DeviceKind.ros6)) is None


# -- apply_site: journal, preimage, generation, quarantine ---------------------

LIVE_ROW = {
    ".id": "*1",
    "address": "10.0.0.1/32",
    "interface": "ether9",
    "comment": "sdwan:test:lo",
    # A secret-named prop on a live row: the preimage must store it masked.
    "password": "clearsecret123",
}


def _sections() -> list[ConfigSection]:
    return [
        ConfigSection(
            path="/ip/address",
            owner_tag="sdwan:test:",
            key=("address",),
            items=[
                ConfigItem(
                    props={
                        "address": "10.0.0.1/32",
                        "interface": "lo",
                        # Changed vs the live row, so the set op carries it
                        # and the journal summary must mask it.
                        "password": "newsecret456",
                    },
                    tag="sdwan:test:lo",
                )
            ],
        )
    ]


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


async def _seed_site(db: async_sessionmaker[AsyncSession], **overrides) -> Site:
    site = _site(id="site-1", **overrides)
    async with db() as s:
        s.add(site)
        await s.commit()
    return site


def _patch_apply(monkeypatch, driver: FakeDriver, safe_mode) -> None:  # noqa: ANN001
    """Stand in at the service's boundaries: driver, render, safe-mode factory."""

    @asynccontextmanager
    async def fake_open_driver(site, box=None):  # noqa: ANN001, ANN202
        yield driver

    async def fake_render(session, site):  # noqa: ANN001, ANN202
        return _sections()

    monkeypatch.setattr("app.services.reconcile.open_driver", fake_open_driver)
    monkeypatch.setattr("app.services.reconcile.render_device", fake_render)
    monkeypatch.setattr(
        "app.services.reconcile.build_safe_mode_session", lambda site: safe_mode
    )


async def test_apply_site_journals_ops_preimage_and_result(db, monkeypatch) -> None:
    site = await _seed_site(db)
    driver = FakeDriver(live={"/ip/address": [LIVE_ROW]})
    _patch_apply(monkeypatch, driver, FakeSafeMode())

    async with db() as s:
        site = await s.get(Site, "site-1")
        job = new_job(site, JobKind.apply, None)
        s.add(job)
        await s.flush()
        result = await apply_site(s, site, job)
        await s.commit()
        assert result.state == JobState.succeeded
        assert result.result["generation"] == 1
        assert result.result["safe_mode_used"] is True
        assert result.result["partial"] is False
        assert site.apply_generation == 1

        entries = (
            await s.scalars(
                select(ApplyJournalEntry)
                .where(ApplyJournalEntry.site_id == "site-1")
                .order_by(ApplyJournalEntry.step)
            )
        ).all()
        ops = [e for e in entries if e.event == "op"]
        assert len(ops) == 1
        assert ops[0].state == "applied"
        assert ops[0].generation == 1
        assert ops[0].op_path == "/ip/address"
        # The journal summary is the redacted op: the new secret is masked,
        # never stored in the clear.
        assert "newsecret456" not in (ops[0].detail or "")
        assert mask("newsecret456") in (ops[0].detail or "")
        assert any(e.event == "chunk_commit" for e in entries)

        backups = (await s.scalars(select(ApplyBackup))).all()
        assert len(backups) == 1
        assert backups[0].generation == 1
        assert backups[0].kind == "scoped_preimage"
        payload = json.dumps(backups[0].payload)
        assert "clearsecret123" not in payload
        assert mask("clearsecret123") in payload
        rows = backups[0].payload["/ip/address"]
        assert rows[0]["interface"] == "ether9"  # the pre-apply state


async def test_apply_site_generation_increments_and_preimages_prune(
    db, monkeypatch
) -> None:
    site = await _seed_site(db)
    driver = FakeDriver(live={"/ip/address": [LIVE_ROW]})
    _patch_apply(monkeypatch, driver, FakeSafeMode())

    async with db() as s:
        for expected in (1, 2, 3):
            site = await s.get(Site, "site-1")
            job = new_job(site, JobKind.apply, None)
            s.add(job)
            await s.flush()
            result = await apply_site(s, site, job)
            await s.commit()
            assert result.state == JobState.succeeded
            assert result.result["generation"] == expected

        generations = (
            await s.scalars(
                select(ApplyBackup.generation).order_by(ApplyBackup.generation)
            )
        ).all()
        assert generations == [2, 3]  # newest two generations kept
        assert site.apply_generation == 3


async def test_apply_site_refuses_quarantined_site_without_a_driver(
    db, monkeypatch
) -> None:
    site = await _seed_site(
        db, quarantined=True, quarantine_reason="suspect config, check by hand"
    )
    opened = False

    @asynccontextmanager
    async def forbidden_open_driver(site, box=None):  # noqa: ANN001, ANN202
        nonlocal opened
        opened = True
        yield FakeDriver()

    monkeypatch.setattr("app.services.reconcile.open_driver", forbidden_open_driver)

    async with db() as s:
        site = await s.get(Site, "site-1")
        job = new_job(site, JobKind.apply, None)
        s.add(job)
        await s.flush()
        result = await apply_site(s, site, job)
        await s.commit()
        assert result.state == JobState.failed
        assert "quarantined" in (result.error or "")
        assert "suspect config" in (result.error or "")
        assert opened is False
        assert site.apply_generation == 0  # a refused apply consumes nothing


async def test_apply_site_safe_mode_failure_quarantines(db, monkeypatch) -> None:
    site = await _seed_site(db)
    # One op (a set), the push fails on it under safe mode.
    driver = FakeDriver(live={"/ip/address": [LIVE_ROW]}, fail_at=0)
    _patch_apply(monkeypatch, driver, FakeSafeMode())

    async with db() as s:
        site = await s.get(Site, "site-1")
        job = new_job(site, JobKind.apply, None)
        s.add(job)
        await s.flush()
        result = await apply_site(s, site, job)
        await s.commit()
        assert result.state == JobState.failed
        assert site.quarantined is True
        assert site.quarantine_reason
        assert site.quarantined_at is not None
        events = (
            await s.scalars(
                select(ApplyJournalEntry).where(ApplyJournalEntry.site_id == "site-1")
            )
        ).all()
        assert any(e.event == "chunk_abort" for e in events)
        assert any(e.event == "quarantine" for e in events)
        audits = (
            await s.scalars(
                select(AuditEvent).where(AuditEvent.action == "site.quarantine")
            )
        ).all()
        assert len(audits) == 1
        assert audits[0].detail["automatic"] is True


async def test_apply_site_legacy_partial_failure_quarantines(db, monkeypatch) -> None:
    site = await _seed_site(db)
    # Legacy flow (no safe mode offered): two address adds, first lands,
    # second fails, device stays reachable -- a partial config with no
    # rollback coming, which is the legacy quarantine case.
    two_sections = [
        ConfigSection(
            path="/ip/address",
            owner_tag="sdwan:test:",
            key=("address",),
            items=[
                ConfigItem(
                    props={"address": "10.0.0.1/32", "interface": "lo"},
                    tag="sdwan:test:a",
                ),
                ConfigItem(
                    props={"address": "10.0.0.2/32", "interface": "lo"},
                    tag="sdwan:test:b",
                ),
            ],
        )
    ]

    @asynccontextmanager
    async def fake_open_driver(site, box=None):  # noqa: ANN001, ANN202
        yield FakeDriver(fail_nth=("/ip/address", 2))

    async def fake_render(session, site):  # noqa: ANN001, ANN202
        return two_sections

    monkeypatch.setattr("app.services.reconcile.open_driver", fake_open_driver)
    monkeypatch.setattr("app.services.reconcile.render_device", fake_render)
    monkeypatch.setattr(
        "app.services.reconcile.build_safe_mode_session", lambda site: None
    )

    async with db() as s:
        site = await s.get(Site, "site-1")
        job = new_job(site, JobKind.apply, None)
        s.add(job)
        await s.flush()
        result = await apply_site(s, site, job)
        await s.commit()
        assert result.state == JobState.failed
        assert result.result["partial"] is True
        assert result.result["safe_mode_used"] is False
        assert site.quarantined is True


# -- quarantine API --------------------------------------------------------------


@pytest.fixture
async def api() -> AsyncIterator[
    tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]]
]:
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


async def _seed_user(
    maker: async_sessionmaker[AsyncSession], role: Role, email: str
) -> None:
    async with maker() as s:
        s.add(User(email=email, role=role, password_hash=hash_password("correct-horse")))
        await s.commit()


async def _token(client: httpx.AsyncClient, email: str) -> str:
    resp = await client.post(
        "/auth/login", json={"email": email, "password": "correct-horse"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_quarantine_endpoints_set_release_and_scope(api) -> None:
    client, maker = api
    await _seed_user(maker, Role.operator, "op@example.com")
    await _seed_user(maker, Role.viewer, "view@example.com")
    async with maker() as s:
        s.add_all(
            [
                _site(id="site-1"),
                _site(id="site-2", name="other-tenant", tenant_id="other"),
            ]
        )
        await s.commit()

    op = _auth(await _token(client, "op@example.com"))

    # Set it: state, reason and timestamp come back on the site, along with
    # the new read fields.
    resp = await client.post(
        "/sites/site-1/quarantine", json={"reason": "config looks wrong"}, headers=op
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["quarantined"] is True
    assert body["quarantine_reason"] == "config looks wrong"
    assert body["quarantined_at"] is not None
    assert body["apply_generation"] == 0
    assert body["ssh_port"] is None

    # A reason is required.
    resp = await client.post("/sites/site-1/quarantine", json={}, headers=op)
    assert resp.status_code == 422

    # A viewer may not quarantine.
    viewer = _auth(await _token(client, "view@example.com"))
    resp = await client.post(
        "/sites/site-1/quarantine", json={"reason": "x"}, headers=viewer
    )
    assert resp.status_code == 403

    # Cross-tenant: the other tenant's site is a 404, exactly like a
    # missing one -- its existence is not leaked by a different answer.
    resp = await client.post(
        "/sites/site-2/quarantine", json={"reason": "x"}, headers=op
    )
    assert resp.status_code == 404
    resp = await client.post("/sites/site-2/quarantine/release", headers=op)
    assert resp.status_code == 404

    # Release it: fields clear, and the journal holds both transitions.
    resp = await client.post("/sites/site-1/quarantine/release", headers=op)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["quarantined"] is False
    assert body["quarantine_reason"] is None
    assert body["quarantined_at"] is None

    async with maker() as s:
        events = (
            await s.scalars(
                select(ApplyJournalEntry).where(ApplyJournalEntry.site_id == "site-1")
            )
        ).all()
        # Not order-asserted: created_at has second resolution on SQLite,
        # and the two transitions can share one. The pair is the content.
        assert sorted(e.event for e in events) == ["quarantine", "release"]
