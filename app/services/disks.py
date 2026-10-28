"""Disk discovery, storage and RAID service (Phase 5).

Discovery is read-only and goes through the privileged helper (``lsblk``,
``findmnt``, ``/proc/mdstat``, ``smartctl``). State-changing operations pass
the helper's fixed allowlist with an explicit confirmation and an intent
digest that binds the operation to exactly the parameters that were previewed.
All actions are audited.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.errors import BadRequestError
from app.helper.protocol import (
    intent_digest,
    validate_device_name,
    validate_fstype,
    validate_mount_options,
    validate_mountpoint,
)
from app.models import User
from app.schemas.disks import (
    DiskPartition,
    HelperStatus,
    MountPreview,
    OperationResult,
    PhysicalDisk,
    RaidArray,
    RaidList,
    RaidMember,
    ScrubPreview,
    SmartAttribute,
    SmartData,
    UnmountPreview,
    Volume,
)
from app.services import audit
from app.services.helper_client import call_helper, helper_ping

logger = logging.getLogger(__name__)

_RAID_NAME_RE = re.compile(r"^md[0-9]+$")

_MDSTAT_LEVELS = frozenset(
    {
        "linear",
        "raid0",
        "raid1",
        "raid4",
        "raid5",
        "raid6",
        "raid10",
        "multipath",
        "faulty",
        "container",
    }
)
_MEMBER_CHAR_STATE = {"U": "active sync", "_": "failed", "S": "spare", "R": "replacement"}


# --------------------------------------------------------------------------
# Parsing (pure functions, unit-tested with fixtures)
# --------------------------------------------------------------------------


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y"}:
            return True
        if lowered in {"0", "false", "no", "n"}:
            return False
    return None


def _collect_mountpoints(node: dict[str, object]) -> list[str]:
    mountpoints: list[str] = []
    mountpoint = node.get("mountpoint")
    if isinstance(mountpoint, str) and mountpoint:
        mountpoints.append(mountpoint)
    raw_children = node.get("children")
    if isinstance(raw_children, list):
        for child in raw_children:
            if isinstance(child, dict):
                mountpoints.extend(_collect_mountpoints(child))
    return mountpoints


def _parse_partition(node: dict[str, object]) -> DiskPartition:
    return DiskPartition(
        name=node.get("name", ""),
        path=node.get("path", ""),
        size_bytes=_as_int(node.get("size")) or 0,
        fs_type=node.get("fstype"),
        label=node.get("label"),
        partlabel=node.get("partlabel"),
        uuid=node.get("uuid"),
        mountpoints=_collect_mountpoints(node),
    )


def _parse_disk_node(node: dict[str, object]) -> PhysicalDisk:
    raw_children = node.get("children")
    children = [
        child
        for child in (raw_children if isinstance(raw_children, list) else [])
        if isinstance(child, dict) and child.get("type") == "part"
    ]
    return PhysicalDisk(
        name=node.get("name", ""),
        path=node.get("path", ""),
        size_bytes=_as_int(node.get("size")) or 0,
        rotational=_as_bool(node.get("rota")),
        transport=node.get("tran"),
        model=node.get("model"),
        serial=node.get("serial"),
        wwn=node.get("wwn"),
        mountpoints=_collect_mountpoints(node),
        partitions=[_parse_partition(child) for child in children],
    )


def parse_lsblk(stdout: str) -> list[PhysicalDisk]:
    """Parse ``lsblk -J -b -o ...`` output into physical disks with partitions."""
    data = json.loads(stdout)
    nodes = data.get("blockdevices", []) or []
    return [_parse_disk_node(node) for node in nodes if node.get("type") == "disk"]


def parse_lsblk_detail(stdout: str) -> PhysicalDisk:
    """Parse ``lsblk -J`` for a single device (its root node is the device)."""
    data = json.loads(stdout)
    nodes = data.get("blockdevices", []) or []
    if not nodes:
        raise BadRequestError("the device list came back empty; did the device disappear?")
    return _parse_disk_node(nodes[0])


def parse_findmnt(stdout: str) -> list[Volume]:
    """Parse ``findmnt -J -b -o ...`` output into mounted volumes."""
    data = json.loads(stdout)
    volumes: list[Volume] = []

    def walk(filesystems: list[dict[str, object]]) -> None:
        for fs in filesystems or []:
            volumes.append(
                Volume(
                    target=fs.get("target", ""),
                    source=fs.get("source", ""),
                    fs_type=fs.get("fstype"),
                    options=[o for o in str(fs.get("options") or "").split(",") if o],
                    used_bytes=_as_int(fs.get("used")),
                    avail_bytes=_as_int(fs.get("avail")),
                    size_bytes=_as_int(fs.get("size")),
                )
            )
            raw_children = fs.get("children")
            if isinstance(raw_children, list):
                walk(raw_children)

    raw = data.get("filesystems")
    if isinstance(raw, list):
        walk(raw)
    return volumes


def _split_mdstat_blocks(text: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("md") and " : " in line:
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _parse_md_block(block: list[str]) -> RaidArray:
    header = block[0]
    tokens = header.split()
    # /proc/mdstat header: "md0 : active raid1 sdb1[1] sdc1[0]"
    name = tokens[0].rstrip(":").strip() if tokens else ""
    have_colon = len(tokens) >= 2 and tokens[1] == ":"
    rest = tokens[2:] if have_colon else tokens[1:]
    # Without a level (e.g. "md2 : inactive sdh1[0]") the first rest token is
    # the state and the members follow it.
    state: str | None = rest[0] if rest else None
    level: str | None = None
    members_tokens: list[str] = []
    if len(rest) >= 2 and rest[1] in _MDSTAT_LEVELS:
        level = rest[1]
        members_tokens = rest[2:]
    elif len(rest) >= 1:
        members_tokens = rest[1:]

    body = "\n".join(block[1:])
    size_match = re.search(r"(\d+)\s+blocks", body)
    size_bytes = int(size_match.group(1)) * 1024 if size_match else None

    active_total = re.search(r"\[(\d+)/(\d+)\]", body)
    active_disks = int(active_total.group(1)) if active_total else None
    total_disks = int(active_total.group(2)) if active_total else None

    device_chars = ""
    state_str = re.search(r"\[([U_S_R_]+)\]", body)
    if state_str:
        device_chars = state_str.group(1)

    sync_action: str | None = None
    progress = re.search(r"(resync|recovery|reshape|check)\s*=\s*(\S+)", body)
    if progress:
        sync_action = progress.group(1).strip().lower()
    else:
        pending = re.search(r"(?:resync|recovery|reshape|check)=PENDING", body)
        if pending:
            sync_action = "pending"

    members: list[RaidMember] = []
    for index, token in enumerate(members_tokens):
        member_match = re.match(r"^([^[]+)\[\d+\](?:\((S)\))?$", token)
        if not member_match:
            continue
        member_device = member_match.group(1)
        if member_match.group(2) == "S":
            members.append(RaidMember(device=member_device, state="spare"))
            continue
        char = device_chars[index] if index < len(device_chars) else "?"
        state_label = _MEMBER_CHAR_STATE.get(char, "unknown")
        members.append(RaidMember(device=member_device, state=state_label))

    degraded = bool(
        (active_disks is not None and total_disks is not None and active_disks < total_disks)
        or "_" in device_chars
    )
    return RaidArray(
        name=name,
        path=f"/dev/{name}" if name else "",
        level=level,
        state=state,
        size_bytes=size_bytes,
        active_disks=active_disks,
        total_disks=total_disks,
        sync_action=sync_action,
        degraded=degraded,
        members=members,
    )


def parse_mdstat(text: str) -> list[RaidArray]:
    return [_parse_md_block(block) for block in _split_mdstat_blocks(text)]


def parse_smartctl(stdout: str, *, device: str, returncode: int) -> SmartData:
    """Parse ``smartctl -j -H -A -i`` output; tolerate devices without SMART."""
    if returncode != 0 or not stdout:
        return SmartData(device=device, error=f"smartctl exited with status {returncode}")
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return SmartData(device=device, error="smartctl output was not valid JSON")

    attributes: list[SmartAttribute] = []
    table = (data.get("ata_smart_attributes") or {}).get("table") or []
    for entry in table:
        raw = entry.get("raw")
        raw_value = (
            raw.get("string") if isinstance(raw, dict) else (str(raw) if raw is not None else None)
        )
        attributes.append(
            SmartAttribute(
                id=_as_int(entry.get("id")),
                name=entry.get("name"),
                value=_as_int(entry.get("value")),
                worst=_as_int(entry.get("worst")),
                threshold=_as_int(entry.get("thresh")),
                raw=raw_value,
            )
        )

    status = data.get("smart_status") or {}
    passed = status.get("passed")
    health = None if passed is None else ("passed" if passed else "failed")
    temperature = (data.get("temperature") or {}).get("current")
    capacity = (data.get("user_capacity") or {}).get("bytes")
    messages = [m.get("string", "") for m in (data.get("messages") or []) if isinstance(m, dict)]
    device_info = data.get("device") or {}
    return SmartData(
        device=device,
        available=True,
        health=health,
        model=data.get("model_name"),
        serial=data.get("serial_number"),
        firmware=data.get("firmware_version"),
        interface=device_info.get("type"),
        capacity_bytes=_as_int(capacity),
        temperature=_as_int(temperature),
        attributes=attributes,
        messages=messages,
    )


# --------------------------------------------------------------------------
# Parameter validation (fail fast with 400 before any helper call)
# --------------------------------------------------------------------------


def _device_arg(name: str) -> str:
    try:
        return validate_device_name(name)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc


def _validated_mountpoint(value: str) -> str:
    try:
        return validate_mountpoint(value)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc


def _validated_fstype(value: str) -> str:
    try:
        return validate_fstype(value)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc


def _validated_options(value: str) -> str:
    try:
        return validate_mount_options(value)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc


def _raid_name_arg(name: str) -> str:
    if not _RAID_NAME_RE.fullmatch(name):
        raise BadRequestError("RAID device name must look like 'md0'")
    return name


def _ensure_intent(op: str, params: dict[str, Any], provided: str) -> None:
    expected = intent_digest(op, params)
    if not provided or provided != expected:
        raise BadRequestError(
            "confirmation intent does not match the operation parameters; re-run the preview",
            details={"op": op},
        )


def _validate_confirm(confirm: bool) -> None:
    if not confirm:
        raise BadRequestError("explicit confirmation is required (confirm: true)")


def _audit_ctx(context: dict[str, Any] | None) -> dict[str, Any]:
    source = context or {}
    return {
        "ip_address": source.get("ip_address"),
        "user_agent": source.get("user_agent"),
        "request_id": source.get("request_id"),
    }


# --------------------------------------------------------------------------
# Read-only discovery
# --------------------------------------------------------------------------


def helper_status(settings: Settings) -> HelperStatus:
    """Probe the helper without failing the page when it is down."""
    socket_path = str(settings.resolved_helper_socket)
    if not settings.helper_enabled:
        return HelperStatus(
            enabled=False,
            available=False,
            socket=socket_path,
            error="the privileged helper is not enabled (NAS_HELPER_ENABLED=false)",
        )
    try:
        helper_ping(settings)
    except Exception as exc:  # every failure means "unavailable"
        logger.warning("helper probe failed", extra={"error": str(exc)})
        return HelperStatus(enabled=True, available=False, socket=socket_path, error=str(exc))
    return HelperStatus(enabled=True, available=True, socket=socket_path)


def list_disks(settings: Settings) -> list[PhysicalDisk]:
    response = call_helper(settings, "disk.list", {})
    return parse_lsblk(response["stdout"])


def disk_detail(settings: Settings, name: str) -> PhysicalDisk:
    _device_arg(name)
    response = call_helper(settings, "disk.detail", {"name": name})
    return parse_lsblk_detail(response["stdout"])


def smart_status(settings: Settings, name: str) -> SmartData:
    _device_arg(name)
    response = call_helper(settings, "disk.smart", {"name": name})
    return parse_smartctl(
        response.get("stdout", ""),
        device=f"/dev/{name}",
        returncode=int(response.get("returncode", 1)),
    )


def list_volumes(settings: Settings) -> list[Volume]:
    response = call_helper(settings, "volume.list", {})
    return parse_findmnt(response["stdout"])


def list_raid(settings: Settings) -> RaidList:
    response = call_helper(settings, "raid.list", {})
    return RaidList(
        arrays=parse_mdstat(response.get("mdstat", "")),
        detail_scan=response.get("detail_scan", ""),
    )


# --------------------------------------------------------------------------
# Dry-run previews (what would happen, plus the intent to confirm)
# --------------------------------------------------------------------------


def mount_preview(
    settings: Settings, *, name: str, mountpoint: str, fstype: str, options: str
) -> MountPreview:
    device = _device_arg(name)
    mountpoint = _validated_mountpoint(mountpoint)
    fstype = _validated_fstype(fstype)
    options = _validated_options(options)
    # Reachability + existence of the device are part of the preview.
    disk_detail(settings, name)
    warnings: list[str] = []
    if not Path(mountpoint).exists():
        warnings.append("mount point does not exist yet; it will be created on confirm")
    params = {"name": name, "mountpoint": mountpoint, "fstype": fstype, "options": options}
    return MountPreview(
        action="mount",
        device=device,
        mountpoint=mountpoint,
        fstype=fstype,
        options=options,
        intent=intent_digest("mount", params),
        warnings=warnings,
    )


def unmount_preview(settings: Settings, *, name: str, mountpoint: str | None) -> UnmountPreview:
    device = _device_arg(name)
    detail = disk_detail(settings, name)
    known = detail.mountpoints
    if mountpoint is None:
        if len(known) != 1:
            raise BadRequestError(
                "device has multiple mount points; pass an explicit mountpoint",
                details={"mountpoints": known},
            )
        mountpoint = known[0]
    mountpoint = _validated_mountpoint(mountpoint)
    warnings: list[str] = []
    if mountpoint not in known:
        warnings.append(f"{device} is not currently mounted at {mountpoint}")
    params = {"name": name, "mountpoint": mountpoint}
    return UnmountPreview(
        action="unmount",
        device=device,
        mountpoint=mountpoint,
        intent=intent_digest("unmount", params),
        warnings=warnings,
    )


def scrub_preview(settings: Settings, *, name: str) -> ScrubPreview:
    _raid_name_arg(name)
    raid = list_raid(settings)  # reachability + array existence are part of the preview
    if not any(array.name == name for array in raid.arrays):
        raise BadRequestError(f"no such RAID array: {name}")
    params = {"name": name}
    return ScrubPreview(
        action="raid.scrub",
        name=name,
        sync_action="check",
        intent=intent_digest("raid.scrub", params),
        warnings=["a check scrub reads every block and reports mismatches; it never writes."],
    )


# --------------------------------------------------------------------------
# Confirmed actions (explicit confirmation + intent + audit)
# --------------------------------------------------------------------------


def mount_device(
    db: Any,
    settings: Settings,
    *,
    name: str,
    mountpoint: str,
    fstype: str,
    options: str,
    confirm: bool,
    intent: str,
    actor: User,
    context: dict[str, Any] | None = None,
) -> OperationResult:
    _device_arg(name)
    mountpoint = _validated_mountpoint(mountpoint)
    fstype = _validated_fstype(fstype)
    options = _validated_options(options)
    _validate_confirm(confirm)
    params = {"name": name, "mountpoint": mountpoint, "fstype": fstype, "options": options}
    _ensure_intent("mount", params, intent)
    payload = {**params, "intent": intent}
    try:
        result = call_helper(settings, "mount", payload)
    except Exception as exc:
        _record_failure(
            db=db,
            event=audit.DISK_ACTION_FAILED,
            actor=actor,
            target=f"/dev/{name}",
            details=payload,
            context=context,
            error=exc,
        )
        raise
    audit.record_audit_event(
        db,
        event=audit.DISK_MOUNTED,
        category=audit.CATEGORY_DISK,
        actor=actor,
        target_type="block_device",
        target_id=result.get("device") or f"/dev/{name}",
        details={
            "device": result.get("device"),
            "mountpoint": result.get("mountpoint"),
            "fstype": result.get("fstype"),
            "options": result.get("options"),
        },
        **_audit_ctx(context),
        commit=False,
    )
    db.commit()
    return OperationResult(
        ok=True,
        message=f"Mounted {result.get('device')} at {result.get('mountpoint')}",
        details=result,
    )


def unmount_device(
    db: Any,
    settings: Settings,
    *,
    name: str,
    mountpoint: str | None,
    confirm: bool,
    intent: str,
    actor: User,
    context: dict[str, Any] | None = None,
) -> OperationResult:
    device = _device_arg(name)
    detail = disk_detail(settings, name)
    known = detail.mountpoints
    if mountpoint is None:
        if len(known) != 1:
            raise BadRequestError(
                "device has multiple mount points; pass an explicit mountpoint",
                details={"mountpoints": known},
            )
        mountpoint = known[0]
    mountpoint = _validated_mountpoint(mountpoint)
    _validate_confirm(confirm)
    params = {"name": name, "mountpoint": mountpoint}
    _ensure_intent("unmount", params, intent)
    payload = {**params, "intent": intent}
    try:
        result = call_helper(settings, "unmount", payload)
    except Exception as exc:
        _record_failure(
            db=db,
            event=audit.DISK_ACTION_FAILED,
            actor=actor,
            target=device,
            details=payload,
            context=context,
            error=exc,
        )
        raise
    audit.record_audit_event(
        db,
        event=audit.DISK_UNMOUNTED,
        category=audit.CATEGORY_DISK,
        actor=actor,
        target_type="block_device",
        target_id=result.get("device") or device,
        details={"device": result.get("device"), "mountpoint": result.get("mountpoint")},
        **_audit_ctx(context),
        commit=False,
    )
    db.commit()
    return OperationResult(
        ok=True,
        message=f"Unmounted {result.get('device')} from {result.get('mountpoint')}",
        details=result,
    )


def scrub_array(
    db: Any,
    settings: Settings,
    *,
    name: str,
    confirm: bool,
    intent: str,
    actor: User,
    context: dict[str, Any] | None = None,
) -> OperationResult:
    _raid_name_arg(name)
    _validate_confirm(confirm)
    params = {"name": name}
    _ensure_intent("raid.scrub", params, intent)
    payload = {**params, "intent": intent}
    try:
        result = call_helper(settings, "raid.scrub", payload)
    except Exception as exc:
        _record_failure(
            db=db,
            event=audit.RAID_ACTION_FAILED,
            actor=actor,
            target=f"/dev/{name}",
            details=payload,
            context=context,
            error=exc,
        )
        raise
    audit.record_audit_event(
        db,
        event=audit.RAID_SCRUB_STARTED,
        category=audit.CATEGORY_RAID,
        actor=actor,
        target_type="md_device",
        target_id=result.get("name") or name,
        details={
            "array": result.get("name"),
            "sync_action": result.get("sync_action"),
            "previous_action": result.get("previous_action"),
        },
        **_audit_ctx(context),
        commit=False,
    )
    db.commit()
    return OperationResult(
        ok=True,
        message=f"Started a {result.get('sync_action', 'check')} scrub on {result.get('name')}",
        details=result,
    )


def _record_failure(
    *,
    db: Any,
    event: str,
    actor: User,
    target: str,
    details: dict[str, Any],
    context: dict[str, Any] | None,
    error: Exception,
) -> None:
    """Audit a failed privileged action so the trail shows what was attempted."""
    category = audit.CATEGORY_DISK if event == audit.DISK_ACTION_FAILED else audit.CATEGORY_RAID
    try:
        audit.record_audit_event(
            db,
            event=event,
            category=category,
            outcome=audit.OUTCOME_FAILURE,
            actor=actor,
            target_type="block_device" if event == audit.DISK_ACTION_FAILED else "md_device",
            target_id=target,
            details={"attempt": details, "error": str(error)},
            **_audit_ctx(context),
            commit=False,
        )
        db.commit()
    except Exception:  # auditing must never mask the original error
        logger.exception("failed to record audit event for %s", event)
