from __future__ import annotations

import os
import shutil
import subprocess
from collections import namedtuple
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.db.base import utcnow_naive
from app.db.session import Database
from app.models.monitoring import MetricSample
from app.schemas.disks import PhysicalDisk, SmartData
from app.services import disks, monitoring
from app.services.monitoring import checks, processes, samples, services
from app.services.monitoring.host import load_per_core

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


# ---------------------------------------------------------------------------
# Phase 10: processes, services, RAID, SMART, samples and history
# ---------------------------------------------------------------------------


class FakeProc:
    def __init__(self, pid: int, cpu: float, rss: int, *, deny: bool = False) -> None:
        self.pid = pid
        self.info = {
            "pid": pid,
            "name": f"p{pid}",
            "username": "root",
            "cpu_percent": cpu,
            "memory_info": SimpleNamespace(rss=rss),
            "memory_percent": rss / 1000,
            "status": "running",
            "cmdline": ["--password", "hunter2"],
        }
        self.deny = deny

    def create_time(self) -> float:
        if self.deny:
            raise psutil.AccessDenied(self.pid)
        return 1_700_000_000.0


def test_top_processes_sorts_and_never_returns_command_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    procs = [FakeProc(1, 5.0, 900), FakeProc(2, 50.0, 100), FakeProc(3, 1.0, 50, deny=True)]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: iter(procs))
    by_cpu = processes.top_processes(sort="cpu", limit=10)
    assert [p.pid for p in by_cpu] == [2, 1]
    by_memory = processes.top_processes(sort="memory", limit=1)
    assert [p.pid for p in by_memory] == [1]
    dumped = by_cpu[0].model_dump()
    assert "cmdline" not in dumped
    assert "hunter2" not in repr(dumped)
    assert by_cpu[0].started_at is not None


def test_top_processes_real_smoke() -> None:
    rows = processes.top_processes(limit=5)
    assert 0 < len(rows) <= 5


def test_load_per_core() -> None:
    assert load_per_core([4.0, 1.0, 1.0], 8) == 0.5
    assert load_per_core(None, 8) is None
    assert load_per_core([1.0], None) is None


SHOW_OUTPUT = """Id=smbd.service
Description=Samba SMB Daemon
LoadState=loaded
ActiveState=active
SubState=running
UnitFileState=enabled
ActiveEnterTimestamp=Tue 2026-11-03 10:00:00 SAST

Id=nmbd.service
Description=
LoadState=not-found
ActiveState=inactive
SubState=dead
UnitFileState=

Id=nas-manager-worker.service
Description=NAS Manager sync worker
LoadState=loaded
ActiveState=failed
SubState=failed
UnitFileState=enabled

Id=nas-manager.service
LoadState=loaded
ActiveState=activating
SubState=start-pre
"""


@pytest.fixture
def fake_systemctl(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert kwargs["env"]["PATH"] == "/usr/bin:/bin"
        assert "shell" not in kwargs
        return subprocess.CompletedProcess(argv, 0, SHOW_OUTPUT, "")

    monkeypatch.setattr(shutil, "which", lambda name, path=None: "/usr/bin/systemctl")
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_unit_states_parse_and_classify(
    fake_systemctl: list[list[str]], settings: Settings
) -> None:
    units = ["smbd.service", "nmbd.service", "nas-manager-worker.service", "nas-manager.service"]
    states = {s.unit: s for s in services.unit_states(settings, units)}
    assert fake_systemctl[0][:3] == ["/usr/bin/systemctl", "show", "--no-pager"]
    assert fake_systemctl[0][-5:] == ["--", *units]
    assert states["smbd.service"].status == "ok"
    assert states["smbd.service"].summary == "active (running)"
    assert states["nmbd.service"].status == "unknown"
    assert states["nmbd.service"].summary == "unit not installed"
    assert states["nas-manager-worker.service"].status == "critical"
    assert states["nas-manager-worker.service"].is_down
    assert states["nas-manager.service"].status == "warning"


@pytest.mark.parametrize("name", ["-H.service", "../x.service", "a b.service", "x.target", ""])
def test_unit_names_are_validated(settings: Settings, name: str) -> None:
    with pytest.raises(ValueError, match="invalid systemd unit name"):
        services.unit_states(settings, [name])


def test_monitor_services_setting_is_validated(tmp_path: Path) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="invalid systemd unit name"):
        Settings(_env_file=None, data_dir=tmp_path, monitor_services="smbd.service,--kill")
    ok = Settings(
        _env_file=None, data_dir=tmp_path, monitor_services=" a.service, a.service ,b.timer"
    )
    assert ok.monitor_service_list == ["a.service", "b.timer"]


