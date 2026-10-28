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

Rules: routes validate and delegate; services hold logic and talk to the OS or database; schemas define the API contract; models define persistence. Background jobs will live in `app/jobs/` (not created yet).

## Module map

| Module | Responsibility |
|--------|----------------|
| `app/main.py` | Application factory: logging, directories, database, middleware, routers, startup checks |
| `app/__main__.py` | `python -m app` entry point (uvicorn, application-controlled logging) |
| `app/cli.py` | `python -m app.cli bootstrap-admin` (first-run administrator) |
| `app/core/config.py` | `Settings` from `NAS_*` variables (incl. sessions, throttling, Argon2id costs) |
| `app/core/logging.py` | JSON/text formatters, redaction, rotating file handler |
| `app/core/middleware.py` | Request id, access log (path only, never query string), security headers |
| `app/core/csrf.py` | CSRF protection middleware (double-submit + session-bound) |
| `app/core/errors.py` | `AppError` hierarchy and the JSON error envelope |
| `app/core/permissions.py` | Permission-token registry and default role grants |
| `app/db/` | Naming conventions, engine/session factory, migration helpers, `get_db` |
| `app/api/v1/deps.py` | `CurrentUser`, `get_current_user`, `require_permission(...)` |
| `app/api/v1/` | `auth`, `users`, `groups`, `roles`, `audit`, `health`, `system`, `files`, `samba`, `settings`, `public_uploads`, `disks` |
| `app/services/` | `passwords`, `auth_service`, `users_service`, `groups_service`, `roles_service`, `security`, `audit`, `throttle`, `bootstrap_service`, `health`, `monitoring`, `settings_service`, `files`, `samba`, `public_uploads` |
| `app/services/helper_client.py` | Thin `AF_UNIX` client for the privileged helper; maps helper errors to typed `AppError`s (503 unavailable, 400 validation/intent/operation) |
| `app/services/disks.py` | Disk/volume/RAID discovery parsing (`lsblk`, `findmnt`, `/proc/mdstat`, `smartctl`), preview + confirmed actions, audit |
| `app/helper/` | Privileged helper (root process): `operations.py` fixed allowlist, `server.py` Unix socket + `SO_PEERCRED`, `protocol.py` framing/intent digest, `__main__.py` entry |
| `app/models/` | `user`, `group`, `role`, `session`, `audit_event`, `app_setting`, `samba_share`, `public_upload` |
| `app/web/` | Jinja2 templates + modular JS (login, dashboard, users, groups, audit, profile, files, samba, settings, uploads, public_upload, disks) |

## Authentication and authorization

1. `POST /api/v1/auth/login` (or the `/login` form) verifies the password with Argon2id after a per-IP throttle check, then creates a server-side `auth_sessions` row. The database stores only SHA-256 of the token and of the CSRF token; the browser keeps the raw session token in an `HttpOnly` cookie.
2. Every request passes through `CSRFProtectMiddleware`: state-changing requests that carry the `nas_csrf` cookie must echo it (`X-CSRF-Token` header or `_csrf` form field), and authenticated requests must additionally match the digest bound to their session.
3. `get_current_user` resolves the session cookie, rejects revoked/expired sessions and deactivated users, and slides the idle expiry window. `require_permission("permission")` guards each route: 401 anonymous, 403 without the permission.
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

## Planned additions

API tokens (6), sync engine and persistent job queue (7), gallery (8), streaming (9), alerting (10), backups (11), config export/import (12), audit/syslog (13).
