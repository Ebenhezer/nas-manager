"""Fixed allowlist of privileged operations for the helper (Phase 5).

Every operation maps to a handler in ``OPERATIONS``. Handlers build their
own ``argv`` from strictly validated parameters and run with ``shell=False``
and a minimal environment; a client can never supply command lines, option
flags (device names may not start with ``-``), paths outside ``/dev`` or the
configured Samba config path, or filesystem types outside the allowlist.
State-changing operations (mount/umount/scrub/samba apply) are additionally
bound to a deterministic ``intent`` digest computed from the *validated*
parameters, so a confirmation can never be replayed against different
arguments.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat as stat_module
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.helper.protocol import (
    MAX_PAYLOAD_BYTES,
    intent_digest,
    validate_device_name,
    validate_fstype,
    validate_mount_options,
    validate_mountpoint,
)

logger = logging.getLogger(__name__)
# The helper runs commands with a fixed, minimal environment and no shell.
# Samba config markers are a protocol contract between the web process and the
# helper; they mirror app/services/samba.py (a test keeps them in sync).
SAFE_ENV: dict[str, str] = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
}
MANAGED_BEGIN = "# ==== NAS-MANAGER MANAGED BEGIN ===="
MANAGED_END = "# ==== NAS-MANAGER MANAGED END ===="

_RUN_TIMEOUT_SECONDS = 30
_SMART_TIMEOUT_SECONDS = 60

LSBLK_COLUMNS = (
    "NAME,PATH,TYPE,SIZE,ROTA,TRAN,MODEL,SERIAL,WWN,MOUNTPOINT,FSTYPE,LABEL,PARTLABEL,UUID"
)
FINDMNT_COLUMNS = "TARGET,SOURCE,FSTYPE,OPTIONS,USED,AVAIL,SIZE"

_RAID_NAME_RE = re.compile(r"^md[0-9]+$")


class HelperError(Exception):
    """Base for errors the helper reports back to the caller."""

    code = "helper_error"

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class UnknownOperationError(HelperError):
    code = "unknown_operation"


class ValidationError(HelperError):
    code = "validation_error"


class IntentMismatchError(HelperError):
    code = "intent_mismatch"


class OperationFailedError(HelperError):
    code = "operation_failed"


# --------------------------------------------------------------------------
# Low-level command execution
# --------------------------------------------------------------------------


def _run(
    argv: list[str], *, timeout: int = _RUN_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    """Run one allowlisted command and fail with a typed error on missing
    binaries, timeouts or non-zero exit codes. Never a shell, never caller
    input as argv fragments beyond the validated positional parameters."""
    try:
        return subprocess.run(  # noqa: S603 - argv is built by allowlisted handlers only
            argv, capture_output=True, text=True, timeout=timeout, env=SAFE_ENV, check=False
        )
    except FileNotFoundError as exc:
        command = argv[0]
        raise OperationFailedError(
            f"required command is not installed: {command}", details={"command": command}
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise OperationFailedError(f"command timed out: {argv[0]}") from exc


def _run_checked(argv: list[str], *, timeout: int = _RUN_TIMEOUT_SECONDS) -> str:
    result = _run(argv, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-500:]
        raise OperationFailedError(
            f"command failed: {argv[0]}",
            details={"command": argv[0], "returncode": result.returncode, "output": detail},
        )
    return result.stdout


# --------------------------------------------------------------------------
# Validation helpers used across operations
# --------------------------------------------------------------------------


def _block_device_path(name: str) -> str:
    path = validate_device_name(name)
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError as exc:
        raise ValidationError(f"device does not exist: {path}") from exc
    except OSError as exc:
        raise OperationFailedError(f"cannot stat device {path}") from exc
    if not stat_module.S_ISBLK(mode):
        raise ValidationError(f"not a block device: {path}")
    return path


def _sync_action_path(name: str) -> str:
    if not _RAID_NAME_RE.fullmatch(name):
        raise ValidationError(f"invalid RAID device name: {name}")
    path = Path("/sys/block") / name / "md" / "sync_action"
    if not path.exists():
        raise ValidationError(f"not an md RAID device: {name}")
    return str(path)


def _ensure_intent(op: str, params: dict[str, Any], provided: str) -> None:
    """Require the caller's intent digest to match the *validated* parameters.

    The ``intent`` key itself is excluded from the binding so the digest is
    computed over exactly the parameters the operation performs, matching the
    digest the web process computed from its own validated inputs.
    """
    binding = {key: value for key, value in params.items() if key != "intent"}
    expected = intent_digest(op, binding)
    if provided != expected:
        raise IntentMismatchError(
            "confirmation intent does not match the operation parameters",
            details={"op": op},
        )


def _findmnt_targets(source: str) -> list[str]:
    """Mount targets whose source is ``source`` (e.g. ``/dev/sdb1``)."""
    result = _run(["findmnt", "-n", "-r", "-o", "TARGET", "--source", source])
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _path_is_mountpoint(path: str) -> bool:
    result = _run(["findmnt", "-n", "--target", path])
    return result.returncode == 0


def _ensure_device_free(name: str, mountpoint: str) -> None:
    device = f"/dev/{name}"
    targets = _findmnt_targets(device)
    if targets:
        raise OperationFailedError(
            "device is already mounted",
            details={"device": device, "mountpoints": targets},
        )
    if _path_is_mountpoint(mountpoint):
        raise OperationFailedError(
            "mount point is already in use", details={"mountpoint": mountpoint}
        )


def _op_ping(params: dict[str, Any], settings: Settings) -> dict[str, Any]:
    del params
    attempts = settings.helper_timeout_seconds
    return {"pong": True, "version": settings.app_name or "nas-helper", "timeout_seconds": attempts}


# --------------------------------------------------------------------------
# Read-only discovery operations
# --------------------------------------------------------------------------


def _op_disk_list(params: dict[str, Any], settings: Settings) -> dict[str, Any]:
    del params, settings
    stdout = _run_checked(["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS])
    return {"stdout": stdout}


def _op_disk_detail(params: dict[str, Any], settings: Settings) -> dict[str, Any]:
    del settings
    device = _block_device_path(params["name"])
    stdout = _run_checked(["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS, device])
    return {"stdout": stdout}


def _op_disk_smart(params: dict[str, Any], settings: Settings) -> dict[str, Any]:
    del settings
    device = _block_device_path(params["name"])
    # smartctl exits non-zero (but usefully) for devices without SMART, so the
    # raw output and exit code are returned and the app decides how to render.
    result = _run(["smartctl", "-j", "-H", "-A", "-i", device], timeout=_SMART_TIMEOUT_SECONDS)
    return {
        "stdout": result.stdout,
        "stderr": (result.stderr or "")[-500:],
        "returncode": result.returncode,
    }


def _op_volume_list(params: dict[str, Any], settings: Settings) -> dict[str, Any]:
    del params, settings
    stdout = _run_checked(["findmnt", "-J", "-b", "-o", FINDMNT_COLUMNS])
    return {"stdout": stdout}


def _op_raid_list(params: dict[str, Any], settings: Settings) -> dict[str, Any]:
    del params, settings
    try:
        mdstat = Path("/proc/mdstat").read_text(encoding="utf-8")
    except OSError as exc:
        raise OperationFailedError("cannot read /proc/mdstat") from exc
    scan = ""
    with suppress(OperationFailedError):
        # mdadm is optional; the app falls back to /proc/mdstat alone.
        scan = _run_checked(["mdadm", "--detail", "--scan"])
    return {"mdstat": mdstat, "detail_scan": scan}


# --------------------------------------------------------------------------
# State-changing operations (explicit confirmation required)
# --------------------------------------------------------------------------


def _op_mount(params: dict[str, Any], settings: Settings) -> dict[str, Any]:
    del settings
    name = params["name"]
    device = _block_device_path(name)
    mountpoint = validate_mountpoint(params["mountpoint"])
    fstype = validate_fstype(params["fstype"])
    options = validate_mount_options(params["options"])
    # The confirmation is bound to the canonicalized parameters, exactly as the
    # web process computes it before previewing.
    binding = {"name": name, "mountpoint": mountpoint, "fstype": fstype, "options": options}
    _ensure_intent("mount", binding, params["intent"])
    _ensure_device_free(name, mountpoint)

    target_dir = Path(mountpoint)
    if not target_dir.exists():
        target_dir.mkdir(parents=True, exist_ok=True)
    if not target_dir.is_dir():
        raise OperationFailedError(f"mount point is not a directory: {mountpoint}")

    argv = ["mount", "-t", fstype]
    if options:
        argv.extend(["-o", options])
    argv.extend([device, mountpoint])
    _run_checked(argv)
    return {"device": device, "mountpoint": mountpoint, "fstype": fstype, "options": options}


def _op_unmount(params: dict[str, Any], settings: Settings) -> dict[str, Any]:
    del settings
    device = _block_device_path(params["name"])
    mountpoint = validate_mountpoint(params["mountpoint"])
    binding = {"name": params["name"], "mountpoint": mountpoint}
    _ensure_intent("unmount", binding, params["intent"])

    targets = _findmnt_targets(device)
    if mountpoint not in targets:
        raise OperationFailedError(
            "device is not mounted at the requested mount point",
            details={"device": device, "mountpoint": mountpoint},
        )
    _run_checked(["umount", mountpoint])
    return {"device": device, "mountpoint": mountpoint}


def _op_raid_scrub(params: dict[str, Any], settings: Settings) -> dict[str, Any]:
    del settings
    name = params["name"]
    sync_action_path = _sync_action_path(name)
    _ensure_intent("raid.scrub", params, params["intent"])
    previous = ""
    try:
        previous = Path(sync_action_path).read_text(encoding="utf-8").strip()
        Path(sync_action_path).write_text("check", encoding="utf-8")
    except OSError as exc:
        raise OperationFailedError(
            f"cannot start a scrub on {name}", details={"path": sync_action_path}
        ) from exc
    return {"name": name, "sync_action": "check", "previous_action": previous}


def _op_samba_apply(params: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Stage, validate, back up and atomically install ``smb.conf``, then
    reload ``smbd``. The helper takes over the write+reload that the
    unprivileged web process can never perform on a production host."""
    raw_path = params.get("config_path")
    content = params.get("content")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValidationError("config_path is required")
    config_path = Path(raw_path).resolve()
    configured = settings.samba_config_path.resolve()
    if config_path != configured:
        raise ValidationError("config_path is not the configured Samba configuration path")
    if not isinstance(content, str) or not content:
        raise ValidationError("content is required")
    if len(content.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValidationError("content exceeds the maximum size")
    stripped = [line.strip() for line in content.splitlines()]
    if MANAGED_BEGIN not in stripped or MANAGED_END not in stripped:
        raise ValidationError("content is missing the managed configuration markers")
    _run_testparm(content)

    backup_path = _backup_config(config_path, settings.resolved_samba_backup_dir)
    _atomic_replace(config_path, content)

    reload_performed = False
    reload_message: str | None = None
    try:
        result = _run(["smbcontrol", "all", "reload-config"])
        if result.returncode == 0:
            reload_performed = True
        else:
            detail = (result.stderr or result.stdout or "").strip()[-500:] or "unknown error"
            reload_message = detail
    except OperationFailedError as exc:
        reload_message = exc.message

    return {
        "config_path": str(config_path),
        "backup_path": backup_path,
        "validated": True,
        "reload_performed": reload_performed,
        "reload_message": reload_message,
    }


# --------------------------------------------------------------------------
# Samba apply internals
# --------------------------------------------------------------------------


def _run_testparm(text: str) -> bool:
    """Validate ``text`` with ``testparm -s`` against a temp copy."""
    with tempfile.NamedTemporaryFile(
        "w", prefix="nas-smb-", suffix=".conf", delete=False
    ) as handle:
        handle.write(text)
        tmp_path = handle.name
    try:
        result = _run(["testparm", "-s", tmp_path])
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-500:]
        raise ValidationError("content failed testparm validation", details={"testparm": detail})
    return True


