"""Read /system/resource and normalise it."""

from __future__ import annotations

from app.drivers.base import DeviceDriver
from app.schemas.health import DeviceHealth


def _int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _text(value: object) -> str | None:
    # coerce() has already turned RouterOS strings into Python values, so a
    # missing field arrives as None -- and str(None) is "None", which is exactly
    # the bug that put the word "None" under every port in the panel.
    if value is None or value == "":
        return None
    return str(value)


async def read_health(driver: DeviceDriver) -> DeviceHealth:
    rows = await driver.read("/system/resource")
    row = rows[0] if rows else {}
    return DeviceHealth(
        cpu_load_percent=_int(row.get("cpu-load")),
        cpu_count=_int(row.get("cpu-count")),
        cpu_frequency_mhz=_int(row.get("cpu-frequency")),
        free_memory_bytes=_int(row.get("free-memory")),
        total_memory_bytes=_int(row.get("total-memory")),
        free_disk_bytes=_int(row.get("free-hdd-space")),
        total_disk_bytes=_int(row.get("total-hdd-space")),
        uptime=_text(row.get("uptime")),
        version=_text(row.get("version")),
        board_name=_text(row.get("board-name")),
        architecture=_text(row.get("architecture-name")),
    )
