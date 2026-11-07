"""Automatic mounting of USB / removable drives (Phase 16).

The helper (root) polls ``lsblk`` and mounts every hot-plugged USB or
removable partition with a supported file system under
``NAS_REMOVABLE_MOUNT_DIR``, the way a desktop does. When a drive is pulled
out, its now-dead mount is lazily unmounted and its folder removed. Internal
disks, RAID members, encrypted volumes and anything listed in ``/etc/fstab``
are never touched; those stay manual on the Disks page.

Mount options are chosen here, never by a client: always ``nosuid,nodev,
noexec``; file systems without Unix owners (FAT, exFAT, NTFS) are owned by
``root:<helper_socket_group>`` with group read/write, so the unprivileged
service user can copy to them. Every mount, unmount and failure is kept in
a small event buffer that the worker drains into the audit log.
"""

from __future__ import annotations

import contextlib
import grp
import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.mounts import PROC_MOUNTS, MountEntry, mounts_under, read_mounts
from app.helper.operations import (
    LSBLK_COLUMNS,
    OperationFailedError,
    ValidationError,
    _run_checked,
)
from app.helper.protocol import validate_device_name

logger = logging.getLogger(__name__)

SUPPORTED_FSTYPES = frozenset(
    {"vfat", "exfat", "ntfs", "ntfs3", "ext2", "ext3", "ext4", "btrfs", "xfs", "hfsplus"}
)
# File systems without Unix ownership: owner/group/mode come from options.
FOREIGN_FSTYPES = frozenset({"vfat", "exfat", "ntfs", "ntfs3"})
BASE_OPTIONS = ("nosuid", "nodev", "noexec", "relatime")
FSTAB = Path("/etc/fstab")
PROC_FILESYSTEMS = Path("/proc/filesystems")

# A device that failed to mount is retried after this long, not every poll.
_RETRY_AFTER_SECONDS = 60.0
_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

_events: deque[dict[str, Any]] = deque(maxlen=200)
_failed: dict[str, float] = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class Candidate:
    name: str
    device: str
    fstype: str
    label: str | None
    uuid: str | None
    size_bytes: int


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _truthy(value: object) -> bool:
    # lsblk -J prints booleans in newer versions and "1"/"0" in older ones.
    return value is True or value in ("1", 1, "true")


def _mounted(node: dict[str, Any]) -> bool:
    if node.get("mountpoint"):
        return True
    return any(point for point in node.get("mountpoints") or [] if point)


def is_removable_disk(disk: dict[str, Any]) -> bool:
    return disk.get("tran") == "usb" or _truthy(disk.get("rm")) or _truthy(disk.get("hotplug"))


def fstab_specs(path: Path = FSTAB) -> set[str]:
    """Devices named in fstab (``/dev/...``, ``UUID=``, ``LABEL=``, ...)."""
    specs: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return specs
    for line in text.splitlines():
        fields = line.split("#", 1)[0].split()
        if fields:
            specs.add(fields[0])
    return specs


def _in_fstab(node: dict[str, Any], specs: set[str]) -> bool:
    keys = {node.get("path"), f"/dev/{node.get('name')}"}
    if node.get("uuid"):
        keys.add(f"UUID={node['uuid']}")
    if node.get("label"):
        keys.add(f"LABEL={node['label']}")
    if node.get("partuuid"):
        keys.add(f"PARTUUID={node['partuuid']}")
    return bool(keys & specs)


