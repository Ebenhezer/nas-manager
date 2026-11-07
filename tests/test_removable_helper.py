"""Phase 16: the helper's automatic mounting of USB / removable drives."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.core.mounts import mounts_under, parse_mounts
from app.helper import operations, removable

from .test_helper import make_settings


def disk(
    name: str,
    *,
    tran: str | None = "usb",
    rm: object = False,
    children: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "name": name,
        "path": f"/dev/{name}",
        "type": "disk",
        "tran": tran,
        "rm": rm,
        "hotplug": False,
        "size": 8_000_000_000,
        "fstype": None,
        "mountpoint": None,
        "label": None,
        "uuid": None,
    }
    node.update(extra)
    if children is not None:
        node["children"] = children
    return node


def part(
    name: str,
    fstype: str | None = "vfat",
    *,
    label: str | None = "STICK",
    uuid: str | None = "AB12-CD34",
    mountpoint: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "path": f"/dev/{name}",
        "type": "part",
        "size": 4_000_000_000,
        "fstype": fstype,
        "label": label,
        "uuid": uuid,
        "mountpoint": mountpoint,
    }


def lsblk(*disks: dict[str, Any]) -> str:
    return json.dumps({"blockdevices": list(disks)})


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def test_only_unmounted_supported_partitions_of_removable_disks_qualify() -> None:
    listing = lsblk(
        disk("sda", tran="sata", children=[part("sda1", "ext4", label="data")]),
        disk(
            "sdb",
            children=[
                part("sdb1"),
                part("sdb2", "swap"),
                part("sdb3", "ext4", mountpoint="/mnt/x"),
            ],
        ),
        disk("sdc", tran=None, rm=True, children=[part("sdc1", "exfat", label="CARD", uuid="1")]),
        disk("sdd", tran=None, hotplug="1", children=[part("sdd1", "linux_raid_member")]),
        disk("sde", fstype="ntfs", label="WHOLE", uuid="W"),  # no partition table
        disk("sdf", children=[part("sdf1", "vfat", label="IN_FSTAB", uuid="F1")]),
        disk("nvme0n1", tran="nvme", children=[part("nvme0n1p1", "vfat")]),
    )
    found = removable.candidates(listing, fstab={"UUID=F1"})
    assert [(c.name, c.fstype, c.device) for c in found] == [
        ("sdb1", "vfat", "/dev/sdb1"),
        ("sdc1", "exfat", "/dev/sdc1"),
        ("sde", "ntfs", "/dev/sde"),
    ]


def test_fstab_specs_ignore_comments(tmp_path: Path) -> None:
    fstab = tmp_path / "fstab"
    fstab.write_text(
        "# comment\nUUID=abc / ext4 defaults 0 1\n/dev/sdb1 /mnt/b vfat noauto 0 0 # x\n\n"
    )
    assert removable.fstab_specs(fstab) == {"UUID=abc", "/dev/sdb1"}
    assert removable.fstab_specs(tmp_path / "missing") == set()


def test_device_names_from_lsblk_are_validated() -> None:
    listing = lsblk(disk("sdb", children=[part("-oexec", "vfat")]))
    assert removable.candidates(listing, fstab=set()) == []


# ---------------------------------------------------------------------------
# Names and options
# ---------------------------------------------------------------------------


def _candidate(**kwargs: Any) -> removable.Candidate:
    base: dict[str, Any] = {
        "name": "sdb1",
        "device": "/dev/sdb1",
        "fstype": "vfat",
        "label": "My Stick!",
        "uuid": "AB12",
        "size_bytes": 1,
    }
    base.update(kwargs)
    return removable.Candidate(**base)


def test_folder_names_are_sanitized_and_unique(tmp_path: Path) -> None:
    assert removable.folder_name(_candidate()) == "My_Stick"
    assert removable.folder_name(_candidate(label="../..")) == "AB12"
    assert removable.folder_name(_candidate(label=None, uuid=None)) == "sdb1"
    (tmp_path / "My_Stick").mkdir()
    (tmp_path / "My_Stick" / "busy").write_text("x")
    assert removable.mount_target(tmp_path, _candidate()) == tmp_path / "My_Stick_2"
    (tmp_path / "Empty").mkdir()
    assert removable.mount_target(tmp_path, _candidate(label="Empty")) == tmp_path / "Empty"


def test_mount_options(tmp_path: Path) -> None:
    filesystems = tmp_path / "filesystems"
    filesystems.write_text("nodev\tsysfs\n\tvfat\n\tntfs3\n")
    assert removable.mount_arguments("vfat", 990, filesystems=filesystems) == (
        "vfat",
        "nosuid,nodev,noexec,relatime,uid=0,gid=990,dmask=0007,fmask=0117,flush,utf8",
    )
    assert removable.mount_arguments("exfat", 990, filesystems=filesystems)[1].endswith(
        "gid=990,dmask=0007,fmask=0117"
    )
    assert removable.mount_arguments("ntfs", 990, filesystems=filesystems)[0] == "ntfs3"
    filesystems.write_text("\tvfat\n")
    assert removable.mount_arguments("ntfs", 990, filesystems=filesystems)[0] == "ntfs"
    assert removable.mount_arguments("ext4", 990, filesystems=filesystems) == (
        "ext4",
        "nosuid,nodev,noexec,relatime",
    )


# ---------------------------------------------------------------------------
# Reconcile: mount on plug-in, lazy unmount on removal
# ---------------------------------------------------------------------------


class FakeSystem:
    """subprocess.run stand-in: lsblk answers ``listing``; mount/umount edit a fake mount table."""

    def __init__(self, mounts: Path) -> None:
        self.mounts = mounts
        self.listing = lsblk()
        self.calls: list[list[str]] = []
        self.fail_mount = False
        mounts.write_text("proc /proc proc rw 0 0\n")

    def __call__(self, argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if argv[0] == "lsblk":
            return subprocess.CompletedProcess(argv, 0, stdout=self.listing, stderr="")
        if argv[0] == "mount":
            if self.fail_mount:
                return subprocess.CompletedProcess(argv, 32, stdout="", stderr="wrong fs type")
            device, target, options = argv[-2], argv[-1], argv[4]
            with self.mounts.open("a") as fh:
                fh.write(
                    f"{device} {target.replace(' ', chr(92) + '040')} {argv[2]} rw,{options} 0 0\n"
                )
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[0] == "umount":
            target = argv[-1]
            lines = [
                line for line in self.mounts.read_text().splitlines() if line.split()[1] != target
            ]
            self.mounts.write_text("\n".join(lines) + "\n")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[0] == "sync":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unexpected")


@pytest.fixture
def system(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSystem:
    removable.reset_state()
    fake = FakeSystem(tmp_path / "mounts")
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(removable, "PROC_MOUNTS", fake.mounts)
    monkeypatch.setattr(removable, "service_gid", lambda settings: os.getgid())
    yield fake
    removable.reset_state()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path, removable_mount_dir=tmp_path / "media")


def test_reconcile_mounts_new_drives_and_unmounts_removed_ones(
    settings: Settings, system: FakeSystem, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fstab = tmp_path / "fstab"
    fstab.write_text("")
    base = settings.removable_mount_dir
    plugged = {"/dev/sdb1"}
    monkeypatch.setattr(removable, "device_exists", lambda device: device in plugged)
    system.listing = lsblk(disk("sdb", children=[part("sdb1", label="HOLIDAY")]))
    events = removable.reconcile(settings, mounts_path=system.mounts, fstab=fstab)
    assert [(e["action"], e["mountpoint"]) for e in events] == [("mounted", str(base / "HOLIDAY"))]
    mount_call = next(call for call in system.calls if call[0] == "mount")
    assert mount_call[:3] == ["mount", "-t", "vfat"]
    assert "nosuid,nodev,noexec" in mount_call[4] and mount_call[-2:] == [
        "/dev/sdb1",
        str(base / "HOLIDAY"),
    ]
    drives = mounts_under(base, parse_mounts(system.mounts.read_text()))
    assert [d.target for d in drives] == [str(base / "HOLIDAY")]

    # Already mounted (lsblk now reports the mount point): nothing new happens.
    system.listing = lsblk(
        disk("sdb", children=[part("sdb1", label="HOLIDAY", mountpoint=str(base / "HOLIDAY"))])
    )
    assert removable.reconcile(settings, mounts_path=system.mounts, fstab=fstab) == []

    # The stick is pulled out: /dev/sdb1 no longer exists, so it is lazily unmounted.
    plugged.clear()
    system.listing = lsblk()
    events = removable.reconcile(settings, mounts_path=system.mounts, fstab=fstab)
    assert [(e["action"], e["reason"]) for e in events] == [("unmounted", "removed")]
    assert ["umount", "-l", str(base / "HOLIDAY")] in system.calls
    assert not (base / "HOLIDAY").exists()
    assert [e["action"] for e in removable.drain_events()] == ["mounted", "unmounted"]
    assert removable.drain_events() == []


def test_reconcile_keeps_mounts_whose_device_still_exists(
    settings: Settings, system: FakeSystem, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = settings.removable_mount_dir
    base.mkdir(parents=True)
    (base / "LIVE").mkdir()
    with system.mounts.open("a") as fh:
        fh.write(f"/dev/sdz1 {base / 'LIVE'} vfat rw 0 0\n")
    monkeypatch.setattr(removable, "device_exists", lambda device: device == "/dev/sdz1")
    removable.reconcile(settings, mounts_path=system.mounts, fstab=tmp_path / "none")
    assert not any(call[0] == "umount" for call in system.calls)
    assert (base / "LIVE").is_dir()


def test_failed_mount_is_reported_once_and_retried_later(
    settings: Settings, system: FakeSystem, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system.fail_mount = True
    system.listing = lsblk(disk("sdb", children=[part("sdb1", label="BROKEN")]))
    events = removable.reconcile(settings, mounts_path=system.mounts, fstab=tmp_path / "none")
    assert [e["action"] for e in events] == ["failed"]
    assert "wrong fs type" in events[0]["error"]
    assert not (settings.removable_mount_dir / "BROKEN").exists()
    assert removable.reconcile(settings, mounts_path=system.mounts, fstab=tmp_path / "none") == []
    monkeypatch.setattr(removable, "_RETRY_AFTER_SECONDS", 0.0)
    system.fail_mount = False
    events = removable.reconcile(settings, mounts_path=system.mounts, fstab=tmp_path / "none")
    assert [e["action"] for e in events] == ["mounted"]


def test_helper_operations(
    settings: Settings, system: FakeSystem, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(removable, "FSTAB", tmp_path / "none")
    status = operations.dispatch("removable.status", {}, settings)
    assert status == {
        "automount": True,
        "mount_dir": str(settings.removable_mount_dir),
        "poll_seconds": 3,
    }
    system.listing = lsblk(disk("sdb", children=[part("sdb1", label="DOCS")]))
    events = operations.dispatch("removable.reconcile", {}, settings)["events"]
    assert [e["action"] for e in events] == ["mounted"]
    target = str(settings.removable_mount_dir / "DOCS")
    assert (
        operations.dispatch("removable.flush", {"mountpoint": target}, settings)["flushed"] is True
    )
    assert ["sync", "-f", target] in system.calls
    with pytest.raises(operations.ValidationError):
        operations.dispatch("removable.flush", {"mountpoint": "/etc"}, settings)
    assert [
        e["action"] for e in operations.dispatch("removable.events", {}, settings)["events"]
    ] == ["mounted"]
    off = make_settings(tmp_path, removable_automount=False)
    with pytest.raises(operations.OperationFailedError):
        operations.dispatch("removable.reconcile", {}, off)


def test_mount_table_parsing_unescapes_paths() -> None:
    entries = parse_mounts(
        "/dev/sdb1 /media/nas-manager/My\\040Stick vfat ro,nosuid 0 0\nbad line\n"
    )
    assert entries[0].target == "/media/nas-manager/My Stick"
    assert entries[0].read_only
    assert mounts_under(Path("/media/nas-manager"), entries) == entries
    assert mounts_under(Path("/media"), entries) == []
