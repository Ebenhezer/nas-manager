# Architecture

## Layers

```
HTTP request
   │
   ▼
RequestContextMiddleware   request id, access log, security headers
   ▼
CSRFProtectMiddleware      double-submit + session-bound token check
   │
   ├── /api/v1/*   app/api/v1/*      thin routes, no business logic
   │                    │
   │                    ▼
   │              app/services/*     business logic, system access
   │                    │
   │                    ▼
   │              app/models/*       SQLAlchemy ORM    ←→  migrations/ (Alembic)
   │              app/schemas/*      Pydantic request/response models
   │
   └── /           app/web/*         Jinja2 templates + static assets
                                     (Bootstrap vendored, modular ES JavaScript)
```

Rules: routes validate and delegate; services hold logic and talk to the OS or database; schemas define the API contract; models define persistence. Background work runs in a separate worker process (`app/worker/`, Phase 7) that claims jobs from the database; the web process only records intent.

## Module map

| Module | Responsibility |
|--------|----------------|
| `app/main.py` | Application factory: logging, directories, database, middleware, routers, startup checks |
| `app/__main__.py` | `python -m app` entry point (uvicorn, application-controlled logging) |
| `app/cli.py` | `python -m app.cli bootstrap-admin` (first-run administrator) |
| `app/worker/` | `python -m app.worker`: the sync worker loop (`runner.py`: heartbeat, lease renewal/reclaim, scheduling, atomic claiming, thread pool, run-history pruning) |
| `app/core/config.py` | `Settings` from `NAS_*` variables (incl. sessions, throttling, Argon2id costs) |
| `app/core/logging.py` | JSON/text formatters, redaction, rotating file handler |
| `app/core/middleware.py` | Request id, access log (path only, never query string), security headers |
| `app/core/csrf.py` | CSRF protection middleware (double-submit + session-bound) |
| `app/core/errors.py` | `AppError` hierarchy and the JSON error envelope |
| `app/core/permissions.py` | Permission-token registry and default role grants |
| `app/core/secrets.py` | Fernet key file (`NAS_CREDENTIALS_KEY_FILE`, created 0600) and `CredentialCipher` for remote credentials at rest |
| `app/db/` | Naming conventions, engine/session factory, migration helpers, `get_db` |
| `app/api/v1/deps.py` | `CurrentUser` (session or API token), `get_current_user`, `require_permission(...)`, `require_session_auth` |
| `app/api/v1/` | `auth`, `users`, `groups`, `roles`, `audit`, `health`, `system`, `files`, `samba`, `settings`, `public_uploads`, `disks`, `api_tokens`, `uploads` (resumable), `sync` (+ `/info`), `remotes`, `remote_sync` (jobs, runs, conflicts, status) |
| `app/services/` | `passwords`, `auth_service`, `users_service`, `groups_service`, `roles_service`, `security`, `audit`, `throttle`, `bootstrap_service`, `health`, `monitoring`, `settings_service`, `files`, `samba`, `public_uploads`, `api_tokens`, `resumable_uploads`, `sync`, `remotes`, `remote_sync`, `sync_engine` |
| `app/services/remote/` | Remote store providers behind one interface (`base.py`): `sftp` (paramiko, pinned host key), `ftp` (FTP/FTPS, `ftplib`), `webdav` and `gdrive` (httpx2), `s3` (boto3); `registry.py` holds the field specs, validation and factory |
| `app/services/helper_client.py` | Thin `AF_UNIX` client for the privileged helper; maps helper errors to typed `AppError`s (503 unavailable, 400 validation/intent/operation) |
| `app/services/disks.py` | Disk/volume/RAID discovery parsing (`lsblk`, `findmnt`, `/proc/mdstat`, `smartctl`), preview + confirmed actions, audit |
| `app/helper/` | Privileged helper (root process): `operations.py` fixed allowlist, `server.py` Unix socket + `SO_PEERCRED`, `protocol.py` framing/intent digest, `__main__.py` entry |
| `app/models/` | `user`, `group`, `role`, `session`, `audit_event`, `app_setting`, `samba_share`, `public_upload`, `api_token`, `upload_session`, `file_change`, `remote` (connections, sync jobs, baselines, runs, conflicts) |
| `app/web/` | Jinja2 templates + modular JS (login, dashboard, users, groups, audit, profile + API tokens, files, samba, settings, uploads, public_upload, disks, api_tokens admin, remote_sync) |

## Authentication and authorization

1. `POST /api/v1/auth/login` (or the `/login` form) verifies the password with Argon2id after a per-IP throttle check, then creates a server-side `auth_sessions` row. The database stores only SHA-256 of the token and of the CSRF token; the browser keeps the raw session token in an `HttpOnly` cookie.
2. Every request passes through `CSRFProtectMiddleware`: state-changing requests that carry the `nas_csrf` cookie must echo it (`X-CSRF-Token` header or `_csrf` form field), and authenticated requests must additionally match the digest bound to their session.
3. `get_current_user` resolves an `Authorization: Bearer` API token when the header is present (authoritative: a bad value is 401), otherwise the session cookie; it rejects revoked/expired sessions or tokens and deactivated users, and slides the idle expiry window. A token's permissions are its scopes ∩ the owner's effective permissions; `require_session_auth` keeps account-control endpoints (token management, password change, logout) session-only. `require_permission("permission")` guards each route: 401 anonymous, 403 without the permission.
4. Effective permissions = union of the permissions of the user's direct roles and of the roles granted to the user's groups (`app/services/security.py`).
5. Login abuse controls: persistent per-account lockout (threshold and cooldown in settings) plus an in-process per-IP sliding window (429). All outcomes are written to `audit_events`.