def candidates(lsblk_json: str, *, fstab: set[str]) -> list[Candidate]:
    """Unmounted partitions of removable disks that may be auto-mounted."""
    data = json.loads(lsblk_json)
    found: list[Candidate] = []
    for disk in data.get("blockdevices") or []:
        if disk.get("type") != "disk" or not is_removable_disk(disk):
            continue
        parts = [child for child in disk.get("children") or [] if child.get("type") == "part"]
        for node in parts or [disk]:
            fstype = (node.get("fstype") or "").lower()
            if fstype not in SUPPORTED_FSTYPES or _mounted(node) or _in_fstab(node, fstab):
                continue
            try:
                # Only ever hand mount a validated /dev/<name>, never a path from lsblk.
                device = validate_device_name(str(node.get("name") or ""))
            except ValueError:
                continue
            found.append(
                Candidate(
                    name=str(node.get("name")),
                    device=device,
                    fstype=fstype,
                    label=node.get("label") or None,
                    uuid=node.get("uuid") or None,
                    size_bytes=int(node.get("size") or 0),
                )
            )
    return found


# ---------------------------------------------------------------------------
# Mount points and options
# ---------------------------------------------------------------------------


def folder_name(candidate: Candidate) -> str:
    """A safe folder name: the volume label, else the UUID, else the device name."""
    for raw in (candidate.label, candidate.uuid, candidate.name):
        if raw:
            cleaned = _NAME_RE.sub("_", raw).strip("._-")[:64]
            if cleaned:
                return cleaned
    return "usb"


def mount_target(base: Path, candidate: Candidate) -> Path:
    """``base/<name>``; ``_2``, ``_3`` ... when that folder is in use."""
    name = folder_name(candidate)
    target = base / name
    counter = 2
    while target.exists() and (os.path.ismount(target) or any(target.iterdir())):
        target = base / f"{name}_{counter}"
        counter += 1
    return target


def _ntfs_driver(filesystems: Path = PROC_FILESYSTEMS) -> str:
    try:
        known = filesystems.read_text(encoding="utf-8")
    except OSError:
        known = ""
    return "ntfs3" if re.search(r"\bntfs3\b", known) else "ntfs"


def mount_arguments(
    fstype: str, gid: int, *, filesystems: Path = PROC_FILESYSTEMS
) -> tuple[str, str]:
    """The ``-t`` type and ``-o`` options the helper uses for ``fstype``."""
    options = list(BASE_OPTIONS)
    if fstype in {"ntfs", "ntfs3"}:
        fstype = _ntfs_driver(filesystems)
    if fstype in FOREIGN_FSTYPES:
        options += ["uid=0", f"gid={gid}", "dmask=0007", "fmask=0117"]
    if fstype == "vfat":
        # Write each file through promptly, so pulling the stick loses little.
        options += ["flush", "utf8"]
    return fstype, ",".join(options)


def service_gid(settings: Settings) -> int:
    try:
        return grp.getgrnam(settings.helper_socket_group).gr_gid
    except KeyError:
        logger.warning(
            "group %r not found; removable drives are root-only", settings.helper_socket_group
        )
        return 0


def ensure_base(settings: Settings) -> Path:
    base = settings.removable_mount_dir
    if not base.exists():
        base.mkdir(parents=True, mode=0o750)
        os.chown(base, 0, service_gid(settings))
        os.chmod(base, 0o750)  # noqa: S103 - group traversal for the service user
    return base


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def _record(sink: list[dict[str, Any]], action: str, **details: Any) -> None:
    event = {"action": action, "at": datetime.now(UTC).isoformat(), **details}
    with _lock:
        _events.append(event)
    sink.append(event)


def drain_events() -> list[dict[str, Any]]:
    with _lock:
        events = list(_events)
        _events.clear()
    return events


