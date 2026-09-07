"""What the device reports about itself.

One read of /system/resource. Not a time series -- that is plan-v2 M7 and needs
storage, retention and rollups. This answers "is the box healthy right now",
which is the question you have while looking at a site page.
"""

from __future__ import annotations

from pydantic import BaseModel


class DeviceHealth(BaseModel):
    cpu_load_percent: int | None = None
    cpu_count: int | None = None
    cpu_frequency_mhz: int | None = None
    free_memory_bytes: int | None = None
    total_memory_bytes: int | None = None
    free_disk_bytes: int | None = None
    total_disk_bytes: int | None = None
    uptime: str | None = None
    version: str | None = None
    board_name: str | None = None
    architecture: str | None = None
