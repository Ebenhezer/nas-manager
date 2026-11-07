"""USB / removable drives as seen by the web process (Phase 16).

The root helper mounts plugged-in USB drives under
``NAS_REMOVABLE_MOUNT_DIR``; this module only *reads* the kernel's mount
table to list them for the Files page, so it needs no helper call. Access
is governed by ``removable:read`` / ``removable:write`` (see
``app.api.v1.files``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.core.mounts import PROC_MOUNTS, mounts_under, read_mounts

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RemovableDrive:
    root: str
    label: str
    device: str
    fstype: str
    total_bytes: int
    free_bytes: int
    writable: bool


def _drive(target: str, device: str, fstype: str, read_only: bool) -> RemovableDrive | None:
    try:
        usage = os.statvfs(target)
    except OSError:
        return None  # vanished between reading the table and now
    return RemovableDrive(
        root=target,
        label=Path(target).name,
        device=device,
        fstype=fstype,
        total_bytes=usage.f_blocks * usage.f_frsize,
        free_bytes=usage.f_bavail * usage.f_frsize,
        writable=not read_only and os.access(target, os.W_OK | os.X_OK),
    )


def list_drives(settings: Settings, *, mounts_path: Path | None = None) -> list[RemovableDrive]:
    """Mounted removable drives, sorted by label."""
    base = settings.removable_mount_dir
    entries = mounts_under(base, read_mounts(mounts_path or PROC_MOUNTS))
    drives = [
        drive
        for entry in entries
        if (drive := _drive(entry.target, entry.source, entry.fstype, entry.read_only)) is not None
    ]
    return sorted(drives, key=lambda drive: drive.label.lower())


def find_drive(settings: Settings, root_str: str) -> RemovableDrive | None:
    candidate = os.path.normpath(root_str or "")
    for drive in list_drives(settings):
        if drive.root == candidate:
            return drive
    return None


def resolve_drive(settings: Settings, root_str: str) -> Path:
    """The mount folder of a plugged-in drive, or 404 (e.g. it was pulled out)."""
    drive = find_drive(settings, root_str)
    if drive is None:
        raise NotFoundError("That USB drive is no longer connected", details={"root": root_str})
    return Path(drive.root)


_EVENT_NAMES = {
    "mounted": "DISK_REMOVABLE_MOUNTED",
    "unmounted": "DISK_REMOVABLE_UNMOUNTED",
    "failed": "DISK_REMOVABLE_MOUNT_FAILED",
}


def record_helper_events(session: Session, settings: Settings) -> int:
    """Copy the helper's mount/unmount events into the audit log (worker tick)."""
    from app.services import audit
    from app.services.helper_client import call_helper

    if not settings.helper_enabled:
        return 0
    try:
        events = call_helper(settings, "removable.events", {}).get("events") or []
    except Exception:  # helper down or too old: try again next tick
        logger.debug("could not read removable drive events from the helper", exc_info=True)
        return 0
    for event in events:
        action = str(event.get("action"))
        name = _EVENT_NAMES.get(action)
        if name is None:
            continue
        audit.record_audit_event(
            session,
            event=getattr(audit, name),
            category=audit.CATEGORY_DISK,
            outcome=audit.OUTCOME_FAILURE if action == "failed" else audit.OUTCOME_SUCCESS,
            actor_username="system",
            target_type="block_device",
            target_id=str(event.get("device") or ""),
            details={
                key: event.get(key)
                for key in (
                    "device",
                    "label",
                    "uuid",
                    "fstype",
                    "mountpoint",
                    "reason",
                    "error",
                    "at",
                )
                if event.get(key) is not None
            },
            commit=False,
        )
    session.commit()
    return len(events)
