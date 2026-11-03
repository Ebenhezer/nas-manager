"""Configuration import, preview and rollback (Phase 12).

An import runs every section of a document, in dependency order, inside
**one database transaction**: rows are written directly (the per-object
services commit on their own, so only their validators are reused) and
flushed, then the transaction is committed, or rolled back for a preview or
when anything failed. A preview therefore runs exactly the code an import
runs and reports the same plan.

Rules:

* Objects are matched by natural key (name, username). ``merge`` (the
  default) creates and updates and never deletes. ``replace`` also deletes
  objects that are missing from a section the document contains, but only
  for plain configuration (Samba shares, groups, sync and backup jobs, alert
  rules and channels, upload quotas); users, roles, remote connections,
  backup repositories and media libraries are never deleted by an import.
* Every value is validated with the same validators the API uses. Problems
  are collected per object and the whole import is refused if any remain.
* The importing administrator must still be an active administrator
  afterwards, and at least one administrator must remain.
* Before the commit, the current configuration is captured as a
  pre-import snapshot (secrets included, encrypted with the credentials key)
  and, for SQLite, the database file is copied. ``rollback`` deletes what the
  import created and re-applies the snapshot.
* ``smb.conf`` is not rewritten by an import: the Samba page shows the
  pending change and applies it with its usual validation and backup.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.core.permissions import DEFAULT_USER_ROLE, validate_permissions
from app.core.secrets import CredentialCipher
from app.db.base import utcnow_naive
from app.models import (
    AlertChannel,
    AlertRule,
    ApiToken,
    AppSetting,
    AuthSession,
    BackupJob,
    BackupRepository,
    BackupRun,
    Group,
    MediaLibrary,
    PublicUploadQuota,
    RemoteConnection,
    Role,
    RolePermission,
    SambaShare,
    SyncJob,
    User,
)
from app.models.config_import import IMPORT_APPLIED, IMPORT_ROLLED_BACK, ConfigImportRun
from app.models.media import LIBRARY_SHARED, SCAN_QUEUED
from app.models.remote import PROVIDERS, RUN_ACTIVE_STATES
from app.services import audit, remote_sync, remotes, samba
from app.services.backup import jobs as backup_jobs
from app.services.backup import sources
from app.services.config_transfer import export
from app.services.groups_service import validate_group_name
from app.services.media import libraries
from app.services.monitoring import alerts as alert_rules
from app.services.monitoring import channels
from app.services.passwords import hash_password
from app.services.remote import registry
from app.services.remote.base import normalize_remote_path
from app.services.roles_service import ROLE_NAME_RE
from app.services.users_service import (
    count_admin_users,
    normalize_username,
    user_is_admin,
    validate_email,
    validate_username,
)

logger = logging.getLogger(__name__)

MODE_MERGE = "merge"
MODE_REPLACE = "replace"
MODES = (MODE_MERGE, MODE_REPLACE)
_MAX_ITEMS = 10_000
_KEEP_DATABASE_COPIES = 10
_QUOTA_KEY_RE = re.compile(r"^(user|ip|token):[^\x00-\x1f\x7f]{1,120}$")
_ADMIN_ROLE = "admin"


class ImportValidationError(BadRequestError):
    code = "import_invalid"


@dataclass
class Plan:
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    samba_changed: bool = False

    def touch(self, name: str) -> None:
        """List a section in the plan even when nothing in it changes."""
        self._section(name)

    def _section(self, name: str) -> dict[str, Any]:
        return self.sections.setdefault(
            name, {"create": [], "update": [], "delete": [], "unchanged": 0}
        )

    def record(self, section: str, action: str, key: str) -> None:
        entry = self._section(section)
        if action == "unchanged":
            entry["unchanged"] += 1
        else:
            entry[action].append(key)
        if section == "samba_shares" and action != "unchanged":
            self.samba_changed = True

    def error(self, section: str, key: str, message: str) -> None:
        self.errors.append({"section": section, "key": key, "error": message})

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sections": self.sections,
            "warnings": self.warnings,
            "errors": self.errors,
            "samba_apply_required": self.samba_changed,
        }


@dataclass
class Context:
    session: Session
    settings: Settings
    cipher: CredentialCipher
    actor: User
    mode: str
    secrets: dict[str, dict[str, Any]] | None
    plan: Plan
    skipped_repositories: set[str] = field(default_factory=set)

    def secret_for(self, section: str, key: str) -> dict[str, Any] | None:
        if self.secrets is None:
            return None
        value = self.secrets.get(section, {}).get(key)
        return dict(value) if isinstance(value, dict) else None


# ---------------------------------------------------------------------------
# Value helpers (documents are untrusted input)
# ---------------------------------------------------------------------------


def _text(item: dict[str, Any], key: str, *, default: str | None = None, limit: int = 4096) -> str:
    value = item.get(key, default)
    if value is None:
        if default is None:
            raise BadRequestError(f"'{key}' is required")
        return default
    if not isinstance(value, str) or len(value) > limit:
        raise BadRequestError(f"'{key}' must be a string of at most {limit} characters")
    return value


def _opt_text(item: dict[str, Any], key: str, *, limit: int = 4096) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    return _text(item, key, limit=limit)


def _flag(item: dict[str, Any], key: str, default: bool) -> bool:
    value = item.get(key, default)
    if not isinstance(value, bool):
        raise BadRequestError(f"'{key}' must be true or false")
    return value


def _number(item: dict[str, Any], key: str, default: int | None) -> int | None:
    value = item.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequestError(f"'{key}' must be an integer")
    return value


def _names(item: dict[str, Any], key: str) -> list[str]:
    value = item.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BadRequestError(f"'{key}' must be a list of strings")
    return list(value)


def _mapping(item: dict[str, Any], key: str) -> dict[str, Any]:
    value = item.get(key, {})
    if not isinstance(value, dict):
        raise BadRequestError(f"'{key}' must be an object")
    return dict(value)


def _items(sections: dict[str, Any], name: str) -> list[dict[str, Any]] | None:
    if name not in sections:
        return None
    value = sections[name]
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        raise ImportValidationError(
            f"Section '{name}' must be a list of at most {_MAX_ITEMS} objects"
        )
    if not all(isinstance(item, dict) for item in value):
        raise ImportValidationError(f"Section '{name}' must contain objects")
    return value


def _assign(row: Any, values: dict[str, Any]) -> bool:
    changed = False
    for key, value in values.items():
        if getattr(row, key) != value:
            setattr(row, key, value)
            changed = True
    return changed


def _each(
    ctx: Context,
    section: str,
    items: list[dict[str, Any]],
    key_field: str,
    handler: Callable[[Context, dict[str, Any], str], None],
) -> set[str]:
    """Run ``handler`` per item, collecting problems; returns the keys seen."""
    seen: set[str] = set()
    for item in items:
        raw_key = item.get(key_field)
        key = raw_key if isinstance(raw_key, str) else repr(raw_key)
        if key in seen:
            ctx.plan.error(section, key, "listed twice")
            continue
        seen.add(key)
        try:
            handler(ctx, item, key)
        except AppError as exc:
            ctx.plan.error(section, key, exc.message)
        except (ValueError, TypeError, KeyError) as exc:
            ctx.plan.error(section, key, str(exc) or exc.__class__.__name__)
    return seen


def _by_name(session: Session, model: Any, name: str) -> Any:
    column = model.username if model is User else model.name
    return (
        session.execute(select(model).where(func.lower(column) == name.lower())).scalars().first()
    )


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _import_settings(ctx: Context, values: Any) -> None:
    if not isinstance(values, dict):
        ctx.plan.error("settings", "settings", "must be an object")
        return
    for key, value in values.items():
        if key not in export.SETTINGS_KEYS:
            ctx.plan.warn(f"Unknown setting '{key}' was ignored.")
            continue
        if not isinstance(value, bool):
            ctx.plan.error("settings", key, "must be true or false")
            continue
        stored = "1" if value else "0"
        row = ctx.session.get(AppSetting, key)
        if row is None:
            ctx.session.add(AppSetting(key=key, value=stored))
            ctx.plan.record("settings", "create", key)
        elif row.value != stored:
            row.value = stored
            ctx.plan.record("settings", "update", key)
        else:
            ctx.plan.record("settings", "unchanged", key)


def _import_role(ctx: Context, item: dict[str, Any], key: str) -> None:
    name = key.strip().lower()
    if not ROLE_NAME_RE.fullmatch(name):
        raise BadRequestError("Invalid role name")
    permissions = sorted(set(_names(item, "permissions")))
    validate_permissions(permissions)
    description = _text(item, "description", default="", limit=1000)
    role = _by_name(ctx.session, Role, name)
    if role is None:
        role = Role(name=name, description=description, system=False)
        role.permissions = [RolePermission(permission=p) for p in permissions]
        ctx.session.add(role)
        ctx.session.flush()
        ctx.plan.record("roles", "create", name)
        return
    current = sorted(p.permission for p in role.permissions)
    changed = False
    if role.name == _ADMIN_ROLE:
        if current != permissions:
            ctx.plan.warn("The admin role always keeps every permission; its list was ignored.")
    elif current != permissions:
        role.permissions = [RolePermission(permission=p) for p in permissions]
        changed = True
    if not role.system and role.description != description:
        role.description = description
        changed = True
    ctx.session.flush()
    ctx.plan.record("roles", "update" if changed else "unchanged", name)


def _roles_named(session: Session, names: list[str]) -> list[Role]:
    found = []
    for name in sorted({n.strip().lower() for n in names}):
        role = _by_name(session, Role, name)
        if role is None:
            raise NotFoundError(f"Unknown role '{name}'")
        found.append(role)
    return found


def _import_group(ctx: Context, item: dict[str, Any], key: str) -> None:
    name = validate_group_name(key)
    description = _text(item, "description", default="", limit=1000)
    roles = _roles_named(ctx.session, _names(item, "roles"))
    group = _by_name(ctx.session, Group, name)
    if group is None:
        group = Group(name=name, description=description)
        group.roles = roles
        ctx.session.add(group)
        ctx.session.flush()
        ctx.plan.record("groups", "create", name)
        return
    changed = _assign(group, {"description": description})
    if sorted(r.name for r in group.roles) != sorted(r.name for r in roles):
        group.roles = roles
        changed = True
    ctx.session.flush()
    ctx.plan.record("groups", "update" if changed else "unchanged", name)


def _import_user(ctx: Context, item: dict[str, Any], key: str) -> None:
    username = normalize_username(key)
    validate_username(username)
    email = _opt_text(item, "email", limit=255)
    validate_email(email)
    values: dict[str, Any] = {
        "display_name": _text(item, "display_name", default="", limit=128),
        "email": email.strip() if email and email.strip() else None,
        "is_active": _flag(item, "is_active", True),
    }
    role_names = _names(item, "roles") or [DEFAULT_USER_ROLE]
    roles = _roles_named(ctx.session, role_names)
    groups = []
    for group_name in sorted(set(_names(item, "groups"))):
        group = _by_name(ctx.session, Group, group_name)
        if group is None:
            raise NotFoundError(f"Unknown group '{group_name}'")
        groups.append(group)
    secret = ctx.secret_for("users", username)
    password_hash = (secret or {}).get("password_hash")
    if password_hash is not None and (
        not isinstance(password_hash, str) or not password_hash.startswith("$argon2")
    ):
        raise BadRequestError("The stored password hash is not an Argon2 hash")

    user = _by_name(ctx.session, User, username)
    if user is None:
        if password_hash is None:
            # No hash in the document: the account cannot be used until an
            # administrator resets its password.
            password_hash = hash_password(secrets.token_urlsafe(32), ctx.settings)
            values["is_active"] = False
            ctx.plan.warn(
                f"User '{username}' is imported without a password and stays inactive "
                "until an administrator resets the password and activates it."
            )
        user = User(username=username, password_hash=password_hash, **values)
        user.roles = roles
        user.groups = groups
        ctx.session.add(user)
        ctx.session.flush()
        ctx.plan.record("users", "create", username)
        return
    was_active = user.is_active
    changed = _assign(user, values)
    if password_hash is not None and user.password_hash != password_hash:
        user.password_hash = password_hash
        changed = True
    if sorted(r.name for r in user.roles) != sorted(r.name for r in roles):
        user.roles = roles
        changed = True
    if sorted(g.name for g in user.groups) != sorted(g.name for g in groups):
        user.groups = groups
        changed = True
    if was_active and not user.is_active:
        now = utcnow_naive()
        ctx.session.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        ctx.session.execute(
            update(ApiToken)
            .where(ApiToken.user_id == user.id, ApiToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
    ctx.session.flush()
    ctx.plan.record("users", "update" if changed else "unchanged", username)


def _import_share(ctx: Context, item: dict[str, Any], key: str) -> None:
    name = samba.validate_share_name(key)
    path = _text(item, "path", limit=1024)
    enabled = _flag(item, "enabled", True)
    if not os.path.isabs(path):
        raise BadRequestError("Share path must be absolute")
    if os.path.isdir(path):
        path = samba.validate_share_path(path)
    elif enabled:
        enabled = False
        ctx.plan.warn(
            f"Samba share '{name}' points at {path}, which does not exist here; "
            "it is imported disabled."
        )
    values: dict[str, Any] = {
        "path": path,
        "comment": samba.validate_free_text(_text(item, "comment", default=""), field="comment"),
        "read_only": _flag(item, "read_only", False),
        "browseable": _flag(item, "browseable", True),
        "guest_ok": _flag(item, "guest_ok", False),
        "valid_users": samba.validate_free_text(
            _text(item, "valid_users", default=""), field="valid_users"
        ),
        "write_list": samba.validate_free_text(
            _text(item, "write_list", default=""), field="write_list"
        ),
        "enabled": enabled,
    }
    if values["guest_ok"]:
        ctx.plan.warn(f"Samba share '{name}' allows guest access.")
    share = _by_name(ctx.session, SambaShare, name)
    if share is None:
        ctx.session.add(SambaShare(name=name, **values))
        ctx.session.flush()
        ctx.plan.record("samba_shares", "create", name)
        return
    changed = _assign(share, values)
    ctx.plan.record("samba_shares", "update" if changed else "unchanged", name)


def _import_connection(ctx: Context, item: dict[str, Any], key: str) -> None:
    name = remotes._clean_name(key)
    provider = _text(item, "provider", limit=16)
    if provider not in PROVIDERS:
        raise BadRequestError("Unknown provider", details={"allowed": list(PROVIDERS)})
    row = _by_name(ctx.session, RemoteConnection, name)
    if row is not None and row.provider != provider:
        raise BadRequestError("The provider of an existing connection cannot change")
    stored = ctx.cipher.decrypt_json(row.secrets_encrypted) if row is not None else {}
    imported = ctx.secret_for("remote_connections", name)
    # Imported secrets (including server-managed ones such as OAuth refresh
    # tokens) are applied as the stored state; nothing comes from the client.
    config, secret_values = registry.validate_config(
        provider, _mapping(item, "config"), {}, stored | (imported or {})
    )
    declared = set(_names(item, "secret_fields"))
    if declared - set(secret_values):
        ctx.plan.warn(
            f"Remote connection '{name}' is missing credentials "
            f"({', '.join(sorted(declared - set(secret_values)))}); re-enter them."
        )
    encrypted = ctx.cipher.encrypt_json(secret_values) if secret_values else None
    if row is None:
        row = RemoteConnection(name=name, provider=provider, created_by=ctx.actor.id)
        row.config = config
        row.secrets_encrypted = encrypted
        ctx.session.add(row)
        ctx.session.flush()
        ctx.plan.record("remote_connections", "create", name)
        return
    changed = row.config != config or stored != secret_values
    if changed:
        row.config = config
        row.secrets_encrypted = encrypted
    ctx.plan.record("remote_connections", "update" if changed else "unchanged", name)


def _connection_id(session: Session, name: Any, *, what: str) -> int:
    if not isinstance(name, str) or not name:
        raise BadRequestError(f"{what} is required")
    row = _by_name(session, RemoteConnection, name)
    if row is None:
        raise NotFoundError(f"Unknown remote connection '{name}'")
    return int(row.id)


def _import_sync_job(ctx: Context, item: dict[str, Any], key: str) -> None:
    name = remote_sync._clean_name(key)
    connection_id = _connection_id(ctx.session, item.get("connection"), what="connection")
    root, local_path = remote_sync._clean_local(
        ctx.settings, _text(item, "local_root", limit=1024), _text(item, "local_path", default="")
    )
    values: dict[str, Any] = {
        "connection_id": connection_id,
        "local_root": root,
        "local_path": local_path,
        "remote_path": normalize_remote_path(_text(item, "remote_path", default="")),
        "direction": _text(item, "direction", limit=16),
        "conflict_policy": _text(item, "conflict_policy", default="keep_both", limit=16),
        "delete_policy": _text(item, "delete_policy", default="ignore", limit=16),
        "schedule_minutes": remote_sync._schedule(_number(item, "schedule_minutes", None)),
        "enabled": _flag(item, "enabled", False),
    }
    remote_sync._validate_enums(
        values["direction"], values["conflict_policy"], values["delete_policy"]
    )
    excludes = remote_sync._clean_excludes(_names(item, "exclude_patterns"))
    job = _by_name(ctx.session, SyncJob, name)
    if values["enabled"] and (job is None or not job.enabled):
        ctx.plan.warn(f"Sync job '{name}' is enabled and will run on its schedule.")
    if job is None:
        job = SyncJob(name=name, created_by=ctx.actor.id, **values)
        job.exclude_patterns = excludes
        remote_sync._reschedule(job, utcnow_naive())
        ctx.session.add(job)
        ctx.session.flush()
        ctx.plan.record("sync_jobs", "create", name)
        return
    target = {"connection_id", "local_root", "local_path", "remote_path", "direction"}
    target_changed = any(getattr(job, k) != values[k] for k in target)
    if target_changed and remote_sync.active_run(ctx.session, job.id) is not None:
        raise ConflictError("The sync job has an active run")
    changed = _assign(job, values)
    if job.exclude_patterns != excludes:
        job.exclude_patterns = excludes
        changed = True
    if target_changed:
        remote_sync.reset_baseline(ctx.session, job.id)
    if changed:
        remote_sync._reschedule(job, utcnow_naive())
    ctx.plan.record("sync_jobs", "update" if changed else "unchanged", name)


def _import_library(ctx: Context, item: dict[str, Any], key: str) -> None:
    name = libraries._clean_name(key)
    root, rel = libraries._clean_folder(
        ctx.settings, _text(item, "root", limit=1024), _text(item, "path", default="")
    )
    group_ids = []
    for group_name in sorted(set(_names(item, "groups"))):
        group = _by_name(ctx.session, Group, group_name)
        if group is None:
            raise NotFoundError(f"Unknown group '{group_name}'")
        group_ids.append(int(group.id))
    values = {
        "root": root,
        "rel_path": rel,
        "enabled": _flag(item, "enabled", True),
        "include_videos": _flag(item, "include_videos", True),
    }
    library = _by_name(ctx.session, MediaLibrary, name)
    if library is not None and library.kind != LIBRARY_SHARED:
        raise ConflictError("A personal library already uses this name")
    if library is None:
        library = MediaLibrary(
            name=name,
            kind=LIBRARY_SHARED,
            scan_state=SCAN_QUEUED,
            created_by=ctx.actor.id,
            **values,
        )
        ctx.session.add(library)
        ctx.session.flush()
        libraries._write_groups(ctx.session, library.id, group_ids)
        ctx.plan.record("media_libraries", "create", name)
        return
    changed = _assign(library, values)
    if sorted(libraries.library_group_ids(ctx.session, library.id)) != group_ids:
        libraries._write_groups(ctx.session, library.id, group_ids)
        changed = True
    if changed:
        library.scan_state = SCAN_QUEUED
    ctx.plan.record("media_libraries", "update" if changed else "unchanged", name)


def _import_rule(ctx: Context, item: dict[str, Any], key: str) -> None:
    name = alert_rules._clean_name(key)
    threshold = item.get("threshold")
    if threshold is not None and (
        isinstance(threshold, bool) or not isinstance(threshold, int | float)
    ):
        raise BadRequestError("'threshold' must be a number")
    values: dict[str, Any] = {
        "target": alert_rules._clean_target(_opt_text(item, "target", limit=255)),
        "comparison": _text(item, "comparison", default="gt", limit=4),
        "threshold": float(threshold) if threshold is not None else None,
        "duration_seconds": _number(item, "duration_seconds", 0),
        "severity": _text(item, "severity", default="warning", limit=16),
        "enabled": _flag(item, "enabled", True),
        "notify": _flag(item, "notify", True),
    }
    if not 0 <= int(values["duration_seconds"]) <= 7 * 86400:
        raise BadRequestError("duration_seconds must be between 0 and 604800")
    metric = _text(item, "metric", limit=32)
    rule = _by_name(ctx.session, AlertRule, name)
    if rule is None:
        if _flag(item, "builtin", False):
            ctx.plan.warn(f"Built-in rule '{name}' does not exist here and was skipped.")
            return
        rule = AlertRule(name=name, metric=metric, builtin=False, **values)
        alert_rules._validate_rule(rule)
        ctx.session.add(rule)
        ctx.session.flush()
        ctx.plan.record("alert_rules", "create", name)
        return
    if rule.metric != metric:
        raise BadRequestError("The metric of an existing rule cannot change")
    changed = _assign(rule, values)
    alert_rules._validate_rule(rule)
    ctx.plan.record("alert_rules", "update" if changed else "unchanged", name)


def _import_channel(ctx: Context, item: dict[str, Any], key: str) -> None:
    name = channels._clean_name(key)
    kind = _text(item, "kind", limit=16)
    allow_insecure = _flag(item, "allow_insecure", False)
    row = _by_name(ctx.session, AlertChannel, name)
    if row is not None and row.kind != kind:
        raise BadRequestError("The kind of an existing channel cannot change")
    stored = ctx.cipher.decrypt_json(row.secrets_encrypted) if row is not None else {}
    imported = ctx.secret_for("alert_channels", name) or {}
    config, secret_values = channels.validate(
        kind,
        _mapping(item, "config"),
        {k: (str(v) if v is not None else None) for k, v in imported.items()},
        stored,
        allow_insecure=allow_insecure,
    )
    declared = set(_names(item, "secret_fields"))
    if declared - set(secret_values):
        ctx.plan.warn(f"Notification channel '{name}' is missing secrets; re-enter them.")
    values: dict[str, Any] = {
        "min_severity": channels._check_severity(
            _text(item, "min_severity", default="warning", limit=16)
        ),
        "allow_insecure": allow_insecure,
        "send_resolved": _flag(item, "send_resolved", True),
        "enabled": _flag(item, "enabled", True),
    }
    encrypted = ctx.cipher.encrypt_json(secret_values) if secret_values else None
    if row is None:
        row = AlertChannel(name=name, kind=kind, created_by=ctx.actor.id, **values)
        row.config = config
        row.secrets_encrypted = encrypted
        ctx.session.add(row)
        ctx.session.flush()
        ctx.plan.record("alert_channels", "create", name)
        return
    changed = _assign(row, values)
    if row.config != config or stored != secret_values:
        row.config = config
        row.secrets_encrypted = encrypted
        changed = True
    ctx.plan.record("alert_channels", "update" if changed else "unchanged", name)


def _import_quota(ctx: Context, item: dict[str, Any], key: str) -> None:
    if not _QUOTA_KEY_RE.match(key):
        raise BadRequestError("uploader_key must look like user:<name>, ip:<address> or token:<id>")
    quota = _number(item, "quota_bytes", None)
    if quota is None or quota < 0:
        raise BadRequestError("quota_bytes must be a non-negative integer")
    row = ctx.session.get(PublicUploadQuota, key)
    if row is None:
        ctx.session.add(PublicUploadQuota(uploader_key=key, quota_bytes=quota, used_bytes=0))
        ctx.session.flush()
        ctx.plan.record("public_upload_quotas", "create", key)
        return
    changed = _assign(row, {"quota_bytes": quota})
    ctx.plan.record("public_upload_quotas", "update" if changed else "unchanged", key)


def _import_repository(ctx: Context, item: dict[str, Any], key: str) -> None:
    name = backup_jobs._clean_name(key, "Repository")
    kind = _text(item, "target_kind", limit=16)
    target: dict[str, Any] = {
        "target_kind": kind,
        "local_root": _opt_text(item, "local_root", limit=1024),
        "repo_path": _text(item, "repo_path", default=""),
    }
    if kind == "remote":
        target["connection_id"] = _connection_id(
            ctx.session, item.get("connection"), what="connection"
        )
    secret = ctx.secret_for("backup_repositories", name) or {}
    passphrase = secret.get("passphrase")
    row = _by_name(ctx.session, BackupRepository, name)
    if row is None:
        if not isinstance(passphrase, str) or not passphrase:
            ctx.skipped_repositories.add(name)
            ctx.plan.warn(
                f"Backup repository '{name}' was skipped: its passphrase is only in an "
                "export made with a passphrase."
            )
            return
        backup_jobs._check_passphrase(passphrase)
        row = BackupRepository(
            name=name,
            target_kind="",
            repo_path="",
            passphrase_encrypted=ctx.cipher.encrypt_json({"passphrase": passphrase}),
            created_by=ctx.actor.id,
        )
        backup_jobs._apply_target(ctx.session, ctx.settings, row, target)
        ctx.session.add(row)
        ctx.session.flush()
        ctx.plan.record("backup_repositories", "create", name)
        ctx.plan.warn(
            f"Backup repository '{name}' must be initialized on the Backups page before use "
            "(an existing repository is adopted)."
        )
        return
    before = (row.target_kind, row.local_root, row.connection_id, row.repo_path)
    backup_jobs._apply_target(ctx.session, ctx.settings, row, target)
    changed = before != (row.target_kind, row.local_root, row.connection_id, row.repo_path)
    if (
        isinstance(passphrase, str)
        and passphrase
        and backup_jobs.passphrase_of(row, ctx.cipher) != passphrase
    ):
        backup_jobs._check_passphrase(passphrase)
        row.passphrase_encrypted = ctx.cipher.encrypt_json({"passphrase": passphrase})
        changed = True
    if changed:
        if backup_jobs._has_active_run(ctx.session, row.id):
            raise ConflictError("The repository has an active run")
        row.initialized_at = None
        ctx.plan.warn(f"Backup repository '{name}' changed and must be initialized again.")
    ctx.plan.record("backup_repositories", "update" if changed else "unchanged", name)


def _import_backup_job(ctx: Context, item: dict[str, Any], key: str) -> None:
    name = backup_jobs._clean_name(key, "Job")
    repository_name = _text(item, "repository", limit=64)
    if repository_name in ctx.skipped_repositories:
        ctx.plan.warn(f"Backup job '{name}' was skipped with its repository.")
        return
    repository = _by_name(ctx.session, BackupRepository, repository_name)
    if repository is None:
        raise NotFoundError(f"Unknown backup repository '{repository_name}'")
    source_connection = item.get("source_connection")
    values: dict[str, Any] = {
        "repository_id": repository.id,
        "source_kind": _text(item, "source_kind", limit=16),
        "source_root": _opt_text(item, "source_root", limit=1024),
        "source_path": _text(item, "source_path", default=""),
        "source_connection_id": (
            _connection_id(ctx.session, source_connection, what="source_connection")
            if source_connection
            else None
        ),
        "schedule_minutes": backup_jobs._schedule(_number(item, "schedule_minutes", None)),
        "enabled": _flag(item, "enabled", False),
    }
    for field_name, default in (
        ("keep_last", 7),
        ("keep_daily", 7),
        ("keep_weekly", 4),
        ("keep_monthly", 6),
    ):
        values[field_name] = backup_jobs._keep(_number(item, field_name, default), field_name)
    excludes = backup_jobs._clean_excludes(_names(item, "exclude_patterns"))
    job = _by_name(ctx.session, BackupJob, name)
    if values["enabled"] and (job is None or not job.enabled):
        ctx.plan.warn(f"Backup job '{name}' is enabled and will run on its schedule.")
    if job is None:
        job = BackupJob(name=name, created_by=ctx.actor.id, **values)
        job.exclude_patterns = excludes
        backup_jobs._apply_source(ctx.session, ctx.settings, job)
        backup_jobs._reschedule(job, utcnow_naive())
        ctx.session.add(job)
        ctx.session.flush()
        ctx.plan.record("backup_jobs", "create", name)
        return
    changed = _assign(job, values)
    if job.exclude_patterns != excludes:
        job.exclude_patterns = excludes
        changed = True
    backup_jobs._apply_source(ctx.session, ctx.settings, job)
    if changed:
        backup_jobs._reschedule(job, utcnow_naive())
    ctx.plan.record("backup_jobs", "update" if changed else "unchanged", name)


# ---------------------------------------------------------------------------
# Deletion (replace mode and rollback)
# ---------------------------------------------------------------------------


def _delete_row(ctx: Context, section: str, row: Any, key: str) -> None:
    session = ctx.session
    if isinstance(row, SyncJob) and remote_sync.active_run(session, row.id) is not None:
        raise ConflictError("The sync job has an active run")
    if (
        isinstance(row, BackupJob)
        and session.execute(
            select(BackupRun.id).where(
                BackupRun.job_id == row.id, BackupRun.status.in_(RUN_ACTIVE_STATES)
            )
        ).first()
    ):
        raise ConflictError("The backup job has an active run")
    if isinstance(row, RemoteConnection):
        users = [
            *session.execute(select(SyncJob.name).where(SyncJob.connection_id == row.id)).scalars(),
            *session.execute(
                select(BackupRepository.name).where(BackupRepository.connection_id == row.id)
            ).scalars(),
            *session.execute(
                select(BackupJob.name).where(BackupJob.source_connection_id == row.id)
            ).scalars(),
        ]
        if users:
            raise ConflictError(f"Still used by: {', '.join(sorted(users))}")
    if isinstance(row, BackupRepository):
        if session.execute(select(BackupJob.id).where(BackupJob.repository_id == row.id)).first():
            raise ConflictError("Backup jobs still use this repository")
        if backup_jobs._has_active_run(session, row.id):
            raise ConflictError("The repository has an active run")
    if isinstance(row, User) and row.id == ctx.actor.id:
        raise ConflictError("The importing administrator cannot be removed")
    session.delete(row)
    session.flush()
    ctx.plan.record(section, "delete", key)


_REPLACEABLE: dict[str, tuple[Any, str]] = {
    "groups": (Group, "name"),
    "samba_shares": (SambaShare, "name"),
    "sync_jobs": (SyncJob, "name"),
    "alert_rules": (AlertRule, "name"),
    "alert_channels": (AlertChannel, "name"),
    "public_upload_quotas": (PublicUploadQuota, "uploader_key"),
    "backup_jobs": (BackupJob, "name"),
}

_DELETABLE_BY_NAME: dict[str, Any] = {
    "roles": Role,
    "groups": Group,
    "users": User,
    "samba_shares": SambaShare,
    "remote_connections": RemoteConnection,
    "sync_jobs": SyncJob,
    "media_libraries": MediaLibrary,
    "alert_rules": AlertRule,
    "alert_channels": AlertChannel,
    "public_upload_quotas": PublicUploadQuota,
    "backup_repositories": BackupRepository,
    "backup_jobs": BackupJob,
}


def _delete_missing(ctx: Context, section: str, seen: set[str]) -> None:
    model, key_field = _REPLACEABLE[section]
    stmt = select(model)
    if model is AlertRule:
        stmt = stmt.where(AlertRule.builtin.is_(False))
    for row in list(ctx.session.execute(stmt).scalars()):
        key = str(getattr(row, key_field))
        if key.lower() in {s.lower() for s in seen}:
            continue
        try:
            _delete_row(ctx, section, row, key)
        except AppError as exc:
            ctx.plan.error(section, key, f"cannot delete: {exc.message}")


# ---------------------------------------------------------------------------
# Running an import
# ---------------------------------------------------------------------------

_HANDLERS: dict[str, tuple[str, Callable[[Context, dict[str, Any], str], None]]] = {
    "roles": ("name", _import_role),
    "groups": ("name", _import_group),
    "users": ("username", _import_user),
    "samba_shares": ("name", _import_share),
    "remote_connections": ("name", _import_connection),
    "sync_jobs": ("name", _import_sync_job),
    "media_libraries": ("name", _import_library),
    "alert_rules": ("name", _import_rule),
    "alert_channels": ("name", _import_channel),
    "public_upload_quotas": ("uploader_key", _import_quota),
    "backup_repositories": ("name", _import_repository),
    "backup_jobs": ("name", _import_backup_job),
}


def parse_document(
    document: Any, passphrase: str | None
) -> tuple[dict[str, Any], dict[str, dict[str, Any]] | None]:
    """Check the envelope; return ``(sections, secrets or None)``."""
    if not isinstance(document, dict) or document.get("format") != export.FORMAT:
        raise ImportValidationError("This is not a NAS Manager configuration export")
    if document.get("version") != export.VERSION:
        raise ImportValidationError(
            "Unsupported export version", details={"version": document.get("version")}
        )
    sections = document.get("sections")
    if not isinstance(sections, dict):
        raise ImportValidationError("The export has no sections")
    unknown = sorted(set(sections) - set(export.SECTIONS))
    if unknown:
        raise ImportValidationError("Unknown sections", details={"sections": unknown})
    sealed = document.get("secrets")
    if sealed is None:
        return sections, None
    if not passphrase:
        raise BadRequestError(
            "This export contains encrypted secrets: give its passphrase",
            details={"code": "passphrase_required"},
        )
    return sections, export.unseal_secrets(sealed, sections, passphrase)


def run_sections(ctx: Context, sections: dict[str, Any]) -> None:
    if "settings" in sections:
        _import_settings(ctx, sections["settings"])
    for name in export.SECTIONS[1:]:
        items = _items(sections, name)
        if items is None:
            continue
        key_field, handler = _HANDLERS[name]
        ctx.plan.touch(name)
        seen = _each(ctx, name, items, key_field, handler)
        if ctx.mode == MODE_REPLACE and name in _REPLACEABLE:
            _delete_missing(ctx, name, seen)
    ctx.session.flush()
    if count_admin_users(ctx.session) == 0:
        ctx.plan.error("users", "*", "the import would leave no administrator")
    ctx.session.refresh(ctx.actor)
    if not ctx.actor.is_active or not user_is_admin(ctx.session, ctx.actor):
        ctx.plan.error(
            "users", ctx.actor.username, "the import would remove your own administrator access"
        )


def _risk_warnings(plan: Plan, mode: str) -> list[str]:
    found = list(plan.warnings)
    deletions = sum(len(section["delete"]) for section in plan.sections.values())
    if mode == MODE_REPLACE and deletions:
        found.insert(0, f"{deletions} object(s) missing from the file will be deleted.")
    return found


def preview(
    session: Session,
    settings: Settings,
    cipher: CredentialCipher,
    *,
    document: Any,
    passphrase: str | None,
    mode: str,
    actor: User,
) -> dict[str, Any]:
    """Run the import without committing; return the plan."""
    if mode not in MODES:
        raise BadRequestError("Unknown mode", details={"allowed": list(MODES)})
    sections, secret_values = parse_document(document, passphrase)
    ctx = Context(session, settings, cipher, actor, mode, secret_values, Plan())
    try:
        run_sections(ctx, sections)
    finally:
        session.rollback()
    result = ctx.plan.as_dict()
    result["warnings"] = _risk_warnings(ctx.plan, mode)
    result["secrets_included"] = secret_values is not None
    return result


def apply(
    session: Session,
    settings: Settings,
    cipher: CredentialCipher,
    *,
    document: Any,
    passphrase: str | None,
    mode: str,
    confirm: bool,
    source_name: str,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> tuple[ConfigImportRun, dict[str, Any]]:
    if mode not in MODES:
        raise BadRequestError("Unknown mode", details={"allowed": list(MODES)})
    sections, secret_values = parse_document(document, passphrase)
    snapshot = export.snapshot_document(session, settings, cipher)
    ctx = Context(session, settings, cipher, actor, mode, secret_values, Plan())
    try:
        run_sections(ctx, sections)
    except Exception:
        session.rollback()
        raise
    plan = ctx.plan
    if plan.errors:
        session.rollback()
        raise ImportValidationError(
            "The import was refused; nothing was changed",
            details={"errors": plan.errors[:200], "plan": plan.as_dict()},
        )
    warnings = _risk_warnings(plan, mode)
    if warnings and not confirm:
        session.rollback()
        raise remote_sync.ConfirmationRequiredError(
            "This import needs confirmation; repeat it with confirm=true",
            details={"warnings": warnings, "plan": plan.as_dict()},
        )
    run = ConfigImportRun(
        status=IMPORT_APPLIED,
        mode=mode,
        source_name=source_name[:255],
        exported_at=str(document.get("exported_at") or "")[:64] or None,
        snapshot_encrypted=cipher.encrypt_json(snapshot),
        actor_id=actor.id,
        actor_username=actor.username,
    )
    run.sections = plan.sections
    run.warnings = warnings
    session.add(run)
    session.flush()
    audit.record_audit_event(
        session,
        event=audit.CONFIG_IMPORTED,
        category=audit.CATEGORY_CONFIG,
        actor=actor,
        target_type="config_import",
        target_id=run.id,
        details={
            "mode": mode,
            "source": run.source_name,
            "secrets_included": secret_values is not None,
            "summary": _summary(plan),
            "confirmed": confirm,
        },
        **audit.request_context(context),
        commit=False,
    )
    # A file copy of the pre-import database, for manual recovery.
    run.database_copy = _copy_database(settings, run.id)
    session.commit()
    _prune_database_copies(settings)
    result = plan.as_dict() | {"warnings": warnings, "secrets_included": secret_values is not None}
    return run, result


def _summary(plan: Plan) -> dict[str, dict[str, int]]:
    return {
        name: {
            "create": len(entry["create"]),
            "update": len(entry["update"]),
            "delete": len(entry["delete"]),
            "unchanged": entry["unchanged"],
        }
        for name, entry in plan.sections.items()
    }


def _copies_dir(settings: Settings) -> Path:
    return settings.data_dir / "config-snapshots"


def _copy_database(settings: Settings, run_id: int) -> str | None:
    try:
        source = sources.sqlite_path(settings)
    except AppError:
        return None
    target_dir = _copies_dir(settings)
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = target_dir / f"before-import-{run_id}.db"
    try:
        sources.copy_database(source, target)
    except (AppError, OSError) as exc:
        logger.warning("pre-import database copy failed", extra={"error": str(exc)})
        return None
    os.chmod(target, 0o600)
    return str(target)


def _prune_database_copies(settings: Settings) -> None:
    copies = sorted(
        _copies_dir(settings).glob("before-import-*.db"), key=lambda p: p.stat().st_mtime
    )
    for old in copies[:-_KEEP_DATABASE_COPIES]:
        old.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# History and rollback
# ---------------------------------------------------------------------------


def list_runs(session: Session, *, limit: int = 50) -> list[ConfigImportRun]:
    return list(
        session.execute(
            select(ConfigImportRun).order_by(ConfigImportRun.id.desc()).limit(limit)
        ).scalars()
    )


def get_run_or_404(session: Session, run_id: int) -> ConfigImportRun:
    row = session.get(ConfigImportRun, run_id)
    if row is None:
        raise NotFoundError("Import not found", details={"id": run_id})
    return row


def rollback(
    session: Session,
    settings: Settings,
    cipher: CredentialCipher,
    run: ConfigImportRun,
    *,
    confirm: bool,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> tuple[ConfigImportRun, dict[str, Any]]:
    """Undo an import: delete what it created, then re-apply the snapshot."""
    if run.status != IMPORT_APPLIED:
        raise ConflictError("This import was already rolled back")
    latest = session.execute(
        select(ConfigImportRun.id)
        .where(ConfigImportRun.status == IMPORT_APPLIED)
        .order_by(ConfigImportRun.id.desc())
        .limit(1)
    ).scalar_one()
    if latest != run.id:
        raise ConflictError(
            "Only the most recent import can be rolled back; roll back newer ones first"
        )
    if not confirm:
        raise remote_sync.ConfirmationRequiredError(
            "Rolling back needs confirmation; repeat it with confirm=true",
            details={
                "warnings": [
                    "Objects created by the import are deleted and everything it changed or "
                    "deleted is restored to the state before the import. Changes made since "
                    "the import to those objects are lost."
                ]
            },
        )
    snapshot = cipher.decrypt_json(run.snapshot_encrypted)
    sections = snapshot.get("sections")
    if not isinstance(sections, dict):
        raise ConflictError("The pre-import snapshot is unreadable")
    ctx = Context(
        session, settings, cipher, actor, MODE_MERGE, snapshot.get("plain_secrets") or {}, Plan()
    )
    created = run.sections
    try:
        for name in reversed(export.SECTIONS[1:]):
            model = _DELETABLE_BY_NAME[name]
            for key in (created.get(name) or {}).get("create", []):
                row = (
                    session.get(PublicUploadQuota, key)
                    if model is PublicUploadQuota
                    else _by_name(session, model, key)
                )
                if row is None:
                    continue
                try:
                    _delete_row(ctx, name, row, key)
                except AppError as exc:
                    ctx.plan.error(name, key, f"cannot delete: {exc.message}")
        stored_settings = sections.get("settings") or {}
        for key in (created.get("settings") or {}).get("create", []):
            if key not in stored_settings:
                session.execute(delete(AppSetting).where(AppSetting.key == key))
        run_sections(ctx, sections)
    except Exception:
        session.rollback()
        raise
    if ctx.plan.errors:
        session.rollback()
        raise ConflictError(
            "The rollback was refused; nothing was changed",
            details={"errors": ctx.plan.errors[:200]},
        )
    run.status = IMPORT_ROLLED_BACK
    run.rolled_back_at = utcnow_naive()
    run.rolled_back_by = actor.username
    audit.record_audit_event(
        session,
        event=audit.CONFIG_ROLLED_BACK,
        category=audit.CATEGORY_CONFIG,
        actor=actor,
        target_type="config_import",
        target_id=run.id,
        details={"summary": _summary(ctx.plan)},
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return run, ctx.plan.as_dict()


def decode_upload(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ImportValidationError("The file is not valid JSON") from exc
