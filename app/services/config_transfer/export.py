"""Configuration export (Phase 12).

The document is JSON::

    {"format": "nas-manager-config", "version": 1, "exported_at", "app_version",
     "sections": {...}, "secrets": null | {"kdf": {...}, "sealed": "<base64>"}}

``sections`` holds every configuration object by its natural key (names,
usernames), never database ids, so a document can be imported into another
installation. Runtime state (sessions, audit log, history, journals,
indexes, counters, timestamps) is never exported.

Secrets (password hashes, remote credentials, channel secrets, backup
passphrases) are only included when the caller gives a passphrase: they are
then sealed together with a SHA-256 of the canonical ``sections`` using the
backup encryption (scrypt + AES-256-GCM), which also lets an import detect a
modified document. Without a passphrase the document lists only which secret
fields exist.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__
from app.core.config import Settings
from app.core.errors import BadRequestError
from app.core.secrets import CredentialCipher
from app.db.base import utcnow_naive
from app.models import (
    AlertChannel,
    AlertRule,
    BackupJob,
    BackupRepository,
    Group,
    MediaLibrary,
    PublicUploadQuota,
    RemoteConnection,
    Role,
    SambaShare,
    SyncJob,
    User,
)
from app.models.media import LIBRARY_SHARED
from app.services import settings_service
from app.services.backup import crypto
from app.services.media.libraries import library_group_ids

FORMAT = "nas-manager-config"
VERSION = 1

SETTINGS_KEYS = (
    settings_service.OPERATOR_VIEW_FILES_SAMBA_KEY,
    settings_service.PUBLIC_UPLOAD_ENABLED_KEY,
)

# Import order: a section only references sections before it.
SECTIONS = (
    "settings",
    "roles",
    "groups",
    "users",
    "samba_shares",
    "remote_connections",
    "sync_jobs",
    "media_libraries",
    "alert_rules",
    "alert_channels",
    "public_upload_quotas",
    "backup_repositories",
    "backup_jobs",
)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sections_digest(sections: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(sections)).hexdigest()


def _names(session: Session, model: Any) -> dict[int, str]:
    return {row.id: row.name for row in session.execute(select(model)).scalars()}


def collect(
    session: Session, settings: Settings, cipher: CredentialCipher
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(sections, secrets)`` describing the current configuration."""
    secrets: dict[str, dict[str, Any]] = {
        "users": {},
        "remote_connections": {},
        "alert_channels": {},
        "backup_repositories": {},
    }
    sections: dict[str, Any] = {}

    stored = {key: settings_service.get_setting(session, key) for key in SETTINGS_KEYS}
    sections["settings"] = {
        key: value.strip().lower() in {"1", "true", "yes", "on"}
        for key, value in stored.items()
        if value is not None
    }

    roles = list(session.execute(select(Role).order_by(Role.name)).scalars())
    sections["roles"] = [
        {
            "name": role.name,
            "description": role.description,
            "system": role.system,
            "permissions": sorted(p.permission for p in role.permissions),
        }
        for role in roles
    ]

    groups = list(session.execute(select(Group).order_by(Group.name)).scalars())
    sections["groups"] = [
        {
            "name": group.name,
            "description": group.description,
            "roles": sorted(role.name for role in group.roles),
        }
        for group in groups
    ]

    users = list(session.execute(select(User).order_by(User.username)).scalars())
    sections["users"] = []
    for user in users:
        sections["users"].append(
            {
                "username": user.username,
                "display_name": user.display_name,
                "email": user.email,
                "is_active": user.is_active,
                "roles": sorted(role.name for role in user.roles),
                "groups": sorted(group.name for group in user.groups),
            }
        )
        secrets["users"][user.username] = {"password_hash": user.password_hash}

    sections["samba_shares"] = [
        {
            "name": share.name,
            "path": share.path,
            "comment": share.comment,
            "read_only": share.read_only,
            "browseable": share.browseable,
            "guest_ok": share.guest_ok,
            "valid_users": share.valid_users,
            "write_list": share.write_list,
            "enabled": share.enabled,
        }
        for share in session.execute(select(SambaShare).order_by(SambaShare.name)).scalars()
    ]

    connections = list(
        session.execute(select(RemoteConnection).order_by(RemoteConnection.name)).scalars()
    )
    sections["remote_connections"] = []
    for conn in connections:
        values = cipher.decrypt_json(conn.secrets_encrypted)
        sections["remote_connections"].append(
            {
                "name": conn.name,
                "provider": conn.provider,
                "config": conn.config,
                "secret_fields": sorted(k for k, v in values.items() if v),
            }
        )
        secrets["remote_connections"][conn.name] = values
    connection_names = {conn.id: conn.name for conn in connections}

    sections["sync_jobs"] = [
        {
            "name": job.name,
            "connection": connection_names.get(job.connection_id),
            "local_root": job.local_root,
            "local_path": job.local_path,
            "remote_path": job.remote_path,
            "direction": job.direction,
            "conflict_policy": job.conflict_policy,
            "delete_policy": job.delete_policy,
            "exclude_patterns": job.exclude_patterns,
            "schedule_minutes": job.schedule_minutes,
            "enabled": job.enabled,
        }
        for job in session.execute(select(SyncJob).order_by(SyncJob.name)).scalars()
    ]

    group_names = {group.id: group.name for group in groups}
    sections["media_libraries"] = [
        {
            "name": library.name,
            "root": library.root,
            "path": library.rel_path,
            "enabled": library.enabled,
            "include_videos": library.include_videos,
            "groups": sorted(
                group_names[gid]
                for gid in library_group_ids(session, library.id)
                if gid in group_names
            ),
        }
        for library in session.execute(
            select(MediaLibrary)
            .where(MediaLibrary.kind == LIBRARY_SHARED)
            .order_by(MediaLibrary.name)
        ).scalars()
    ]

    sections["alert_rules"] = [
        {
            "name": rule.name,
            "metric": rule.metric,
            "target": rule.target,
            "comparison": rule.comparison,
            "threshold": rule.threshold,
            "duration_seconds": rule.duration_seconds,
            "severity": rule.severity,
            "enabled": rule.enabled,
            "notify": rule.notify,
            "builtin": rule.builtin,
        }
        for rule in session.execute(select(AlertRule).order_by(AlertRule.name)).scalars()
    ]

    sections["alert_channels"] = []
    for channel in session.execute(select(AlertChannel).order_by(AlertChannel.name)).scalars():
        values = cipher.decrypt_json(channel.secrets_encrypted)
        sections["alert_channels"].append(
            {
                "name": channel.name,
                "kind": channel.kind,
                "config": channel.config,
                "min_severity": channel.min_severity,
                "allow_insecure": channel.allow_insecure,
                "send_resolved": channel.send_resolved,
                "enabled": channel.enabled,
                "secret_fields": sorted(k for k, v in values.items() if v),
            }
        )
        secrets["alert_channels"][channel.name] = values

    sections["public_upload_quotas"] = [
        {"uploader_key": row.uploader_key, "quota_bytes": row.quota_bytes}
        for row in session.execute(
            select(PublicUploadQuota).order_by(PublicUploadQuota.uploader_key)
        ).scalars()
    ]

    repositories = list(
        session.execute(select(BackupRepository).order_by(BackupRepository.name)).scalars()
    )
    sections["backup_repositories"] = []
    for repo in repositories:
        sections["backup_repositories"].append(
            {
                "name": repo.name,
                "target_kind": repo.target_kind,
                "connection": connection_names.get(repo.connection_id or 0),
                "local_root": repo.local_root,
                "repo_path": repo.repo_path,
                "secret_fields": ["passphrase"],
            }
        )
        secrets["backup_repositories"][repo.name] = cipher.decrypt_json(repo.passphrase_encrypted)
    repository_names = {repo.id: repo.name for repo in repositories}

    sections["backup_jobs"] = [
        {
            "name": job.name,
            "repository": repository_names.get(job.repository_id),
            "source_kind": job.source_kind,
            "source_root": job.source_root,
            "source_path": job.source_path,
            "source_connection": connection_names.get(job.source_connection_id or 0),
            "exclude_patterns": job.exclude_patterns,
            "schedule_minutes": job.schedule_minutes,
            "enabled": job.enabled,
            "keep_last": job.keep_last,
            "keep_daily": job.keep_daily,
            "keep_weekly": job.keep_weekly,
            "keep_monthly": job.keep_monthly,
        }
        for job in session.execute(select(BackupJob).order_by(BackupJob.name)).scalars()
    ]
    return sections, secrets


