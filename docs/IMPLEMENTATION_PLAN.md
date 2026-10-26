# Implementation Plan

Status legend: **Done** (implemented and verified), **Next**, **Planned**.

## Phases

| # | Phase | Status |
|---|-------|--------|
| 1 | Foundation, database, dashboard, configuration, logging, systemd deployment | **Done** |
| 2 | Authentication, users, groups, permissions, audit events | **Done** |
| 3 | File manager and Samba integration | **Done** |
| 4 | Public upload folder and quotas | **Done** |
| 5 | Disk discovery, storage and RAID management (+ privileged helper) | Planned |
| 6 | REST API, API tokens, mobile-client support | Planned |
| 7 | Cloud and remote synchronization | Planned |
| 8 | Photo gallery and personal media libraries | Planned |
| 9 | Video playback and streaming | Planned |
| 10 | System monitoring and alerting | Planned |
| 11 | Backup and restore | Planned |
| 12 | Configuration export and import | Planned |
| 13 | Syslog and comprehensive auditing | Planned |
| 14 | Security hardening, deployment testing, documentation | Planned |

## Phase 1: completed

Delivered:

- Project layout: `app/{api,core,db,models,schemas,services,web}`, `migrations/`, `deploy/`, `docs/`, `tests/`.
- Configuration via typed `NAS_*` environment variables (pydantic-settings); `.env.example` without secrets.
- Structured logging (JSON or text), request-id correlation, secret redaction, size-based file rotation, stdout for journald.
- SQLAlchemy 2 models, Alembic migrations (`0001`: `app_settings`), SQLite with WAL and foreign keys on.
- Versioned API under `/api/v1`: `GET /health`, `GET /system/summary`.
- Bootstrap 5 dashboard (vendored, no CDN) showing host, CPU, memory, storage usage and network counters.
- Uniform JSON error envelope; security headers including a strict CSP.
- Hardened systemd unit (`deploy/systemd/nas-manager.service`) and production env template (`deploy/nas-manager.env.example`).
- Pinned dependencies (`requirements.txt`, `requirements-dev.txt`).

### Verification (executed in this workspace, Python 3.13.16, Ubuntu 24.04)

| Check | Result |
|-------|--------|
| `pytest` | 77 passed |
| Coverage (`app/`) | 97 % (`app/__main__.py` is exercised only by the live run below) |
| `ruff check` / `ruff format --check` | clean |
| `mypy` (strict, `app` + `tests`) | no issues in 38 files |
| `alembic upgrade head` via CLI on a fresh directory | applied `0001`, `alembic current` shows head |
| Live server (`python -m app`) | `/api/v1/health` = ok, dashboard 200, static assets 200, `/api/docs` 404 in production mode, path traversal on `/static` = 404 |
| Non-loopback bind without acknowledgement | refused at startup |
| `systemd-analyze verify` | no directive errors; only reports `/opt/nas-manager/...` missing, which is expected in this workspace |

### Not verified

- The unit was **not started under a real systemd instance** (none available in this workspace). Run the steps in `INSTALLATION.md` on the target Ubuntu host and check `systemctl status` and `journalctl -u nas-manager`.
- The dashboard and management JavaScript were **not exercised in a browser**. Server-side checks confirm the HTML, static assets, the absence of inline script/style (CSP compliance) and the full cookie/CSRF flows (TestClient + curl); client rendering still needs a manual look.
- PostgreSQL was not tested; only the URL-driven abstraction exists.

## Phase 2: completed

Delivered:

