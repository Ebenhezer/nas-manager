# API Reference

Base path: `/api/v1`. All responses are JSON. Interactive docs (`/api/docs`) are available only with `NAS_DEBUG=true`.

## Conventions

- Every response includes `X-Request-ID` (client may send its own matching `[A-Za-z0-9-]{1,64}`).
- Errors use one envelope:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Request validation failed",
    "details": [{"loc": ["path", "count"], "msg": "...", "type": "int_parsing"}],
    "request_id": "f434d34277c14caaa0f77ce834345ebc"
  }
}
```

Codes in use: `http_error` (404, 405, ...), `validation_error` (422), `conflict` (409), `not_found` (404), `bad_request` (400), `unauthorized` (401), `forbidden` (403), `too_many_requests` (429), `csrf_failure` (403), `quota_exceeded` (413), `service_unavailable` (503), `internal_error` (500).

- **Authentication**: except `GET /health`, the public upload endpoints, the `/public/upload` page and the static assets, every endpoint requires a session cookie (`nas_session`, `HttpOnly`) obtained from `POST /auth/login` (JSON) or the `/login` form. Anonymous calls return `401`; an authenticated caller without the required permission returns `403`.
- **CSRF**: state-changing requests that carry browser cookies must echo the `nas_csrf` cookie value via the `X-CSRF-Token` header (or the `_csrf` form field). Authenticated requests must echo the token bound to the session. A cookie-less client (e.g., a script) is not blocked.
- Role and group fields use summary objects; `roles`/`groups` are returned with their nested summaries.

## Public endpoints

### `GET /health`

| Status | Meaning |
|--------|---------|
| 200 `ok` | Database reachable and schema at the latest migration |
| 200 `degraded` | Database reachable, migrations pending (`alembic upgrade head`) |
| 503 `unhealthy` | Database unreachable |

```json
{
  "status": "ok",
  "version": "0.1.0",
  "environment": "production",
  "database": {"reachable": true, "schema_current": true, "revision": "0003", "head_revision": "0003"}
}
```

## Authentication

### `POST /auth/login`

Body: `{"username": "admin", "password": "..."}`.

- 200 sets `nas_session` (HttpOnly) and `nas_csrf` cookies and returns:

```json
{
  "user": {"id": 1, "username": "admin", "display_name": "Administrator"},
  "roles": ["admin"],
  "permissions": ["audit:read", "dashboard:read", "users:read", "users:write"],
  "csrf_token": "raw-token-to-echo"
}
```

- 401 on bad credentials, locked (`message` mentions the lockout) or deactivated accounts; 429 when the per-IP window is exhausted (`retry_after_seconds` in `details`).

### `POST /auth/logout` → 204

Revokes the session and clears both cookies.

### `GET /auth/me` → 200

```json
{
  "user": {"id": 1, "username": "admin", "display_name": "Administrator"},
  "roles": ["admin"],
  "groups": [],
  "permissions": ["audit:read", "dashboard:read"],
  "session_expires_at": "2026-10-20T11:00:00"
}
```

### `POST /auth/change-password` → 200

Body: `{"current_password": "...", "new_password": "..."}`. 400 for a wrong current password or a new password that violates the policy.

## Users (`users:read` / `users:write`)

- `GET /users` → list of user details.
- `POST /users` (201) — create. Body: `{"username", "password", "display_name", "email", "roles": ["viewer"], "groups": [id]}`. Usernames are lower-cased; roles default to `["viewer"]`; 409 on duplicates (case-insensitive), 400 on invalid usernames/passwords.
- `GET /users/{id}` → user detail (includes `roles`, `groups`, `is_locked`, `last_login_at`).
- `PATCH /users/{id}` — profile (display_name, email).
- `PATCH /users/{id}/active` — body `{"active": bool}` (deactivating revokes sessions; 409 for self).
- `POST /users/{id}/unlock` — clears lockout.
- `POST /users/{id}/roles` — body `{"roles": ["operator"]}` (replaces direct roles; 409 against last-admin demotion).
- `POST /users/{id}/reset-password` — body `{"new_password": "..."}`; revokes all sessions.
- `DELETE /users/{id}` → 204 (409 for self, or when the last administrator would vanish).

## Groups (`groups:read` / `groups:write`)

- `GET /groups`, `POST /groups` (`{"name", "description"}`), `GET /groups/{id}`.
- `PATCH /groups/{id}` — description.
- `PATCH /groups/{id}/members` — `{"user_ids": [id, ...]}` (replaces the membership).
- `PATCH /groups/{id}/roles` — `{"roles": ["admin", ...]}` (replaces the group's roles).
- `DELETE /groups/{id}` → 204 (409 if the group is the only vehicle of remaining admin power).

## Roles (`roles:read` / `roles:write`)

- `GET /roles` → list with `permissions`, `system` flag and descriptions.
- `POST /roles` (201) — `{"name", "description", "permissions": ["dashboard:read"]}`; unknown tokens → 400; reserved system names → 409.
- `PATCH /roles/{id}` — description only (system roles immutable, 403).
- `PUT /roles/{id}/permissions` — `{"permissions": [...]}` (system roles immutable, 403).
- `DELETE /roles/{id}` → 204 (system roles immutable).

## Audit (`audit:read`)

`GET /audit/events?limit=50&offset=0&category=user&event=user.created&outcome=success&actor=admin`

Returns `{"events": [...], "total": n, "limit": 50, "offset": 0}` newest-first. `limit` is capped at 200. Events carry `event`, `category`, `outcome`, `actor_username`, `target_type`/`target_id`, `ip_address`, `request_id` and `details`.

## `GET /system/summary` (`dashboard:read`)

Host snapshot used by the dashboard.

```json
{
  "host": {"hostname": "nas", "os": "Ubuntu 24.04.5 LTS", "kernel": "6.8.0", "uptime_seconds": 410.6},
  "cpu": {"percent": 0.0, "logical_cores": 2, "load_average": [0.56, 0.2, 0.07]},
  "memory": {"total": 8419655680, "used": 624263168, "available": 7795392512, "percent": 7.4,
             "swap_total": 0, "swap_used": 0, "swap_percent": 0.0},
  "disks": [{"path": "/", "total": 270553174016, "used": 14459338752, "free": 31818113024, "percent": 31.2}],
  "network": {"bytes_sent": 5276713, "bytes_received": 57867779}
}
```

Notes: byte values are integers; `cpu.percent` is measured since the previous call (the first call after start may be low); `disks` lists `/` and the data directory, collapsed to one entry when they share a filesystem.

## Files (`files:read` / `files:write`)

The file manager operates only inside the configured managed roots (`NAS_FILE_ROOTS`; default `<data_dir>/files`). Every path is resolved against a root and **rejected** when it escapes (absolute paths, `..`, null bytes, symlinks out of the root).

- `GET /files/roots` → list of configured root paths.
- `GET /files/list?root=<root>&path=<rel>` → `{"root", "path", "entries": [{"name", "path", "is_dir", "size", "modified", "mode"}]}` (directories first, then name).
- `GET /files/content?root=<root>&path=<rel>` → file bytes (`Content-Disposition: attachment`).
- `GET /files/search?root=<root>&q=<text>&limit=<n>` → case-insensitive name matches, bounded (`limit` defaults to `NAS_FILE_SEARCH_LIMIT`).
- `POST /files/upload` (multipart) — fields: `file`, `root`, `path` (optional target directory), `overwrite` (optional). Streamed; rejects anything above `NAS_MAX_UPLOAD_BYTES` (400) and refuses to overwrite without `overwrite=true` (409).
- `POST /files/mkdir` — `{"root", "path", "name"}` (201/200 entry).
- `PATCH /files/rename` — `{"root", "path", "new_name"}` (same-directory only; names may not contain `/`).
- `DELETE /files` — body `{"root", "path", "recursive": false}`. Empty directories may be deleted without `recursive`; non-empty directories require `recursive=true` (400 otherwise).

Mutations and downloads are audited (`file.uploaded`, `file.downloaded`, `file.deleted`, `file.renamed`, `file.created_dir`).

## Samba (`samba:read` / `samba:write`)

- `GET /samba/shares` → list of shares.
- `POST /samba/shares` (201) — `{"name", "path", "comment", "read_only", "browseable", "guest_ok", "valid_users", "write_list", "enabled"}`. Name is lower-cased; `path` must be an absolute existing directory; free-text fields must be single-line (400 on config-injection attempts); duplicate names → 409.
- `GET /samba/shares/{id}` · `PATCH /samba/shares/{id}` (partial update) · `DELETE /samba/shares/{id}` (204).
- `GET /samba/status` → config path/exists, managed markers/count, backup dir, `pending_changes`, `helper_available` and `reload_required`, plus a message.
- `POST /samba/apply` — renders and validates the managed config (`testparm -s` against a temp copy). When the privileged helper is enabled (`NAS_HELPER_ENABLED=true`), the helper re-validates, backs up, writes `smb.conf` atomically and reloads `smbd` (`reload_required: false`, `helper_available: true`). Without the helper the web process writes where it can and reports `reload_required: true`. Invalid config → 400 (nothing written); config path not writable / `testparm` missing → 500 `configuration_error`.

Share changes and applies are audited (`samba.share.created/updated/deleted`, `samba.config.applied`).

## Settings (`settings:read` / `settings:write`)

- `GET /settings` → `{"operator_view_files_samba": bool, "public_upload_enabled": bool}`.
- `PUT /settings` — partial update; either or both of `{"operator_view_files_samba": bool}`, `{"public_upload_enabled": bool}`. When `operator_view_files_samba` is enabled, users holding the `operator` role may **view** the Files and Samba pages (read-only); writes are never granted by this switch. `public_upload_enabled` overrides `NAS_PUBLIC_UPLOAD_ENABLED` once flipped: when false the public upload endpoints return 403 (existing files are kept). Defaults: off / the config value. Changes are audited (`settings.updated`, `public_upload.toggle_changed`).

## Public upload folder

The drop folder is **upload-only**: there is no public listing, download or search. Uploads are counted per uploader — `user:<username>` when the request carries a valid session, otherwise `ip:<client address>` — against that uploader's quota, enforced atomically (a rejected upload consumes nothing and is audited as `public_upload.rejected`).

### Public (`GET /public/uploads`, `POST /public/uploads`)

- `GET /public/uploads` → upload policy (no file data):

```json
{"enabled": true, "max_file_bytes": 1073741824, "filename_max_length": 255}
```

- `POST /public/uploads` (multipart `file`) → **201** with the stored metadata:

```json
{"id": 3, "uploader_key": "ip:203.0.113.9", "filename": "report (1).pdf", "size_bytes": 2048, "created_at": "2026-10-22T12:00:00"}
```

Colliding names are auto-uniquified (`report (1).pdf`); nothing is ever overwritten and the response reports the final name. Errors: `400` (name/cap), `403` (disabled), `413 quota_exceeded` (per-uploader quota exhausted). Cookie-less clients need no CSRF token; browsers must echo the `nas_csrf` cookie.

### Admin (`uploads:read` / `uploads:write`)

- `GET /public/uploads/status?limit=100&offset=0` → folder policy plus per-uploader usage:

```json
{
  "enabled": true, "dir": "/var/lib/nas-manager/uploads",
  "max_file_bytes": 1073741824, "default_quota_bytes": 1073741824,
  "total_uploaders": 1, "total_files": 2, "total_used_bytes": 150,
  "uploaders": [{"uploader_key": "ip:203.0.113.9", "quota_bytes": 1073741824,
                 "used_bytes": 150, "file_count": 2, "over_quota": false}],
  "limit": 100, "offset": 0
}
```

- `GET /public/uploads/files?uploader_key=<key>&limit=100&offset=0` → `{"files": [{"id", "uploader_key", "filename", "size_bytes", "ip_address", "created_at"}], "total", "limit", "offset"}`, newest first.
- `PATCH /public/uploads/quotas/{uploader_key}` — body `{"quota_bytes": n}`. Returns the updated `UploaderSummary` (same shape as above). Lowering below current usage flags `over_quota` and blocks new uploads without touching existing files. Audited (`public_upload.quota_changed`).
- `DELETE /public/uploads/files/{id}` → 204. Removes the stored file and decrements usage. Audited (`public_upload.file_deleted`).
- `POST /public/uploads/cleanup` — body `{}` or `{"older_than_days": 30}`. Removes stale `.nas-upload-*.part` temp files, optionally purges tracked files older than N days (rows + disk, audited), then reconciles every uploader's usage from the remaining records. Returns `{"removed_temp_files", "removed_files", "removed_bytes", "reconciled_uploaders", "message"}`. Audited (`public_upload.cleanup`).

All upload mutations are audited under category `public_upload` (`public_upload.uploaded`, `public_upload.rejected`, `public_upload.quota_changed`, `public_upload.file_deleted`, `public_upload.cleanup`, `public_upload.toggle_changed`).

## Disks, storage and RAID (`disks:read` / `disks:write`)

Discovery is **read-only** and goes through the privileged helper; every response is parsed from `lsblk`/`findmnt`/`/proc/mdstat`/`smartctl` output. All privileged actions require `disks:write` (built-in admin only; operator gets `disks:read`).

- `GET /disks` → physical disks with partitions:

```json
[{"name": "sda", "path": "/dev/sda", "size_bytes": 1000204886016, "rotational": true,
  "transport": "sata", "model": "Samsung SSD 870", "serial": "S3Z1...", "wwn": "0x5002...",
  "mountpoints": ["/mnt/media"], "partitions": [{"name": "sda1", "path": "/dev/sda1",
  "size_bytes": 500107862016, "fs_type": "ext4", "mountpoints": ["/mnt/media"]}]}]