## Database

SQLAlchemy 2 with a naming convention so constraints are stable across SQLite and PostgreSQL. The backend is selected purely by `NAS_DATABASE_URL`; SQLite gets WAL, `foreign_keys=ON` and a busy timeout on every connection. Alembic runs in batch mode for SQLite `ALTER` support. Models are registered in `app/models/__init__.py`, and a test asserts that models and migrations have no drift. Timestamps stored by auth code are naive UTC wall-clock (SQLite serialises `DateTime` without an offset); `app/db/base.py` exposes both `utcnow()` and `utcnow_naive()`.

## Request lifecycle and observability

Every response carries `X-Request-ID`. A client-supplied id is honoured only if it matches `[A-Za-z0-9-]{1,64}`; otherwise a new one is generated. The id is in each log line and in the error envelope, and it is recorded on every audit event. Logs go to stdout (journald under systemd) and to a size-rotated file. Extra fields whose names look sensitive (`password`, `token`, `secret`, `authorization`, `api_key`, `credential`) are replaced with `***`.

## Deployment topology

```
systemd: nas-manager.service        (user nas-manager, no capabilities, sandboxed)
   └── uvicorn → FastAPI app → SQLite in /var/lib/nas-manager
systemd: nas-manager-worker.service (user nas-manager, same sandbox)
   └── python -m app.worker → claims sync runs from the same database,
       talks to remote stores, writes inside the managed file roots
systemd: nas-manager-helper.service (root, allowlisted operations only)
   └── Unix socket at /run/nas-manager/helper.sock (0660 root:<group>,
       SO_PEERCRED uid allowlist) ← the helper_client talks to this
```

The web process never executes arbitrary commands and never runs as root. The helper does the reverse: it is the only process allowed to mount/unmount block devices, read SMART data, write `/etc/samba/smb.conf` + reload `smbd`, start RAID scrubs and run discovery commands (`lsblk`/`findmnt`/`mdadm`); it exposes a fixed allowlist of validated operations and never takes command lines from clients.

## File manager, Samba and public uploads (Phase 3/4)

- **File manager.** `app/services/files.py` is the only module that touches the managed filesystem. It resolves every requested path against one of the configured roots (`NAS_FILE_ROOTS`, default `<data_dir>/files`) with `Path.resolve()` + `is_relative_to` and refuses absolute paths, `..` traversal, null bytes and symlinks that resolve outside the root. Listing/search use `lstat` and `os.walk(followlinks=False)`; uploads stream to a `.nas-upload-*.part` temp file in the destination and are atomically renamed, so a partial upload never appears under the final name and the size cap cannot be bypassed. All mutations and downloads are audited.
- **Samba.** `app/services/samba.py` renders the database's `samba_shares` into a managed section delimited by `# ==== NAS-MANAGER MANAGED BEGIN/END ====` markers, preserving every line outside it. Before writing it validates the result with `testparm -s` against a temporary copy (mocked in tests), backs up the previous config into `NAS_SAMBA_BACKUP_DIR` and writes atomically. When `NAS_HELPER_ENABLED=true` the apply step hands the validated config to the privileged helper, which re-validates, backs up, writes atomically and reloads `smbd` (`reload_required: false`); without the helper the web process writes where it can and reports `reload_required`.
- **Disks, storage and RAID (Phase 5).** Everything privileged happens inside the helper. `GET /disks`, `GET /disks/{name}`, `GET /disks/{name}/smart`, `GET /storage/volumes` and `GET /storage/raid` are read-only discovery through the helper socket; parsing lives in `app/services/disks.py`. `POST` actions (`mount`, `unmount`, `scrub`) are two-step: a `…/preview` endpoint returns the exact plan plus a deterministic intent digest, and the confirm call must send `confirm: true` and match that digest (the helper re-derives the digest from its own re-validated parameters). Every action, including failures, is audited (`disk.mounted`, `disk.unmounted`, `disk.action_failed`, `raid.scrub_started`, `raid.action_failed`).
- **Runtime settings.** `app/services/settings_service.py` stores non-secret key/value switches in `app_settings` (e.g. `operator_view_files_samba`, `public_upload_enabled`). `app/services/security.py::effective_permissions_for_user` consults that switch, so the API guards and the web pages pick it up without separate plumbing.
- **Public upload folder.** `app/services/public_uploads.py` owns the drop folder (`NAS_PUBLIC_UPLOAD_DIR`, default `<data_dir>/uploads`) and is the only module that writes to it. It is upload-only (no public listing/download/search); a public policy endpoint exposes only the enabled flag and the per-file cap. Files land in per-uploader subdirectories named `sha256(uploader_key)[:24]`, stream to a temp file and are atomically renamed with auto-uniquified names (never overwrite; names stay ≤ 255 bytes even after the counter suffix). Usage is keyed `user:<name>` / `ip:<address>`. The quota check and reservation live in the same transaction as the file record: the uploader's `public_upload_quotas` row is locked with an `UPDATE`, re-read under the lock, and `used_bytes` incremented with the `public_upload_files` insert — serialising concurrent uploads on SQLite (WAL single writer) and PostgreSQL (row lock). Deletes and age-purging decrement/reconcile the ledger; cleanup removes stale temp files and reconciles every quota row from the remaining file records. Both `public_upload_*` tables and the `uploads:read`/`uploads:write` tokens come from migration `0004`.