- **Models + migration `0002`**: `users`, `groups`, `roles`, `role_permissions`, associations (`user_groups`, `user_roles`, `group_roles`), `auth_sessions`, `audit_events`. System roles (`admin`, `operator`, `viewer`) are seeded by the migration with a validated permission registry (`app/core/permissions.py`).
- **RBAC**: effective permissions = union of the user's direct role permissions and the permissions of roles granted to the user's groups. `require_permission("...")` FastAPI dependency (401 anonymous / 403 lacking permission).
- **Sessions**: server-side rows keyed by the SHA-256 of an opaque cookie token; hard TTL + idle ceiling with sliding expiry; logout and admin reset revoke sessions (also on deactivation/delete via FK cascade).
- **Passwords**: Argon2id (`argon2-cffi`, costs from settings), policy (length 12–128, not equal to username), timing equalisation for unknown usernames, never stored or logged in plaintext.
- **Throttling**: per-account lockout (persistent, `failed_login_count` + `locked_until`) and per-IP sliding-window limiter (`429` with `Retry-After`, audit event).
- **CSRF**: double-submit cookie + session-bound token digest; first-page-load regression covered; cookie-bound form/JS flows for both web forms and the JSON API.
- **Audit**: append-only `audit_events` with actor, outcome, target, IP, user agent and request id; searchable/paginated `GET /api/v1/audit/events` (`audit:read`).
- **API** (`/api/v1`): `auth` (login/logout/me/change-password), `users` CRUD + roles/active/unlock/reset-password, `groups` CRUD + members + roles, `roles` CRUD + permissions. Web UI: login, dashboard (now protected), users, groups, audit, profile pages with modular JavaScript.
- **First-run bootstrap**: `python -m app.cli bootstrap-admin` (interactive or `--password-stdin`); never reads passwords from argv or environment; idempotent with `--reset-password`.
- **Binding guard removed**: authentication exists now, so `NAS_ALLOW_UNAUTHENTICATED_BIND` is gone and any interface may be bound; the session cookie must be marked Secure when TLS terminates in front (startup warning otherwise).

### Verification (executed in this workspace, Python 3.12.3, Ubuntu 24.04)

| Check | Result |
|-------|--------|
| `pytest` | 165 passed |
| Coverage (`app/`) | 90 % |
| `ruff check` / `ruff format --check` | clean |
| `mypy` (strict, `app` + `tests`) | no issues in 77 source files |
| `alembic upgrade head` on a fresh directory | applies `0001` + `0002`; roles seeded; `alembic current` shows head |
| Live server (`python -m app`) | `/api/v1/health` public 200; anonymous `/` → 302 `/login`; login page embeds a CSRF token matching its `nas_csrf` cookie; form login → 303; dashboard/users/groups/audit pages 200; `/api/v1/auth/me` returns identity + permissions; API create-user with CSRF → 201; logout → 204 then `/me` → 401; POST without CSRF → 403 |
| Lockout/throttling | account locks after N failures (audited), locked login refused, expiry unlocks, per-IP window returns 429 |
| Last-admin safety | cannot delete/deactivate self, cannot demote the final administrator, deleting a group that is the sole vehicle of admin power is refused |

### Not verified

- The web UI interaction was verified through TestClient and curl only — no real browser run.
- Only a single-process deployment was exercised; multi-worker deployments need shared session storage (the token is stored in SQLite, so multiple processes on one host still share it).
- No remote access through a reverse proxy/TLS was tested end-to-end (cookie `Secure` flag is config-only).

## Phase 3: completed

Delivered:

- **File manager with a path jail.** `app/services/files.py` resolves every requested relative path against a configured managed root and refuses absolute paths, `..` traversal, null bytes and symlinks that resolve outside the root (`Path.resolve()` + `is_relative_to`). Listing never follows symlinks; uploads stream to a temporary file in the destination directory and are atomically moved into place, so a partial upload never appears under the final name and the size cap cannot be bypassed.
- **File operations + API** (`files:read` / `files:write`): `GET /files/roots`, `GET /files/list`, `GET /files/content` (download), `GET /files/search`, `POST /files/upload` (multipart), `POST /files/mkdir`, `PATCH /files/rename` (same-root only), `DELETE /files` (empty directories without `recursive`; non-empty only with `recursive=true`). Every mutation and download is audited (`file.uploaded/downloaded/deleted/renamed/created_dir`).
- **Configuration**: `NAS_FILE_ROOTS` (managed roots, default `<data_dir>/files`), `NAS_MAX_UPLOAD_BYTES`, `NAS_FILE_SEARCH_LIMIT`.
- **Samba share model + migration `0003`** (`samba_shares`): lower-cased unique name, absolute existing path, comment, read_only, browseable, guest_ok, valid_users/write_list (single-line, so no config injection), enabled.
- **Samba service** (`app/services/samba.py`): renders shares into a managed section delimited by `# ==== NAS-MANAGER MANAGED BEGIN/END ====` markers, preserving everything outside; validates with `testparm -s` against a temporary copy (mocked in tests) before writing; backs up the previous config (`smb.conf.<ts>.bak`); writes atomically. Reloading `smbd` is deliberately deferred to the Phase 5 privileged helper — the module reports `reload_required` and never executes privileged commands.
- **Samba API** (`samba:read` / `samba:write`): share CRUD, `GET /samba/status`, `POST /samba/apply`. Audited (`samba.share.created/updated/deleted`, `samba.config.applied`).
- **Runtime settings + operator view toggle.** A Settings page/API (`settings:read` / `settings:write`, audited `settings.updated`) exposes `operator_view_files_samba` (stored in `app_settings`). Default off: only admin (or custom roles granted `files:*` / `samba:*`) can open Files/Samba. When on, holders of the `operator` role gain `files:read` + `samba:read` **only** (view, never write). The toggle is honoured inside `effective_permissions_for_user`, so API guards and web pages pick it up automatically, and admin-power checks (which query `role_permissions` directly) are unaffected.
- **Web UI**: Files page (root selector, breadcrumbs, upload, new folder, rename, delete, download, search), Samba page (status card, share create/edit form, Apply), Settings page (the toggle). Nav links are permission-gated; all markup stays CSP-compatible (no inline script/style).
- **Errors**: new `ConfigurationError` (500) for Samba operational failures (config path not writable, `testparm` missing or timing out).