def reset_state() -> None:
    """Forget events and failed devices (tests)."""
    with _lock:
        _events.clear()
        _failed.clear()


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def _mount_one(base: Path, candidate: Candidate, gid: int, sink: list[dict[str, Any]]) -> None:
    key = candidate.uuid or candidate.device
    with _lock:
        failed_at = _failed.get(key)
    if failed_at is not None and time.monotonic() - failed_at < _RETRY_AFTER_SECONDS:
        return
    target = mount_target(base, candidate)
    fstype, options = mount_arguments(candidate.fstype, gid)
    created = not target.exists()
    target.mkdir(mode=0o750, exist_ok=True)
    try:
        _run_checked(["mount", "-t", fstype, "-o", options, candidate.device, str(target)])
    except OperationFailedError as exc:
        if created:
            with contextlib.suppress(OSError):
                target.rmdir()
        with _lock:
            _failed[key] = time.monotonic()
        _record(
            sink,
            "failed",
            **asdict(candidate),
            mountpoint=str(target),
            error=f"{exc.message}: {(exc.details or {}).get('output', '')}".strip(": "),
        )
        logger.warning("could not mount %s: %s", candidate.device, exc.message)
        return
    with _lock:
        _failed.pop(key, None)
    _record(sink, "mounted", **asdict(candidate), mountpoint=str(target), options=options)
    logger.info("mounted removable drive %s at %s", candidate.device, target)


def device_exists(device: str) -> bool:
    return os.path.exists(device)


def _unmount_stale(base: Path, entries: list[MountEntry], sink: list[dict[str, Any]]) -> None:
    for entry in mounts_under(base, entries):
        if not entry.source.startswith("/dev/") or device_exists(entry.source):
            continue
        try:
            # Lazy: the device is gone, so nothing can be flushed any more.
            _run_checked(["umount", "-l", entry.target])
        except OperationFailedError as exc:
            _record(sink, "failed", device=entry.source, mountpoint=entry.target, error=exc.message)
            continue
        with contextlib.suppress(OSError):
            Path(entry.target).rmdir()
        _record(
            sink,
            "unmounted",
            device=entry.source,
            mountpoint=entry.target,
            fstype=entry.fstype,
            reason="removed",
        )
        logger.info("removable drive %s was removed; unmounted %s", entry.source, entry.target)


def _remove_empty_folders(base: Path, entries: list[MountEntry]) -> None:
    mounted = {entry.target for entry in mounts_under(base, entries)}
    for child in base.iterdir():
        if child.is_dir() and not child.is_symlink() and str(child) not in mounted:
            with contextlib.suppress(OSError):
                child.rmdir()  # only succeeds when empty


def reconcile(
    settings: Settings, *, mounts_path: Path | None = None, fstab: Path | None = None
) -> list[dict[str, Any]]:
    """Mount new removable drives and clean up removed ones; returns new events."""
    mounts_path = mounts_path or PROC_MOUNTS
    fstab = fstab or FSTAB
    sink: list[dict[str, Any]] = []
    base = ensure_base(settings)
    _unmount_stale(base, read_mounts(mounts_path), sink)
    gid = service_gid(settings)
    stdout = _run_checked(["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS])
    for candidate in candidates(stdout, fstab=fstab_specs(fstab)):
        _mount_one(base, candidate, gid, sink)
    _remove_empty_folders(base, read_mounts(mounts_path))
    return sink


def flush(
    settings: Settings, mountpoint: str, *, mounts_path: Path | None = None
) -> dict[str, Any]:
    """Write cached data of one auto-mounted drive to the device."""
    base = settings.removable_mount_dir
    entries = read_mounts(mounts_path or PROC_MOUNTS)
    known = {entry.target for entry in mounts_under(base, entries)}
    target = str(Path(mountpoint))
    if target not in known:
        raise ValidationError(
            "not an auto-mounted removable drive", details={"mountpoint": mountpoint}
        )
    _run_checked(["sync", "-f", target], timeout=300)
    return {"mountpoint": target, "flushed": True}


def run_loop(settings: Settings, stop: threading.Event) -> None:
    """Poll for plugged-in and removed drives until ``stop`` is set."""
    logger.info(
        "auto-mounting removable drives under %s every %ss",
        settings.removable_mount_dir,
        settings.removable_poll_seconds,
    )
    while not stop.is_set():
        try:
            reconcile(settings)
        except Exception:  # keep polling; the next pass may succeed
            logger.exception("removable drive scan failed")
        stop.wait(settings.removable_poll_seconds)