```

- `GET /disks/{name}` → the block device and its children (partition/md/…).
- `GET /disks/{name}/smart` → SMART health summary: `health` (`passed`/`failed`), model/serial/firmware, temperature, capacity and up to 12 attributes. Devices without SMART return `available: false` with the smartctl exit code, not an error.
- `GET /disks/status` → `{"enabled", "available", "socket", "error"}` (helper reachability probe; the UI shows an offline badge when it fails).
- `GET /storage/volumes` → mounted volumes (`target`, `source`, `fs_type`, `options`, `used_bytes`, `avail_bytes`, `size_bytes`).
- `GET /storage/raid` → `{"arrays": [{"name": "md0", "path": "/dev/md0", "level": "raid1", "state": "active", "size_bytes": ..., "active_disks": 2, "total_disks": 2, "sync_action": null, "degraded": false, "members": [{"device": "sdb1", "state": "active sync"}]}], "detail_scan": "..."}`.

### Confirmed actions (preview first, then `confirm: true` + matching `intent`)

Every action has a `…/preview` endpoint that shows the exact plan, data-loss warnings and a deterministic `intent` digest, and a confirm endpoint that requires `confirm: true` **and** that exact digest (mismatch → 400). Actions, including failures, are audited under `disk`/`raid` categories.

- `POST /disks/{name}/mount/preview` — body `{"mountpoint": "/mnt/data", "fstype": "ext4", "options": "defaults"}`. Filesystem types allowed: `ext2`, `ext3`, `ext4`, `xfs`, `btrfs`, `vfat`, `exfat`, `ntfs`, `ntfs3`, `iso9660`, `udf`, `f2fs`. Options are a comma-separated allowlist; `loop`, `bind`, `rbind` and `remount` are refused (400).
- `POST /disks/{name}/mount` — body adds `confirm: true` and `intent`. The helper re-validates everything (device exists and is a block device, mount point not in use, filesystem/options allow-listed) and refuses an intent mismatch. Returns `{"ok": true, "message": "Mounted /dev/sdb1 at /mnt/data", "details": {...}}`. Helper down → 503 `service_unavailable` (audited as `disk.action_failed`).
- `POST /disks/{name}/unmount/preview` — body `{"mountpoint": null}` resolves the device's current mount point when omitted (400 if it has several).
- `POST /disks/{name}/unmount` — body `{"mountpoint": "/mnt/data", "confirm": true, "intent": "..."}`. The helper refuses when the device is not mounted at that exact mount point.
- `POST /storage/raid/{name}/scrub/preview` and `POST /storage/raid/{name}/scrub` — body for the confirm call `{"confirm": true, "intent": "..."}`. Starts a **check** scrub only (`md/sync_action = check`; never `repair`), audited as `raid.scrub_started`.

The Samba page's `POST /samba/apply` integrates with the same helper (see above).

## Planned


API tokens, resumable transfers and sync cursors (6). Each phase will extend this document.