### Verification (executed in this workspace)

| Check | Result |
|-------|--------|
| `pytest` | 212 passed |
| `ruff check` / `ruff format --check` | clean |
| `mypy` (strict, `app` + `tests`) | no issues in 89 source files |
| `alembic upgrade head` on a fresh directory | applies `0001` + `0002` + `0003`; drift test finds no differences |
| File manager | traversal/absolute/encoded/symlink-escape all rejected; upload/download round-trip; overwrite refusal; size cap enforced with no temp leftover; mkdir/rename/delete (empty + recursive) flows; bounded case-insensitive search; audit rows written |
| Samba | CRUD + name/path/newline validation; apply preserves unmanaged config and replaces only the managed range; invalid config refused (nothing written or backed up); backups created before writes; reload never invoked; status reports `reload_required` |
| Operator toggle | default off → operator 403/401; admin flips on → read 200, writes still 403; group-carried operator role honoured; viewer cannot flip; change audited |

### Not verified

- The web UI (Files/Samba/Settings pages) was verified through TestClient only — no real browser run.
- No real Samba installation was exercised: `testparm` is mocked in tests and apply writes to disposable temp paths. On a target host, confirm the rendered config is accepted by the installed `testparm` and that `smbd` picks up the changes after the operator applies them (until the Phase 5 helper provides the reload).

## Phase 4: completed

Delivered:

- **Public upload folder** (`app/services/public_uploads.py`). A dedicated drop folder (config `NAS_PUBLIC_UPLOAD_DIR`, default `<data_dir>/uploads`) that unauthenticated clients can write to. It is strictly *upload-only*: there is no public listing, download or search endpoint. Files land in per-uploader subdirectories named `sha256(uploader_key)[:24]` (collision-safe, no key leakage); uploads stream to a `.nas-upload-*.part` temp file and are atomically renamed, colliding names are auto-uniquified (`file (1).ext`) so nothing is ever overwritten, and filenames are sanitized with the same rules as the file manager (no separators, no `.`/`..`, no null bytes, ≤ 255 bytes).
- **Strict per-uploader quotas, atomic against concurrent uploads.** Every upload counts against `user:<username>` (when the request carries a valid session) or `ip:<client address>` (anonymous). The quota check and the reservation happen inside the same database transaction that records the accepted file: the uploader's `public_upload_quotas` row is locked (`UPDATE`), re-read under the lock, and `used_bytes` is incremented in the same commit as the file's row — on SQLite (WAL single writer) and PostgreSQL (row lock) a concurrent upload cannot interleave past the check. Rejections return `413 quota_exceeded` and roll the reservation back (no quota consumed, no temp file left), and are recorded as `public_upload.rejected` failure audit events.
- **Migration `0004`** (`public_upload_quotas`, `public_upload_files`) and two new permission tokens `uploads:read` / `uploads:write`. The migration grants admin read+write and operator read-only; custom roles may be granted the tokens through the roles API.
- **API** — public: `GET /public/uploads` (policy: enabled + max file size, no file data) and `POST /public/uploads` (multipart upload, no auth). Admin (`uploads:read`/`uploads:write`): `GET /public/uploads/status` (per-uploader usage, over-quota flags), `GET /public/uploads/files` (records, filterable per uploader), `PATCH /public/uploads/quotas/{key}` (set quota; lowering below use blocks new uploads without touching files), `DELETE /public/uploads/files/{id}` (single file; decrements usage; audited), `POST /public/uploads/cleanup` (stale temp files, optional purge of files older than N days, then reconciles every usage ledger from the remaining file records). All mutations audited under category `public_upload`.
- **Runtime master switch.** The Settings page/API gained `public_upload_enabled`, which overrides `NAS_PUBLIC_UPLOAD_ENABLED` once flipped; disabled uploads return 403 and the public page shows the state.
- **Web UI**: `/public/upload` — an anonymous drag-and-drop/click page (upload-only, no listing) that stays fully CSP-compatible; `/uploads` — the admin page with folder status, per-uploader quota editing with a usage progress bar, over-quota flags, file records with per-file delete, temp cleanup and age-based purge. Nav link is permission-gated.

