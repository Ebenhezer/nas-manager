"""Top processes by CPU or memory.

Command lines are deliberately never collected: they routinely carry
passwords and tokens passed as arguments, and this list is visible to every
holder of ``system:read``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

import psutil

from app.schemas.monitoring import ProcessInfo

logger = logging.getLogger(__name__)

_ATTRS = ["pid", "name", "username", "cpu_percent", "memory_info", "memory_percent", "status"]


def _as_info(info: dict[str, Any], create_time: float | None) -> ProcessInfo:
    memory = info.get("memory_info")
    return ProcessInfo(
        pid=int(info["pid"]),
        name=str(info.get("name") or "?"),
        username=info.get("username"),
        status=str(info.get("status") or "unknown"),
        cpu_percent=round(float(info.get("cpu_percent") or 0.0), 1),
        memory_percent=round(float(info.get("memory_percent") or 0.0), 2),
        memory_rss=int(getattr(memory, "rss", 0) or 0),
        started_at=(
            datetime.fromtimestamp(create_time, UTC).replace(tzinfo=None) if create_time else None
        ),
    )


def top_processes(*, sort: Literal["cpu", "memory"] = "cpu", limit: int = 25) -> list[ProcessInfo]:
    """The ``limit`` busiest processes.

    ``cpu_percent`` is measured since psutil last saw each process (the
    first call for a process reports 0.0), which is good enough for a list
    that refreshes every few seconds.
    """
    rows: list[ProcessInfo] = []
    for proc in psutil.process_iter(_ATTRS):
        try:
            info = dict(proc.info)
            create_time = proc.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        rows.append(_as_info(info, create_time))
    if sort == "memory":
        rows.sort(key=lambda row: (row.memory_rss, row.cpu_percent), reverse=True)
    else:
        rows.sort(key=lambda row: (row.cpu_percent, row.memory_rss), reverse=True)
    return rows[:limit]
