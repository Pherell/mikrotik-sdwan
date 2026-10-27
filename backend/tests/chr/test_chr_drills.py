"""Live C1 chaos drills (plan C1 phase 3) against the three lab CHRs.

Each drill maps to one item of the plan and keeps its own evidence
dump under CHR_EVIDENCE_DIR:

a) ``test_controller_killed_mid_apply`` -- a multi-chunk apply runs in
   a subprocess (tests/chr/_kill_drill.py) and is SIGKILLed mid-push.
   Asserts the device is left exactly on a committed chunk boundary,
   and records what the kill leaves in the DB (the job falls back to
   its last committed state, ``queued`` -- the ``running`` transition
   and the journal rows die with the killed transaction, the trade-off
   documented in services/reconcile.py), then performs the operator
   recovery and shows a later apply converging the device.
b) ``test_safe_mode_*`` -- Safe Mode failure shapes, driven through the
   NEW ``SshSafeModeSession`` class (not raw ssh): a healthy socket
   close rolls the held change back in about a second; a silent
   partition (a frozen TCP relay in front of the SSH port) leaves the
   change held -- neither committed nor rolled back -- for the whole
   observation window.
c) ``test_wan_flap_baseline`` -- flaps the CHR1<->CHR3 underlay at
   ~1 Hz and records BGP/WireGuard transition counts. BASELINE ONLY:
   damping is plan A3, which is not built yet, so there is deliberately
   no pass/fail on transition counts.
d) ``test_preimage_restore`` -- corrupts controller-owned rows on one
   CHR and restores them from the C4 scoped pre-image of a real apply,
   asserting the filtered-read state matches the pre-corruption state.

Skipped by default like every live CHR test (CHR_LIVE=1 + CHR_PASSWORD,
via scripts/chr-test.sh). Run after test_chr_topology.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from app.drivers.base import ConfigOp, OpKind
from app.drivers.redact import SECRET_PROPS
from app.drivers.safemode import SshSafeModeSession
from app.models.apply_journal import ApplyBackup, ApplyJournalEntry
from app.models.enums import JobState
from app.models.job import Job
from app.models.site import Site
from app.reconcile.plan import build_plan
from app.services.fabric import render_device
from app.services.reconcile import active_apply_job
from tests.chr.lab_topology import (
    EVIDENCE_DIR,
    NODES,
    WIPE_MENUS,
    apply_one,
    dump_evidence,
    get_site,
    lab_password,
    make_driver,
    owned_rows,
    wipe_device_state,
)

pytestmark = pytest.mark.chr

BACKEND_DIR = Path(__file__).resolve().parents[2]
MARKER_PATH = "/ip/firewall/address-list"
MARKER_LIST = "c1-drill"


def _marker(address: str) -> ConfigOp:
    return ConfigOp(
        kind=OpKind.add,
        path=MARKER_PATH,
        props={"list": MARKER_LIST, "address": address},
        comment="sdwan:c1-drill",
    )


async def _marker_present(driver, address: str) -> bool:
    rows = await driver.read(MARKER_PATH)
    return any(
        r.get("list") == MARKER_LIST and str(r.get("address", "")).startswith(address)
        for r in rows
    )


async def _remove_marker(driver, address: str) -> None:
    rows = await driver.read(MARKER_PATH)
    for r in rows:
        if r.get("list") == MARKER_LIST and str(
            r.get("address", "")
        ).startswith(address):
            await driver.apply(
                [ConfigOp(kind=OpKind.remove, path=MARKER_PATH, item_id=r[".id"])]
            )


async def _wait_gone(driver, address: str, within: float) -> float | None:
    """Seconds until the marker row disappears, or None on timeout."""
    start = time.monotonic()
    while time.monotonic() - start < within:
        if not await _marker_present(driver, address):
            return time.monotonic() - start
        await asyncio.sleep(0.25)
    return None


def _session(node: str, port: int | None = None) -> SshSafeModeSession:
    return SshSafeModeSession(
        "127.0.0.1",
        "admin",
        lab_password(),
        port=port or NODES[node]["ssh"],
        pin=False,
        connect_timeout=15,
        read_timeout=30,
    )


# -- drill b(i): commit + healthy-close rollback, through the class -------


async def test_safe_mode_class_commit_and_healthy_close_rollback() -> None:
    driver = make_driver("chr3")
    await driver.connect()
    try:
        await _remove_marker(driver, "192.0.2.202")
        await _remove_marker(driver, "192.0.2.201")

        # Commit path: a change pushed while the class holds Safe Mode
        # persists after commit + close.
        session = _session("chr3")
        await session.enter()
        result = await driver.apply([_marker("192.0.2.202")])
        assert result.ok, result.error
        await session.commit()
        await session.close()
        assert await _marker_present(driver, "192.0.2.202")
        await _remove_marker(driver, "192.0.2.202")

        # Abort path: closing the session without releasing rolls the
        # held change back, fast (healthy network: the socket close IS
        # the signal; the ~9 minutes in the docs is the partition case).
        session = _session("chr3")
        await session.enter()
        result = await driver.apply([_marker("192.0.2.201")])
        assert result.ok, result.error
        assert await _marker_present(driver, "192.0.2.201")
        await session.abort()
        gone_in = await _wait_gone(driver, "192.0.2.201", 10)
        assert gone_in is not None, "held change did not roll back on close"
        dump_evidence(
            "phase3b-healthy-close.json",
            {"node": "chr3", "rollback_seconds_after_abort": round(gone_in, 2)},
        )
    finally:
        await _remove_marker(driver, "192.0.2.201")
        await _remove_marker(driver, "192.0.2.202")
        await driver.close()


# -- drill b(ii): silent partition ----------------------------------------


class _Relay:
    """A freezable TCP relay: freeze() stops both pumps mid-stream.

    Sockets stay open on both sides -- no FIN, no RST -- which is what a
    silent network partition looks like to the device holding Safe Mode.
    """

    def __init__(self, listen_port: int, target_port: int) -> None:
        self.listen_port = listen_port
        self.target_port = target_port
        self._gate = asyncio.Event()
        self._gate.set()
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()

    def freeze(self) -> None:
        self._gate.clear()

    def thaw(self) -> None:
        self._gate.set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", self.listen_port
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in self._tasks:
            task.cancel()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            t_reader, t_writer = await asyncio.open_connection(
                "127.0.0.1", self.target_port
            )
        except OSError:
            writer.close()
            return
        tasks = {
            asyncio.ensure_future(self._pump(reader, t_writer)),
            asyncio.ensure_future(self._pump(t_reader, writer)),
        }
        self._tasks |= tasks
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            self._tasks -= tasks

    async def _pump(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                await self._gate.wait()
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.close()
            except OSError:
                pass


async def test_safe_mode_silent_partition_holds() -> None:
    relay = _Relay(listen_port=13224, target_port=NODES["chr3"]["ssh"])
    await relay.start()
    driver = make_driver("chr3")
    await driver.connect()
    session = _session("chr3", port=13224)
    try:
        await _remove_marker(driver, "192.0.2.203")
        await session.enter()
        result = await driver.apply([_marker("192.0.2.203")])
        assert result.ok, result.error
        assert await _marker_present(driver, "192.0.2.203")

        # Partition: for 60s the held change must stay exactly held --
        # visible on the device, but neither committed nor rolled back.
        # (Bounded window on purpose: the device-side timeout for a
        # silent partition is ~9 minutes of TCP timeout; this drill
        # proves the hold, it does not wait out the timeout.)
        relay.freeze()
        samples = []
        for i in range(12):
            await asyncio.sleep(5)
            samples.append(
                {
                    "t_seconds": (i + 1) * 5,
                    "marker_present": await _marker_present(driver, "192.0.2.203"),
                }
            )
        assert all(s["marker_present"] for s in samples), samples

        # Heal, then abort: only now may the rollback happen.
        relay.thaw()
        await asyncio.sleep(1)
        await session.abort()
        gone_in = await _wait_gone(driver, "192.0.2.203", 15)
        assert gone_in is not None, "no rollback after partition healed + abort"
        dump_evidence(
            "phase3b-partition.json",
            {
                "node": "chr3",
                "observation_window_seconds": 60,
                "samples": samples,
                "rollback_seconds_after_heal_and_abort": round(gone_in, 2),
            },
        )
    finally:
        relay.thaw()
        await session.close()
        await relay.stop()
        await _remove_marker(driver, "192.0.2.203")
        await driver.close()


# -- drill a: controller SIGKILLed mid-apply -------------------------------


async def _owned_counts(driver) -> dict[str, int]:
    counts = {}
    for path in WIPE_MENUS:
        try:
            counts[path] = len(await owned_rows(driver, path))
        except Exception:
            counts[path] = 0
    return counts


async def test_controller_killed_mid_apply(sm, topology) -> None:
    site_name = "chr-spoke2"
    site = await get_site(sm, site_name)
    # Retire debris from any earlier drill run (a stale queued job from
    # a previous kill would otherwise shadow this run's lock checks).
    async with sm() as s:
        stale_jobs = (
            await s.scalars(
                select(Job).where(
                    Job.site_id == site.id,
                    Job.state.in_([JobState.queued, JobState.running]),
                )
            )
        ).all()
        for stale in stale_jobs:
            stale.state = JobState.failed
            stale.error = "retired by a later C1 drill run"
        await s.commit()
    driver = make_driver("chr2")
    await driver.connect()
    try:
        # Clean device slate -> the drill's plan is the full render, and
        # its chunk boundaries are computable in advance.
        await wipe_device_state(driver)
        async with sm() as s:
            site = await s.get(Site, site.id)
            plan = await build_plan(driver, await render_device(s, site))
        ops = plan.ops()
        assert len(ops) >= 9, f"plan too small for a mid-apply kill: {len(ops)}"
        chunk = 3
        boundaries: list[dict[str, int]] = []
        running: dict[str, int] = {}
        for i, op in enumerate(ops):
            if op.kind == OpKind.add:
                running[op.path] = running.get(op.path, 0) + 1
            if (i + 1) % chunk == 0 or i == len(ops) - 1:
                boundaries.append(dict(running))
        boundary_totals = {sum(b.values()) for b in boundaries} | {0}

        log_path = EVIDENCE_DIR / "phase3a-kill-drill.log"
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "SDWAN_SAFE_MODE_CHUNK_SIZE": str(chunk),
            "SDWAN_DEVICE_CONNECT_TIMEOUT": "15",
            "SDWAN_DEVICE_READ_TIMEOUT": "60",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
        for var in ("http_proxy", "https_proxy", "HTTP_PROXY",
                    "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
            env.pop(var, None)

        killed_mid_apply = False
        job_id = None
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "tests.chr._kill_drill", site_name,
            cwd=BACKEND_DIR,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            # Wait for the push to be visibly under way, give at
            # least one chunk time to commit, then SIGKILL.
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                counts = await _owned_counts(driver)
                if sum(counts.values()) > 0:
                    break
                if proc.returncode is not None:
                    break
                await asyncio.sleep(2)
            await asyncio.sleep(8)
            if proc.returncode is None:
                proc.send_signal(signal.SIGKILL)
                killed_mid_apply = True
            await asyncio.wait_for(proc.wait(), timeout=30)
        finally:
            if proc.returncode is None:
                proc.kill()
        out, _ = await proc.communicate()
        log_path.write_bytes(out or b"")
        log_text = (out or b"").decode("utf-8", "replace")
        for line in log_text.splitlines():
            if line.startswith("KILLDRILL job="):
                job_id = line.split("job=")[1].split()[0]
        assert job_id, f"drill subprocess never started an apply:\n{log_text}"
        if not killed_mid_apply:
            pytest.skip("apply finished before the kill could land; rerun")

        # Let the held chunk's rollback land (socket close ~1s).
        await asyncio.sleep(10)
        after = await _owned_counts(driver)
        total_after = sum(after.values())
        # The device must sit exactly on a committed chunk boundary:
        # every menu's count is one of the boundary counts for it.
        for path, count in after.items():
            allowed = {b.get(path, 0) for b in boundaries} | {0}
            assert count in allowed, (
                f"{path}: {count} rows is not a chunk boundary {allowed}"
            )
        assert total_after in boundary_totals
        assert 0 < total_after < sum(
            1 for op in ops if op.kind == OpKind.add
        ), "kill did not land mid-apply"

        # DB state: the killed worker's transaction died with it.
        async with sm() as s:
            job = await s.get(Job, job_id)
            assert job is not None
            # The job's last *committed* state is 'queued': apply_site
            # set 'running' and journalled inside the transaction that
            # died with the process, so both rolled back together. The
            # stale row still holds the per-device apply lock (the lock
            # query matches queued as well as running).
            assert job.state == JobState.queued, (
                f"expected the job rolled back to 'queued', got {job.state}"
            )
            journal_rows = (
                await s.scalars(
                    select(ApplyJournalEntry).where(
                        ApplyJournalEntry.job_id == job_id
                    )
                )
            ).all()
            assert journal_rows == [], (
                "journal rows survived a kill that should have rolled "
                "the whole apply transaction back"
            )
            site_now = await s.get(Site, site.id)
            assert site_now.quarantined is False
            stale = await active_apply_job(s, site.id)
            assert stale is not None and stale.id == job_id
            dump_evidence(
                "phase3a-kill.json",
                {
                    "job_id": job_id,
                    "plan_ops": len(ops),
                    "chunk_size": chunk,
                    "boundary_totals": sorted(boundary_totals),
                    "device_rows_after_kill": after,
                    "total_after_kill": total_after,
                    "job_state_after_kill": str(job.state),
                    "journal_rows_for_job": 0,
                    "site_quarantined": False,
                },
            )
            # Operator recovery: retire the stale job, then apply.
            job.state = JobState.failed
            job.error = "killed mid-apply (C1 drill); operator recovery"
            await s.commit()

        job2 = await apply_one(sm, site_name)
        assert job2.state == JobState.succeeded, job2.error
        final = await _owned_counts(driver)
        expected = boundaries[-1]
        for path, count in expected.items():
            assert final[path] == count, (
                f"{path}: recovery apply left {final[path]}, want {count}"
            )
    finally:
        await driver.close()


# -- drill c: WAN flap baseline --------------------------------------------


async def test_wan_flap_baseline(sm, topology) -> None:
    """Baseline only -- no damping verdict (A3 is not built yet)."""
    driver3 = make_driver("chr3")
    driver1 = make_driver("chr1")
    await driver3.connect()
    await driver1.connect()
    samples: list[dict] = []
    try:
        rows = await driver3.read("/ip/address")
        target = next(
            r for r in rows if str(r.get("address", "")).startswith("10.255.1.2")
        )
        target_id = target[".id"]

        async def set_disabled(disabled: bool) -> None:
            await driver3.apply(
                [
                    ConfigOp(
                        kind=OpKind.set,
                        path="/ip/address",
                        item_id=target_id,
                        props={"disabled": disabled},
                    )
                ]
            )

        stop = asyncio.Event()

        async def flap() -> None:
            state = True
            while not stop.is_set():
                await set_disabled(state)
                state = not state
                await asyncio.sleep(0.5)

        async def sample() -> None:
            while not stop.is_set():
                sessions = await driver1.read("/routing/bgp/session")
                peers = await owned_rows(driver1, "/interface/wireguard/peers")
                samples.append(
                    {
                        "t": round(time.monotonic(), 1),
                        "bgp_established": sum(
                            1
                            for s in sessions
                            if s.get("established") in ("true", True)
                        ),
                        "bgp_total": len(sessions),
                        "wg_peer_rx_total": sum(
                            int(p.get("rx") or 0) for p in peers
                        ),
                    }
                )
                await asyncio.sleep(2)

        flap_task = asyncio.ensure_future(flap())
        sample_task = asyncio.ensure_future(sample())
        try:
            await asyncio.sleep(36)
        finally:
            stop.set()
            await asyncio.gather(flap_task, sample_task, return_exceptions=True)
            await set_disabled(False)

        counts = [s["bgp_established"] for s in samples]
        transitions = sum(
            1 for a, b in zip(counts, counts[1:], strict=False) if a != b
        )
        dump_evidence(
            "phase3c-flap-baseline.json",
            {
                "flap": "chr3 /ip/address 10.255.1.2/30 disabled toggled "
                "every ~0.5s for 36s (REST round-trips bound the rate)",
                "samples": samples,
                "bgp_established_series": counts,
                "bgp_established_transitions": transitions,
                "note": "baseline measurement only; damping is plan A3 "
                "and is not built, so no pass/fail is attached",
            },
        )
        assert len(samples) >= 10
        rows = await driver3.read("/ip/address")
        restored = next(r for r in rows if r[".id"] == target_id)
        assert restored.get("disabled") in ("false", False)
    finally:
        await driver3.close()
        await driver1.close()


# -- drill d: restore from the C4 scoped pre-image --------------------------

_VOLATILE = {
    ".id", "dynamic", "actual-interface", "last-handshake", "tx", "rx",
    "uptime", "current-endpoint-address", "current-endpoint-port",
    "established", "prefix-count", "inactive", "active", "network",
    "immediate-gw", "pref-src", "vrf", "slave", "invalid", "running",
    "link-downs", "responder", "seen", "status",
}


def _comparable(row: dict, keys: set[str]) -> dict:
    return {
        k: row.get(k)
        for k in sorted(keys)
        if k not in _VOLATILE and k not in SECRET_PROPS
    }


def _row_keys(payload_row: dict) -> set[str]:
    return set(payload_row)


async def _snapshot(driver, paths: list[str]) -> dict[str, list[dict]]:
    snap = {}
    for path in paths:
        snap[path] = await owned_rows(driver, path)
    return snap


def _project(rows: list[dict], payload_rows: list[dict]) -> list[dict]:
    keys: set[str] = set()
    for r in payload_rows:
        keys |= _row_keys(r)
    if payload_rows and "endpoint-address" in payload_rows[0]:
        # WireGuard peer rows: RouterOS assigns their names itself
        # (peerNN, from a per-device counter); the name is not intent
        # and changes on every re-add, so it cannot be compared.
        keys.discard("name")
    return sorted(
        (_comparable(r, keys) for r in rows),
        key=lambda r: json.dumps(r, sort_keys=True, default=str),
    )


async def test_preimage_restore(sm, topology) -> None:
    site_name = "chr-spoke3"
    site = await get_site(sm, site_name)
    driver = make_driver("chr3")
    await driver.connect()
    try:
        # Converge first: the drill needs the site fully applied so
        # the forced generation's pre-image is the rich, good state --
        # on a never-applied device it would be nearly empty and there
        # would be nothing meaningful to corrupt or restore.
        job = await apply_one(sm, site_name)
        assert job.state == JobState.succeeded, job.error

        # Converge intent first: a previous partial run of this drill
        # may have left the extra prefix in place, in which case the
        # "force a generation" apply below would find nothing to push.
        async with sm() as s:
            site = await s.get(Site, site.id)
            if "10.222.0.0/24" in (site.local_prefixes or []):
                site.local_prefixes = [
                    p for p in site.local_prefixes if p != "10.222.0.0/24"
                ]
                await s.commit()
                job = await apply_one(sm, site_name)
                assert job.state == JobState.succeeded, job.error

        # The state the next generation's pre-image must capture.
        before = await _snapshot(driver, WIPE_MENUS)

        # Force a new generation: change the intent slightly (one extra
        # local prefix) and apply. The generation's scoped pre-image is
        # by construction the state from *before* this push.
        async with sm() as s:
            site = await s.get(Site, site.id)
            prefixes = list(site.local_prefixes or [])
            if "10.222.0.0/24" not in prefixes:
                site.local_prefixes = prefixes + ["10.222.0.0/24"]
                await s.commit()
        job = await apply_one(sm, site_name)
        assert job.state == JobState.succeeded, job.error
        generation = (job.result or {}).get("generation")
        assert generation, job.result

        async with sm() as s:
            backup = await s.scalar(
                select(ApplyBackup).where(
                    ApplyBackup.site_id == site.id,
                    ApplyBackup.generation == generation,
                )
            )
        assert backup is not None, "no scoped preimage for the generation"
        payload: dict[str, list[dict]] = backup.payload
        paths = [p for p, rows in payload.items() if rows]
        assert paths, "preimage payload is empty"

        # Fidelity: the pre-image equals the state captured before the
        # push, on every path it covers (projected onto its own keys).
        for path in paths:
            assert _project(before[path], payload[path]) == _project(
                payload[path], payload[path]
            ), f"preimage does not match the state it was taken from: {path}"

        # Corrupt three owned rows that the pre-image covers: delete an
        # address row, disable a route row, delete a wireguard peer.
        now = await _snapshot(driver, paths)
        corrupted = []

        def _current(path: str, prow: dict) -> dict:
            for row in now.get(path, []):
                if all(row.get(k) == prow.get(k)
                       for k in ("address", "interface", "dst-address",
                                 "name", "list", "comment")
                       if k in prow):
                    if row.get("comment") == prow.get("comment"):
                        return row
            raise AssertionError(f"payload row not on device: {path} {prow}")

        prow = next(
            r
            for r in payload["/ip/address"]
            if not str(r.get("address", "")).startswith("10.255.1.2")
        )
        victim = _current("/ip/address", prow)
        result = await driver.apply(
            [ConfigOp(kind=OpKind.remove, path="/ip/address", item_id=victim[".id"])]
        )
        assert result.ok, result.error
        corrupted.append(
            {"path": "/ip/address", "what": "removed", "row": prow.get("address")}
        )

        if payload.get("/ip/route"):
            prow = payload["/ip/route"][0]
            victim = _current("/ip/route", prow)
            result = await driver.apply(
                [
                    ConfigOp(
                        kind=OpKind.set,
                        path="/ip/route",
                        item_id=victim[".id"],
                        props={"disabled": True},
                    )
                ]
            )
            assert result.ok, result.error
            corrupted.append(
                {"path": "/ip/route", "what": "disabled",
                 "row": prow.get("dst-address")}
            )

        if payload.get("/interface/wireguard/peers"):
            prow = payload["/interface/wireguard/peers"][0]
            victim = _current("/interface/wireguard/peers", prow)
            result = await driver.apply(
                [
                    ConfigOp(
                        kind=OpKind.remove,
                        path="/interface/wireguard/peers",
                        item_id=victim[".id"],
                    )
                ]
            )
            assert result.ok, result.error
            corrupted.append(
                {"path": "/interface/wireguard/peers", "what": "removed",
                 "row": prow.get("interface")}
            )

        # Restore from the pre-image payload itself (not from intent):
        # re-add payload rows that are gone, set rows that drifted.
        for path in paths:
            keys: set[str] = set()
            for r in payload[path]:
                keys |= _row_keys(r)
            current = await owned_rows(driver, path)
            current_comp = [_comparable(r, keys) for r in current]
            for prow in payload[path]:
                pcomp = _comparable(prow, keys)
                if pcomp in current_comp:
                    continue
                match = None
                for crow, ccomp in zip(current, current_comp, strict=False):
                    shared = {
                        k
                        for k in ("name", "interface", "address", "dst-address",
                                  "list", "comment")
                        if k in pcomp and ccomp.get(k) == pcomp.get(k)
                    }
                    if "comment" in shared and len(shared) >= 2:
                        match = (crow, ccomp)
                        break
                if match is not None:
                    crow, ccomp = match
                    diff = {
                        k: v for k, v in pcomp.items() if ccomp.get(k) != v
                    }
                    if diff:
                        result = await driver.apply(
                            [
                                ConfigOp(
                                    kind=OpKind.set,
                                    path=path,
                                    item_id=crow[".id"],
                                    props=diff,
                                )
                            ]
                        )
                        assert result.ok, result.error
                else:
                    props = {
                        k: v
                        for k, v in prow.items()
                        if k not in _VOLATILE and k not in SECRET_PROPS
                    }
                    result = await driver.apply(
                        [
                            ConfigOp(
                                kind=OpKind.add,
                                path=path,
                                props=props,
                                comment=prow.get("comment"),
                            )
                        ]
                    )
                    assert result.ok, result.error

        # Every pre-image row must be back, comparably, on the device.
        after = await _snapshot(driver, paths)
        for path in paths:
            keys = set()
            for r in payload[path]:
                keys |= _row_keys(r)
            present = [_comparable(r, keys) for r in after[path]]
            for prow in payload[path]:
                assert _comparable(prow, keys) in present, (
                    f"{path}: pre-image row not restored: {prow}"
                )
        # The secret boundary of the pre-image, asserted -- not
        # assumed. The payload's copy of the wireguard peer's
        # preshared-key is masked, so the peer restored above came back
        # WITHOUT its PSK and its tunnel cannot handshake; secret
        # material only ever returns from intent. Prove the negative,
        # then heal the controller's own way (drop the row, re-apply)
        # and prove the secret is back.
        secret_boundary = None
        if payload.get("/interface/wireguard/peers"):
            prow = payload["/interface/wireguard/peers"][0]
            masked = prow.get("preshared-key") or ""
            assert "*" in masked, "preimage preshared-key was not masked"
            peers_now = await owned_rows(driver, "/interface/wireguard/peers")
            restored = next(
                p for p in peers_now if p.get("interface") == prow.get("interface")
            )
            assert not restored.get("preshared-key"), (
                "peer restored from the preimage unexpectedly has a PSK"
            )
            result = await driver.apply(
                [
                    ConfigOp(
                        kind=OpKind.remove,
                        path="/interface/wireguard/peers",
                        item_id=restored[".id"],
                    )
                ]
            )
            assert result.ok, result.error
            job = await apply_one(sm, site_name)
            assert job.state == JobState.succeeded, job.error
            peers_now = await owned_rows(driver, "/interface/wireguard/peers")
            healed = next(
                p for p in peers_now if p.get("interface") == prow.get("interface")
            )
            assert healed.get("preshared-key"), (
                "intent re-apply did not restore the preshared-key"
            )
            secret_boundary = {
                "path": "/interface/wireguard/peers",
                "payload_psk_masked": True,
                "psk_present_after_preimage_restore": False,
                "psk_restored_by": "intent re-apply after removing the row",
                "note": "the planner's filtered reads exclude secret "
                "props, so PSK drift on an existing row is invisible to "
                "convergence; the row must be removed (or never lost) "
                "for intent to re-render it",
            }

        dump_evidence(
            "phase3d-preimage-restore.json",
            {
                "site": site_name,
                "generation": generation,
                "corrupted": corrupted,
                "restored_paths": paths,
                "secret_boundary": secret_boundary,
            },
        )

        # Retire the intent change; the device converges back.
        async with sm() as s:
            site = await s.get(Site, site.id)
            site.local_prefixes = [
                p for p in (site.local_prefixes or []) if p != "10.222.0.0/24"
            ]
            await s.commit()
        job = await apply_one(sm, site_name)
        assert job.state == JobState.succeeded, job.error
        final = await _snapshot(driver, paths)
        for path in paths:
            assert _project(final[path], payload[path]) == _project(
                payload[path], payload[path]
            ), f"{path}: after retiring the intent change the device " \
               "differs from the pre-image state"
    finally:
        await driver.close()