### Verification (executed in this workspace)

| Check | Result |
|-------|--------|
| `pytest` | 235 passed (230 pre-existing + 23 new/moved in this phase) |
| `ruff check` / `ruff format --check` | clean |
| `mypy` (strict, `app` + `tests`) | no issues |
| `alembic upgrade head` on a fresh directory | applies `0001`–`0004`; drift test finds no differences; downgrade drops the new tables and revokes the granted tokens |
| Public uploads | anonymous round-trip (201), user/IP keying, sanitization + traversal rejection, auto-uniquified colliding names, `GET` policy without auth, no anonymous status/listing (401) |
| Quotas | over-quota upload → 413, usage unchanged and no file/temp residue, failure audit written; exact-fit upload succeeds; admin lowering quota below use flags `over_quota` and blocks new uploads |
| Caps | per-file cap enforced mid-stream (400) with no temp leftover |
| Admin | status totals + per-uploader summaries; files page + per-uploader filter; single-file delete removes disk + decrements ledger; cleanup removes temps, purges old files, reconciles usage (counts returned and audited) |
| Permissions | anonymous → 401 on all admin routes; operator read-only (status/files 200, quota/delete/cleanup 403); admin full access |
| Settings toggle | default on; PUT off → 403 for uploads and policy reports disabled; audited (`public_upload.toggle_changed`) |

### Not verified

- The public upload page (`/public/upload`) and the Uploads admin page were exercised via TestClient and curl only — no real browser run (drag-and-drop, progress bar styling, purge form).
- The concurrency guarantee is enforced by the row-lock design and documented, but a true multi-process race was not hammered in CI; the single-writer SQLite path is covered by the transaction tests.
- Anonymous quotas are keyed by client IP, which proxies/NAT can coalesce and botnets can fragment; API-token keying arrives with Phase 6.

## Design decisions

