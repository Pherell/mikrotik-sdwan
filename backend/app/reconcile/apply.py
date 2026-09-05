"""Safe apply: back up, arm a dead-man rollback, push, verify, disarm.

The rollback is a RouterOS scheduler entry that restores the pre-apply backup
after a timeout. Two details make it work:

* The backup is taken **before** the scheduler is added, so the backup does not
  contain the scheduler. If the rollback fires, the restore removes it -- there
  is no second firing to clean up.
* Verification runs on a **fresh connection**. Reusing the existing socket would
  prove nothing: the whole failure this guards against is losing management
  reachability, and an established connection can outlive the rule that allowed
  it.

If the push breaks management access, the controller never reaches the disarm
step, the scheduler fires, and the router restores itself. Restoring a backup
reboots the device -- the UI must say so before the operator confirms.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.drivers.base import (
    ApplyResult,
    ConfigOp,
    DeviceDriver,
    DriverError,
    OpKind,
)

log = logging.getLogger(__name__)

_SCHEDULER = "/system/scheduler"
_ROLLBACK_PREFIX = "sdwan-rollback-"
_BACKUP_PREFIX = "sdwan-pre-"


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

    def note(self, message: str) -> None:
        self.log.append(f"{datetime.now(UTC).strftime('%H:%M:%S')} {message}")


async def safe_apply(
    driver: DeviceDriver,
    ops: list[ConfigOp],
    *,
    job_id: str,
    timeout_seconds: int = 120,
    verify: bool = True,
) -> ApplyOutcome:
    """Push ``ops`` with a self-restoring safety net."""
    outcome = ApplyOutcome(ok=False)
    if not ops:
        outcome.ok = True
        outcome.note("nothing to apply")
        return outcome

    token = job_id[:8]
    backup_name = f"{_BACKUP_PREFIX}{token}"
    outcome.backup_name = backup_name
    outcome.rollback_token = token

    # 1. Back up before anything else, and before arming, so the restore point
    #    is free of our own scaffolding.
    try:
        await driver.backup(backup_name)
        outcome.note(f"backup saved as {backup_name}")
    except DriverError as exc:
        outcome.error = f"could not take a pre-apply backup, refusing to continue: {exc}"
        outcome.note(outcome.error)
        return outcome

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
    return outcome


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


async def find_stale_rollbacks(driver: DeviceDriver) -> list[dict]:
    """Armed rollbacks left on a device, e.g. by a controller that crashed."""
    rows = await driver.read(_SCHEDULER)
    return [r for r in rows if str(r.get("name", "")).startswith(_ROLLBACK_PREFIX)]
