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
from collections.abc import Callable
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
    _run,
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
_failed: dict[str, tuple[float, str]] = {}
# The latest pass's view of every removable partition (removable.devices).
_status: dict[str, DeviceStatus] = {}
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


@dataclass(frozen=True)
class FstabEntry:
    spec: str
    target: str = ""
    fstype: str = ""
    line_no: int = 0


def fstab_entries(path: Path = FSTAB) -> list[FstabEntry]:
    """The non-comment lines of fstab."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries: list[FstabEntry] = []
    for number, line in enumerate(text.splitlines(), start=1):
        fields = line.split("#", 1)[0].split()
        if fields:
            entries.append(
                FstabEntry(
                    spec=fields[0],
                    target=fields[1] if len(fields) > 1 else "",
                    fstype=fields[2] if len(fields) > 2 else "",
                    line_no=number,
                )
            )
    return entries


def fstab_specs(path: Path = FSTAB) -> set[str]:
    """Devices named in fstab (``/dev/...``, ``UUID=``, ``LABEL=``, ...)."""
    return {entry.spec for entry in fstab_entries(path)}


FstabArg = set[str] | list[FstabEntry]


def _as_entries(fstab: FstabArg) -> list[FstabEntry]:
    if isinstance(fstab, set):
        return [FstabEntry(spec=spec) for spec in sorted(fstab)]
    return fstab


def fstab_match(
    node: dict[str, Any], fstab: FstabArg, mounts: list[MountEntry] | None = None
) -> FstabEntry | None:
    """The fstab line that really means this device, if any.

    Stable identifiers (UUID, LABEL, PARTUUID, ...) always count. Kernel names
    such as ``/dev/sda`` are handed out in detection order, so a plugged-in
    stick can get the name an fstab line uses for another disk; such a line
    only counts when it is not the root or swap and its mount point is not
    already in use by a different device.
    """
    name = node.get("name")
    stable: set[str] = set()
    for key, prefix, directory in (
        ("uuid", "UUID=", "by-uuid"),
        ("label", "LABEL=", "by-label"),
        ("partuuid", "PARTUUID=", "by-partuuid"),
        ("partlabel", "PARTLABEL=", "by-partlabel"),
    ):
        if node.get(key):
            stable.update({f"{prefix}{node[key]}", f"/dev/disk/{directory}/{node[key]}"})
    kernel = {f"/dev/{name}", node.get("path")}
    for entry in _as_entries(fstab):
        if entry.spec in stable:
            return entry
        if entry.spec not in kernel:
            continue
        if entry.fstype == "swap" or entry.target in {"/", "none", "swap"}:
            continue
        sources = {m.source for m in mounts or [] if m.target == entry.target}
        if sources and f"/dev/{name}" not in sources:
            continue  # that mount point belongs to another disk
        return entry
    return None


@dataclass
class DeviceStatus:
    """What the helper made of one removable partition (Disks page)."""

    name: str
    device: str
    label: str | None
    uuid: str | None
    fstype: str | None
    size_bytes: int
    # pending (about to be mounted) | mounted | failed | unsupported | ignored
    state: str
    reason: str | None = None
    mountpoint: str | None = None

    def candidate(self) -> Candidate:
        return Candidate(
            name=self.name,
            device=self.device,
            fstype=self.fstype or "",
            label=self.label,
            uuid=self.uuid,
            size_bytes=self.size_bytes,
        )


_IGNORED_FSTYPES = {
    "linux_raid_member": "part of a RAID array (managed on the Disks page)",
    "crypto_luks": "encrypted (LUKS) volume",
    "swap": "swap space",
    "lvm2_member": "LVM volume",
}
# Partition-table placeholders (e.g. an extended partition) are not volumes.
_MIN_VOLUME_BYTES = 1024 * 1024


def probe_filesystem(device: str) -> dict[str, str]:
    """``blkid -p``: read the file system straight from the device.

    ``lsblk`` takes FSTYPE/LABEL/UUID from the udev database, which is empty
    in containers, under WSL, on minimal installs and while udev is still
    probing a freshly plugged stick; this low-level probe needs no udev.
    """
    result = _run(["blkid", "-p", "-o", "export", device])
    if result.returncode != 0:
        return {}
    found: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            found[key.strip()] = value.strip()
    return found


def inspect(
    lsblk_json: str,
    *,
    fstab: FstabArg,
    probe: Callable[[str], dict[str, str]] | None = None,
    mounts: list[MountEntry] | None = None,
) -> list[DeviceStatus]:
    """Every partition of every removable disk, with what to do about it."""
    probe = probe or probe_filesystem
    data = json.loads(lsblk_json)
    found: list[DeviceStatus] = []
    for disk in data.get("blockdevices") or []:
        if disk.get("type") != "disk" or not is_removable_disk(disk):
            continue
        parts = [child for child in disk.get("children") or [] if child.get("type") == "part"]
        for node in parts or [disk]:
            size = int(node.get("size") or 0)
            if parts and size < _MIN_VOLUME_BYTES:
                continue
            try:
                # Only ever hand mount a validated /dev/<name>, never a path from lsblk.
                device = validate_device_name(str(node.get("name") or ""))
            except ValueError:
                continue
            fstype = (node.get("fstype") or "").lower()
            label = node.get("label") or None
            uuid = node.get("uuid") or None
            if not fstype:
                details = probe(device)
                fstype = details.get("TYPE", "").lower()
                label = label or details.get("LABEL") or None
                uuid = uuid or details.get("UUID") or None
            status = DeviceStatus(
                name=str(node.get("name")),
                device=device,
                label=label,
                uuid=uuid,
                fstype=fstype or None,
                size_bytes=size,
                state="pending",
            )
            points = [p for p in [node.get("mountpoint"), *(node.get("mountpoints") or [])] if p]
            probed = {**node, "label": label, "uuid": uuid}
            if points:
                status.state, status.mountpoint = "mounted", points[0]
            elif not fstype:
                status.state = "unsupported"
                status.reason = "file system not recognised (empty or unformatted?)"
            elif fstype in _IGNORED_FSTYPES:
                status.state, status.reason = "ignored", _IGNORED_FSTYPES[fstype]
            elif fstype not in SUPPORTED_FSTYPES:
                status.state, status.reason = "unsupported", f"unsupported file system: {fstype}"
            elif (entry := fstab_match(probed, fstab, mounts)) is not None:
                where = f", line {entry.line_no}" if entry.line_no else ""
                status.state = "ignored"
                status.reason = (
                    f"listed in /etc/fstab{where}: {entry.spec} {entry.target}".rstrip()
                    + " (the system mounts it, not the NAS)"
                )
            found.append(status)
    return found


def candidates(
    lsblk_json: str,
    *,
    fstab: FstabArg,
    probe: Callable[[str], dict[str, str]] | None = None,
    mounts: list[MountEntry] | None = None,
) -> list[Candidate]:
    """Unmounted partitions of removable disks that may be auto-mounted."""
    return [
        status.candidate()
        for status in inspect(lsblk_json, fstab=fstab, probe=probe, mounts=mounts)
        if status.state == "pending"
    ]


_DRIVER_HINTS = {
    "exfat": (
        "this system has no exFAT driver: install exfatprogs (Linux 5.7 or newer) or exfat-fuse"
    ),
    "ntfs": "this system has no NTFS driver: install ntfs-3g (or use a kernel with ntfs3)",
    "ntfs3": "this system has no NTFS driver: install ntfs-3g (or use a kernel with ntfs3)",
    "hfsplus": "this system has no HFS+ driver (kernel module hfsplus)",
    "btrfs": "this system has no btrfs driver (kernel module btrfs)",
    "xfs": "this system has no XFS driver (kernel module xfs)",
}


def explain_mount_error(fstype: str, output: str) -> str:
    """A plain-language reason for a failed mount, with the raw message."""
    text = output.strip()
    lowered = text.lower()
    if "unknown filesystem type" in lowered or "no such device" in lowered:
        hint = _DRIVER_HINTS.get(fstype, f"this system has no driver for {fstype}")
        return f"{hint} ({text})" if text else hint
    if "wrong fs type" in lowered or "bad superblock" in lowered:
        return (
            "the drive could not be read; it may need repair "
            f"(check it on a Windows or Mac computer) ({text})"
        )
    if "read-only" in lowered or "write-protected" in lowered:
        return f"the drive is write-protected ({text})"
    return text or "mount failed"


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
    """Forget events, failed devices and statuses (tests)."""
    with _lock:
        _events.clear()
        _failed.clear()
        _status.clear()


def device_statuses() -> list[dict[str, Any]]:
    with _lock:
        return [asdict(status) for status in sorted(_status.values(), key=lambda s: s.name)]


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def _mount_one(base: Path, status: DeviceStatus, gid: int, sink: list[dict[str, Any]]) -> None:
    candidate = status.candidate()
    key = candidate.uuid or candidate.device
    with _lock:
        failed = _failed.get(key)
    if failed is not None and time.monotonic() - failed[0] < _RETRY_AFTER_SECONDS:
        status.state, status.reason = "failed", failed[1]
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
        reason = explain_mount_error(fstype, str((exc.details or {}).get("output", "")))
        with _lock:
            _failed[key] = (time.monotonic(), reason)
        status.state, status.reason = "failed", reason
        _record(sink, "failed", **asdict(candidate), mountpoint=str(target), error=reason)
        logger.warning("could not mount %s: %s", candidate.device, reason)
        return
    with _lock:
        _failed.pop(key, None)
    status.state, status.reason, status.mountpoint = "mounted", None, str(target)
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
    settings: Settings,
    *,
    mounts_path: Path | None = None,
    fstab: Path | None = None,
    retry: bool = False,
) -> list[dict[str, Any]]:
    """Mount new removable drives and clean up removed ones; returns new events.

    ``retry`` forgets earlier mount failures, so they are tried again now
    (the Disks page's "Try again") instead of after the back-off.
    """
    mounts_path = mounts_path or PROC_MOUNTS
    fstab = fstab or FSTAB
    if retry:
        with _lock:
            _failed.clear()
    sink: list[dict[str, Any]] = []
    base = ensure_base(settings)
    _unmount_stale(base, read_mounts(mounts_path), sink)
    gid = service_gid(settings)
    stdout = _run_checked(["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS])
    statuses = inspect(stdout, fstab=fstab_entries(fstab), mounts=read_mounts(mounts_path))
    for status in statuses:
        if status.state == "pending":
            _mount_one(base, status, gid, sink)
    with _lock:
        _status.clear()
        _status.update({status.device: status for status in statuses})
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