1. **No CDN.** Bootstrap 5.3.8 is vendored under `app/web/static/vendor/bootstrap/` so a NAS works offline and the CSP can stay `default-src 'self'`.
2. **Migrations are explicit.** `NAS_AUTO_MIGRATE` defaults to false; the systemd unit runs `alembic upgrade head` in `ExecStartPre`. `/health` reports `degraded` when the schema is behind.
3. **Health semantics.** 200 `ok`, 200 `degraded` (schema behind), 503 `unhealthy` (database unreachable).
4. **Revision lookup is a direct query** of `alembic_version`, not `MigrationContext`, to keep health checks cheap and silent.
5. **Docs endpoints** (`/api/docs`, `/api/openapi.json`) exist only when `NAS_DEBUG=true`.
6. **Privileged operations** will use a separate allowlisted helper service (Phase 5). The web unit drops all capabilities and must never gain them.
7. **Sessions are server-side.** The database stores only SHA-256 of the token, so a DB leak cannot be replayed; revocation (logout, password reset, deactivation) actually kills sessions. The browser cookie is `HttpOnly`.
8. **CSRF is double-submit + session-bound.** The `nas_csrf` cookie must be echoed (header for fetch, `_csrf` for forms) and, for authenticated requests, must match the digest stored with the session. Cookie-less API clients are not blocked (they cannot be CSRF-attacked).
9. **RBAC is role-based with groups.** Effective permissions are the union of direct roles and the roles of the user's groups; the registry in `app/core/permissions.py` is the single source of truth and rejects unknown tokens.
10. **Last-administrator protection.** Deleting/deactivating yourself, demoting the final administrator, or removing a group/role that is the sole vehicle of admin power is refused (409).
11. **Usernames are normalised to lowercase** (validated against `[a-z0-9][a-z0-9._-]*`); uniqueness is case-insensitive. The username is immutable after creation.
12. **Bootstrap is CLI-only.** The web application has no endpoint that creates the first administrator; `python -m app.cli bootstrap-admin` reads the password from a prompt or stdin only.
13. **The file manager is jailed to configured roots.** Every relative path is resolved and re-checked with `is_relative_to`; uploads land via a temp file + atomic rename so quota/cap checks happen before the final name exists. File roots are config (`NAS_FILE_ROOTS`), defaulting to `<data_dir>/files`.
14. **Samba config is merged, not rewritten.** Only the range between the managed markers is touched; everything else is preserved verbatim, the result is validated with `testparm -s` against a temp copy, the previous file is backed up, and the write is atomic. Reloading `smbd` is a privileged step deferred to the Phase 5 helper; status reports `reload_required` rather than pretending it happened.
15. **Least-privilege default with a runtime relaxation.** `operator`/`viewer` cannot open Files/Samba. An admin may flip `operator_view_files_samba` in Settings to grant operators read-only access; writes are never granted by the toggle.
16. **The public drop folder is upload-only by design.** There is no public listing/download/search; a lightweight public policy endpoint exposes only whether uploads are enabled and the per-file cap. Quotas are keyed per uploader (`user:` when a session is present, `ip:` otherwise) and the quota check/reservation is one database transaction with the file's record (row lock → re-read → check → commit), so concurrent uploads cannot bypass a quota. Anonymous IP keying is a Phase 4 pragmatic default; token-keyed quotas arrive with Phase 6.
17. **The drop folder never overwrites.** Colliding names are auto-uniquified (`photo (1).jpg`), the response returns the final stored name, and the byte-length-safe uniquifier keeps names within the 255-byte filesystem limit even after the counter suffix is appended.
18. **Storage is the ledger, cleanup reconciles it.** `public_upload_files` records every accepted file; `public_upload_quotas.used_bytes` is incremented/decremented in the same transactions that insert/delete file rows. The admin cleanup endpoint removes stale temp files, optionally purges files older than N days (rows + disk, audited) and then recomputes every quota row from the remaining records, so out-of-band drift heals on demand.

## Known limitations

- The management UI was exercised via TestClient and curl, not a real browser.
- Only SQLite has been exercised; PostgreSQL support is configuration-only so far.
- `instance_id` is stored but not yet used by any feature.
- The per-IP login limiter is in-process (single process; resets on restart). The account lockout is persistent in the database.
- Sessions are not yet exposed through the API for mobile clients (that is Phase 6 with tokens/resumable transfers).
- Monitoring is limited to CPU, memory, swap, two disk-usage entries and network byte counters. No processes, services, RAID or per-disk data (Phase 5 and 10).
- No background job framework yet (needed from Phase 7 onward).
- `smbd` reload is not performed by Phase 3: the web process is unprivileged and the privileged helper arrives in Phase 5. The app writes and validates the config and reports `reload_required`.
- The file manager has no per-share or per-user folder permissions yet (that lands with quotas in Phase 4 and disks in Phase 5); access is governed by the `files:*`/`samba:*` permissions and the operator toggle.
- Anonymous public-upload quotas are keyed by client IP (`ip:`) — NAT/proxies coalesce addresses and botnets can fragment them; per-token quotas arrive with the Phase 6 API tokens. Uploads are also served on whatever interface the app binds; a public deployment must put the drop page behind TLS and (optionally) a rate limit.

## Next: Phase 5 (outline)

Disk discovery, storage and RAID management through the privileged helper: read-only discovery of physical disks, volumes, filesystems and RAID arrays; a fixed allowlist of validated operations (mount/unmount with explicit confirmation, scrub, SMART status); the helper also takes over the deferred `smbd` reload from Phase 3/4.
