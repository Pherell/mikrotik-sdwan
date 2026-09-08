"""Read RouterOS's own log off a device.

The router's log is the only place that says *why* it did something. An IPsec
proposal that both ends reject, a DHCP lease that never arrives, a script that
failed at 3am -- none of that appears in any menu, only here. Which makes it
the natural companion to the diagnostics page: that page says a tunnel is
down, this one says the phase 1 proposal did not match.

RouterOS keeps this in memory by default, so it is short and it resets on
reboot. Nothing here writes; nothing here is stored.
"""

from __future__ import annotations

from typing import Any

from app.drivers.base import DeviceDriver
from app.schemas.log import DeviceLogEntry

# RouterOS topic vocabulary, reduced to something a UI can colour. A line
# carries several topics ("ipsec,error"), and the worst one wins -- an entry
# tagged "ipsec,error" is an error that happens to be about ipsec, not an
# ipsec message that happens to be tagged error.
_ERROR_TOPICS = frozenset({"error", "critical", "emergency", "alert"})
_WARN_TOPICS = frozenset({"warning", "failure"})


def severity_of(topics: list[str]) -> str:
    lowered = {t.lower() for t in topics}
    if lowered & _ERROR_TOPICS:
        return "error"
    if lowered & _WARN_TOPICS:
        return "warning"
    return "info"


def _topics(raw: Any) -> list[str]:
    """RouterOS returns topics comma-separated in one string.

    Split once, here. Splitting it in the API layer and again in the UI is the
    same rule written three times, and the third copy is always the one that
    forgets to strip whitespace.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    return [part.strip() for part in str(raw).split(",") if part.strip()]


async def read_device_log(
    driver: DeviceDriver,
    *,
    limit: int = 200,
    topic: str | None = None,
    contains: str | None = None,
) -> list[DeviceLogEntry]:
    """The most recent log lines, newest first.

    Filtering happens here rather than on the device. RouterOS's REST query
    syntax matches a topic string exactly, so asking it for "error" misses
    every line tagged "ipsec,error" -- which is every line anyone actually
    wants. Reading the buffer and filtering locally is both correct and
    cheap, because the buffer is small by construction.
    """
    rows = await driver.read("/log")
    if not isinstance(rows, list):
        return []

    entries: list[DeviceLogEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        topics = _topics(row.get("topics"))
        message = str(row.get("message") or "")
        if topic and topic.lower() not in {t.lower() for t in topics}:
            continue
        if contains and contains.lower() not in message.lower():
            continue
        entries.append(
            DeviceLogEntry(
                time=str(row.get("time") or "") or None,
                topics=topics,
                message=message,
                severity=severity_of(topics),
            )
        )

    # RouterOS returns the buffer oldest-first. Newest-first is what anyone
    # reading a log after an incident wants, and truncating before reversing
    # would keep the oldest lines rather than the relevant ones.
    entries.reverse()
    return entries[:limit]
