from __future__ import annotations

import os
from collections import namedtuple
from pathlib import Path

import psutil
import pytest

from app.services import monitoring

VMem = namedtuple("VMem", "total used available percent")
Swap = namedtuple("Swap", "total used percent")
Net = namedtuple("Net", "bytes_sent bytes_recv")
Usage = namedtuple("Usage", "total used free percent")


@pytest.fixture
def fake_psutil(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(psutil, "virtual_memory", lambda: VMem(100, 40, 60, 40.0))
    monkeypatch.setattr(psutil, "swap_memory", lambda: Swap(10, 1, 10.0))
    monkeypatch.setattr(psutil, "net_io_counters", lambda: Net(5, 7))
    monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None: 12.5)
    monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: 4)
    monkeypatch.setattr(psutil, "boot_time", lambda: 0.0)


def test_summary_uses_psutil_values(fake_psutil: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(psutil, "disk_usage", lambda p: Usage(1000, 250, 750, 25.0))
    summary = monitoring.collect_system_summary([Path("/srv/data")])
    assert summary.cpu.percent == 12.5
    assert summary.cpu.logical_cores == 4
    assert summary.memory.total == 100
    assert summary.memory.swap_percent == 10.0
    assert summary.network.bytes_sent == 5
    assert summary.network.bytes_received == 7
    assert [d.path for d in summary.disks] == ["/", "/srv/data"]
    assert summary.host.uptime_seconds > 0


def test_duplicate_disk_paths_are_listed_once(
    fake_psutil: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(psutil, "disk_usage", lambda p: Usage(1, 1, 0, 100.0))
    summary = monitoring.collect_system_summary([Path("/")])
    assert [d.path for d in summary.disks] == ["/"]


def test_unreadable_disk_is_skipped_not_fatal(
    fake_psutil: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_usage(path: str) -> Usage:
        if path == "/gone":
            raise FileNotFoundError(path)
        return Usage(1000, 1, 999, 0.1)

    monkeypatch.setattr(psutil, "disk_usage", fake_usage)
    summary = monitoring.collect_system_summary([Path("/gone")])
    assert [d.path for d in summary.disks] == ["/"]


def test_load_average_unavailable(fake_psutil: None, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_load() -> tuple[float, float, float]:
        raise OSError

    monkeypatch.setattr(os, "getloadavg", no_load)
    monkeypatch.setattr(psutil, "disk_usage", lambda p: Usage(1, 1, 0, 100.0))
    assert monitoring.collect_system_summary().cpu.load_average is None


def test_real_psutil_smoke() -> None:
    summary = monitoring.collect_system_summary()
    assert summary.memory.total > 0
    assert summary.disks


def test_paths_on_the_same_filesystem_are_listed_once(
    fake_psutil: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(psutil, "disk_usage", lambda p: Usage(1, 1, 0, 100.0))
    sub = tmp_path / "sub"
    sub.mkdir()
    summary = monitoring.collect_system_summary([tmp_path, sub])
    assert len(summary.disks) <= 2  # "/" plus at most one entry for the shared tmp filesystem
    assert str(sub) not in [d.path for d in summary.disks]