def _backup_config(config_path: Path, backup_dir: Path) -> str | None:
    if not config_path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    backup_path = backup_dir / f"smb.conf.{stamp}.bak"
    shutil.copy2(config_path, backup_path)
    return str(backup_path)


def _atomic_replace(config_path: Path, text: str) -> None:
    """Atomic write preserving the previous file's mode when one exists."""
    mode = 0o644
    if config_path.exists():
        mode = stat_module.S_IMODE(os.stat(config_path).st_mode)
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    fd, tmp_name = tempfile.mkstemp(dir=str(config_path.parent), prefix=".smb.conf.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, config_path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Registry and dispatch
# --------------------------------------------------------------------------

OperationHandler = Callable[[dict[str, Any], Settings], dict[str, Any]]

OPERATIONS: dict[str, OperationHandler] = {
    "ping": _op_ping,
    "disk.list": _op_disk_list,
    "disk.detail": _op_disk_detail,
    "disk.smart": _op_disk_smart,
    "volume.list": _op_volume_list,
    "raid.list": _op_raid_list,
    "mount": _op_mount,
    "unmount": _op_unmount,
    "raid.scrub": _op_raid_scrub,
    "samba.apply": _op_samba_apply,
}


def dispatch(op: str, params: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Execute one allowlisted operation with validated parameters.

    Raises ``HelperError`` subclasses; callers (the server) translate them
    into the response envelope. This function is the single entry point the
    socket server uses, and the one unit tests exercise directly.
    """
    handler = OPERATIONS.get(op)
    if handler is None:
        raise UnknownOperationError(f"unknown operation: {op}", details={"op": op})
    if not isinstance(params, dict):
        raise ValidationError("params must be a JSON object")
    try:
        return handler(params, settings)
    except HelperError:
        raise
    except (ValueError, TypeError) as exc:
        raise ValidationError(str(exc)) from exc
