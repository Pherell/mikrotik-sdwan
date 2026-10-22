"""A1 central SLA evaluator: windowed classification from the samples the
poller already collects, persisted as the compliance record.

The on-device netwatch probes judge one test at a time; these tests are for
the second, central judgement -- bucketed means over a window, loss from
counter deltas, and a row per path per bucket that a compliance view can
read back later.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.drivers.ros7_rest import Ros7RestDriver
from app.main import create_app
from app.models import Base, Fabric, Link, Site, User, Wan
from app.models.enums import Role, SiteStatus, Topology, Transport
from app.models.policy import Policy, SdwanGroup, SlaProfile
from app.models.telemetry import Sample, SlaEvaluation
from app.security import hash_password
from app.telemetry.evaluator import Thresholds, classify, evaluate_all, evaluate_site
from app.telemetry.poller import (
    LINK_METRICS,
    LOSS_PERCENT,
    NW_DONE_TESTS,
    NW_DOWN,
    NW_FAILED_TESTS,
    NW_RESPONSE_COUNT,
    NW_SENT_COUNT,
    RTT_AVG_MS,
    poll_site,
)
from tests.fakeros.server import FakeRouterOS

NOW = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)
THRESH = Thresholds(loss_percent=20.0, latency_ms=100.0, jitter_ms=50.0)


def _sample(metric: str, value: float, at: datetime) -> SimpleNamespace:
    return SimpleNamespace(metric=metric, value=value, collected_at=at)


def _at(minute: int, second: int = 0) -> datetime:
    return datetime(2026, 1, 1, 12, minute, second, tzinfo=UTC)


# -- classify: bucketed means -------------------------------------------------


def test_latency_is_a_mean_of_bucket_means_not_a_flat_mean() -> None:
    # Bucket 12:00 holds three samples of 10ms; bucket 12:25 holds one of
    # 100ms. Mean of bucket means is (10 + 100) / 2 = 55; a flat mean over
    # the four samples would be 32.5 and would let the crowded bucket
    # outvote the rest of the window.
    samples = [
        _sample(RTT_AVG_MS, 10.0, _at(1)),
        _sample(RTT_AVG_MS, 10.0, _at(2)),
        _sample(RTT_AVG_MS, 10.0, _at(3)),
        _sample(RTT_AVG_MS, 100.0, _at(26)),
    ]
    stats, classification = classify(samples, THRESH, now=NOW)
    assert stats.latency_ms == 55.0
    assert classification == "in_sla"


def test_counter_delta_loss_is_exact() -> None:
    samples = [
        _sample(NW_SENT_COUNT, 0.0, _at(5)),
        _sample(NW_RESPONSE_COUNT, 0.0, _at(5)),
        _sample(NW_SENT_COUNT, 100.0, _at(15)),
        _sample(NW_RESPONSE_COUNT, 90.0, _at(15)),
        _sample(NW_SENT_COUNT, 200.0, _at(25)),
        _sample(NW_RESPONSE_COUNT, 180.0, _at(25)),
    ]
    stats, classification = classify(samples, THRESH, now=NOW)
    assert stats.loss_percent == pytest.approx(10.0)
    assert classification == "in_sla"


def test_a_counter_reset_mid_window_is_skipped_not_counted() -> None:
    # The third pair restarts near zero (device reboot / probe re-render).
    # The segment across the reset must be skipped; the two honest segments
    # each lose 10 of 100, so the window loss is still exactly 10%.
    samples = [
        _sample(NW_SENT_COUNT, 100.0, _at(5)),
        _sample(NW_RESPONSE_COUNT, 100.0, _at(5)),
        _sample(NW_SENT_COUNT, 200.0, _at(10)),
        _sample(NW_RESPONSE_COUNT, 190.0, _at(10)),
        _sample(NW_SENT_COUNT, 10.0, _at(15)),
        _sample(NW_RESPONSE_COUNT, 10.0, _at(15)),
        _sample(NW_SENT_COUNT, 110.0, _at(20)),
        _sample(NW_RESPONSE_COUNT, 100.0, _at(20)),
    ]
    stats, _ = classify(samples, THRESH, now=NOW)
    assert stats.loss_percent == pytest.approx(10.0)


def test_loss_falls_back_to_bucketed_loss_percent_without_counters() -> None:
    samples = [
        _sample(LOSS_PERCENT, 10.0, _at(1)),
        _sample(LOSS_PERCENT, 30.0, _at(26)),
    ]
    stats, classification = classify(samples, THRESH, now=NOW)
    assert stats.loss_percent == 20.0
    # Exactly at the threshold is not a breach.
    assert classification == "in_sla"


# -- classify: down ------------------------------------------------------------


def test_down_via_the_latest_nw_down_sample() -> None:
    samples = [
        _sample(RTT_AVG_MS, 10.0, _at(20)),
        _sample(NW_DOWN, 0.0, _at(10)),
        _sample(NW_DOWN, 1.0, _at(25)),
    ]
    stats, classification = classify(samples, THRESH, now=NOW)
    assert stats.latest_down is True
    assert classification == "down"


def test_down_via_sent_but_nothing_answered_even_with_status_up() -> None:
    samples = [
        _sample(NW_SENT_COUNT, 100.0, _at(5)),
        _sample(NW_RESPONSE_COUNT, 50.0, _at(5)),
        _sample(NW_SENT_COUNT, 200.0, _at(25)),
        _sample(NW_RESPONSE_COUNT, 50.0, _at(25)),
        _sample(NW_DOWN, 0.0, _at(25)),
    ]
    _, classification = classify(samples, THRESH, now=NOW)
    assert classification == "down"


# -- classify: degraded boundary ----------------------------------------------


def test_a_mean_exactly_at_the_threshold_is_in_sla_just_above_is_degraded() -> None:
    at = [_sample(RTT_AVG_MS, 100.0, _at(10))]
    _, classification = classify(at, THRESH, now=NOW)
    assert classification == "in_sla"

    above = [_sample(RTT_AVG_MS, 100.1, _at(10))]
    _, classification = classify(above, THRESH, now=NOW)
    assert classification == "degraded"


def test_jitter_is_ignored_when_the_profile_sets_no_jitter_threshold() -> None:
    thresholds = Thresholds(loss_percent=20.0, latency_ms=100.0, jitter_ms=None)
    samples = [
        _sample(RTT_AVG_MS, 10.0, _at(10)),
        _sample("rtt_jitter_ms", 9999.0, _at(10)),
    ]
    stats, classification = classify(samples, thresholds, now=NOW)
    assert stats.jitter_ms == 9999.0
    assert classification == "in_sla"


def test_no_usable_signal_classifies_as_none() -> None:
    stats, classification = classify([], THRESH, now=NOW)
    assert classification is None
    assert stats.samples_used == 0

    unrelated = [_sample("cpu_percent", 42.0, _at(10))]
    _, classification = classify(unrelated, THRESH, now=NOW)
    assert classification is None


# -- poller: counters land from the same netwatch read -------------------------

NETWATCH = {
    "tool/netwatch": [
        {
            "host": "10.255.0.1",
            "status": "down",
            "loss-percent": "100",
            "rtt-avg": "0ms",
            "done-tests": "42",
            "failed-tests": "3",
            "sent-count": "1000",
            "response-count": "970",
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


async def test_the_poller_writes_the_netwatch_counters_and_down_flag(
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

    by_metric = {sample.metric: sample.value for sample in samples if sample.link_id == "link-1"}
    assert by_metric[NW_DONE_TESTS] == 42.0
    assert by_metric[NW_FAILED_TESTS] == 3.0
    assert by_metric[NW_SENT_COUNT] == 1000.0
    assert by_metric[NW_RESPONSE_COUNT] == 970.0
    assert by_metric[NW_DOWN] == 1.0
    # Counters are cumulative, not chartable: they stay out of the set the
    # series API validates against.
    for metric in (NW_DONE_TESTS, NW_FAILED_TESTS, NW_SENT_COUNT, NW_RESPONSE_COUNT, NW_DOWN):
        assert metric not in LINK_METRICS


# -- evaluate_site: persistence, upsert, previous_class ------------------------


def _policy_stack() -> tuple[SlaProfile, SdwanGroup, Policy]:
    profile = SlaProfile(
        id="sla-1", tenant_id="t", name="voice", loss_percent=20, latency_ms=100,
        jitter_ms=None, probe_interval_seconds=10, probe_count=10,
        recovery_seconds=60, eval_window_minutes=30,
    )
    group = SdwanGroup(
        id="grp-1", tenant_id="t", name="g1",
        members=[{"uplink": "fibre", "weight": 1}], strategy="failover",
        sla_profile_id="sla-1", sla_profile=profile,
    )
    policy = Policy(
        id="pol-1", tenant_id="t", name="p1", enabled=True, site_ids=[],
        sdwan_group_id="grp-1", sdwan_group=group,
    )
    return profile, group, policy


def _seed_sample(link_id: str, metric: str, value: float, at: datetime) -> Sample:
    return Sample(
        tenant_id="t", site_id="site-a", link_id=link_id,
        metric=metric, value=value, collected_at=at,
    )


async def _tagged_setup(db: async_sessionmaker[AsyncSession]) -> None:
    """Site A with its WAN tagged 'fibre', one link, and the policy stack."""
    near, far, fabric, link = _site_and_link()
    near.wans[0].tags = {"fibre": "yes"}
    profile, group, policy = _policy_stack()
    async with db() as s:
        s.add_all([near, far, fabric, link, profile, group, policy])
        await s.commit()


async def test_evaluate_site_upserts_within_a_bucket_and_updates_stats(
    db: async_sessionmaker[AsyncSession],
) -> None:
    await _tagged_setup(db)
    async with db() as s:
        s.add(_seed_sample("link-1", RTT_AVG_MS, 50.0, _at(29)))
        await s.commit()

    async with db() as s:
        site = await s.get(Site, "site-a")
        rows = await evaluate_site(s, site, now=NOW)
        await s.commit()
    assert len(rows) == 1
    assert rows[0].classification == "in_sla"
    assert rows[0].latency_ms == 50.0
    assert rows[0].sdwan_group_id == "grp-1"
    assert rows[0].sla_profile_id == "sla-1"
    assert rows[0].previous_class is None

    # More evidence lands inside the same bucket (12:30): the row is updated
    # in place -- new bucket mean (50 + 200) / 2 = 125 breaches the profile's
    # 100ms -- and no second row appears.
    async with db() as s:
        s.add(_seed_sample("link-1", RTT_AVG_MS, 200.0, _at(31)))
        await s.commit()
    async with db() as s:
        site = await s.get(Site, "site-a")
        rows = await evaluate_site(s, site, now=_at(32))
        await s.commit()
        all_rows = list(await s.scalars(select(SlaEvaluation)))
    assert len(rows) == 1
    assert len(all_rows) == 1
    assert all_rows[0].classification == "degraded"
    assert all_rows[0].latency_ms == 125.0
    assert all_rows[0].previous_class is None  # stable across in-bucket updates


async def test_a_later_bucket_records_the_previous_classification(
    db: async_sessionmaker[AsyncSession],
) -> None:
    await _tagged_setup(db)
    async with db() as s:
        s.add(_seed_sample("link-1", RTT_AVG_MS, 50.0, _at(29)))
        await s.commit()
    async with db() as s:
        site = await s.get(Site, "site-a")
        await evaluate_site(s, site, now=NOW)
        await s.commit()

    later = datetime(2026, 1, 1, 12, 40, tzinfo=UTC)
    async with db() as s:
        s.add(_seed_sample("link-1", RTT_AVG_MS, 500.0, datetime(2026, 1, 1, 12, 39, tzinfo=UTC)))
        await s.commit()
    async with db() as s:
        site = await s.get(Site, "site-a")
        rows = await evaluate_site(s, site, now=later)
        await s.commit()
        all_rows = list(
            await s.scalars(select(SlaEvaluation).order_by(SlaEvaluation.bucket_start))
        )
    assert len(rows) == 1
    assert len(all_rows) == 2
    assert all_rows[1].classification == "degraded"
    assert all_rows[1].previous_class == "in_sla"


async def test_group_coverage_by_wan_tag_and_the_uncovered_link_gets_the_default(
    db: async_sessionmaker[AsyncSession],
) -> None:
    near, far, fabric, link = _site_and_link()
    near.wans[0].tags = {"fibre": "yes"}
    # A second link on a second, untagged WAN of the same site: no group
    # member names it, so it is judged against DEFAULT_SLA (300ms), not the
    # profile's 100ms. 250ms is degraded under the profile, in SLA under
    # the default -- the classification proves which thresholds were used.
    near_wan2 = Wan(id="wan-a2", site_id="site-a", name="wan2", interface="ether2", tags={})
    far_wan2 = Wan(id="wan-b2", site_id="site-b", name="wan2", interface="ether2")
    link2 = Link(
        id="link-2", fabric_id="fab-1", a_wan_id="wan-a2", b_wan_id="wan-b2",
        slug="branch-hq-2", a_tunnel_ip="10.255.0.2", b_tunnel_ip="10.255.0.3",
        subnet="10.255.0.2/31", enabled=True, state="applied",
    )
    near.wans = [near.wans[0], near_wan2]
    far.wans = [far.wans[0], far_wan2]
    profile, group, policy = _policy_stack()
    async with db() as s:
        s.add_all([near, far, fabric, link, link2, profile, group, policy])
        s.add(_seed_sample("link-1", RTT_AVG_MS, 250.0, _at(29)))
        s.add(_seed_sample("link-2", RTT_AVG_MS, 250.0, _at(29)))
        await s.commit()

    async with db() as s:
        site = await s.get(Site, "site-a")
        rows = await evaluate_site(s, site, now=NOW)
        await s.commit()

    by_link = {r.link_id: r for r in rows}
    assert by_link["link-1"].sdwan_group_id == "grp-1"
    assert by_link["link-1"].sla_profile_id == "sla-1"
    assert by_link["link-1"].classification == "degraded"
    assert by_link["link-2"].sdwan_group_id is None
    assert by_link["link-2"].sla_profile_id is None
    assert by_link["link-2"].classification == "in_sla"


async def test_evaluate_all_sweeps_provisioned_sites(
    db: async_sessionmaker[AsyncSession],
) -> None:
    await _tagged_setup(db)
    async with db() as s:
        site = await s.get(Site, "site-a")
        site.status = SiteStatus.reachable
        s.add(_seed_sample("link-1", RTT_AVG_MS, 50.0, datetime.now(UTC) - timedelta(minutes=1)))
        await s.commit()

    async with db() as s:
        written = await evaluate_all(s)
        await s.commit()
    assert written >= 1


# -- GET /links/{id}/evaluations ------------------------------------------------


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


def _evaluation(bucket: datetime, classification: str) -> SlaEvaluation:
    return SlaEvaluation(
        tenant_id="t", site_id="site-a", link_id="link-1",
        bucket_start=bucket, window_start=bucket - timedelta(minutes=30),
        window_end=bucket, window_minutes=30, loss_percent=1.0,
        latency_ms=42.0, jitter_ms=None, classification=classification,
        previous_class=None, evaluated_at=bucket,
    )


async def test_the_evaluations_endpoint_returns_newest_first(
    api: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, maker = api
    near, far, fabric, link = _site_and_link()
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    async with maker() as s:
        s.add_all(
            [
                near, far, fabric, link,
                User(email="op@example.com", role=Role.operator,
                     password_hash=hash_password("correct-horse"), tenant_id="t"),
                _evaluation(now - timedelta(minutes=10), "in_sla"),
                _evaluation(now - timedelta(minutes=5), "degraded"),
            ]
        )
        await s.commit()

    token = await _token(client, "op@example.com")
    resp = await client.get(
        "/links/link-1/evaluations", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["link_id"] == "link-1"
    assert [e["classification"] for e in body["evaluations"]] == ["degraded", "in_sla"]


async def test_an_evaluations_link_in_another_tenant_is_not_found(
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
        "/links/link-1/evaluations", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 404