def seal_secrets(
    sections: dict[str, Any], secrets: dict[str, Any], passphrase: str, *, scrypt_n: int
) -> dict[str, Any]:
    params = crypto.KdfParams.new(scrypt_n)
    keys = crypto.derive_keys(passphrase, params)
    payload = canonical({"sections_sha256": sections_digest(sections), "secrets": secrets})
    return {
        "kdf": params.as_dict(),
        "sealed": base64.b64encode(crypto.seal(payload, keys)).decode(),
    }


def unseal_secrets(
    sealed: Any, sections: dict[str, Any], passphrase: str
) -> dict[str, dict[str, Any]]:
    """Decrypt the secrets of a document and check the sections were not modified."""
    if not isinstance(sealed, dict):
        raise BadRequestError("The document's secrets block is malformed")
    try:
        params = crypto.KdfParams.from_dict(sealed.get("kdf"))
        data = base64.b64decode(str(sealed.get("sealed", "")), validate=True)
        payload = json.loads(crypto.unseal(data, crypto.derive_keys(passphrase, params)))
    except (crypto.DecryptionError, ValueError) as exc:
        raise BadRequestError(
            "The passphrase does not decrypt this export (or the file was modified)",
            details={"code": "wrong_passphrase"},
        ) from exc
    if not isinstance(payload, dict) or payload.get("sections_sha256") != sections_digest(sections):
        raise BadRequestError(
            "The export was modified after it was created (its checksum does not match)"
        )
    values = payload.get("secrets")
    if not isinstance(values, dict):
        raise BadRequestError("The document's secrets block is malformed")
    return {str(k): dict(v) for k, v in values.items() if isinstance(v, dict)}


def build_document(
    session: Session,
    settings: Settings,
    cipher: CredentialCipher,
    *,
    passphrase: str | None,
) -> dict[str, Any]:
    sections, secrets = collect(session, settings, cipher)
    if passphrase is not None and len(passphrase) < crypto.MIN_PASSPHRASE_LENGTH:
        raise BadRequestError(
            f"The export passphrase must be at least {crypto.MIN_PASSPHRASE_LENGTH} characters"
        )
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": utcnow_naive().isoformat() + "Z",
        "app_version": __version__,
        "sections": sections,
        "secrets": (
            seal_secrets(sections, secrets, passphrase, scrypt_n=settings.backup_scrypt_n)
            if passphrase
            else None
        ),
    }


def snapshot_document(
    session: Session, settings: Settings, cipher: CredentialCipher
) -> dict[str, Any]:
    """The pre-import snapshot: the full state with plaintext secrets (the
    caller encrypts the whole document with the credentials key)."""
    sections, secrets = collect(session, settings, cipher)
    return {"format": FORMAT, "version": VERSION, "sections": sections, "plain_secrets": secrets}