## API tokens, resumable uploads and sync (Phase 6)

- **API tokens.** `app/services/api_tokens.py` mints `nas_…` tokens (SHA-256 stored in `api_tokens`, raw value returned once), validates scopes against the registry and the creator's permissions, enforces lifetime and per-user caps, and revokes. The CSRF middleware skips `Authorization: Bearer` requests; a dedicated in-process `token_throttle` limits failed lookups per IP.
- **Resumable uploads.** `app/services/resumable_uploads.py` keeps one `upload_sessions` row per in-flight upload and accumulates bytes in `.nas-resumable-<id>.part` next to the destination. `PATCH` appends at an exact offset under a per-upload lock and fsyncs before advancing; completion reuses `files.commit_staged` / `public_uploads.commit_staged`, the same commit path as single-shot uploads (jail/overwrite re-check, or quota lock + reservation).
- **Sync cursors.** `app/services/sync.py` appends a `file_changes` row for every file-manager mutation inside the mutation's own transaction; the row id is the cursor. `changes_since` pages forward per root and answers `reset` when the cursor predates the retained journal; `prune` drops rows past `NAS_SYNC_CHANGE_RETENTION_DAYS` (keeping the newest).
- Migration `0006` adds the three tables and the `api_tokens:read`/`api_tokens:write` permissions (admin both, operator read).

## Remote and cloud sync (Phase 7)

- **Two processes, one database.** The web process manages connections and jobs and *queues* runs (`sync_runs` rows). The worker (`python -m app.worker`, its own systemd unit, same unprivileged user) does every transfer. Each tick it writes a heartbeat (`app_settings.worker.heartbeat`, shown in the UI), renews the leases of the runs it executes, fails runs whose lease expired (a dead worker), queues scheduled runs for enabled jobs, and claims queued runs with a conditional `UPDATE … WHERE status='queued'` (at most one run per job, up to `NAS_WORKER_CONCURRENCY` in parallel). SIGTERM stops claiming and asks running jobs to stop after their current file.
- **Providers.** `RemoteProvider` (`app/services/remote/base.py`) exposes `list/walk`, `stat`, `download`, `upload` (temp name + rename where the protocol allows), `makedirs` and `move` (only used to archive), over POSIX paths relative to the connection's base. Every name a server returns is validated (no separators, `..`, control characters, over-long names) before it can become a local path; rejected entries are reported, never synced.
- **Engine.** `app/services/sync_engine.py` is a three-way sync: local scan (regular files, no symlinks, temp files and `.nas-sync-archive/` skipped) × remote walk × per-file baseline (`sync_job_entries`, the state after the last successful sync of that file). `plan()` is pure; `SyncExecutor` applies actions and commits the baseline and run counters after each one, so a crash or cancel leaves a consistent, resumable state. Downloads stage as `.nas-remote-*.part` next to the target, are re-checked against the jail and moved into place atomically, and are journaled in `file_changes`, so Phase 6 sync clients see them.
- **Data-safety rules.** Nothing is ever hard-deleted. One-way jobs never overwrite a changed destination (skipped and reported). Conflicts (both sides changed, or first sync with different content after an MD5/byte comparison) follow the job policy: `keep_both` (default; the other version is kept under `name (conflict <side> <timestamp>).ext`), `prefer_local` / `prefer_remote` (the losing version is moved to `.nas-sync-archive/` first). Deletions are reported (`ignore`, default) or archived on the other side (`archive`); a deletion against a modification restores the modified file. A deletion guard fails a run before any action when it would archive too much (`NAS_REMOTE_SYNC_MAX_DELETE_COUNT` / `_PERCENT`). Risky job settings need `confirm: true`; dry runs show the plan.
- **Credentials.** Non-secret settings live in `remote_connections.config`; secrets are one Fernet token per connection (`app/core/secrets.py`). Google Drive uses OAuth 2.0 authorization code + PKCE; the state is single-use, bound to the user who started it and expires after 10 minutes.
- Migration `0007` adds `remote_connections`, `sync_jobs`, `sync_job_entries`, `sync_runs`, `sync_conflicts` and the `remotes:read`/`remotes:write` permissions (admin both, operator read).

## Planned additions

Gallery (8), streaming (9), alerting (10), backups (11), config export/import (12), audit/syslog (13).
