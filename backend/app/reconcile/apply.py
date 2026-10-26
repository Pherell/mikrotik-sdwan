"""Safe apply: back up, make the push transactional, verify, clean up.

Two transaction mechanisms, one contract:

* **Safe Mode (B1, preferred when a session is offered).** An SSH session
  holds RouterOS Safe Mode while the push runs over the normal driver, in
  chunks of at most ``chunk_size`` ops. Each chunk is verified reachable and
  then committed by releasing Safe Mode; a chunk that fails -- or that
  breaks management reachability -- is rolled back by closing the holding
  session. Only the *uncommitted* chunk is protected: an abort cannot take
  back earlier chunks, which is why a mid-apply failure marks the outcome
  ``partial`` and recommends quarantine (C3). No dead-man scheduler is
  armed in this flow; the full backup is still taken first, as the
  disaster layer underneath everything.
* **Dead-man rollback (legacy fallback).** A RouterOS scheduler entry
  restores the pre-apply backup after a timeout. Two details make it work:

  * The backup is taken **before** the scheduler is added, so the backup
    does not contain the scheduler. If the rollback fires, the restore
    removes it -- there is no second firing to clean up.
  * Verification runs on a **fresh connection**. Reusing the existing
    socket would prove nothing: the whole failure this guards against is
    losing management reachability, and an established connection can
    outlive the rule that allowed it.

  If the push breaks management access, the controller never reaches the
  disarm step, the scheduler fires, and the router restores itself.
  Restoring a backup reboots the device -- the UI must say so before the
  operator confirms.

Safe Mode is used only when the caller passes a session *and* it can be
held. If ``enter()`` raises :class:`SafeModeUnavailable` -- including the
case where a human already holds Safe Mode from another console, which is
treated as a lock, never hijacked -- the legacy flow runs instead and the
fallback is written into the outcome log where an operator will see it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.drivers.base import (
    ApplyResult,
    ConfigOp,
    DeviceDriver,
    DriverError,
    OpKind,
)
from app.drivers.safemode import SafeModeSession, SafeModeUnavailable

log = logging.getLogger(__name__)

# Journal callbacks (C3). ``record_step`` is called once per op with its
# global index in the push, its state -- 'applied', 'failed' (the op the
# driver stopped on) or 'skipped' (never attempted because an earlier op
# failed) -- and the push error for the failed one. ``record_event`` marks
# chunk boundaries: ('chunk_commit' | 'chunk_abort', detail).
RecordStep = Callable[[int, ConfigOp, str, str | None], Awaitable[None]]
RecordEvent = Callable[[str, str], Awaitable[None]]

_SCHEDULER = "/system/scheduler"
_ROLLBACK_PREFIX = "sdwan-rollback-"
_BACKUP_PREFIX = "sdwan-pre-"

# Pre-apply backups are full, unencrypted configuration files sitting on the
# device. Keeping a couple is useful; keeping one per apply forever fills the
# flash of a small board and leaves years of credentials readable to anyone who
# gets at the filesystem.
_KEEP_BACKUPS = 2


@dataclass(slots=True)
class ApplyOutcome:
    ok: bool
    applied: int = 0
    error: str | None = None
    # True when the device was left with the rollback armed because the
    # controller could not reach it again. The router will restore itself.
    rollback_armed: bool = False
    rolled_back: bool = False
    backup_name: str | None = None
    rollback_token: str | None = None
    log: list[str] = field(default_factory=list)
    # The device holds a *prefix* of this apply and no more: some ops were
    # committed (or pushed, in the legacy flow) and the rest were not, and
    # nothing rolled the prefix back. Only meaningful when ok is False.
    partial: bool = False
    # The device should be quarantined (C3): its configuration is in a
    # state the controller cannot vouch for, so no further pushes should
    # reach it until an operator looks. Set by the safe-mode failure paths
    # and by a legacy push that failed halfway while the device stayed
    # reachable (partial config, no rollback coming).
    quarantine_recommended: bool = False
    # True when the push actually ran under a held Safe Mode session --
    # False for the legacy flow, including when safe mode was offered but
    # unavailable and the apply fell back.
    safe_mode_used: bool = False

    def note(self, message: str) -> None:
        self.log.append(f"{datetime.now(UTC).strftime('%H:%M:%S')} {message}")


async def safe_apply(
    driver: DeviceDriver,
    ops: list[ConfigOp],
    *,
    job_id: str,
    timeout_seconds: int = 120,
    verify: bool = True,
    safe_mode: SafeModeSession | None = None,
    chunk_size: int = 90,
    record_step: RecordStep | None = None,
    record_event: RecordEvent | None = None,
) -> ApplyOutcome:
    """Push ``ops`` with a self-restoring safety net.

    With ``safe_mode`` (a session object, not yet entered) the push runs
    transactionally in chunks; without it -- or when the session cannot be
    held -- the legacy dead-man flow runs, byte for byte the behaviour this
    function has always had. The journal callbacks are optional and purely
    observational: a caller that passes none gets no journal, and a journal
    write that fails is logged, never allowed to strand a device mid-apply.
    """
    outcome = ApplyOutcome(ok=False)
    if not ops:
        outcome.ok = True
        outcome.note("nothing to apply")
        return outcome

    token = job_id[:8]
    backup_name = f"{_BACKUP_PREFIX}{token}"
    outcome.backup_name = backup_name
    outcome.rollback_token = token

    # 1. Back up before anything else, under both flows: in the legacy flow
    #    so the restore point is free of our own scaffolding, in the
    #    safe-mode flow as the disaster layer under the chunks -- Safe Mode
    #    rolls back a chunk, it does not resurrect a device that a
    #    committed chunk broke in a way reachability checks cannot see.
    try:
        await driver.backup(backup_name)
        outcome.note(f"backup saved as {backup_name}")
    except DriverError as exc:
        outcome.error = f"could not take a pre-apply backup, refusing to continue: {exc}"
        outcome.note(outcome.error)
        return outcome

    if safe_mode is not None:
        try:
            await safe_mode.enter()
        except SafeModeUnavailable as exc:
            outcome.note(
                f"safe mode unavailable, falling back to dead-man apply: {exc}"
            )
        else:
            return await _safe_mode_flow(
                driver,
                ops,
                outcome,
                safe_mode=safe_mode,
                chunk_size=max(1, chunk_size),
                verify=verify,
                record_step=record_step,
                record_event=record_event,
                backup_name=backup_name,
            )

    return await _legacy_flow(
        driver,
        ops,
        outcome,
        token=token,
        backup_name=backup_name,
        timeout_seconds=timeout_seconds,
        verify=verify,
        record_step=record_step,
    )


async def _legacy_flow(
    driver: DeviceDriver,
    ops: list[ConfigOp],
    outcome: ApplyOutcome,
    *,
    token: str,
    backup_name: str,
    timeout_seconds: int,
    verify: bool,
    record_step: RecordStep | None,
) -> ApplyOutcome:
    """The original flow: arm the dead-man switch, push everything, verify,
    disarm. Unchanged behaviour; the journal callback is the only addition."""
    # 2. Arm the dead-man switch.
    try:
        await _arm(driver, token, backup_name, timeout_seconds)
        outcome.rollback_armed = True
        outcome.note(f"rollback armed, fires in {timeout_seconds}s")
    except DriverError as exc:
        outcome.error = f"could not arm the rollback, refusing to continue: {exc}"
        outcome.note(outcome.error)
        return outcome

    # 3. Push.
    result: ApplyResult
    try:
        result = await driver.apply(ops)
    except DriverError as exc:
        result = ApplyResult(ok=False, error=str(exc))
    outcome.applied = result.applied
    outcome.log.extend(result.log)
    await _record_steps(record_step, ops, start=0, result=result)

    # 4. Verify on a fresh connection, then disarm. Disarm comes before
    #    reporting so a slow report cannot race the scheduler.
    reachable = True
    if verify:
        reachable = await _verify(driver)
        outcome.note("management reachable after apply" if reachable else
                     "device did not answer after apply")

    if not reachable:
        # Leave it armed on purpose: the router will restore itself. Lead with
        # that fact -- it is what the operator needs to know first, and it must
        # not be buried when the push reported an error of its own.
        outcome.ok = False
        detail = f" The push also reported: {result.error}" if result.error else ""
        outcome.error = (
            "Device stopped answering after the push. The rollback is armed: the "
            f"router will restore {backup_name} in about {timeout_seconds}s and "
            f"reboot.{detail}"
        )
        outcome.note("leaving rollback armed")
        return outcome

    try:
        await _disarm(driver, token)
        outcome.rollback_armed = False
        outcome.note("rollback disarmed")
        pruned = await _prune_backups(driver)
        if pruned:
            outcome.note(f"removed {pruned} superseded backup(s)")
    except DriverError as exc:
        # Reachable but disarm failed: the router will roll back a good config.
        # Surface it loudly rather than reporting success.
        outcome.ok = False
        outcome.error = (
            f"Applied successfully but could not disarm the rollback ({exc}). "
            f"Remove {_ROLLBACK_PREFIX}{token} from /system/scheduler by hand "
            "before it fires."
        )
        outcome.note(outcome.error)
        return outcome

    outcome.ok = result.ok
    outcome.error = result.error
    if not result.ok:
        outcome.note(
            "apply failed but the device is still reachable; "
            "config is partially applied and was NOT rolled back"
        )
        # What is on the device now is a prefix of the plan that nothing
        # will undo -- the textbook quarantine case (C3). A push that
        # failed on its very first op changed nothing, so it does not
        # count: partial means ops actually landed.
        outcome.partial = result.applied > 0
        outcome.quarantine_recommended = outcome.partial
    return outcome


async def _safe_mode_flow(
    driver: DeviceDriver,
    ops: list[ConfigOp],
    outcome: ApplyOutcome,
    *,
    safe_mode: SafeModeSession,
    chunk_size: int,
    verify: bool,
    record_step: RecordStep | None,
    record_event: RecordEvent | None,
    backup_name: str,
) -> ApplyOutcome:
    """Push in chunks under a held Safe Mode session, committing per chunk.

    ``safe_mode`` is already entered on entry to this function. The session
    is closed in a ``finally`` whatever happens -- an open session left
    behind would hold its last chunk hostage until TCP timeout, and
    closing is also the abort mechanism, so there is no path out of here
    that leaks it.
    """
    outcome.safe_mode_used = True
    chunks = [ops[i : i + chunk_size] for i in range(0, len(ops), chunk_size)]
    outcome.note(
        f"safe mode held; applying {len(ops)} ops in {len(chunks)} chunk(s) "
        f"of at most {chunk_size}"
    )
    committed = 0  # ops in chunks already released from safe mode
    try:
        for index, chunk in enumerate(chunks):
            label = f"chunk {index + 1}/{len(chunks)}"
            if index > 0:
                # commit() released safe mode but kept the SSH session;
                # take it again for this chunk.
                try:
                    await safe_mode.enter()
                except SafeModeUnavailable as exc:
                    outcome.ok = False
                    outcome.partial = committed > 0
                    outcome.quarantine_recommended = True
                    outcome.error = (
                        f"Safe mode could not be re-taken for {label} ({exc}). "
                        f"{committed} ops from earlier chunks are committed; "
                        "the remaining ops were not pushed."
                    )
                    outcome.note(outcome.error)
                    return outcome

            result: ApplyResult
            try:
                result = await driver.apply(chunk)
            except DriverError as exc:
                result = ApplyResult(ok=False, error=str(exc))
            outcome.applied += result.applied
            outcome.log.extend(result.log)
            await _record_steps(
                record_step, chunk, start=index * chunk_size, result=result
            )

            if not result.ok:
                # Abort: closing the session is what rolls this chunk
                # back. Earlier chunks are already committed and stay --
                # that asymmetry is the whole reason for quarantine.
                await safe_mode.abort()
                outcome.rolled_back = True
                await _record_event(
                    record_event,
                    "chunk_abort",
                    f"{label} aborted after apply failure: {result.error}",
                )
                outcome.ok = False
                outcome.partial = committed > 0
                outcome.quarantine_recommended = True
                stays = (
                    f"{committed} ops from earlier chunks stay committed, so "
                    "the device is partially configured."
                    if committed
                    else "No earlier chunk was committed, so nothing from "
                    "this apply persists."
                )
                outcome.error = (
                    f"Apply failed in {label}: {result.error}. The chunk was "
                    f"rolled back by aborting safe mode. {stays}"
                )
                outcome.note(outcome.error)
                return outcome

            reachable = True
            if verify:
                reachable = await _verify(driver)
                outcome.note(
                    f"management reachable after {label}"
                    if reachable
                    else f"device did not answer after {label}"
                )
            if not reachable:
                # The management path is gone, so there is nothing to
                # disarm or undo over REST -- but closing our own session
                # is exactly what triggers the device-side rollback of the
                # held chunk, and it needs no management path to work.
                await safe_mode.abort()
                outcome.rolled_back = True
                await _record_event(
                    record_event,
                    "chunk_abort",
                    f"{label} rolled back via safe-mode session close: "
                    "device unreachable after push",
                )
                outcome.ok = False
                outcome.partial = committed > 0
                outcome.quarantine_recommended = True
                stays = (
                    f"{committed} ops from earlier chunks remain committed."
                    if committed
                    else "No chunk had committed, so the device keeps its "
                    "pre-apply configuration."
                )
                outcome.error = (
                    "Device stopped answering after the push. The held chunk "
                    "was in safe mode and has been rolled back by closing the "
                    f"holding session. {stays} The full backup {backup_name} "
                    "also remains on the device."
                )
                outcome.note(outcome.error)
                return outcome

            try:
                await safe_mode.commit()
            except DriverError as exc:
                # The release could not be confirmed: this chunk may or
                # may not persist once the session dies. Unknown state is
                # a quarantine state -- say so rather than guessing.
                await safe_mode.abort()
                await _record_event(
                    record_event,
                    "chunk_abort",
                    f"{label} commit unconfirmed: {exc}",
                )
                outcome.ok = False
                outcome.partial = committed > 0
                outcome.quarantine_recommended = True
                outcome.error = (
                    f"Safe mode commit for {label} could not be confirmed "
                    f"({exc}). The chunk's state is unknown; the device "
                    "needs inspection before further applies."
                )
                outcome.note(outcome.error)
                return outcome

            committed += len(chunk)
            await _record_event(
                record_event, "chunk_commit", f"{label} committed ({len(chunk)} ops)"
            )

        outcome.ok = True
        outcome.note(f"all {len(chunks)} chunk(s) committed under safe mode")
        pruned = await _prune_backups(driver)
        if pruned:
            outcome.note(f"removed {pruned} superseded backup(s)")
        return outcome
    finally:
        await safe_mode.close()


async def _record_steps(
    record_step: RecordStep | None,
    chunk: list[ConfigOp],
    *,
    start: int,
    result: ApplyResult,
) -> None:
    """Journal one chunk's ops from its ApplyResult.

    The driver contract is "in order, stop at the first failure", so the
    states fall out of position: everything before ``result.applied``
    landed, the op at that position is the failure (when the push failed),
    and everything after it was never attempted.
    """
    if record_step is None:
        return
    for offset, op in enumerate(chunk):
        if offset < result.applied:
            state, error = "applied", None
        elif offset == result.applied and not result.ok:
            state, error = "failed", result.error
        else:
            state, error = "skipped", None
        try:
            await record_step(start + offset, op, state, error)
        except Exception as exc:  # noqa: BLE001 - journaling must not strand a device
            # A journal write failing mid-apply must never leave a router
            # sitting in safe mode or with a rollback half-armed because
            # the *controller's bookkeeping* broke. Log it on the outcome
            # and keep pushing; the gap is visible in the journal itself.
            log.warning("journal record_step failed for op %d: %s", start + offset, exc)


async def _record_event(
    record_event: RecordEvent | None, event: str, detail: str
) -> None:
    if record_event is None:
        return
    try:
        await record_event(event, detail)
    except Exception as exc:  # noqa: BLE001 - same reasoning as _record_steps
        log.warning("journal record_event failed for %s: %s", event, exc)


async def _arm(
    driver: DeviceDriver, token: str, backup_name: str, timeout_seconds: int
) -> None:
    """Add the scheduler entry that restores ``backup_name``.

    ``interval`` is used rather than ``start-time`` so the countdown is relative
    to now and needs no clock agreement between controller and router.
    """
    await driver.apply(
        [
            ConfigOp(
                kind=OpKind.add,
                path=_SCHEDULER,
                props={
                    "name": f"{_ROLLBACK_PREFIX}{token}",
                    "interval": f"{timeout_seconds}s",
                    "on-event": f'/system/backup/load name={backup_name} password=""',
                    "policy": "read,write,policy,test,reboot",
                },
                comment=f"sdwan:rollback:{token}",
            )
        ]
    )


async def _disarm(driver: DeviceDriver, token: str) -> None:
    name = f"{_ROLLBACK_PREFIX}{token}"
    rows = await driver.read(_SCHEDULER, {"name": name})
    for row in rows:
        item_id = str(row.get(".id", ""))
        if not item_id:
            continue
        result = await driver.apply(
            [ConfigOp(kind=OpKind.remove, path=_SCHEDULER, item_id=item_id)]
        )
        if not result.ok:
            raise DriverError(result.error or f"could not remove {name}")


async def _verify(driver: DeviceDriver) -> bool:
    """Prove management still works, on a connection opened after the push."""
    try:
        await driver.close()
        await driver.connect()
        rows = await driver.read("/system/resource")
    except Exception as exc:  # noqa: BLE001 - any failure here means unreachable
        log.info("post-apply verification failed: %s", exc)
        return False
    return bool(rows)


async def _prune_backups(driver: DeviceDriver) -> int:
    """Delete all but the most recent pre-apply backups.

    Best effort: a device that will not list or delete files is not a reason to
    fail an apply that already succeeded, so failures here are swallowed after
    being logged.
    """
    try:
        # Only the columns needed to pick the stale ones. Without .proplist
        # RouterOS returns every field it has for every file, and on a router
        # holding a binary one (a firmware package, a hotspot image) the
        # response is not valid UTF-8 -- decoding it raised UnicodeDecodeError
        # and failed an apply that had already finished successfully.
        files = await driver.read(
            "/file", {".proplist": ".id,name,creation-time,last-modified"}
        )
    except Exception as exc:
        # Deliberately broad. This is cleanup *after* a successful push: there
        # is no failure here worth turning a good apply into a bad one, and
        # DriverError alone does not cover what a device can do to a response.
        log.info("could not list files to prune backups: %s", exc)
        return 0

    ours = sorted(
        (f for f in files if str(f.get("name", "")).startswith(_BACKUP_PREFIX)),
        # 7.24 reports last-modified; older builds report creation-time. Keying
        # on a field the device does not have sorts every backup equal, which
        # silently makes "keep the most recent" keep an arbitrary one.
        key=lambda f: str(f.get("last-modified") or f.get("creation-time") or ""),
    )
    removed = 0
    for stale in ours[:-_KEEP_BACKUPS] if len(ours) > _KEEP_BACKUPS else []:
        item_id = str(stale.get(".id", ""))
        if not item_id:
            continue
        try:
            result = await driver.apply(
                [ConfigOp(kind=OpKind.remove, path="/file", item_id=item_id)]
            )
            removed += 1 if result.ok else 0
        except Exception as exc:  # pragma: no cover - device-specific
            log.info("could not remove %s: %s", stale.get("name"), exc)
    return removed


async def find_stale_rollbacks(driver: DeviceDriver) -> list[dict]:
    """Armed rollbacks left on a device, e.g. by a controller that crashed."""
    rows = await driver.read(_SCHEDULER)
    return [r for r in rows if str(r.get("name", "")).startswith(_ROLLBACK_PREFIX)]
