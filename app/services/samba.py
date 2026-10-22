"""Samba share management and safe ``smb.conf`` rendering.

Shares are the source of truth in the database. This service renders them
into a clearly delimited managed section of ``smb.conf`` and preserves every
line the application does not manage. Before writing it validates the result
with ``testparm -s`` against a temporary copy (never the live file) and backs
up the previous configuration.

The web process must never run as root and must not gain privileges, so it
can only write where the OS allows (development/test paths; production stages
the config and relies on the Phase 5 helper). Reloading ``smbd`` is a
privileged operation that is deferred: this module reports ``reload_required``
and never executes reload/systemctl commands.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import (
    BadRequestError,
    ConfigurationError,
    ConflictError,
    NotFoundError,
)
from app.models import SambaShare, User
from app.services import audit

SHARE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

MANAGED_BEGIN = "# ==== NAS-MANAGER MANAGED BEGIN ===="
MANAGED_END = "# ==== NAS-MANAGER MANAGED END ===="

_TESTPARM_TIMEOUT_SECONDS = 30


class _RequestContext(TypedDict, total=False):
    ip_address: str | None
    user_agent: str | None
    request_id: str | None


def _audit_ctx(context: dict[str, str | None] | None) -> _RequestContext:
    source = context or {}
    return {
        "ip_address": source.get("ip_address"),
        "user_agent": source.get("user_agent"),
        "request_id": source.get("request_id"),
    }


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate_share_name(name: str) -> str:
    value = name.strip().lower()
    if not SHARE_NAME_RE.fullmatch(value):
        raise ValueError(
            "share name must be 1-64 characters: lowercase letters, digits, dots, "
            "underscores or hyphens, starting with a letter or digit"
        )
    return value


def validate_share_path(path: str) -> str:
    value = path.strip()
    absolute = Path(value).expanduser()
    if not absolute.is_absolute():
        raise ValueError("share path must be an absolute path")
    if not absolute.is_dir():
        raise ValueError(f"share path does not exist or is not a directory: {value}")
    return str(absolute)


def validate_free_text(value: str, *, field: str) -> str:
    """Sanitise free-form Samba directives: no newlines means no config injection."""
    cleaned = value.strip()
    if "\n" in cleaned or "\r" in cleaned:
        raise ValueError(f"{field} must be a single line (no newlines)")
    return cleaned


def _validated_create_payload(
    *,
    name: str,
    path: str,
    comment: str,
    read_only: bool,
    browseable: bool,
    guest_ok: bool,
    valid_users: str,
    write_list: str,
    enabled: bool,
) -> dict[str, object]:
    try:
        safe_name = validate_share_name(name)
        safe_path = validate_share_path(path)
        safe_comment = validate_free_text(comment, field="comment")
        safe_users = validate_free_text(valid_users, field="valid_users")
        safe_write = validate_free_text(write_list, field="write_list")
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    return {
        "name": safe_name,
        "path": safe_path,
        "comment": safe_comment,
        "read_only": read_only,
        "browseable": browseable,
        "guest_ok": guest_ok,
        "valid_users": safe_users,
        "write_list": safe_write,
        "enabled": enabled,
    }


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------


def get_share_or_404(session: Session, share_id: int) -> SambaShare:
    share = session.get(SambaShare, share_id)
    if share is None:
        raise NotFoundError("Samba share not found", details={"share_id": share_id})
    return share


def list_shares(session: Session) -> list[SambaShare]:
    return list(session.execute(select(SambaShare).order_by(SambaShare.name)).scalars().all())


def create_share(
    session: Session,
    *,
    name: str,
    path: str,
    comment: str,
    read_only: bool,
    browseable: bool,
    guest_ok: bool,
    valid_users: str,
    write_list: str,
    enabled: bool,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> SambaShare:
    payload = _validated_create_payload(
        name=name,
        path=path,
        comment=comment,
        read_only=read_only,
        browseable=browseable,
        guest_ok=guest_ok,
        valid_users=valid_users,
        write_list=write_list,
        enabled=enabled,
    )
    existing = session.execute(
        select(SambaShare.id).where(SambaShare.name == payload["name"])
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(
            "A share with that name already exists", details={"name": payload["name"]}
        )
    share = SambaShare(**payload)
    session.add(share)
    session.commit()
    audit.record_audit_event(
        session,
        event=audit.SAMBA_SHARE_CREATED,
        category=audit.CATEGORY_SAMBA,
        actor=actor,
        target_type="samba_share",
        target_id=share.id,
        details={"name": share.name, "path": share.path},
        **_audit_ctx(context),
        commit=False,
    )
    session.commit()
    return share


def update_share(
    session: Session,
    share: SambaShare,
    *,
    path: str | None,
    comment: str | None,
    read_only: bool | None,
    browseable: bool | None,
    guest_ok: bool | None,
    valid_users: str | None,
    write_list: str | None,
    enabled: bool | None,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> SambaShare:
    try:
        if path is not None:
            share.path = validate_share_path(path)
        if comment is not None:
            share.comment = validate_free_text(comment, field="comment")
        if valid_users is not None:
            share.valid_users = validate_free_text(valid_users, field="valid_users")
        if write_list is not None:
            share.write_list = validate_free_text(write_list, field="write_list")
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    if read_only is not None:
        share.read_only = read_only
    if browseable is not None:
        share.browseable = browseable
    if guest_ok is not None:
        share.guest_ok = guest_ok
    if enabled is not None:
        share.enabled = enabled
    session.commit()
    audit.record_audit_event(
        session,
        event=audit.SAMBA_SHARE_UPDATED,
        category=audit.CATEGORY_SAMBA,
        actor=actor,
        target_type="samba_share",
        target_id=share.id,
        details={"name": share.name, "path": share.path},
        **_audit_ctx(context),
        commit=False,
    )
    session.commit()
    return share


def delete_share(
    session: Session,
    share: SambaShare,
    *,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> None:
    name = share.name
    session.delete(share)
    session.commit()
    audit.record_audit_event(
        session,
        event=audit.SAMBA_SHARE_DELETED,
        category=audit.CATEGORY_SAMBA,
        actor=actor,
        target_type="samba_share",
        target_id=name,
        details={"name": name},
        **_audit_ctx(context),
        commit=False,
    )
    session.commit()


# --------------------------------------------------------------------------
# smb.conf rendering, validation and applying
# --------------------------------------------------------------------------


def _render_share_section(share: SambaShare) -> list[str]:
    """The ``[share]`` stanza for one share (no surrounding blank lines)."""
    lines = [f"[{share.name}]"]
    lines.append(f"    path = {share.path}")
    lines.append(f"    comment = {share.comment}")
    lines.append("    read only = yes" if share.read_only else "    read only = no")
    lines.append("    browseable = yes" if share.browseable else "    browseable = no")
    lines.append("    guest ok = yes" if share.guest_ok else "    guest ok = no")
    if share.valid_users:
        lines.append(f"    valid users = {share.valid_users}")
    if share.write_list:
        lines.append(f"    write list = {share.write_list}")
    return lines


def render_managed_block(shares: list[SambaShare]) -> str:
    """The full managed section (markers included) for the given shares."""
    sections: list[str] = []
    for share in shares:
        if sections:
            sections.append("")
        sections.extend(_render_share_section(share))
    block = "\n".join(sections)
    return f"{MANAGED_BEGIN}\n{block}\n{MANAGED_END}"


def merge_managed_section(existing_text: str, managed_block: str) -> str:
    """Replace the managed range in ``existing_text`` with ``managed_block``.

    Everything outside the markers is preserved verbatim. When the markers are
    missing or malformed, stray marker lines are dropped and the managed block
    is appended at the end so unmanaged content is never lost.
    """
    lines = existing_text.splitlines()
    begin_idx = next((i for i, line in enumerate(lines) if line.strip() == MANAGED_BEGIN), None)
    end_idx = next((i for i, line in enumerate(lines) if line.strip() == MANAGED_END), None)

    if begin_idx is not None and end_idx is not None and begin_idx < end_idx:
        keep = lines[:begin_idx] + lines[end_idx + 1 :]
    else:
        keep = [line for line in lines if line.strip() not in (MANAGED_BEGIN, MANAGED_END)]

    while keep and not keep[-1].strip():
        keep.pop()
    prefix = "\n".join(keep)
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + managed_block + "\n"


def _run_testparm(text: str) -> None:
    """Validate ``text`` with ``testparm -s`` against a temp copy. Raises
    ``BadRequestError`` on invalid config and ``ConfigurationError`` when the
    validator cannot run. Mocked in tests; never pointed at the live file."""
    with tempfile.NamedTemporaryFile(
        "w", prefix="nas-smb-", suffix=".conf", delete=False
    ) as handle:
        handle.write(text)
        tmp_path = handle.name
    try:
        result = subprocess.run(  # noqa: S603
            ["testparm", "-s", tmp_path],  # noqa: S607 - fixed allowlisted binary, only our temp file
            capture_output=True,
            text=True,
            timeout=_TESTPARM_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ConfigurationError(
            "testparm is not installed; cannot validate the Samba configuration"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ConfigurationError("testparm timed out while validating the configuration") from exc
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-500:]
        raise BadRequestError(
            "Generated Samba configuration is invalid", details={"testparm": detail}
        )


def validate_config_text(shares: list[SambaShare], text: str) -> None:
    """Structural checks plus a ``testparm`` run on the rendered text."""
    stripped = [line.strip() for line in text.splitlines()]
    if MANAGED_BEGIN not in stripped:
        raise BadRequestError("Generated configuration is missing the managed start marker")
    if MANAGED_END not in stripped:
        raise BadRequestError("Generated configuration is missing the managed end marker")
    for share in shares:
        if not share.enabled:
            continue
        if f"[{share.name}]" not in stripped:
            raise BadRequestError(
                "Generated configuration is missing a managed share", details={"name": share.name}
            )
    _run_testparm(text)


def _read_existing(config_path: Path) -> str:
    if not config_path.exists():
        return ""
    try:
        return config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"Cannot read the Samba configuration at {config_path}") from exc


def _backup_config(config_path: Path, backup_dir: Path) -> str | None:
    if not config_path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    backup_path = backup_dir / f"smb.conf.{stamp}.bak"
    shutil.copy2(config_path, backup_path)
    return str(backup_path)


def _atomic_write(config_path: Path, text: str) -> None:
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(config_path.parent), prefix=".smb.conf.", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, config_path)
    except OSError as exc:
        raise ConfigurationError(
            f"Cannot write the Samba configuration at {config_path} "
            "(the web process is unprivileged; a privileged helper applies it in production)"
        ) from exc


def apply_config(
    session: Session,
    settings: Settings,
    *,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> dict[str, object]:
    """Render, validate, back up and write ``smb.conf`` for the current shares.

    Returns ``{"config_path", "backup_path", "reload_required", "message"}``.
    Reloading ``smbd`` is deliberately not attempted (Phase 5 helper).
    """
    shares = list_shares(session)
    existing = _read_existing(settings.samba_config_path)
    managed = render_managed_block(shares)
    merged = merge_managed_section(existing, managed)
    validate_config_text(shares, merged)
    backup_path = _backup_config(settings.samba_config_path, settings.resolved_samba_backup_dir)
    _atomic_write(settings.samba_config_path, merged)
    audit.record_audit_event(
        session,
        event=audit.SAMBA_CONFIG_APPLIED,
        category=audit.CATEGORY_SAMBA,
        actor=actor,
        target_type="smb.conf",
        target_id=str(settings.samba_config_path),
        details={
            "config_path": str(settings.samba_config_path),
            "backup_path": backup_path,
            "managed_share_count": len(shares),
        },
        **_audit_ctx(context),
        commit=False,
    )
    session.commit()
    return {
        "config_path": str(settings.samba_config_path),
        "backup_path": backup_path,
        "reload_required": True,
        "message": (
            "Configuration written and validated. Reloading smbd requires a privileged "
            "helper (Phase 5); until then run `smbcontrol all reload-config` manually."
        ),
    }


def samba_status(session: Session, settings: Settings) -> dict[str, object]:
    """Read-only status of the Samba integration for the current settings."""
    shares = list_shares(session)
    config_path = settings.samba_config_path
    existing = _read_existing(config_path)
    managed_block = render_managed_block(shares)
    merged = merge_managed_section(existing, managed_block)
    # pending_changes = the managed section we would render differs from what
    # is on disk (ignoring anything outside the markers).
    pending_changes = merged != existing
    markers_present = MANAGED_BEGIN in [
        line.strip() for line in existing.splitlines()
    ] and MANAGED_END in [line.strip() for line in existing.splitlines()]
    return {
        "config_path": str(config_path),
        "config_exists": config_path.exists(),
        "managed_markers_present": markers_present,
        "managed_share_count": len(shares),
        "backup_dir": str(settings.resolved_samba_backup_dir),
        "pending_changes": pending_changes,
        "reload_required": True,
        "message": (
            "Reloading smbd requires a privileged helper (Phase 5). "
            "Shares are safe to edit; run Apply to write the validated config."
        ),
    }
