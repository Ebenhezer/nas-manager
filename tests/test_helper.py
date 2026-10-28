"""Unit and integration tests for the privileged helper (Phase 5)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.core.config import Settings
from app.db.migrations import PROJECT_ROOT
from app.helper import operations
from app.helper.operations import (
    LSBLK_COLUMNS,
    OPERATIONS,
    IntentMismatchError,
    OperationFailedError,
    UnknownOperationError,
    ValidationError,
    dispatch,
)
from app.helper.protocol import (
    MAX_PAYLOAD_BYTES,
    _encode_frame,
    intent_digest,
    read_frame,
    validate_device_name,
    validate_mount_options,
    validate_mountpoint,
    write_frame,
)


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "environment": "test",
        "data_dir": tmp_path / "data",
        "log_to_console": False,
        "log_to_file": False,
        "samba_config_path": tmp_path / "smb" / "smb.conf",
        "samba_backup_dir": tmp_path / "smb" / "backups",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def ok(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class FakeRun:
    """subprocess.run substitute that records argv and answers per binary."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.results: dict[str, subprocess.CompletedProcess[str]] = {}

    def __call__(self, argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return self.results.get(argv[0], subprocess.CompletedProcess(argv, 1, stdout="", stderr=""))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


def test_operations_expose_the_fixed_allowlist() -> None:
    assert sorted(OPERATIONS) == [
        "disk.detail",
        "disk.list",
        "disk.smart",
        "mount",
        "ping",
        "raid.list",
        "raid.scrub",
        "samba.apply",
        "unmount",
        "volume.list",
    ]


def test_unknown_operation_is_rejected(settings: Settings) -> None:
    with pytest.raises(UnknownOperationError):
        dispatch("systemctl reboot", {}, settings)


def test_dispatch_rejects_non_object_params(settings: Settings) -> None:
    with pytest.raises(ValidationError):
        dispatch("ping", ["not", "an", "object"], settings)  # type: ignore[arg-type]


def test_ping(settings: Settings) -> None:
    assert dispatch("ping", {}, settings)["pong"] is True


def test_disk_list_runs_fixed_lsblk(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun()
    fake.results["lsblk"] = ok(stdout='{"blockdevices": []}')
    monkeypatch.setattr(subprocess, "run", fake)
    assert dispatch("disk.list", {}, settings)["stdout"] == '{"blockdevices": []}'
    assert fake.calls == [["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS]]


def test_disk_list_reports_missing_binary(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(OperationFailedError) as excinfo:
        dispatch("disk.list", {}, settings)
    assert "not installed" in excinfo.value.message


def test_volume_list_runs_fixed_findmnt(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun()
    fake.results["findmnt"] = ok(stdout='{"filesystems": []}')
    monkeypatch.setattr(subprocess, "run", fake)
    dispatch("volume.list", {}, settings)
    assert fake.calls[0][:4] == ["findmnt", "-J", "-b", "-o"]


def test_raid_list_reads_proc_mdstat_and_mdadm_scan(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun()
    fake.results["mdadm"] = ok(stdout="ARRAY /dev/md0 metadata=1.2\n")
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(Path, "read_text", lambda self, **_: "md0 : active raid1\n")
    result = dispatch("raid.list", {}, settings)
    assert "md0 : active" in result["mdstat"]
    assert result["detail_scan"] == "ARRAY /dev/md0 metadata=1.2\n"
    assert fake.calls[0] == ["mdadm", "--detail", "--scan"]


def test_raid_list_tolerates_missing_mdadm(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun()
    fake.results["mdadm"] = subprocess.CompletedProcess([], 127, stdout="", stderr="not found")
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(Path, "read_text", lambda self, **_: "md0 : active raid1\n")
    assert dispatch("raid.list", {}, settings)["detail_scan"] == ""


# --------------------------------------------------------------------------
# Parameter validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["-o", "--remount", "-l", "..", "sda/x", "s da", ""])
def test_device_name_rejects_option_flags(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_device_name(bad)


@pytest.mark.parametrize("good", ["sda", "sda1", "nvme0n1p1", "md0", "md127", "mapper_x"])
def test_device_name_accepts_common_names(good: str) -> None:
    assert validate_device_name(good) == f"/dev/{good}"


@pytest.mark.parametrize("bad", ["relative/path", "/etc/../../etc/passwd", "", "/dev/null/\x00"])
def test_mountpoint_validation_rejects_traversal(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_mountpoint(bad)


@pytest.mark.parametrize("bad", ["loop", "bind,defaults", "defaults,remount,ro"])
def test_mount_options_reject_dangerous_tokens(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_mount_options(bad)


def test_mount_options_accept_safe_tokens() -> None:
    assert validate_mount_options("defaults,noatime") == "defaults,noatime"


def test_intent_digest_is_deterministic() -> None:
    assert intent_digest("mount", {"b": 2, "a": 1}) == intent_digest("mount", {"a": 1, "b": 2})
    assert intent_digest("mount", {"a": 1}) != intent_digest("unmount", {"a": 1})


# --------------------------------------------------------------------------
# State-changing operations
# --------------------------------------------------------------------------


def _fake_device(monkeypatch: pytest.MonkeyPatch, name: str = "sdb1") -> str:
    monkeypatch.setattr(operations, "_block_device_path", lambda n: f"/dev/{n}")
    monkeypatch.setattr(operations, "_findmnt_targets", lambda source: [])
    monkeypatch.setattr(operations, "_path_is_mountpoint", lambda path: False)
    return f"/dev/{name}"


def test_mount_requires_block_device(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_device(_name: str) -> str:
        raise ValidationError("no such device")

    monkeypatch.setattr(operations, "_block_device_path", no_device)
    params = {"name": "nope", "mountpoint": "/mnt/x", "fstype": "ext4", "options": "defaults"}
    params["intent"] = intent_digest("mount", params)
    with pytest.raises(ValidationError):
        dispatch("mount", params, settings)


def test_mount_rejects_intent_mismatch(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_device(monkeypatch)
    params = {"name": "sdb1", "mountpoint": "/mnt/x", "fstype": "ext4", "options": "defaults"}
    with pytest.raises(IntentMismatchError):
        dispatch("mount", {**params, "intent": "wrong-intent"}, settings)


def test_mount_runs_canonical_argv(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_device(monkeypatch)
    fake = FakeRun()
    fake.results["mount"] = ok()
    monkeypatch.setattr(subprocess, "run", fake)
    params = {
        "name": "sdb1",
        "mountpoint": "/mnt/data",
        "fstype": "ext4",
        "options": "defaults,noatime",
    }
    params["intent"] = intent_digest("mount", params)
    result = dispatch("mount", params, settings)
    assert result["device"] == "/dev/sdb1"
    assert result["mountpoint"] == "/mnt/data"
    mount_call = next(call for call in fake.calls if call[0] == "mount")
    assert mount_call == ["mount", "-t", "ext4", "-o", "defaults,noatime", "/dev/sdb1", "/mnt/data"]


def test_mount_creates_missing_mountpoint(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_device(monkeypatch)
    fake = FakeRun()
    fake.results["mount"] = ok()
    monkeypatch.setattr(subprocess, "run", fake)
    mountpoint = "/tmp/nas-helper-mnt-new"  # noqa: S108
    params = {"name": "sdb1", "mountpoint": mountpoint, "fstype": "ext4", "options": "defaults"}
    params["intent"] = intent_digest("mount", params)
    try:
        dispatch("mount", params, settings)
        assert Path(mountpoint).is_dir()
    finally:
        if Path(mountpoint).exists():
            Path(mountpoint).rmdir()


def test_unmount_only_for_current_mountpoint(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_device(monkeypatch)
    monkeypatch.setattr(operations, "_findmnt_targets", lambda source: ["/mnt/data"])
    fake = FakeRun()
    fake.results["umount"] = ok()
    monkeypatch.setattr(subprocess, "run", fake)
    params = {"name": "sdb1", "mountpoint": "/mnt/elsewhere"}
    params["intent"] = intent_digest("unmount", params)
    with pytest.raises(OperationFailedError):
        dispatch("unmount", params, settings)
    assert not any(call[0] == "umount" for call in fake.calls)


def test_unmount_uses_validated_mountpoint(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_device(monkeypatch)
    monkeypatch.setattr(operations, "_findmnt_targets", lambda source: ["/mnt/data"])
    fake = FakeRun()
    fake.results["umount"] = ok()
    monkeypatch.setattr(subprocess, "run", fake)
    params = {"name": "sdb1", "mountpoint": "/mnt/data"}
    params["intent"] = intent_digest("unmount", params)
    result = dispatch("unmount", params, settings)
    assert result["mountpoint"] == "/mnt/data"
    assert any(call == ["umount", "/mnt/data"] for call in fake.calls)


def test_scrub_requires_real_md_device(settings: Settings) -> None:
    # No /sys/block/md0 exists in this environment, so the scrub must be refused
    # before touching anything.
    params = {"name": "md0"}
    params["intent"] = intent_digest("raid.scrub", params)
    with pytest.raises(ValidationError):
        dispatch("raid.scrub", params, settings)


def test_scrub_writes_check_not_repair(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sync_action = tmp_path / "sync_action"
    sync_action.write_text("idle\n", encoding="utf-8")
    monkeypatch.setattr(operations, "_sync_action_path", lambda name: str(sync_action))
    params = {"name": "md0"}
    params["intent"] = intent_digest("raid.scrub", params)
    result = dispatch("raid.scrub", params, settings)
    assert result["sync_action"] == "check"
    assert result["previous_action"] == "idle"
    assert sync_action.read_text(encoding="utf-8") == "check"


def test_smart_returns_raw_exit_code(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_device(monkeypatch, "sda")
    fake = FakeRun()
    fake.results["smartctl"] = subprocess.CompletedProcess([], 2, stdout="", stderr="no SMART")
    monkeypatch.setattr(subprocess, "run", fake)
    result = dispatch("disk.smart", {"name": "sda"}, settings)
    assert result["returncode"] == 2
    assert fake.calls[0] == ["smartctl", "-j", "-H", "-A", "-i", "/dev/sda"]


# --------------------------------------------------------------------------
# samba.apply: validate → backup → atomic write → reload
# --------------------------------------------------------------------------

VALID_SMB_CONF = (
    "# ==== NAS-MANAGER MANAGED BEGIN ====\n"
    "[media]\n    path = /srv/media\n"
    "# ==== NAS-MANAGER MANAGED END ====\n"
)


def test_samba_apply_rejects_non_configured_path(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun()
    fake.results["testparm"] = ok()
    fake.results["smbcontrol"] = ok()
    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(ValidationError):
        dispatch("samba.apply", {"config_path": "/etc/passwd", "content": VALID_SMB_CONF}, settings)
    assert not any(call[0] == "smbcontrol" for call in fake.calls)


def test_samba_apply_requires_managed_markers(settings: Settings) -> None:
    with pytest.raises(ValidationError):
        dispatch(
            "samba.apply",
            {"config_path": str(settings.samba_config_path), "content": "[x]\npath=/tmp\n"},
            settings,
        )


def test_samba_apply_validates_writes_and_reloads(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun()
    fake.results["testparm"] = ok(stdout="Load smb config files from /tmp/x\n")
    fake.results["smbcontrol"] = ok()
    monkeypatch.setattr(subprocess, "run", fake)
    result = dispatch(
        "samba.apply",
        {"config_path": str(settings.samba_config_path), "content": VALID_SMB_CONF},
        settings,
    )
    assert result["reload_performed"] is True
    assert result["backup_path"] is None  # nothing existed to back up
    assert settings.samba_config_path.read_text(encoding="utf-8") == VALID_SMB_CONF
    assert "testparm" in [call[0] for call in fake.calls]
    assert ["smbcontrol", "all", "reload-config"] in fake.calls
    # The temp file used for validation is cleaned up.
    leftovers = [c for c in fake.calls if c[0] == "testparm"]
    assert leftovers


def test_samba_apply_reports_reload_failure_without_raising(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun()
    fake.results["testparm"] = ok()
    fake.results["smbcontrol"] = subprocess.CompletedProcess([], 1, stdout="", stderr="denied")
    monkeypatch.setattr(subprocess, "run", fake)
    result = dispatch(
        "samba.apply",
        {"config_path": str(settings.samba_config_path), "content": VALID_SMB_CONF},
        settings,
    )
    assert result["reload_performed"] is False
    assert result["reload_message"] == "denied"
    assert settings.samba_config_path.read_text(encoding="utf-8") == VALID_SMB_CONF


def test_samba_apply_backs_up_existing_config(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.samba_config_path.parent.mkdir(parents=True, exist_ok=True)
    settings.samba_config_path.write_text("old config\n", encoding="utf-8")
    fake = FakeRun()
    fake.results["testparm"] = ok()
    fake.results["smbcontrol"] = ok()
    monkeypatch.setattr(subprocess, "run", fake)
    result = dispatch(
        "samba.apply",
        {"config_path": str(settings.samba_config_path), "content": VALID_SMB_CONF},
        settings,
    )
    assert result["backup_path"] is not None
    assert Path(result["backup_path"]).read_text(encoding="utf-8") == "old config\n"


# --------------------------------------------------------------------------
# Framing and peer identity
# --------------------------------------------------------------------------


def test_frame_roundtrip_over_socketpair() -> None:
    left, right = socket.socketpair()
    try:
        write_frame(left, {"op": "ping", "params": {}, "request_id": "abc"})
        received = read_frame(right)
        assert received == {"op": "ping", "params": {}, "request_id": "abc"}
    finally:
        left.close()
        right.close()


def test_oversized_frame_is_refused() -> None:
    with pytest.raises(ValueError):
        _encode_frame({"payload": "x" * (MAX_PAYLOAD_BYTES + 1)})


def test_peer_uid_is_current_process() -> None:
    from app.helper.server import _peer_uid

    left, right = socket.socketpair()
    try:
        assert _peer_uid(right) == os.geteuid()
    finally:
        left.close()
        right.close()


def test_helper_over_socket_end_to_end(tmp_path: Path) -> None:
    """Spawn ``python -m app.helper`` and call it through the real client."""
    from app.services import helper_client

    socket_path = tmp_path / "helper.sock"
    env = {k: v for k, v in os.environ.items() if not k.startswith("NAS_")}
    env.update(
        {
            "NAS_HELPER_SOCKET": str(socket_path),
            "NAS_LOG_TO_CONSOLE": "false",
            "NAS_LOG_TO_FILE": "false",
            "NAS_SAMBA_CONFIG_PATH": str(tmp_path / "smb.conf"),
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.helper"],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if socket_path.exists():
                break
            if proc.poll() is not None:
                pytest.fail("helper exited before binding the socket")
            time.sleep(0.05)
        settings = make_settings(
            tmp_path,
            helper_enabled=True,
            helper_socket=socket_path,
            samba_config_path=tmp_path / "smb.conf",
        )
        result = helper_client.call_helper(settings, "ping", {})
        assert result["pong"] is True
    finally:
        proc.terminate()
        proc.wait(timeout=10)
