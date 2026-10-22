"""Host summary for the dashboard.

Phase 1 scope: basic CPU, memory, disk-usage and network counters only.
Process, RAID and service monitoring are Phase 10.
"""

from __future__ import annotations

import logging
import os
import platform
import socket
import time
from collections.abc import Sequence
from pathlib import Path

import psutil

from app.schemas.system import (
    CpuInfo,
    DiskUsageInfo,
    HostInfo,
    MemoryInfo,
    NetworkInfo,
    SystemSummary,
)

logger = logging.getLogger(__name__)


def prime_cpu_counter() -> None:
    """The first psutil.cpu_percent(interval=None) call returns a meaningless 0.0."""
    psutil.cpu_percent(interval=None)


def _os_name() -> str:
    try:
        return platform.freedesktop_os_release().get("PRETTY_NAME", platform.system())
    except OSError:
        return platform.system()


def _load_average() -> list[float] | None:
    try:
        return [round(value, 2) for value in os.getloadavg()]
    except OSError:
        return None


def _filesystem_key(path: Path) -> str:
    """Identify the filesystem a path lives on, so shared filesystems are listed once."""
    try:
        return f"dev:{os.stat(path).st_dev}"
    except OSError:
        return f"path:{path}"


def _disk_usage(paths: Sequence[Path]) -> list[DiskUsageInfo]:
    disks: list[DiskUsageInfo] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        filesystem = _filesystem_key(path)
        if filesystem in seen:
            continue
        seen.add(filesystem)
        try:
            usage = psutil.disk_usage(key)
        except OSError:
            logger.warning("disk usage unavailable", extra={"path": key})
            continue
        disks.append(
            DiskUsageInfo(
                path=key,
                total=usage.total,
                used=usage.used,
                free=usage.free,
                percent=usage.percent,
            )
        )
    return disks


def collect_system_summary(extra_paths: Sequence[Path] = ()) -> SystemSummary:
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    net = psutil.net_io_counters()
    return SystemSummary(
        host=HostInfo(
            hostname=socket.gethostname(),
            os=_os_name(),
            kernel=platform.release(),
            uptime_seconds=max(0.0, time.time() - psutil.boot_time()),
        ),
        cpu=CpuInfo(
            percent=psutil.cpu_percent(interval=None),
            logical_cores=psutil.cpu_count(logical=True),
            load_average=_load_average(),
        ),
        memory=MemoryInfo(
            total=memory.total,
            used=memory.used,
            available=memory.available,
            percent=memory.percent,
            swap_total=swap.total,
            swap_used=swap.used,
            swap_percent=swap.percent,
        ),
        disks=_disk_usage([Path("/"), *extra_paths]),
        network=NetworkInfo(bytes_sent=net.bytes_sent, bytes_received=net.bytes_recv),
    )
