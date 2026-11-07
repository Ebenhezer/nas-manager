"""Every state-changing API route is accounted for in the audit log (Phase 13).

A new POST/PUT/PATCH/DELETE route fails this test until it is listed here,
either with the audit event(s) it records or as an exemption with a reason,
so auditing is a conscious decision for every endpoint.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI

from app.services import audit

A = audit

ROUTE_EVENTS: dict[str, tuple[str, ...]] = {
    "POST /api/v1/auth/login": (A.AUTH_LOGIN_SUCCESS, A.AUTH_LOGIN_FAILURE, A.AUTH_LOGIN_LOCKED),
    "POST /api/v1/auth/logout": (A.AUTH_LOGOUT,),
    "POST /api/v1/auth/change-password": (A.USER_PASSWORD_CHANGED_SELF,),
    "POST /api/v1/users": (A.USER_CREATED,),
    "PATCH /api/v1/users/{user_id}": (A.USER_UPDATED,),
    "DELETE /api/v1/users/{user_id}": (A.USER_DELETED,),
    "PATCH /api/v1/users/{user_id}/active": (A.USER_ACTIVE_STATE_CHANGED,),
    "POST /api/v1/users/{user_id}/unlock": (A.USER_UNLOCKED,),
    "POST /api/v1/users/{user_id}/roles": (A.USER_ROLES_CHANGED,),
    "POST /api/v1/users/{user_id}/reset-password": (A.USER_PASSWORD_RESET,),
    "POST /api/v1/groups": (A.GROUP_CREATED,),
    "PATCH /api/v1/groups/{group_id}": (A.GROUP_UPDATED,),
    "DELETE /api/v1/groups/{group_id}": (A.GROUP_DELETED,),
    "PATCH /api/v1/groups/{group_id}/members": (A.GROUP_MEMBERS_CHANGED,),
    "PATCH /api/v1/groups/{group_id}/roles": (A.GROUP_ROLES_CHANGED,),
    "POST /api/v1/roles": (A.ROLE_CREATED,),
    "PATCH /api/v1/roles/{role_id}": (A.ROLE_UPDATED,),
    "DELETE /api/v1/roles/{role_id}": (A.ROLE_DELETED,),
    "PUT /api/v1/roles/{role_id}/permissions": (A.ROLE_PERMISSIONS_CHANGED,),
    "POST /api/v1/audit/verify": (A.AUDIT_VERIFIED,),
    "POST /api/v1/files/upload": (A.FILE_UPLOADED,),
    "POST /api/v1/files/mkdir": (A.FILE_CREATED_DIR,),
    "PATCH /api/v1/files/rename": (A.FILE_RENAMED,),
    "DELETE /api/v1/files": (A.FILE_DELETED,),
    "POST /api/v1/files/transfers": (A.FILE_TRANSFER_STARTED,),
    "POST /api/v1/files/transfers/{transfer_id}/cancel": (A.FILE_TRANSFER_CANCEL_REQUESTED,),
    "POST /api/v1/samba/shares": (A.SAMBA_SHARE_CREATED,),
    "PATCH /api/v1/samba/shares/{share_id}": (A.SAMBA_SHARE_UPDATED,),
    "DELETE /api/v1/samba/shares/{share_id}": (A.SAMBA_SHARE_DELETED,),
    "POST /api/v1/samba/apply": (A.SAMBA_CONFIG_APPLIED,),
    "PUT /api/v1/settings": (A.SETTINGS_UPDATED, A.PUBLIC_UPLOAD_TOGGLE_CHANGED),
    "POST /api/v1/disks/{name}/mount": (A.DISK_MOUNTED, A.DISK_ACTION_FAILED),
    "POST /api/v1/disks/{name}/unmount": (A.DISK_UNMOUNTED, A.DISK_ACTION_FAILED),
    "POST /api/v1/storage/raid/{name}/scrub": (A.RAID_SCRUB_STARTED, A.RAID_ACTION_FAILED),
    "POST /api/v1/public/uploads": (A.PUBLIC_UPLOAD_UPLOADED, A.PUBLIC_UPLOAD_REJECTED),
    "PATCH /api/v1/public/uploads/quotas/{uploader_key}": (A.PUBLIC_UPLOAD_QUOTA_CHANGED,),
    "DELETE /api/v1/public/uploads/files/{file_id}": (A.PUBLIC_UPLOAD_FILE_DELETED,),
    "POST /api/v1/public/uploads/cleanup": (A.PUBLIC_UPLOAD_CLEANUP,),
    "POST /api/v1/tokens": (A.API_TOKEN_CREATED,),
    "DELETE /api/v1/tokens/{token_id}": (A.API_TOKEN_REVOKED,),
    "DELETE /api/v1/admin/tokens/{token_id}": (A.API_TOKEN_REVOKED,),
    "POST /api/v1/uploads": (A.UPLOAD_SESSION_CREATED,),
    "POST /api/v1/uploads/cleanup": (A.UPLOAD_SESSION_EXPIRED,),
    "DELETE /api/v1/uploads/{upload_id}": (A.UPLOAD_SESSION_ABORTED,),
    "POST /api/v1/uploads/{upload_id}/complete": (A.UPLOAD_SESSION_COMPLETED,),
    "POST /api/v1/remotes": (A.REMOTE_CREATED,),
    "PATCH /api/v1/remotes/{connection_id}": (A.REMOTE_UPDATED,),
    "DELETE /api/v1/remotes/{connection_id}": (A.REMOTE_DELETED,),
    "POST /api/v1/remotes/{connection_id}/test": (A.REMOTE_TESTED,),
    "POST /api/v1/remotes/{connection_id}/oauth/start": (A.REMOTE_OAUTH_STARTED,),
    "POST /api/v1/sync-jobs": (A.SYNC_JOB_CREATED,),
    "PATCH /api/v1/sync-jobs/{job_id}": (A.SYNC_JOB_UPDATED,),
    "DELETE /api/v1/sync-jobs/{job_id}": (A.SYNC_JOB_DELETED,),
    "POST /api/v1/sync-jobs/{job_id}/run": (A.SYNC_RUN_QUEUED,),
    "POST /api/v1/sync-runs/{run_id}/cancel": (A.SYNC_RUN_CANCEL_REQUESTED,),
    "POST /api/v1/sync-conflicts/{conflict_id}/acknowledge": (A.SYNC_CONFLICT_ACKNOWLEDGED,),
    "POST /api/v1/media/libraries": (A.MEDIA_LIBRARY_CREATED,),
    "PATCH /api/v1/media/libraries/{library_id}": (A.MEDIA_LIBRARY_UPDATED,),
    "DELETE /api/v1/media/libraries/{library_id}": (A.MEDIA_LIBRARY_DELETED,),
    "PUT /api/v1/media/libraries/{library_id}/groups": (A.MEDIA_LIBRARY_GROUPS_CHANGED,),
    "POST /api/v1/media/libraries/{library_id}/scan": (A.MEDIA_SCAN_QUEUED,),
    "POST /api/v1/media/libraries/{library_id}/transcode": (A.MEDIA_TRANSCODE_REQUESTED,),
    "DELETE /api/v1/media/items/{item_id}": (A.MEDIA_ITEM_DELETED,),
    "POST /api/v1/media/items/{item_id}/transcode": (A.MEDIA_TRANSCODE_REQUESTED,),
    "POST /api/v1/media/upload": (A.MEDIA_ITEM_UPLOADED,),
    "POST /api/v1/media/albums": (A.MEDIA_ALBUM_CREATED,),
    "PATCH /api/v1/media/albums/{album_id}": (A.MEDIA_ALBUM_UPDATED,),
    "DELETE /api/v1/media/albums/{album_id}": (A.MEDIA_ALBUM_DELETED,),
    "POST /api/v1/media/albums/{album_id}/items": (A.MEDIA_ALBUM_ITEMS_CHANGED,),
    "DELETE /api/v1/media/albums/{album_id}/items": (A.MEDIA_ALBUM_ITEMS_CHANGED,),
    "POST /api/v1/media/libraries/{library_id}/folders": (A.MEDIA_FOLDER_CREATED,),
    "PATCH /api/v1/media/libraries/{library_id}/folders": (A.MEDIA_FOLDER_RENAMED,),
    "DELETE /api/v1/media/libraries/{library_id}/folders": (A.MEDIA_FOLDER_DELETED,),
    "POST /api/v1/media/items/move": (A.MEDIA_ITEMS_MOVED,),
    "PATCH /api/v1/media/items/{item_id}": (A.MEDIA_ITEM_RENAMED,),
    "POST /api/v1/media/items/delete": (A.MEDIA_ITEM_DELETED,),
    "POST /api/v1/alerts/{alert_id}/acknowledge": (A.ALERT_ACKNOWLEDGED,),
    "POST /api/v1/alert-rules": (A.ALERT_RULE_CREATED,),
    "PATCH /api/v1/alert-rules/{rule_id}": (A.ALERT_RULE_UPDATED,),
    "DELETE /api/v1/alert-rules/{rule_id}": (A.ALERT_RULE_DELETED,),
    "POST /api/v1/alert-channels": (A.ALERT_CHANNEL_CREATED,),
    "PATCH /api/v1/alert-channels/{channel_id}": (A.ALERT_CHANNEL_UPDATED,),
    "DELETE /api/v1/alert-channels/{channel_id}": (A.ALERT_CHANNEL_DELETED,),
    "POST /api/v1/alert-channels/{channel_id}/test": (A.ALERT_CHANNEL_TESTED,),
    "POST /api/v1/backup-repositories": (A.BACKUP_REPOSITORY_CREATED,),
    "PATCH /api/v1/backup-repositories/{repository_id}": (A.BACKUP_REPOSITORY_UPDATED,),
    "DELETE /api/v1/backup-repositories/{repository_id}": (A.BACKUP_REPOSITORY_DELETED,),
    "POST /api/v1/backup-repositories/{repository_id}/init": (A.BACKUP_REPOSITORY_INITIALIZED,),
    "POST /api/v1/backup-repositories/{repository_id}/verify": (A.BACKUP_RUN_QUEUED,),
    "POST /api/v1/backup-repositories/{repository_id}/prune": (A.BACKUP_RUN_QUEUED,),
    "POST /api/v1/backup-jobs": (A.BACKUP_JOB_CREATED,),
    "PATCH /api/v1/backup-jobs/{job_id}": (A.BACKUP_JOB_UPDATED,),
    "DELETE /api/v1/backup-jobs/{job_id}": (A.BACKUP_JOB_DELETED,),
    "POST /api/v1/backup-jobs/{job_id}/run": (A.BACKUP_RUN_QUEUED,),
    "POST /api/v1/backup-runs/{run_id}/cancel": (A.BACKUP_RUN_CANCEL_REQUESTED,),
    "POST /api/v1/backup-snapshots/{snapshot_id}/restore": (A.BACKUP_RUN_QUEUED,),
    "POST /api/v1/config/export": (A.CONFIG_EXPORTED,),
    "POST /api/v1/config/import/preview": (A.CONFIG_IMPORT_PREVIEWED,),
    "POST /api/v1/config/import": (A.CONFIG_IMPORTED, A.CONFIG_IMPORT_REFUSED),
    "POST /api/v1/config/imports/{run_id}/rollback": (A.CONFIG_ROLLED_BACK,),
    "POST /api/v1/syslog/destinations": (A.SYSLOG_DESTINATION_CREATED,),
    "PATCH /api/v1/syslog/destinations/{destination_id}": (A.SYSLOG_DESTINATION_UPDATED,),
    "DELETE /api/v1/syslog/destinations/{destination_id}": (A.SYSLOG_DESTINATION_DELETED,),
    "POST /api/v1/syslog/destinations/{destination_id}/test": (A.SYSLOG_DESTINATION_TESTED,),
}

EXEMPT: dict[str, str] = {
    "POST /api/v1/disks/{name}/mount/preview": "read-only preview, changes nothing",
    "POST /api/v1/disks/{name}/unmount/preview": "read-only preview, changes nothing",
    "POST /api/v1/storage/raid/{name}/scrub/preview": "read-only preview, changes nothing",
    "POST /api/v1/remotes/sftp/host-key": "read-only probe; the pinned key is audited on create",
    "POST /api/v1/media/sync/lookup": "read-only hash lookup used by phone backup clients",
    "POST /api/v1/media/favorites": "private per-user star; changes no files or shared data",
    "PATCH /api/v1/uploads/{upload_id}": (
        "high-volume chunk append to a hidden staging file; creation, completion, abort and "
        "expiry of the upload session are audited"
    ),
}


def _mutating_routes(app: FastAPI) -> set[str]:
    spec = app.openapi()
    return {
        f"{method.upper()} {path}"
        for path, operations in spec["paths"].items()
        for method in operations
        if method in {"post", "put", "patch", "delete"} and path.startswith("/api/v1")
    }


def test_every_mutating_route_is_accounted_for(app: FastAPI) -> None:
    routes = _mutating_routes(app)
    unlisted = sorted(routes - set(ROUTE_EVENTS) - set(EXEMPT))
    assert unlisted == [], "add these routes to ROUTE_EVENTS or EXEMPT"
    stale = sorted((set(ROUTE_EVENTS) | set(EXEMPT)) - routes)
    assert stale == []
    assert not set(ROUTE_EVENTS) & set(EXEMPT)


def test_every_mapped_event_is_recorded_somewhere() -> None:
    app_dir = Path(audit.__file__).resolve().parents[1]
    names = {
        name
        for name, value in vars(audit).items()
        if name.isupper() and isinstance(value, str) and "." in value
    }
    by_value = {getattr(audit, name): name for name in names}
    sources = "\n".join(
        path.read_text()
        for path in app_dir.rglob("*.py")
        if path.name != "audit.py" or path.parent.name != "services"
    )
    for events in ROUTE_EVENTS.values():
        for event in events:
            name = by_value[event]
            assert re.search(rf"\b{name}\b", sources), f"{name} is never recorded"
