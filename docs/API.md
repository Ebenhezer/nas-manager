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

- **Authentication**: except `GET /health`, `GET /info`, the public upload endpoints, the `/public/upload` page and the static assets, every endpoint requires either a session cookie (`nas_session`, `HttpOnly`) obtained from `POST /auth/login` (JSON) or the `/login` form, **or** a personal API token sent as `Authorization: Bearer nas_…` (see [API tokens](#api-tokens)). When an `Authorization` header is present it is authoritative: an invalid, expired, revoked or non-Bearer value returns `401` with `WWW-Authenticate: Bearer` even if a valid session cookie is also sent. Anonymous calls return `401`; an authenticated caller without the required permission returns `403`.
- **CSRF**: state-changing requests that carry browser cookies must echo the `nas_csrf` cookie value via the `X-CSRF-Token` header (or the `_csrf` form field). Authenticated requests must echo the token bound to the session. Cookie-less clients and requests carrying `Authorization: Bearer` are not CSRF-checked (browsers cannot attach that header cross-site without CORS, which is never enabled).
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
  "session_expires_at": "2026-10-20T11:00:00",
  "auth_method": "session",
  "token_name": null,
  "token_expires_at": null
}
```

With a bearer token, `auth_method` is `"token"`, `session_expires_at` is `null`, `token_name`/`token_expires_at` describe the token and `permissions` are the token's effective scopes. `POST /auth/logout` and `POST /auth/change-password` require a session (`403` for tokens).

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

## API tokens

Personal, scoped bearer credentials for scripts and mobile clients. The raw value (`nas_` + 43 URL-safe characters) is returned **once** at creation; the server stores only its SHA-256. A token's effective permissions are its `scopes` ∩ the owner's *current* effective permissions, so removing a role from the owner immediately narrows every token they hold. Creating and revoking tokens requires an interactive session — a token cannot mint or revoke tokens, change the password or log out (`403`). Tokens are revoked automatically when an admin resets the owner's password or deactivates the account (a user changing their own password keeps their tokens).

- `GET /tokens/scopes` (session) → `{"scopes": [...], "default_lifetime_days": 90, "max_lifetime_days": 365, "max_per_user": 20}` — the scopes the caller may grant (their own permissions).
- `POST /tokens` (session) — body `{"name": "Phone", "scopes": ["files:read", "files:write"], "expires_in_days": 30}` → 201:

```json
{"id": 3, "user_id": 1, "name": "Phone", "token_prefix": "nas_Xy12AbC9", "scopes": ["files:read", "files:write"],
 "created_at": "...", "expires_at": "...", "last_used_at": null, "last_used_ip": null,
 "revoked_at": null, "active": true, "token": "nas_Xy12AbC9..."}
```

  `expires_in_days` omitted → the default lifetime; `0` → never expires (only when `NAS_API_TOKEN_MAX_LIFETIME_DAYS=0`). Errors: scope outside your permissions → 403 (`details.scopes`); unknown scope or lifetime above the maximum → 400; duplicate active name or more than `NAS_API_TOKEN_MAX_PER_USER` active tokens → 409.
- `GET /tokens` (session or token) → the caller's tokens (metadata only, never the secret).
- `DELETE /tokens/{id}` (session) → 204. Other users' ids → 404.
- `GET /admin/tokens?user_id=` (`api_tokens:read`) → every user's tokens with `username`.
- `DELETE /admin/tokens/{id}` (`api_tokens:write`) → 204.

Failed bearer lookups are rate limited per client IP (`NAS_API_TOKEN_AUTH_MAX_FAILURES` within `NAS_API_TOKEN_AUTH_WINDOW_SECONDS`; then `429` with `Retry-After`). Audited under category `api_token`: `api_token.created`, `api_token.revoked`, `api_token.auth_failed` (failures inside the window; throttled attempts are not logged so they cannot flood the table).

Public drop-folder uploads made with a bearer token are charged to that token's own quota (`token:<id>`, default `NAS_API_TOKEN_UPLOAD_QUOTA_BYTES`); an invalid token on the drop endpoint is a `401`, not a silent fallback to the IP quota.

## Resumable uploads

Chunked uploads that survive dropped connections. Works with sessions and tokens. Target `files` (a managed directory; needs `files:write` at every step), `public` (the drop folder; charged to `token:<id>` or `user:<name>`) or `media` (Phase 8: the caller's personal media library; needs `media:write` at every step; `path` is an optional folder, default `YYYY/MM`; names are never overwritten, a collision becomes `name (1).ext`).

1. `POST /uploads` — body `{"target": "files", "root": "/srv/files", "path": "photos", "filename": "IMG_1.jpg", "size": 7340032, "overwrite": false, "sha256": "<optional hex>"}` or `{"target": "public", "filename": "IMG_1.jpg", "size": 7340032}` → 201 `{"id", "target", "filename", "offset": 0, "total_bytes", "chunk_max_bytes", "created_at", "expires_at"}` with `Location` and `Upload-Offset`. The destination, name, size caps, overwrite rule and (for `public`) remaining quota are checked here. At most `NAS_RESUMABLE_MAX_OPEN_PER_USER` open uploads (409).
2. `PATCH /uploads/{id}` — headers `Upload-Offset: <n>` (must equal the server's offset, else 409 with `details.offset`) and `Content-Type: application/offset+octet-stream`; body = the next raw bytes, at most `chunk_max_bytes` and never past `size` (400, the chunk is rolled back). Returns the status with the new `Upload-Offset`. If the connection drops mid-chunk the bytes that arrived are kept.
3. `HEAD /uploads/{id}` / `GET /uploads/{id}` — `Upload-Offset` and `Upload-Length` headers (plus JSON for GET): where to resume.
4. `POST /uploads/{id}/complete` — optional body `{"sha256": "..."}` → `{"target": "files", "file": FileEntry}`, `{"target": "public", "public": PublicUploadResult}` or `{"target": "media", "media": MediaItem}`. Incomplete → 409; checksum mismatch → 400 and the upload is discarded; a name that appeared meanwhile (without `overwrite`) → 409 and the bytes are kept; public quota exhausted → 413 and the upload is discarded (the authoritative quota check happens here, under the same row lock as single-shot uploads).
5. `DELETE /uploads/{id}` → 204, discards the bytes.

Upload ids are private to their owner (others get 404). Each `PATCH` slides the expiry (`NAS_RESUMABLE_UPLOAD_TTL_HOURS`); expired uploads are discarded opportunistically and by `POST /uploads/cleanup` (`files:write`, returns `{"expired_uploads", "pruned_changes"}`). Staging files are named `.nas-resumable-<id>.part`, hidden from listings and search, and not touched by the drop-folder cleanup. Audited: `upload.session_created`, `upload.session_completed`, `upload.session_aborted`, `upload.session_expired` (plus the usual `file.uploaded` / `public_upload.uploaded`).

## Sync cursors (`files:read`)

Every change the file manager makes (upload, overwrite, mkdir, rename, delete — including resumable completions) is journaled with a monotonically increasing id. Changes made outside the application (Samba, a shell) are **not** journaled; re-list periodically if those matter.

- `GET /sync/cursor` → `{"cursor": "c42"}`. Take it **before** the initial listing.
- `GET /sync/changes?root=/srv/files&cursor=c42&limit=500` →

```json
{"root": "/srv/files", "cursor": "c47", "has_more": false, "reset": false,
 "changes": [{"id": 43, "kind": "renamed", "path": "docs/b.txt", "old_path": "docs/a.txt",
              "is_dir": false, "size_bytes": null, "created_at": "..."}]}
```

  `kind` is `created`, `modified`, `deleted` or `renamed` (`old_path` set). A recursive delete is one `deleted` entry for the directory. Keep calling with the returned `cursor` while `has_more` is true. `reset: true` means the cursor predates the retained journal (`NAS_SYNC_CHANGE_RETENTION_DAYS`) or comes from another server: re-list the root and continue from the returned `cursor`. A malformed cursor → 400; an unknown root → 404.

## Client discovery and downloads

- `GET /info` (public) → `{"name", "version", "api_version": "1", "features": ["api_tokens", "resumable_uploads", "sync_cursors", "range_downloads", "remote_sync", "media_gallery", "video_streaming", "video_transcoding"], "auth_schemes": ["session", "bearer"], "limits": {"max_upload_bytes", "resumable_chunk_max_bytes", "resumable_upload_ttl_hours", "sync_change_retention_days"}}`. Nothing sensitive (no paths, no counts).
- `GET /files/content` honours `Range: bytes=a-b` (206 + `Content-Range`), so media can be streamed and interrupted downloads resumed.

A Postman collection that walks through all of Phase 6 (token, resumable upload, sync, expected failures, cleanup) is in [`docs/postman/nas-manager-phase6.postman_collection.json`](postman/nas-manager-phase6.postman_collection.json). Set its `password` variable and run the folders in order.

## Remote connections (`remotes:read` / `remotes:write`)

A connection describes how to reach one remote store. Non-secret settings go in `config`, secrets in `secrets`; responses never contain secret values, only `secrets_set` (the names of the secret fields that are stored). Endpoints that set or use credentials need `remotes:write`; **creating/updating connections, the SFTP host-key probe and the Google Drive OAuth flow also need an interactive session** (API tokens get 403).

- `GET /remotes/providers` → the provider catalogue the UI renders its form from: `[{"name": "sftp", "label": "SFTP (SSH)", "oauth": false, "fields": [{"name": "host", "kind": "text", "required": true, "secret": false, ...}]}]`. Providers and their fields:

| Provider | `config` | `secrets` |
|---|---|---|
| `sftp` | `host`*, `port` (22), `username`*, `base_path`, `host_key`* (`"<type> <base64>"`) | `password` and/or `private_key` (OpenSSH/PEM), `passphrase` |
| `ftps` | `host`*, `port` (21), `username`*, `base_path`, `passive` (true) | `password` |
| `ftp` | as `ftps` + `allow_insecure` (must be `true`: plain FTP sends the password in clear) | `password` |
| `webdav` | `url`* (https; http only with `allow_insecure`), `username`, `allow_insecure` | `password` |
| `s3` | `bucket`*, `prefix`, `region`, `endpoint_url` (non-AWS services), `addressing_style` (`auto`/`path`/`virtual`), `access_key_id`, `allow_insecure` | `secret_access_key` (required with `access_key_id`) |
| `gdrive` | `client_id`*, `folder_id` (`root`) | `client_secret`* (the refresh token is set by the OAuth callback only) |

- `GET /remotes` / `GET /remotes/{id}` →

```json
{"id": 1, "name": "Offsite", "provider": "sftp", "config": {"host": "backup.example.org", "port": 22, "username": "nas", "base_path": "/srv/nas", "host_key": "ssh-ed25519 AAAA..."},
 "secrets_set": ["password"], "credentials_readable": true, "oauth_connected": null,
 "last_tested_at": "...", "last_test_ok": true, "last_test_message": "Connected; 3 entries at the base path",
 "job_count": 1, "created_at": "...", "updated_at": "..."}
```

  `credentials_readable: false` means the stored credentials cannot be decrypted with the configured key file (restore the key or re-enter them). `oauth_connected` is set for Google Drive only.
- `POST /remotes` (session) — `{"name", "provider", "config": {...}, "secrets": {...}}` → 201. Unknown fields, secrets sent in `config`, invalid hosts/URLs/keys, plain FTP/HTTP without `allow_insecure` → 400; duplicate name → 409.
- `PATCH /remotes/{id}` (session) — `{"name"?, "config"?, "secrets"?}`. `config` keys merge over the stored ones; in `secrets` a missing key keeps the stored value, `null`/`""` clears it. Changing a Drive `client_id` drops the refresh token.
- `DELETE /remotes/{id}` → 204; 409 while sync jobs use it.
- `POST /remotes/{id}/test` → `{"ok": bool, "message": "..."}` (connect and list the base path; stored as `last_test_*` and audited as `remote.tested`).
- `GET /remotes/{id}/browse?path=backup` (`remotes:write`) → `{"path", "entries": [{"name", "path", "is_dir", "size", "modified"}], "rejected": 0}` (at most `NAS_REMOTE_BROWSE_LIMIT` entries).
- `POST /remotes/sftp/host-key` (session) — `{"host", "port"}` → `{"host", "port", "key_type", "fingerprint": "SHA256:...", "host_key": "ssh-ed25519 AAAA..."}`. Compare the fingerprint with the server before storing `host_key`.
- `POST /remotes/{id}/oauth/start` (session, Google Drive) → `{"authorization_url", "redirect_uri"}`. Needs `NAS_PUBLIC_BASE_URL` (else 500 explaining it). Open the URL in the browser; Google redirects to `GET /remotes/oauth/callback?state=&code=`, which stores the refresh token (encrypted) and redirects to `/remote-sync?oauth=connected` (or `?oauth=error&message=...`). The state is single-use, bound to the user who started the flow and valid for 10 minutes.

Remote failures (unreachable host, refused login, changed SSH host key, HTTP errors) are `502 remote_error` with a credential-free message; the connection test reports them as `ok: false` instead.

## Sync jobs (`remotes:read` / `remotes:write`)

A job pairs a directory inside a managed file root with a folder on a connection. Runs are executed by the worker process (`python -m app.worker`); the API only queues them.

- `POST /sync-jobs` — body:

```json
{"name": "Photos offsite", "connection_id": 1, "local_root": "/srv/files", "local_path": "photos",
 "remote_path": "backup/photos", "direction": "push", "conflict_policy": "keep_both",
 "delete_policy": "ignore", "exclude_patterns": ["*.tmp", "node_modules"],
 "schedule_minutes": 60, "enabled": false, "confirm": false}
```

  `direction`: `push` (NAS → remote), `pull` (remote → NAS), `bidirectional`. `conflict_policy`: `keep_both` (default), `prefer_local`, `prefer_remote`. `delete_policy`: `ignore` (default; deletions are reported, never propagated) or `archive` (the other side's copy is moved into `.nas-sync-archive/job-<id>/<timestamp>/`). The local directory must exist inside a configured root (404 unknown root, 400 traversal/missing directory); remote paths are relative to the connection and may not contain `..`.
  **Confirmation:** creating or changing a job so that it is enabled, two-way, archives deletions or uses a `prefer_*` policy returns `400 confirmation_required` with `details.warnings` (human-readable risks) unless the body has `confirm: true`. Risks already accepted are not asked again.
- `GET /sync-jobs`, `GET /sync-jobs/{id}` → job objects (plus `next_run_at`, `last_run_at`, `last_status`).
- `PATCH /sync-jobs/{id}` — any subset of the create fields (+ `confirm`). Changing the connection, local root/path or remote path resets the job's per-file sync records; changing those or the direction is refused while a run is active (409). `"schedule_minutes": null` makes the job manual-only.
- `DELETE /sync-jobs/{id}` → 204 (job, sync records and history only; never touches files). 409 while a run is active.
- `POST /sync-jobs/{id}/run` — `{"dry_run": false, "override_deletion_guard": false, "confirm": false}` → 202 with the queued run. 409 if the job already has a queued/running run. `override_deletion_guard` needs `confirm: true`.
- `GET /sync-jobs/{id}/runs?limit=20` → newest first.
- `GET /sync-runs/{id}` →

```json
{"id": 7, "job_id": 1, "status": "succeeded", "trigger": "manual", "dry_run": true, "cancel_requested": false,
 "uploaded": 2, "downloaded": 1, "conflicts": 0, "archived": 0, "skipped": 0, "errors": 0, "bytes_transferred": 0,
 "error": null, "created_at": "...", "started_at": "...", "finished_at": "...",
 "summary": {"counts": {"upload": 2, "download": 1}, "actions": [{"op": "upload", "path": "2026/a.jpg", "reason": "new local file"}],
             "truncated": false, "rejected": [], "local_files": 2, "remote_files": 1, "baseline_files": 0}}
```

  `status`: `queued`, `running`, `succeeded`, `partial` (some files failed; see `summary.problems`), `failed` (`error` says why, e.g. the deletion guard or an unreachable remote), `cancelled`. For dry runs the counters are the *planned* numbers and `summary.actions` the plan (at most `NAS_REMOTE_SYNC_PREVIEW_LIMIT` entries); `op` is `upload`, `download`, `resolve` (conflict candidate), `archive_local`, `archive_remote` or `skip` (with `reason`).
- `POST /sync-runs/{id}/cancel` → a queued run becomes `cancelled` at once; a running one stops after its current file. 409 when already finished.
- `GET /sync-jobs/{id}/conflicts?include_acknowledged=false` → `[{"id", "job_id", "run_id", "path", "kind", "resolution", "conflict_copy_path", "acknowledged", "created_at"}]`. `kind`: `both_modified`, `modified_vs_deleted`, `first_sync_differs`; `resolution`: `kept_both`, `local_won_remote_archived`, `remote_won_local_archived`, `kept_local`, `kept_remote`, `restored_to_remote`, `restored_to_local`.
- `POST /sync-conflicts/{id}/acknowledge` → the conflict with `acknowledged: true`.
- `GET /remote-sync/status` → `{"worker_online", "worker_heartbeat_at", "worker_heartbeat_age_seconds", "queued_runs", "running_runs", "public_base_url_set", "file_roots"}`.

Audited under category `remote_sync`: `remote.created/updated/deleted/tested/oauth_connected/oauth_failed`, `sync_job.created/updated/deleted` (with `confirmed`), `sync_run.queued/cancel_requested/finished/failed`, `sync.conflict`, `sync.archived`, `sync.conflict_acknowledged`, `sync.deletion_guard_tripped`. Files written by pull runs appear in the Phase 6 change journal (`/sync/changes`).

A Postman collection for Phase 7 is in [`docs/postman/nas-manager-phase7.postman_collection.json`](postman/nas-manager-phase7.postman_collection.json).

## Photo gallery and media libraries (Phase 8)

Permissions: `media:read` (browse), `media:write` (upload to and delete from your own library, manage your albums), `media:admin` (shared libraries, status). All three system roles have `media:read` + `media:write`; only `admin` has `media:admin`. API tokens need the matching scopes. Items, thumbnails, streams and albums the caller may not browse answer **404** (never 403).

**Libraries**

- `GET /media/libraries` (`media:read`) → `[LibraryOut]`. Creates the caller's personal library on first use (with `media:write`). Administrators also get every other library's record (`browsable: false` for other users' personal libraries). `LibraryOut`: `{"id", "name", "kind": "personal"|"shared", "owner_user_id", "is_own", "browsable", "writable", "root", "path", "enabled", "include_videos", "group_ids", "scan_state": "idle"|"queued"|"running", "last_scan_at", "last_scan_status", "last_scan_error", "last_scan_summary": {"added", "updated", "removed", "unchanged", "failed", "complete"}, "next_scan_at", "counts": {"image", "video", "pending_thumbnails"}, "created_at"}`. Personal libraries are named `~<username>`.
- `GET /media/libraries/{id}` (`media:read`; administrators may read any record).
- `POST /media/libraries` (`media:admin`) — `{"name", "root": "<a configured file root>", "path": "Photos/Family", "include_videos": true, "enabled": true, "group_ids": [3]}` → 201, queued for its first scan. Errors: unknown root 404; traversal, missing folder, unknown group, a name starting with `~` → 400; duplicate name (case-insensitive) → 409.
- `PATCH /media/libraries/{id}` (`media:admin`) — `{"name"?, "enabled"?, "include_videos"?}`; personal libraries cannot be renamed (400).
- `PUT /media/libraries/{id}/groups` (`media:admin`) — `{"group_ids": [...]}` replaces the read grants (shared libraries only).
- `DELETE /media/libraries/{id}` (`media:admin`) → 204. Drops the index, album entries and cache for a shared library; **files are never touched**. Personal libraries → 409.
- `POST /media/libraries/{id}/scan` (owner or `media:admin`) → 202, queues a rescan for the worker.
- `GET /media/status` (`media:admin`) → `{"libraries", "items", "queued_scans", "running_scans", "pending_thumbnails", "worker_online", "worker_heartbeat_at", "pillow_version", "media_dir", "cache_dir", "cache_bytes", "ffmpeg_available", "ffmpeg_version", "transcoding_available", "queued_transcodes", "running_transcodes", "transcode_cache_bytes", "transcode_cache_max_bytes"}`.

**Items**

- `GET /media/items` (`media:read`) — query `library_id`, `q` (file name, folder, camera; case-insensitive, wildcards literal), `kind=image|video`, `date_from`, `date_to` (ISO datetimes against `taken_at`), `camera`, `album_id` (one of your albums), `has_gps`, `sort=taken_desc|taken_asc|name|size_desc`, `limit` (≤ `NAS_MEDIA_SEARCH_LIMIT`), `offset` → `{"items": [MediaItem], "total", "limit", "offset"}`.
- `MediaItem`: `{"id", "library_id", "rel_path", "name", "kind", "mime", "size", "sha256", "width", "height", "taken_at", "camera_make", "camera_model", "orientation", "gps_lat", "gps_lon", "duration_seconds", "video_codec", "audio_codec", "container", "bitrate", "rotation", "playback", "thumb_state": "pending"|"ready"|"failed"|"unsupported", "thumb_error", "indexed_at"}`. `taken_at` is EXIF `DateTimeOriginal` (camera wall clock, no zone) or the file's mtime (UTC). `width`/`height` are the displayed size (EXIF/video rotation applied).
- `GET /media/items/{id}` → `MediaItem`.
- `GET /media/items/{id}/thumbnail?size=small|large` → `image/webp` (320/1280 px, upright), `Cache-Control: private, max-age=86400`. Rendered on demand if the worker has not yet; 404 when none can be made (`details.thumb_state`).
- `GET /media/items/{id}/original[?download=true]` → the file with its allowlisted type (never sniffed; SVG and HTML are not media types here), `Range` supported, `Content-Disposition: inline` (or `attachment` with `download=true`, which is audited as `media.item_downloaded`).
- `DELETE /media/items/{id}` (`media:write`, owner of the personal library) → 204: deletes the file and its index row. Shared-library items → 403.
- `POST /media/upload` (`media:write`) — multipart `file`, optional `path` (folder inside your library, ≤ 8 levels, no hidden parts) and `taken_at` (ISO; picks the default `YYYY/MM` folder and sets the file's mtime) → 201 `MediaItem`. Only allowlisted photo/video extensions (400 otherwise); size capped by `NAS_MAX_UPLOAD_BYTES`; never overwrites.

**Mobile synchronization**

- `POST /media/sync/lookup` (`media:read`) — `{"sha256": ["<hex>", …]}` (≤ 1000) → `{"library_id", "present": {"<sha256>": [item ids]}, "missing": [...]}` for the caller's personal library. Upload only what is missing.
- `GET /media/sync/cursor` → `{"cursor": "m<id>"}`; take it **before** listing a library with `/media/items`.
- `GET /media/sync/changes?library_id=&cursor=&limit=` → `{"library_id", "cursor", "has_more", "reset", "changes": [{"id", "item_id", "rel_path", "kind": "created"|"modified"|"deleted", "sha256", "size", "created_at"}]}`. Same semantics as `/sync/changes`: keep calling while `has_more`; `reset: true` means re-list. Changes made outside the application (Samba, the file manager, a shell) appear after the next scan.

**Albums** (private to their owner; `media:read` to read, `media:write` to change)

- `GET /media/albums` → `[{"id", "name", "description", "cover_item_id", "item_count", "created_at", "updated_at"}]` (counts include only items you can still browse).
- `POST /media/albums` — `{"name", "description"?}` → 201 album with `item_ids`. Duplicate name (case-insensitive) → 409.
- `GET /media/albums/{id}`, `PATCH /media/albums/{id}` (`{"name"?, "description"?, "cover_item_id"?, "clear_cover"?}`; the cover must be in the album), `DELETE /media/albums/{id}` (the photos stay).
- `POST /media/albums/{id}/items` / `DELETE /media/albums/{id}/items` — `{"item_ids": [...]}` (≤ 1000) → `{"changed": n}`. Adding an item you cannot browse → 404.

Audited under category `media`: `media.library_created/updated/deleted/groups_changed`, `media.scan_queued`, `media.item_uploaded/downloaded/deleted`, `media.album_created/updated/deleted/items_changed`.

## Video playback and streaming (Phase 9)

- `GET /media/items/{id}/stream` (`media:read`) → the video with `Range` support (`206` + `Content-Range`, `Accept-Ranges: bytes`), so `<video>` players can seek. Directly playable files (`playback: "direct"`: H.264 + AAC/MP3 in MP4/M4V/MOV, VP8/VP9/AV1 + Opus/Vorbis in WebM) are served as they are (`video/mp4` / `video/webm`); `playback: "transcode"` videos are served from their cached MP4 once it is ready. Otherwise **409 `transcode_required`** with `details: {"item_id", "playback", "state": null|"queued"|"running"|"failed", "progress_percent", "transcoding_available"}`. Images → 400.
- `GET /media/items/{id}/transcode` (`media:read`) → `{"item_id", "state": null|"queued"|"running"|"ready"|"failed", "progress_percent", "error", "output_size", "playback", "transcoding_available", "created_at", "finished_at"}`. `state` is `null` when nothing was requested for the file's *current* content.
- `POST /media/items/{id}/transcode` (`media:read` + access to the item) → 202 with the same shape. Idempotent while queued/running/ready; re-queues after a failure or a source change. `playback: "direct"` → 409; no video stream or not yet probed → 400; ffmpeg missing or `NAS_MEDIA_TRANSCODE_ENABLED=false` → **503 `transcoding_unavailable`**.
- `POST /media/libraries/{id}/transcode` (`media:admin`) → 202 `{"queued": n}`: pre-transcodes every video in the library that needs it.
- `GET /info` lists `video_streaming` always and `video_transcoding` when transcoding is available.

Audited: `media.transcode_requested`; failed transcodes write `media.transcode_failed` (outcome `failure`, with the ffmpeg error).

## System monitoring (Phase 10, `system:read`)

- `GET /monitoring/overview` returns `{"enabled", "summary": <same shape as /system/summary>, "checks": [check…], "alerts": <alert summary>}`. The summary covers the data dir and every managed file root.
- `GET /monitoring/history?range=1h|6h|24h|7d` returns `{"range", "bucket_seconds", "sample_seconds", "points": [{"at", "cpu_percent", "cpu_percent_max", "memory_percent", "swap_percent", "load_per_core", "net_sent_bps", "net_recv_bps", "disks": {"<path>": percent}}]}`.
  - Buckets are 1/2/5/30 minutes.
  - Network rates are `null` for the first sample and across counter resets (reboots).
  - Any other range returns 422.
- `GET /monitoring/processes?sort=cpu|memory&limit=1-500` returns `[{"pid", "name", "username", "status", "cpu_percent", "memory_percent", "memory_rss", "started_at"}]`. `limit` defaults to `NAS_MONITOR_PROCESS_LIMIT`. **Command lines are never returned.**
- `GET /monitoring/services` returns the worker's latest `systemctl show` snapshot: `[{"unit", "status": ok|warning|critical|unknown, "summary", "description", "load_state", "active_state", "sub_state", "unit_file_state", "active_since", "checked_at"}]`.
  - `unknown` means not installed, systemctl unavailable, or no D-Bus.
  - `critical` means `failed` or `inactive`.
  - `warning` means activating or deactivating.
- `GET /monitoring/raid` returns `{"available", "error", "arrays": [<Phase 5 RaidArray>]}`, read live from `/proc/mdstat`.
- `GET /monitoring/checks?kind=service|raid|smart|helper` returns `[{"key", "kind", "target", "status", "summary", "details", "checked_at"}]`.

## Alerts, rules and channels (Phase 10)

### Alerts

- `GET /alerts?state=open|pending|firing|resolved&severity=warning|critical&limit&offset` (`system:read`) returns `{"items": [alert…], "total", "limit", "offset"}`.
  - Each alert is `{"id", "rule_id", "rule_name", "metric", "target", "state", "severity", "message", "value", "started_at", "firing_at", "last_seen_at", "resolved_at", "acknowledged_at", "acknowledged_by"}`.
  - Items are newest first. An unknown state or severity returns 400.
- `GET /alerts/summary` (`system:read`) returns `{"counts": {"firing", "pending", "critical", "warning", "unacknowledged"}, "synthetic": [{"key", "severity", "message"}], "worker_online", "last_sample_at"}`. `synthetic` holds `worker_offline` when the worker heartbeat is stale.
- `POST /alerts/{id}/acknowledge` (`system:write`) returns the alert. Only firing alerts can be acknowledged (409 otherwise). The call is idempotent.

### Rules

- `GET /alert-rules` (`system:read`) returns `[{"id", "name", "metric", "target", "comparison", "threshold", "duration_seconds", "severity", "enabled", "notify", "builtin", "open_alerts", "created_at", "updated_at"}]`.
- `POST /alert-rules` (`system:write`) takes `{"name", "metric", "target"?, "comparison": "gt"|"lt", "threshold"?, "duration_seconds", "severity", "enabled", "notify"}` and returns 201.
  - Metrics: `cpu_percent`, `memory_percent`, `swap_percent`, `disk_percent` (threshold 0–100); `load_per_core` (threshold 0–1000); `raid_degraded`, `raid_failed_member`, `smart_failed`, `service_down`, `helper_unavailable` (no threshold).
  - `target` limits a rule to one disk path, array, device or unit. Empty means all.
  - An unknown metric or severity, or a missing or out-of-range threshold, returns 400. A duplicate name returns 409.
- `PATCH /alert-rules/{id}` (`system:write`) accepts any of the create fields except `metric`; `target: ""` clears the target. Disabling a rule closes its open alerts without notifying. After a threshold or target change, the next evaluation resolves (and notifies about) whatever no longer matches.
- `DELETE /alert-rules/{id}` (`system:write`) returns 204. Built-in rules return 409 (disable them instead).

### Channels

- `GET /alert-channels` (`system:read`) returns `[{"id", "name", "kind", "config", "secrets_set", "credentials_readable", "min_severity", "allow_insecure", "send_resolved", "enabled", "last_test_at", "last_test_ok", "last_test_message", "created_at", "updated_at"}]`. Secret values are never returned.
- `POST /alert-channels` (`system:write`, **session only**) takes `{"name", "kind": "email"|"webhook", "config", "secrets", "min_severity", "allow_insecure", "send_resolved", "enabled"}` and returns 201.
  - Email config: `{"host", "port", "security": "starttls"|"tls"|"none", "username"?, "from_address", "to": [addresses] or "a@x, b@y"}`. Secret: `password`.
  - Webhook config: `{"url"}`. Secrets: `signing_secret` (adds `X-NAS-Signature: sha256=<HMAC of the body>`) and `authorization` (sent as the `Authorization` header).
  - `security: "none"` and `http://` URLs need `allow_insecure: true`. Credentials in the URL, unknown fields and multi-line values return 400.
- `PATCH /alert-channels/{id}` (session only): `config` keys merge; `secrets` keys replace (string), clear (`null`/`""`) or stay unchanged (missing). The kind cannot change.
- `DELETE /alert-channels/{id}` (`system:write`) returns 204. Notifications still queued for the channel are dropped with it.
- `POST /alert-channels/{id}/test` (session only) returns `{"ok", "message"}`. It sends a test message synchronously and records the result.

Webhook body for alert events: `{"event": "firing"|"resolved", "host", "alert": {"id", "rule", "metric", "target", "severity", "state", "message", "value", "started_at", "firing_at", "resolved_at"}, "source": "nas-manager", "version"}`. Test messages send `{"event": "test", "host", "message", "source", "version"}`.

`GET /info` lists the features `monitoring` and `alerting`. A Postman collection covering the whole flow is in `docs/postman/nas-manager-phase10.postman_collection.json`. It needs the worker and a webhook receiver; set `admin_password` and `webhook_url`.

Audited (category `monitoring`): `alert_rule.created/updated/deleted`, `alert_channel.created/updated/deleted/tested` (secret field names only, never values), `alert.acknowledged`, and `alert.notification_failed` (outcome `failure`, actor `system`).

## Planned

Backups (11), configuration export/import (12). Each phase will extend this document.