def test_systemctl_missing_is_unknown(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(shutil, "which", lambda name, path=None: None)
    [state] = services.unit_states(settings, ["smbd.service"])
    assert (state.status, state.error) == ("unknown", "systemctl is not available")


def test_systemctl_timeout_and_failure_are_unknown(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name, path=None: "/usr/bin/systemctl")

    def timeout(argv: list[str], **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(argv, 10)

    monkeypatch.setattr(subprocess, "run", timeout)
    [state] = services.unit_states(settings, ["smbd.service"])
    assert state.error == "systemctl timed out"

    def no_bus(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "Failed to connect to bus: No such file\n")

    monkeypatch.setattr(subprocess, "run", no_bus)
    [state] = services.unit_states(settings, ["smbd.service"])
    assert (state.status, state.error) == ("unknown", "Failed to connect to bus: No such file")


MDSTAT = """Personalities : [raid1]
md0 : active raid1 sdb1[1] sda1[0]
      976630464 blocks super 1.2 [2/2] [UU]

md1 : active raid1 sdd1[2](F) sdc1[0]
      976630464 blocks super 1.2 [2/1] [U_]

unused devices: <none>
"""


def test_read_raid(tmp_path: Path) -> None:
    path = tmp_path / "mdstat"
    path.write_text(MDSTAT)
    status = checks.read_raid(path)
    assert status.available
    assert {a.name: a.degraded for a in status.arrays} == {"md0": False, "md1": True}
    assert checks.read_raid(tmp_path / "missing").arrays == []
    assert checks.read_raid(tmp_path / "missing").available


def test_raid_and_service_checks_are_stored(
    db: Database, migrated_settings: Settings, fake_systemctl: list[list[str]], tmp_path: Path
) -> None:
    path = tmp_path / "mdstat"
    path.write_text(MDSTAT)
    now = utcnow_naive()
    with db.session_factory() as session:
        checks.run_raid_checks(session, now, path=path)
        checks.run_service_checks(session, migrated_settings, now)
        rows = {row.key: row for row in checks.list_checks(session)}
        assert rows["raid:md0"].status == "ok"
        md1 = rows["raid:md1"]
        assert md1.status == "critical"
        assert md1.details["degraded"] is True
        assert "degraded" in md1.summary
        assert rows["service:smbd.service"].status == "ok"
        # An array that disappears is forgotten; an unreadable mdstat keeps rows.
        path.write_text("Personalities : [raid1]\nunused devices: <none>\n")
        checks.run_raid_checks(session, now, path=path)
        assert checks.list_checks(session, "raid") == []
        path.write_text(MDSTAT)
        checks.run_raid_checks(session, now, path=path)
        path.chmod(0)
        try:
            unreadable = checks.read_raid(path)
            if not unreadable.available:  # running as root can still read it
                checks.run_raid_checks(session, now, path=path)
                assert len(checks.list_checks(session, "raid")) == 2
        finally:
            path.chmod(0o644)


def test_helper_and_smart_checks(
    db: Database, migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = utcnow_naive()
    with db.session_factory() as session:
        # Helper disabled: no helper row, nothing to check.
        assert checks.run_helper_check(session, migrated_settings, now) is False
        assert checks.list_checks(session, "helper") == []

        settings = migrated_settings.model_copy(update={"helper_enabled": True})
        monkeypatch.setattr(disks, "helper_ping", lambda s: {"ok": True})
        assert checks.run_helper_check(session, settings, now) is True
        assert checks.list_checks(session, "helper")[0].status == "ok"

        monkeypatch.setattr(
            disks,
            "list_disks",
            lambda s: [
                PhysicalDisk(name="sda", path="/dev/sda", size_bytes=1, model="WD"),
                PhysicalDisk(name="sdb", path="/dev/sdb", size_bytes=1),
                PhysicalDisk(name="sdc", path="/dev/sdc", size_bytes=1),
            ],
        )

        def smart(s: Settings, name: str) -> SmartData:
            if name == "sdc":
                raise RuntimeError("helper hiccup")
            health = "failed" if name == "sdb" else "passed"
            return SmartData(device=f"/dev/{name}", available=True, health=health, temperature=40)

        monkeypatch.setattr(disks, "smart_status", smart)
        checks.run_smart_checks(session, settings, now)
        rows = {r.target: r for r in checks.list_checks(session, "smart")}
        assert rows["sda"].status == "ok" and rows["sda"].details["model"] == "WD"
        assert rows["sdb"].status == "critical"
        assert rows["sdc"].status == "unknown"

        def down(s: Settings) -> Any:
            raise ConnectionRefusedError("refused")

        monkeypatch.setattr(disks, "list_disks", down)
        checks.run_smart_checks(session, settings, now)  # keeps the previous rows
        assert len(checks.list_checks(session, "smart")) == 3


def test_smartctl_failing_disk_bit_is_parsed() -> None:
    """smartctl exit bit 3 means DISK FAILING; the JSON is still valid and parsed."""
    import json as _json

    stdout = _json.dumps({"smart_status": {"passed": False}, "model_name": "X"})
    smart = disks.parse_smartctl(stdout, device="/dev/sda", returncode=0b1000)
    assert smart.available and smart.health == "failed" and smart.model == "X"
    no_status = disks.parse_smartctl(_json.dumps({}), device="/dev/sda", returncode=0b1000)
    assert no_status.health == "failed"
    fatal = disks.parse_smartctl(stdout, device="/dev/sda", returncode=0b10)
    assert not fatal.available and fatal.error


def test_record_sample_and_history(
    db: Database, migrated_settings: Settings, fake_psutil: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(psutil, "disk_usage", lambda p: Usage(1000, 250, 750, 25.0))
    monkeypatch.setattr(os, "getloadavg", lambda: (2.0, 1.0, 0.5))
    with db.session_factory() as session:
        row, _summary = samples.record_sample(session, migrated_settings)
        assert row.cpu_percent == 12.5
        assert row.load_per_core == 0.5
        assert row.disks and row.disks[0]["percent"] == 25.0
        assert samples.latest_sample(session) is not None

        end = utcnow_naive()
        session.query(MetricSample).delete()
        for minute, (cpu, sent) in enumerate([(10.0, 0), (30.0, 6000), (20.0, 12000), (40.0, 100)]):
            sample = MetricSample(
                sampled_at=end - timedelta(minutes=4 - minute),
                cpu_percent=cpu,
                load1=None,
                load_per_core=None,
                memory_percent=50.0,
                swap_percent=0.0,
                net_bytes_sent=sent,
                net_bytes_recv=0,
            )
            sample.disks = [{"path": "/", "percent": cpu}]
            session.add(sample)
        session.commit()
        history = samples.history(session, migrated_settings, "1h", now=end)
        assert history.bucket_seconds == 60
        assert [p.cpu_percent for p in history.points] == [10.0, 30.0, 20.0, 40.0]
        assert history.points[0].net_sent_bps is None  # no earlier sample
        assert history.points[1].net_sent_bps == 100.0  # 6000 bytes in 60 s
        assert history.points[2].net_recv_bps == 0.0  # a zero rate is a rate
        assert history.points[3].net_sent_bps is None  # counter reset (reboot)
        assert history.points[3].disks == {"/": 40.0}

        week = samples.history(session, migrated_settings, "7d", now=end)
        assert len(week.points) == 1
        assert week.points[0].cpu_percent == 25.0
        assert week.points[0].cpu_percent_max == 40.0

        old = MetricSample(
            sampled_at=end - timedelta(days=migrated_settings.monitor_history_days + 1),
            cpu_percent=1.0,
            memory_percent=1.0,
            swap_percent=0.0,
            net_bytes_sent=0,
            net_bytes_recv=0,
        )
        session.add(old)
        session.commit()
        assert samples.prune(session, migrated_settings) == 1
        assert session.execute(select(MetricSample)).scalars().all()
