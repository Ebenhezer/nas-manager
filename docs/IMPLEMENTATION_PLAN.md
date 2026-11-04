# Implementation Plan

Status legend: **Done** (implemented and verified), **Next**, **Planned**.

## Phases

| # | Phase | Status |
|---|-------|--------|
| 1 | Foundation, database, dashboard, configuration, logging, systemd deployment | **Done** |
| 2 | Authentication, users, groups, permissions, audit events | **Done** |
| 3 | File manager and Samba integration | **Done** |
| 4 | Public upload folder and quotas | **Done** |
| 5 | Disk discovery, storage and RAID management (+ privileged helper) | **Done** |
| 6 | REST API, API tokens, mobile-client support | **Done** |
| 7 | Cloud and remote synchronization (+ sync worker) | **Done** |
| 8 | Photo gallery and personal media libraries | **Done** |
| 9 | Video playback and streaming | **Done** |
| 10 | System monitoring and alerting | **Done** |
| 11 | Backup and restore | **Done** |
| 12 | Configuration export and import | **Done** |
| 13 | Syslog and comprehensive auditing | **Done** |
| 14 | Security hardening, deployment testing, documentation | **Done** |

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
- Anonymous quotas are keyed by client IP, which proxies/NAT can coalesce and botnets can fragment; API-token clients are keyed `token:<id>` since Phase 6.

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
16. **The public drop folder is upload-only by design.** There is no public listing/download/search; a lightweight public policy endpoint exposes only whether uploads are enabled and the per-file cap. Quotas are keyed per uploader (`user:` when a session is present, `ip:` otherwise) and the quota check/reservation is one database transaction with the file's record (row lock → re-read → check → commit), so concurrent uploads cannot bypass a quota. Anonymous IP keying is a Phase 4 pragmatic default; API-token clients are keyed `token:<id>` since Phase 6.
17. **The drop folder never overwrites.** Colliding names are auto-uniquified (`photo (1).jpg`), the response returns the final stored name, and the byte-length-safe uniquifier keeps names within the 255-byte filesystem limit even after the counter suffix is appended.
18. **Storage is the ledger, cleanup reconciles it.** `public_upload_files` records every accepted file; `public_upload_quotas.used_bytes` is incremented/decremented in the same transactions that insert/delete file rows. The admin cleanup endpoint removes stale temp files, optionally purges files older than N days (rows + disk, audited) and then recomputes every quota row from the remaining records, so out-of-band drift heals on demand.
19. **API tokens are scoped and never exceed their owner.** Effective access is `scopes ∩ effective_permissions(owner)`, evaluated per request. Only SHA-256 is stored; the raw `nas_…` value is shown once. Tokens expire by default (`NAS_API_TOKEN_MAX_LIFETIME_DAYS`; `0` allows non-expiring tokens) and are revoked on admin password reset and deactivation, but not when users change their own password.
20. **An `Authorization` header is authoritative.** When present it must be a valid bearer token (else 401 + `WWW-Authenticate`), and cookies are ignored for that request, which is why bearer requests skip CSRF. Token management, password change and logout are session-only (`require_session_auth`), so a leaked token cannot entrench itself. HTML pages stay cookie-only.
21. **Resumable uploads reuse the single-shot commit path.** Bytes accumulate next to the destination (`.nas-resumable-<id>.part`, hidden from listings and spared by the drop-folder cleanup), every chunk is offset-checked and fsync'ed before the offset advances, and completion calls the same `commit_staged` helpers as multipart uploads, so the jail, overwrite and quota-lock guarantees are identical. The quota check at creation is advisory; the authoritative one runs at completion.
22. **Sync is a journal, not a scanner.** Every file-manager mutation appends a `file_changes` row inside its own transaction; the id is the cursor. A cursor older than the retained journal (or from another database) gets `reset: true` and the client re-lists. Out-of-band changes (Samba, the shell) are deliberately out of scope for this phase.

23. **Transfers run in a separate worker, not in the web process.** `python -m app.worker` (own systemd unit, same unprivileged user) claims queued runs with a conditional `UPDATE`, holds a lease that its main loop renews every tick, and reclaims runs whose worker died. The web process only records jobs, queued runs and cancel flags, so uvicorn workers never execute a sync and a long transfer never ties up a request.
24. **Three-way sync with a per-file baseline; nothing is hard-deleted.** Each file's state after its last successful sync (`sync_job_entries`) decides who changed. One-way jobs never overwrite a changed destination; conflicts keep both versions by default (`prefer_*` archive the loser first); deletions are reported or archived into `.nas-sync-archive/`, never propagated as deletes; a deletion against a modification restores the file. The baseline is committed after every action, so crashes and cancellations resume cleanly.
25. **Identical files are adopted, not duplicated.** When both sides have a file without a baseline (or both changed), content is compared first: MD5 when the provider reports one (S3 single-part ETag, Drive `md5Checksum`), otherwise by downloading to a temp file and comparing bytes. Only real differences become conflicts.
26. **A missing remote folder is an error once synced.** A first sync into a folder that does not exist yet creates it; a folder that disappears after files were synced fails the run instead of looking like a mass deletion (an unmounted or renamed remote is the likely cause). The deletion guard (`NAS_REMOTE_SYNC_MAX_DELETE_COUNT` / `_PERCENT`, percentage only above 5 archives) covers the remaining "everything vanished" cases.
27. **Credentials: Fernet with a key file; secrets never leave the service layer.** One encrypted blob per connection, a 0600 key file outside the database, API responses that only name the set fields, PATCH that keeps omitted secrets, and session-only credential endpoints. Key rotation is deferred.
28. **Trust is explicit.** SFTP host keys are pinned (probe → administrator confirms the fingerprint → `RejectPolicy`); plain FTP and plain HTTP need `allow_insecure`; Google Drive uses authorization code + PKCE with a single-use, user-bound, 10-minute state. Risky job settings (enabling, two-way, archive, `prefer_*`, overriding the deletion guard) need `confirm: true`, and the 400 response lists the risks so the UI can show them.
29. **Providers are thin and testable.** One `RemoteProvider` interface; WebDAV and Drive use `httpx2` directly (no Google client libraries) so tests can drive them with `MockTransport`; S3 uses boto3 (tested with botocore's `Stubber`); SFTP is tested against a real paramiko SFTP server over loopback.

30. **Personal libraries are private, shared libraries are granted.** Each user's library is `<NAS_MEDIA_DIR>/<username>` and only its owner browses it — administrators manage the record (counts, scans) but cannot open other users' photos. Shared libraries are registered over existing file-root folders, granted per group, and read-only through the gallery (they are maintained with the file manager, Samba or remote sync).
31. **Not visible = not found.** Every item, thumbnail, original, stream, transcode and album lookup goes through one SQL clause (`browsable_clause`), and anything outside it is a 404, so IDs cannot be probed. Albums are private and silently drop items whose library access was revoked.
32. **The index is derived; the files are the truth.** Scans never follow symlinks, skip hidden entries and only re-read changed files; removing a row never touches a file. A missing folder, an interrupted walk, unreadable directories or an empty folder where ≥ 10 items were indexed (an unmounted disk) remove nothing.
33. **Caches are keyed by content fingerprint.** Thumbnails and transcodes carry a hash of the source's size and mtime in their file name, so a changed file can never be served a stale thumbnail or rendition; orphans are pruned hourly and the transcode cache is evicted least recently streamed first.
34. **Phone backup is hash-first.** Clients ask `POST /media/sync/lookup` which SHA-256 digests exist and upload only the rest (multipart or resumable `media` target); uploads never overwrite (hard-link placement, collision suffixes) and journal into `media_changes`, which pages like the Phase 6 file journal.
35. **ffmpeg is quarantined in one module.** Fixed argument lists, no shell, `file:` inputs/outputs, minimal environment, timeouts and kill-on-stop; ffprobe runs on upload (web) and scan (worker), posters and transcodes only in the worker on their own single-thread pools at `Nice=10`. Without ffmpeg, videos still index and download.
36. **Progressive MP4, not HLS.** Browser-compatible originals stream as they are with HTTP Range; everything else gets one cached H.264/AAC `+faststart` MP4, which `<video>` can seek with Range as well. Adaptive bitrate streaming was judged not worth its complexity and storage for a home NAS.

37. **The worker observes; the web process reads.** Samples, service/RAID/SMART checks, rule evaluation and notification delivery all run in the worker, each on its own single-thread pool, so a slow `systemctl`, SMART read or SMTP server never delays a tick. Pages read stored snapshots (`monitor_checks`, `metric_samples`). Only cheap, unprivileged values (the live summary, processes, `/proc/mdstat`) are read per request. The single exception is the channel "Test" button, which sends synchronously so the administrator sees the result.
38. **No new root operations for monitoring.** Service state comes from unprivileged `systemctl show`, RAID from the world-readable `/proc/mdstat`, and SMART from the helper's existing `disk.list`/`disk.smart`. The helper allowlist is unchanged.
39. **Alerts are a small state machine with hysteresis.** The engine is a pure function (rules × observations × open alerts → transitions). The rules are: pending until `duration_seconds`, then firing; a pending alert that clears is dropped; a firing threshold alert resolves only 5 % past the threshold; an unobserved target is kept for an hour; `unknown` checks never open or resolve anything. Messages describe the breach and are frozen on resolution.
40. **Notifications go through an outbox.** A firing or resolution queues one row per matching channel (`min_severity`, `send_resolved`). Delivery retries with exponential backoff, capped at one hour, up to `NAS_ALERT_NOTIFY_MAX_ATTEMPTS`, and the final failure is audited. Disabling a rule closes its alerts silently. A threshold or target change is left to the next evaluation, which resolves and notifies.
41. **Reuse `system:read` / `system:write`.** These tokens already existed (admin both, operator read) and were unused, so Phase 10 needed no permission migration. Channel credentials follow the Phase 7 rules: one Fernet token, names-only responses and audit details, session-only create/update/test.
42. **No command lines in the process list.** They routinely carry secrets, and the list is visible to operators.
43. **A dead worker is reported, not notified.** The web process adds a synthetic `worker_offline` entry to the alert summary from the heartbeat. Notifying about the worker's own death would need an independent watchdog, which is out of scope.

44. **Backups are snapshots over a content-addressed store, encrypted on the NAS.** A repository is self-describing (`repo.json`, sealed manifests, sealed blobs), so it can be read without the database (`backup-recover`). Identical content is stored once; unchanged files are not even re-read. No external backup tool (restic/borg) was adopted: their binaries would be a new runtime dependency outside the pinned Python set and could not reuse the provider layer (SFTP, FTP, WebDAV, S3, Drive) and its pinned-host-key and TLS rules.
45. **Crypto from the already-pinned `cryptography` package.** scrypt → HKDF → AES-256-GCM in authenticated 4 MiB segments; blob names are keyed HMACs. The passphrase is required (no unencrypted repositories) and stored Fernet-encrypted so schedules can run; it is never returned, logged or audited.
46. **`delete` exists, but only inside repositories.** Retention needs it. It is a provider method that sync never calls; garbage collection refuses to run unless every snapshot is readable; deleting a repository or job in the app never touches the target.
47. **A repository is never created implicitly.** Only *Initialize* creates one, only in an empty or new folder; an existing repository is adopted when the passphrase unlocks it. A missing `repo.json` fails a run, so an unmounted disk cannot silently receive backups on the root filesystem.
48. **Restores never delete and never overwrite by default.** New folder by default; `overwrite` needs `confirm` and moves old versions to `.nas-backup-replaced/`. Every file is verified against its SHA-256 before an atomic rename.
49. **The live database is never replaced by the running app.** System snapshots are staged with checksums and installed by a CLI command with the services stopped, keeping the previous files.
50. **One run per repository; one backup-related run per worker.** The claim `UPDATE` requires no other running run on the same repository, so garbage collection can never race a backup still uploading blobs. Backups get their own single-thread pool so they never take sync slots.
51. **Pull backups store into local repositories only.** "Bidirectional" in the master plan is push (NAS → target) plus pull (remote → NAS); a pull into another remote would make the NAS a mere relay.
52. **Configuration documents use natural keys and are self-contained.** References are by name, so a document can be applied to another installation. Only plain configuration is exported; runtime state and user content (albums, personal libraries, history) are not.
53. **Secrets travel only sealed.** Without a passphrase an export has no secrets at all; with one they are sealed with the backup crypto together with a digest of the sections, which doubles as tamper detection.
54. **Import is one transaction that reuses validators, not services.** The per-object services commit on their own, so the importer writes rows directly after running the same validators. The preview is the import rolled back, so it cannot disagree with the real run.
55. **Rollback is logical, not a file swap.** The running application never replaces its own database. Each import stores an encrypted pre-import snapshot; rollback deletes what the import created and re-applies the snapshot in one transaction. A file copy of the database is kept as well, for manual recovery.
56. **Imports never delete accounts or data-bearing objects.** `replace` deletes only plain configuration; users, roles, connections, repositories and libraries must be removed deliberately in the UI. Missing secrets downgrade gracefully (inactive users, credentials to re-enter, skipped repositories) instead of failing.
57. **`smb.conf` is not written by an import.** The deviation from the plan (apply Samba after commit) keeps one write path for `smb.conf`, the Samba page, with `testparm`, a backup and the helper. The import result reports `samba_apply_required`.

58. **Audit events are forwarded from the table, not from the log stream.** The worker sends each destination the events above its cursor and moves the cursor after every accepted batch, so an outage delays events instead of losing them. Application logs, which have no table, are forwarded best effort through a bounded in-memory queue that never blocks a request.
59. **Syslog destinations live in the database.** They are managed on the Logging page like alert channels; every change bumps a version that the worker polls, so nobody has to edit the environment file or restart. They carry no secrets, so API tokens with `system:write` may manage them.
60. **TLS by default, plaintext by confirmation.** TLS always verifies the collector (system store or a per-destination CA); UDP/TCP use the Phase 7 `confirmation_required` pattern.
61. **A hash chain sealed by the worker, not at insert time.** Chaining inside each request would serialise every writer on the previous hash. The worker seals in id order instead; an unsealed event below the sealed head is reported. The canonical form leaves out `actor_user_id` (`ON DELETE SET NULL`). Anchor and head in `app_settings` make pruning and tail truncation verifiable.
62. **Retention only removes what is safe to remove.** Pruning deletes a contiguous prefix of events that are past retention, sealed and forwarded to every enabled destination, and always keeps the newest event, because SQLite reuses the ids of deleted rows at the end of a table. The default retention is 90 days (user decision).
63. **Logs only see committed events.** Audit log lines are emitted from the session's `after_commit` hook, so a refused or rolled-back action never appears in journald or the log file.
64. **Native journald without new dependencies.** The journal's datagram protocol is a few lines of stdlib code. With `NAS_LOG_JOURNALD=auto` the console copy is dropped when the native handler is active, so lines are not duplicated.
65. **Coverage is a test, not a convention.** `tests/test_audit_coverage.py` lists every state-changing route with its event(s) or an exemption reason, so a new unaudited route fails the suite. The sweep found one gap, Google Drive `oauth/start`, now audited as `remote.oauth_started`.
66. **Admission before parsing.** FastAPI reads a multipart body before dependencies run, so the drop endpoint's rate limit, declared-size cap and quota pre-check live in a middleware that sees only headers. The route's streaming cap and the locked quota check stay authoritative; the middleware only refuses early.
67. **Rate limits are per uploader, in-process.** The drop limit uses the same key as the quota (`ip:`, `user:`, `token:`), so an authenticated user behind a shared NAT address keeps their own allowance. Like the login and bearer limiters, it is a per-process sliding window with a capped key table; the nginx example adds a per-address limit that survives restarts of the app.
68. **Trust is explicit.** The proxy list (`NAS_FORWARDED_ALLOW_IPS`) is passed to uvicorn rather than inherited from its defaults, an empty list disables proxy headers, and Host validation and HSTS are opt-in settings that the production template turns on. HSTS is sent only over HTTPS so a plain-HTTP LAN install cannot pin itself by accident.
69. **The helper lives in the host mount namespace.** Mount-namespace sandboxing (`PrivateTmp`, `ProtectHome`, `ProtectSystem`, ...) made a mount performed by the helper invisible to the host in the live test (container root with private propagation). A mount helper whose mounts others cannot see is broken, so the helper trades those directives for the ones that do not create a namespace (`systemd-analyze security` 4.9 → 4.3 nonetheless).
70. **The CLI runs through systemd-run.** `deploy/nas-manager-cli` gives `python -m app.cli` exactly the units' account, environment file, paths and umask, which a `sudo -u` invocation cannot (the old instruction acted on a relative `data/` directory).
71. **Own password changes stay as they are** (decision 19, re-confirmed by the user in Phase 14): other sessions and tokens survive; administrators' resets revoke both.

## Known limitations

- The management UI (including the gallery and video player) was exercised via TestClient and curl, not a real browser.
- Only SQLite has been exercised; PostgreSQL support is configuration-only so far.
- `instance_id` is stored but not yet used by any feature.
- The per-IP login limiter and the drop-folder rate limit (Phase 14) are in-process (single process; reset on restart). The account lockout is persistent in the database.
- The bearer-failure throttle and the per-upload resumable lock are in-process; a multi-process deployment needs shared state for both.
- The sync journal records only changes made through the application; edits over Samba or directly on disk are not journaled (clients should re-list periodically).
- Auditing and syslog (Phase 13): plain syslog has no delivery acknowledgement (RELP is not supported), so a collector that crashes mid-batch can lose that batch; mutual-TLS client certificates are not supported; application-log forwarding is best effort and drops records when its queue is full (counted on the Logging page); a destination that keeps failing holds back audit pruning; the hash chain is tamper-evident, not tamper-proof (a database writer can recompute it); on PostgreSQL, transactions that commit out of id order can show up as "unsealed event inside the sealed range"; audit events written while the worker is down are sealed and forwarded when it starts again; `POST /media/sync/lookup` may create a user's personal library row without an audit event.
- Configuration import (Phase 12): only the most recent import can be rolled back, and changes made after it to the same objects are overwritten by the rollback; `replace` mode never deletes users, roles, connections, repositories or libraries; documents from other app versions are refused (format version 1 only); the import does not apply `smb.conf` (use the Samba page); personal media libraries and albums are not part of the configuration.
- Backups (Phase 11): SQLite only for system-state jobs; no bandwidth limit, partial-file resume or content compression; no filesystem snapshots (files changing during a backup may be captured mid-write); changing a passphrase in the app does not re-key the repository; no built-in alert rule for verify failures (audited and shown on the Backups page); one backup-related run at a time per worker; snapshots of deleted jobs are never pruned automatically; browsing a snapshot downloads and decrypts its manifest in the web process (capped listing).
- Monitoring (Phase 10): a stopped worker cannot notify, including about itself; the UI shows it. History is sampled, by default once a minute, and kept 7 days, so there is no per-second resolution or long-term trending. Disk usage is sampled for the data directory and the managed file roots only (other mounts appear in Disks). Process CPU % is measured between page refreshes. SMART polling needs the helper. Only email and webhook channels exist (no SMS or push app). Alert rules are global, with no per-user subscriptions or quiet hours. Deleting a channel drops notifications still queued for it.
- The sync worker is a single process (`NAS_WORKER_CONCURRENCY` threads); several workers on one database are safe (atomic claims, leases) but were only exercised as one process. Resumable-upload expiry and file-journal pruning still run on request; media journal and cache pruning run in the worker.
- Remote sync limitations (Phase 7): empty directories are not synced; renames are seen as a new file plus a deletion (so with `delete_policy=ignore` the old name stays on the other side); files changed outside the sync between scan and transfer are skipped until the next run; FTP needs `MLSD`; Google Drive skips Google-native documents and duplicate names; there is no bandwidth limit, no per-file size cap and no partial-file resume for remote transfers (an interrupted file restarts); the credentials key cannot be rotated yet.
- `smbd` reload is performed by the Phase 5 helper when `NAS_HELPER_ENABLED=true`; without the helper the web process still writes where it can and reports `reload_required`.
- The file manager has no per-share or per-user folder permissions yet; access is governed by the `files:*`/`samba:*` permissions and the operator toggle. Only the helper can mount/umount disks; there is no fstab persistence or auto-mount yet.
- Media (Phases 8–9): HEIC/HEIF photos are indexed and downloadable but get no thumbnail (Pillow has no HEIF decoder here); albums cannot be shared; personal libraries have no quota; EXIF GPS is shown to everyone who can browse a library and is not stripped from downloads; changes made outside the gallery (file manager, Samba, remote sync) appear only after the next scan (`NAS_MEDIA_SCAN_INTERVAL_MINUTES`); `taken_at` from EXIF is camera wall-clock time without a zone. Video: one progressive MP4 rendition (no HLS/adaptive bitrate, no subtitles or multiple audio tracks), one transcode at a time per worker, and transcode progress is reported only when the duration is known.
- Anonymous public-upload quotas are keyed by client IP (`ip:`) — NAT/proxies coalesce addresses and botnets can fragment them; API-token clients get their own `token:<id>` quota (Phase 6). The drop endpoint is rate limited per uploader since Phase 14 (in-process; the nginx example adds a per-address limit).

## Phase 5: completed

Delivered:

- **Privileged helper process** (`app/helper/`, runs as root via `python -m app.helper`). A Unix socket (`NAS_HELPER_SOCKET`, default `/run/nas-manager/helper.sock`) with length-prefixed JSON framing (max 1 MiB), `SO_PEERCRED` authentication (root or `NAS_HELPER_ALLOWED_UIDS`), a fixed allowlist of exactly 10 operations (`ping`, `disk.list`, `disk.detail`, `disk.smart`, `volume.list`, `raid.list`, `mount`, `unmount`, `raid.scrub`, `samba.apply`), never a shell and never client-supplied command strings. Each handler builds its own `argv` from strictly validated parameters with a minimal `PATH`-only environment.
- **Validation rules in the helper.** Device names must match a fixed pattern and never start with `-`; mount points must be absolute, normalized, free of `..` and null bytes; filesystem types are an allowlist (`ext2…f2fs`, `iso9660`, …); mount options are a comma-separated allowlist that refuses `loop`, `bind`, `rbind` and `remount`. `samba.apply` only ever writes the configured `smb.conf` path, requires the managed markers, re-validates with `testparm -s` against a temp copy, backs up the previous file and writes atomically, then reloads with `smbcontrol all reload-config`.
- **Stateless confirm-then-execute.** Every state-changing operation has a **preview** endpoint that returns the exact plan, data-loss warnings and a deterministic **intent digest**; the confirm call must set `confirm: true` and echo the digest. The helper recomputes the digest from its own re-validated parameters and refuses on mismatch, so a confirmed action is bound to exactly what was previewed. No formatting, partitioning, destructive mount, erase or RAID rebuild is ever automated.
- **Read-only discovery.** `GET /disks` (physical disks + partitions from `lsblk -J`), `GET /disks/{name}` (single-device tree), `GET /disks/{name}/smart` (`smartctl -j -H -A -i`, tolerating devices without SMART), `GET /storage/volumes` (`findmnt -J -b`) and `GET /storage/raid` (`/proc/mdstat` + `mdadm --detail --scan`, with member states, spare detection, failure detection and sync action/percent). All parsing is pure and unit-tested against fixtures.
- **Actions.** `POST /disks/{name}/mount[/preview]`, `POST /disks/{name}/unmount[/preview]`, `POST /storage/raid/{name}/scrub[/preview]` (writes `check` to `/sys/block/mdX/md/sync_action`, never `repair`). Every action is audited (`disk.mounted`, `disk.unmounted`, `disk.action_failed`, `raid.scrub_started`, `raid.action_failed`) including failures.
- **Samba reload taken over by the helper.** When `NAS_HELPER_ENABLED=true`, `POST /samba/apply` hands the rendered config to the helper (write + `smbd` reload, `reload_required: false`); without the helper the Phase 3/4 fallback (staged write, `reload_required: true`) is preserved. `GET /samba/status` adds `helper_available`.
- **RBAC + migration `0005`.** New `disks:read` / `disks:write` tokens; `admin` gets both, `operator` gets read-only discovery. The migration seeds only the tokens (no new tables; discovery reads the live system).
- **Web UI.** A Disks & storage page (physical disks with partition/mount point info, mounted volumes, RAID arrays with a degraded badge, SMART summary, mount/unmount/scrub flows behind a preview-then-confirm dialog). Mount options are offered from a fixed selection; all DOM is built with `textContent` (CSP-compatible). The nav gains a permission-gated Disks link and the Samba page shows helper availability. A `deploy/systemd/nas-manager-helper.service` unit and `NAS_HELPER_*` env templates are included.

### Verification (executed in this workspace, Python 3.12, Ubuntu 24.04)

| Check | Result |
|-------|--------|
| `pytest` | 304 passed (was 233 at Phase 4; +71 for Phase 5) |
| Coverage (`app/`) | 88 % overall; Phase 5 workflow/validation/boundary/failure paths fully covered by unit tests + the helper socket round-trip test |
| `ruff check` / `ruff format --check` | clean (118 files) |
| `mypy` (strict, `app` + `tests`) | no issues in 105 source files |
| `alembic upgrade head` on a fresh directory | applies `0001`–`0005`; drift test finds no differences; `0005 (head)` after upgrade |
| Helper unit tests | allowlist shape; unknown op and non-object params rejected; `lsblk`/`findmnt`/`mdadm` argv fixed; option-flag/mountpoint/options injection rejected; intent mismatch refused; mount creates missing mount point; unmount only for the current mount point; scrub writes `check` only; `samba.apply` validates/backs up/writes/reloads and refuses non-configured paths |
| Helper socket integration | a real `python -m app.helper` subprocess answered `ping` over its Unix socket through the app's client; oversized frames refused; `SO_PEERCRED` reports the peer's UID |
| Disk API | discovery endpoints shape; anon 401 / viewer 403 / operator read-only / admin flows; mount/unmount/scrub confirm-then-execute; missing confirm or wrong intent → 400; helper down → 503 with audited failure; RAID scrub audited; the Disks page renders 200 |
| Live helper + app smoke | `python -m app.helper` bound a socket and answered `ping` through the real client (env-configured socket); `python -m app` booted, health reported `head_revision: 0005`, login page served 200 |
| Samba + helper | apply delegates to the helper, `reload_required: false`, status reports `helper_available` |

### Not verified

- The helper unit was **not run under real systemd**; `systemctl analyze`/start on the target host per `INSTALLATION.md`, and validate that the service account can traverse `/run/nas-manager` (the socket is `0660 root:<group>` with group from `NAS_HELPER_SOCKET_GROUP`).
- **No real disks were mounted, unmounted or scrubbed** in this workspace (none are attached): the mount/unmount/scrub paths are mocked at the subprocess boundary. Exercise them against a scratch disk/USB stick on the NAS host **after** reading the preview text carefully.
- `smartctl`/`testparm`/`smbcontrol`/`mount` were not run against real hardware here; the end-to-end helper test exercises `ping` only.
- The Disks page was exercised via TestClient/curl, not a real browser.
- PostgreSQL still not exercised (URL-driven only).

## Phase 6: completed

Delivered:

- **Personal API tokens** (`app/services/api_tokens.py`, `app/api/v1/api_tokens.py`, migration `0006` table `api_tokens`). `nas_` + 256-bit random tokens, stored as SHA-256 and shown once. Scopes are validated against the registry and must be a subset of the creator's permissions; effective access is `scopes ∩ owner's current permissions`. Name uniqueness per user, per-user cap, default/maximum lifetime, `last_used_at`/`last_used_ip` (written at most once a minute). Owner endpoints `GET/POST /tokens`, `DELETE /tokens/{id}`, `GET /tokens/scopes`; admin oversight `GET /admin/tokens`, `DELETE /admin/tokens/{id}` (new `api_tokens:read`/`api_tokens:write`; admin both, operator read). Tokens are revoked on admin password reset and on deactivation. Audited (`api_token.created/revoked/auth_failed`).
- **Bearer authentication** in `get_current_user`. An `Authorization` header is authoritative (bad value → 401 + `WWW-Authenticate: Bearer`, never a cookie fallback); failed lookups are rate limited per IP by a separate throttle (429 + `Retry-After`). `CurrentUser` carries `token`/`auth_method`; `/auth/me` reports them. Logout, change-password and token management are session-only (`require_session_auth`). The CSRF middleware skips bearer requests.
- **Per-token upload quotas.** Drop-folder uploads with a bearer token are keyed `token:<id>` with their own default quota (`NAS_API_TOKEN_UPLOAD_QUOTA_BYTES`); an invalid token on the drop endpoint is a 401 instead of a silent IP fallback.
- **Resumable uploads** (`app/services/resumable_uploads.py`, `app/api/v1/uploads.py`, table `upload_sessions`): `POST /uploads` → `PATCH` with `Upload-Offset` → `POST …/complete` (optional SHA-256), plus `HEAD`/`GET` for the offset, `DELETE` to abort and `POST /uploads/cleanup`. Works for the `files` target (needs `files:write` at every step) and the `public` drop folder. Chunks are bounded, offset-checked, fsync'ed and rolled back on limit violations, and bytes received before a disconnect are kept. Completion goes through new shared `files.commit_staged` / `public_uploads.commit_staged` helpers (refactored out of the multipart paths), so the jail, overwrite and quota-lock rules are unchanged. Expired sessions are swept. Staging files are hidden from listings and search.
- **Sync cursors** (`app/services/sync.py`, `app/api/v1/sync.py`, table `file_changes`): every upload/overwrite/mkdir/rename/delete is journaled in its own transaction. `GET /sync/cursor` and `GET /sync/changes?root&cursor&limit` page forward with `has_more`, and return `reset` for pruned, foreign or future cursors. Retention is `NAS_SYNC_CHANGE_RETENTION_DAYS`.
- **Client discovery and downloads**: public `GET /info` (API version, feature flags, limits); `GET /files/content` serves `Range` requests (206), verified by test.
- **Web UI**: Profile has a "My API tokens" card (create with scope checkboxes and expiry, copy the one-time secret, revoke). There is an admin `/tokens` page with a permission-gated nav link. Both are CSP-compatible (DOM via `textContent`).
- **Settings**: `NAS_API_TOKEN_*`, `NAS_RESUMABLE_*`, `NAS_SYNC_CHANGE_RETENTION_DAYS` (documented in both env templates).
- **Postman collection** `docs/postman/nas-manager-phase6.postman_collection.json` for manual and runner testing of the whole flow.
- **Fix (pre-existing, Phase 2):** the Profile change-password form's inputs had no `name` attributes, so `profile.js` submitted `"null"` for both passwords. Added the names and a regression test.

### Verification (executed in this workspace, Python 3.12.3, Ubuntu 24.04)

| Check | Result |
|-------|--------|
| `pytest` | 343 passed (304 before Phase 6; +39 in `test_api_tokens.py`, `test_resumable_uploads.py`, `test_sync.py`, `test_public_uploads.py`, `test_dashboard.py`) |
| Coverage (`app/`) | 90 % overall; `sync` 100 %, `api_tokens` service 96 %, `resumable_uploads` 92 %, new routers 99–100 % |
| `ruff check` / `ruff format --check` | clean (135 files) |
| `mypy` (strict, `app` + `tests`) | no issues in 120 source files |
| `alembic upgrade head` on a fresh directory | applies `0001`–`0006`; drift test clean; `downgrade 0005` drops the three tables and the `api_tokens:*` grants; re-upgrade works |
| Live server smoke (`python -m app`, curl) | login → create token (201) → `/auth/me` via Bearer reports `auth_method: token` with the token's scopes → `/sync/cursor` `c0` → resumable upload of 21 bytes in two PATCH chunks (offset 10 after the first, `HEAD` agrees) → complete with SHA-256 (200) → `Range: bytes=6-14` → 206 with the expected bytes → `/sync/changes` lists the `created` entry, cursor `c1` → revoke (204) → the token returns 401 → `/info` public |
| Tokens | raw value shown once / hash only stored; scope intersection and shrink-on-role-loss; scope escalation refused; expired/revoked/malformed → 401 without cookie fallback; throttle → 429 with only in-window failures audited; tokens cannot mint tokens, change the password or log out; admin oversight and operator read-only; password reset and deactivation revoke |
| Resumable uploads | multi-chunk round-trip; resume after a lost acknowledgement; wrong offset → 409 with the current offset; disconnect keeps received bytes; chunk/total limits roll back; checksum mismatch discards; jail/traversal/unknown root rejected; a name that appears while the upload is open → 409 with bytes kept; owner-only (404); `files:write` required; open-upload cap; expiry sweep; token-keyed public quota with authoritative check at completion (413, no leftovers); drop-folder cleanup spares staging files; concurrent request → 409 |
| Postman collection (`docs/postman/`) | all 23 requests replayed against a fresh server with a stdlib Python runner mirroring the collection's checks: all passed, twice in a row |
| Startup and logs | fresh data directory → `/health` `ok` at revision `0006`; after the full collection run the application log has no ERROR/CRITICAL entries or tracebacks (only the expected `NAS_SESSION_COOKIE_SECURE is false` warning for plain-HTTP local runs) |
| Sync | every mutation kind journaled in order; pagination; reset for future/pruned/never-synced cursors; malformed cursor 400; unknown root 404; `files:read` required |

### Not verified

- The new UI (Profile → My API tokens, the `/tokens` admin page) was checked through TestClient only, for page rendering and the absence of inline code. It was not used in a real browser, and the JavaScript was not syntax-checked because no Node runtime is available in this workspace.
- No real mobile client was exercised. The protocol was driven through TestClient and curl.
- The Postman collection's own JavaScript test scripts were not run inside Postman or Newman (no Node runtime); its requests were replayed with an equivalent Python runner.
- The resumable-upload lock and the bearer-failure throttle were exercised in a single process only.
- PostgreSQL still not exercised.

## Phase 7: completed

Delivered:

- **Providers** (`app/services/remote/`): SFTP (paramiko, pinned host key + `RejectPolicy`, password or OpenSSH/PEM key), FTPS (`AUTH TLS` + `PROT P`, verified certificates) and plain FTP (only with `allow_insecure`), WebDAV (httpx2; HTTPS unless `allow_insecure`; `PUT` to a temp name then `MOVE`), S3-compatible storage (boto3: AWS or any `endpoint_url`; flat prefix listing; archive = copy + delete), and Google Drive (Drive v3 over httpx2; OAuth authorization code + PKCE; resumable uploads; duplicate names and Google-native documents reported and skipped). One `RemoteProvider` interface; every remote name is validated before it can become a local path.
- **Credentials at rest** (`app/core/secrets.py`): Fernet, key file `NAS_CREDENTIALS_KEY_FILE` (default `<data_dir>/secrets/credentials.key`, created 0600 in a 0700 directory, loose permissions refused). Responses only list which secret fields are set; PATCH keeps omitted secrets; audit details never contain values.
- **Sync engine** (`app/services/sync_engine.py`): three-way plan (local scan × remote walk × per-file baseline), one-way (push/pull) and two-way jobs, conflict policies `keep_both` / `prefer_local` / `prefer_remote`, delete policies `ignore` / `archive` (`.nas-sync-archive/job-<id>/<timestamp>/`), content comparison before declaring conflicts, deletion guard, exclude patterns, dry runs, atomic staged downloads journaled into `file_changes`, per-action baseline commits (resumable), cancellation between files, and a stop after 10 consecutive errors.
- **Worker** (`app/worker/`, `deploy/systemd/nas-manager-worker.service`): heartbeat, lease renewal and reclaim, schedule-driven queueing, atomic claims (one run per job, `NAS_WORKER_CONCURRENCY` threads), run-history pruning, graceful SIGTERM, refuses an outdated schema. Logs to its own file (`NAS_LOG_FILE_NAME=worker.log`).
- **Migration `0007`**: `remote_connections`, `sync_jobs`, `sync_job_entries`, `sync_runs`, `sync_conflicts`; permissions `remotes:read` (admin, operator) and `remotes:write` (admin).
- **API**: `/remotes` (catalogue, CRUD, test, browse, SFTP host-key probe, Drive OAuth start/callback), `/sync-jobs` (CRUD, run/dry-run, runs, conflicts), `/sync-runs/{id}` (+ cancel), `/sync-conflicts/{id}/acknowledge`, `/remote-sync/status`; `/info` advertises `remote_sync`. Risky settings return `400 confirmation_required` with the list of risks unless `confirm: true`. Remote failures are `502 remote_error`.
- **Web UI**: a **Remote sync** page (worker status, connections with a provider-driven form, host-key fetch + fingerprint confirmation, Drive Connect, jobs, Preview with the planned actions, Run now, run history, conflicts with acknowledge). CSP-compatible; `apiFetch` errors now carry `status`/`code`/`details`.
- **Also**: `NAS_LOG_FILE_NAME` (one log file per process), remote client libraries' loggers held at WARNING, `.nas-remote-*` staging files hidden from file listings, Postman collection `docs/postman/nas-manager-phase7.postman_collection.json`.

### Verification (executed in this workspace, Python 3.12.3, Ubuntu 24.04)

| Check | Result |
|-------|--------|
| `pytest` | 458 passed (343 before Phase 7; +115 in `test_remote_sync_engine.py`, `test_remote_providers.py`, `test_remote_sync_api.py`, `test_worker.py`, `test_secrets.py`, plus additions to `test_migrations.py`, `test_dashboard.py`, `test_logging.py`) |
| Coverage (`app/`) | 89 % overall; `sync_engine` 91 %, `remote_sync` 92 %, `secrets` 92 %, providers 74–84 % (Google Drive lowest), worker loop 91 % (re-measured after the fact by running the suite at commit `3a217dd` in a separate worktree) |
| `ruff check` / `ruff format --check` | clean |
| `mypy` (strict, `app` + `tests`) | no issues in 146 source files |
| `alembic upgrade head` on a fresh directory | applies `0001`–`0007`; drift test clean; `downgrade 0006` drops the five tables and the `remotes:*` grants; re-upgrade works |
| Dependency pins | `pip install --dry-run --ignore-installed --no-deps -r requirements-dev.txt` resolves; `pip check` reports no broken requirements; all new modules import with warnings as errors |
| Engine | every planner rule (direction × change × conflict × deletion), deletion guard, conflict names, exclude matching, scan skipping symlinks/temp/archive; full runs against a local-directory provider: push/pull/two-way, one-way never overwrites a changed destination, all three conflict policies in every direction, identical-file adoption (byte compare and MD5), ignore vs archive deletions (journaled), modified-vs-deleted restore, guard trip + override, dry run, excludes, hostile remote names, cancel, per-file failure retried next run, dead connection, missing local folder, new remote folder created / vanished remote folder refused |
| Providers | SFTP against a real paramiko SFTP server over loopback (round trip, makedirs, move, `dir_exists`, host-key mismatch refused, bad password, host-key probe, unreachable host); WebDAV with `MockTransport` (walk, `..` href rejected, temp `PUT` + `MOVE`, `MKCOL`, 401, malformed XML); S3 with botocore `Stubber` (flat walk, multipart ETag not taken as MD5, folder markers and archive skipped, traversal key rejected, 404 stat, `dir_exists`, error mapping); FTP/FTPS with a fake `ftplib` (MLSD parsing, `PROT P`, rename-over fallback, login refused, no MLSD); Drive with `MockTransport` (token caching and refresh on 401, duplicates and native docs rejected, resumable upload, download, OAuth URL and code exchange) |
| API | CRUD with secrets never returned or audited; PATCH keeps/clears secrets; validation (unknown provider, secrets in config, plain HTTP/FTP, duplicates); connection test recorded and audited; browse + traversal rejected; API tokens refused on credential endpoints; anon 401 / viewer 403 / operator read-only; job lifecycle (dry-run queue, 409 on second run and on delete while active, cancel, delete keeps files); job validation (traversal, unknown root, missing directory, bad enums/patterns); confirmation gating; baseline reset on path change; worker status; conflicts acknowledge; OAuth start → callback stores an encrypted refresh token, single-use state, forged/expired state rejected, missing `NAS_PUBLIC_BASE_URL` explained |
| Worker | heartbeat; scheduled job queued, run and rescheduled; manual/disabled jobs not scheduled; one run per job; expired lease reclaimed and audited; own leases renewed; pruning keeps active runs; stop event cancels a run; `run_forever` exits on stop; `python -m app.worker` exits 1 on an unmigrated database |
| Live run (`python -m app` + `python -m app.worker` + a loopback paramiko SFTP server, scratch data dir) | host-key probe → SFTP connection (secret not echoed) → test ok → two-way job refused without confirm, accepted with it → dry run planned 2 uploads + 1 download and touched nothing → live run uploaded 2 / downloaded 1 (1016 bytes) → both sides edited the same file → conflict kept both versions (remote edit saved locally as `notes (conflict remote …).txt`, local edit on the remote) → pulled and conflict files appear in `/sync/changes` → worker heartbeat online → audit trail complete → `/remote-sync` page 200. Worker stopped cleanly on SIGTERM. Key file 0600 in a 0700 directory; the SFTP password is absent from the database, WAL, logs and audit log; no ERROR entries or tracebacks |
| Postman collection | all 23 requests replayed against the live stack with a Python runner generated from the same definition as the collection: all checks passed, twice in a row (the second pass also showed identical-file adoption: a new job over already-synced files transferred nothing) |
| systemd | `systemd-analyze verify` on the worker unit: only reports `/opt/nas-manager/.venv/bin/python` missing, which is expected in this workspace |

### Not verified

- **No real cloud or remote service was used.** Google Drive (consent screen, real API responses), AWS S3 or another S3-compatible service, real WebDAV servers (Nextcloud, ownCloud, Apache `mod_dav`) and real FTP/FTPS servers were not reachable here; those providers are tested against mocks only. SFTP was exercised against a paramiko test server, not OpenSSH. Test each provider against your real endpoint with a small job and a dry run first.
- The **Remote sync page was not used in a real browser**, and its JavaScript was not syntax-checked (no Node runtime here). A test checks that every element the script looks up exists in the rendered page.
- The **worker unit was not run under real systemd**; in particular `MemoryDenyWriteExecute` with `cryptography`/`paramiko` and outbound network access from the sandbox are unverified.
- Large trees and large files (performance, memory use of a full remote listing, multi-gigabyte transfers, S3 multipart uploads) were not tested.
- Only one worker process was run; PostgreSQL is still unexercised.

## Phase 8: completed

Delivered:

- **Libraries and access** (`app/services/media/libraries.py`, migration `0008`). Personal libraries (`<NAS_MEDIA_DIR>/<username>`, created on first use) are private to their owner; administrators (`media:admin`) register shared libraries over existing folders inside `NAS_FILE_ROOTS`, grant them to groups, enable/disable, rename, rescan and remove them (removal drops the index, album entries and cache, never files). One SQL clause decides what a caller may browse; anything else is a 404. New permissions `media:read`, `media:write` (admin, operator, viewer) and `media:admin` (admin).
- **Index** (`indexer.py`, `images.py`). Scans walk without following symlinks, skip hidden entries and non-allowlisted types, hash new/changed files (SHA-256) and extract dimensions and EXIF (date taken, camera, orientation, GPS) with Pillow 13.0.0; decoding is refused above `NAS_MEDIA_MAX_IMAGE_PIXELS`. Guards keep a missing/unmounted folder, an interrupted walk or unreadable directories from dropping anything. Every change is journaled in `media_changes`.
- **Thumbnails** (`thumbnails.py`). Upright 320/1280 px WebP in a fingerprint-keyed cache, rendered by the worker in batches or on demand; HEIC is indexed without a thumbnail.
- **Uploads, phone backup and sync** (`uploads.py`, `changes.py`). `POST /media/upload` and a new resumable `media` target write into the caller's personal library (`YYYY/MM` by default, never overwriting, `taken_at` sets the folder and mtime); `POST /media/sync/lookup` (hash-first backup), `GET /media/sync/cursor` and `GET /media/sync/changes` (cursor/reset semantics of Phase 6). Owners can delete items (file + index).
- **Search and albums** (`search.py`, `albums.py`). Filters for library, text (name/folder/camera, wildcards literal), kind, date range, camera, GPS, album; four sort orders; paging. Private per-user albums with covers, bulk add/remove and access re-checks.
- **Worker** (`jobs.py`, `app/worker/runner.py`). Scheduled and on-demand scans with atomic claims and leases (re-queued when a worker dies or stops), thumbnail batches, hourly pruning of the media journal and orphaned cache files — on a single-thread media pool separate from sync.
- **API and UI.** `/api/v1/media/...` (libraries, status, items, thumbnails, originals with Range and allowlisted types, upload, sync, albums), `/info` feature `media_gallery`; a **Gallery** page (filters, lazy thumbnail grid, lightbox with metadata, prev/next and keyboard navigation, download, albums, drag-and-drop upload, rescan; an admin *Libraries* card with shared-library creation and group grants). CSP-compatible, DOM via `textContent`.
- **Config:** `NAS_MEDIA_DIR`, `NAS_MEDIA_CACHE_DIR`, `NAS_MEDIA_MAX_IMAGE_PIXELS`, `NAS_MEDIA_SCAN_INTERVAL_MINUTES`, `NAS_MEDIA_THUMBNAIL_BATCH`, `NAS_MEDIA_SEARCH_LIMIT`, `NAS_MEDIA_SCAN_LEASE_SECONDS` (both env templates).

### Verification (executed in this workspace, Python 3.12.3, Ubuntu 24.04)

| Check | Result |
|-------|--------|
| `pytest` | 515 passed (458 before Phase 8; +57 in `test_media_index.py`, `test_media_api.py`, `test_media_worker.py`, plus additions to `test_migrations.py`, `test_dashboard.py`, `test_api_tokens.py`) |
| Coverage (`app/`) | 87 % as printed, which counts the Phase 9 `ffmpeg.py` that was created during the run (0 %); about 89 % without it. `media` router 100 %, `jobs` 100 %, other media services 91–98 % |
| `ruff check` / `ruff format --check` / `mypy` (strict) | clean |
| Migrations | fresh `upgrade head` applies `0001`–`0008`; drift test clean; `downgrade 0007` drops the six media tables and every `media:*` grant; re-upgrade works |
| Index and thumbnails | real JPEG/PNG files generated with Pillow: EXIF date/camera/orientation/GPS, garbage EXIF, corrupt files, pixel-limit refusal (header check and Pillow's hard limit), upright WebP thumbnails, transparency kept; scans add/modify/delete with journal rows, skip hidden/temp/archive/symlinked files and directories, honour `include_videos`, refuse a missing folder, keep a large index when the folder is empty, keep items after a stopped scan or an unreadable directory; fingerprint-keyed regeneration and orphan pruning (in-progress temps spared) |
| API and permissions | anonymous 401; viewer gets a personal library but no admin routes; a role without media permissions → 403 (and the Gallery page refused); token scopes enforced (bearer uploads need no CSRF); personal libraries private between users and from administrators (404 on item, thumbnail, original, album, library, scan, change feed, album add); shared libraries visible through group grants, read-only, hidden after revoke or disable, removal keeps files; validation (traversal, missing folder, unknown root/group, `~` names, duplicates, personal-library restrictions); upload folders, EXIF beating client dates, mtime from `taken_at`, collision suffixes, type/hidden/traversal/depth/size rejections without leftovers; resumable `media` target; Range 206, allowlisted type, `nosniff`, inline vs audited attachment; on-demand thumbnails; search filters (wildcards literal), sorting, paging; album lifecycle and audit trail; hash lookup and change feed |
| Worker | due scan → claimed → indexed → thumbnails rendered on the next tick; interval 0 disables scheduling; next scan waits for the interval; exclusive claims; lease reclaim and renewal; stopped scan re-queued; failed and crashing scans recorded; media work does not occupy sync slots; journal pruning keeps the newest row |
| Live run (`python -m app` + `python -m app.worker`, scratch data dir) | JPEG with EXIF (orientation 6, GPS) uploaded → `2023/12/xmas.jpg`, 900×1600, camera and GPS parsed; large thumbnail WebP 720×1280 (upright); original Range → 206; search and album; a second (viewer) account got 404 on the item, thumbnail, original and album and saw 0 items; a shared library over a file-root folder granted to a group was scanned by the worker and became visible to the member; token-authenticated hash lookup and change feed; worker online, thumbnails all rendered; `/gallery` 200; no ERROR/CRITICAL lines or tracebacks in either log |

### Not verified

- The Gallery page was not used in a real browser, and `gallery.js` was not syntax-checked (no Node runtime; running a downloaded JavaScript parser was not permitted in this session). A test checks that every element the script looks up exists in the rendered page.
- No real phone backup client was used; the protocol was driven through TestClient, curl and the Postman collection.
- Large libraries (tens of thousands of photos, multi-gigabyte videos) were not benchmarked; a scan hashes every new or changed file.
- PostgreSQL still not exercised; the worker unit with the media pools was not run under real systemd.

## Phase 9: completed

Delivered:

- **ffprobe/ffmpeg integration** (`app/services/media/ffmpeg.py`). The only module that runs the binaries: fixed argument lists without a shell, `file:` inputs/outputs, minimal environment, `stdin=/dev/null`, timeouts, `-progress pipe:1` parsing on a reader thread, kill on stop or timeout, temp-then-rename outputs. Binaries from `NAS_FFMPEG_PATH`/`NAS_FFPROBE_PATH` or `PATH`.
- **Video metadata and playback modes** (`video.py`, migration `0009`). Duration, dimensions (rotation applied), codecs, container and bitrate from ffprobe; `playback` is `direct` (H.264 + AAC/MP3 in MP4/M4V/MOV; VP8/VP9/AV1 + Opus/Vorbis in WebM), `transcode`, `unsupported` (no video stream; cover art ignored) or `unknown` (no ffprobe — re-probed by the next scan once ffmpeg is installed). Video thumbnails come from a poster frame (10 % in, at most 1 s).
- **Streaming.** `GET /media/items/{id}/stream` serves direct videos and ready transcodes with HTTP Range (seeking) and the right type, or answers 409 `transcode_required` with the transcode state.
- **Cached transcodes** (`transcode.py`, table `media_transcodes`). On-demand (`POST /media/items/{id}/transcode`, anyone who can watch) or per library (`POST /media/libraries/{id}/transcode`, admin); libx264 + AAC, height cap, `+faststart`; fingerprint-keyed outputs (a changed source is never served a stale rendition), claims/leases/reclaim like scans, re-queue on worker shutdown, recorded and audited failures, LRU eviction above `NAS_MEDIA_TRANSCODE_CACHE_MAX_BYTES`, orphan pruning, cleanup when a video is deleted. 503 `transcoding_unavailable` without ffmpeg or with `NAS_MEDIA_TRANSCODE_ENABLED=false`.
- **Worker.** A third single-thread pool for transcodes; the worker unit now runs at `Nice=10`, best-effort I/O priority 7.
- **UI.** The gallery lightbox plays videos (`<video>` with poster), shows a *Prepare for playback* button with live progress for videos that need a transcode, and explains unplayable files; duration badges on video tiles; a **Videos** page (`/videos`, the gallery preset to videos); *Prepare videos* per library and transcode/cache status for administrators.
- **API extras:** `GET /media/items/{id}/transcode`, transcode and ffmpeg fields in `/media/status`, `playback`/`rotation` on every item, `/info` features `video_streaming` and (when available) `video_transcoding`.
- **Config:** `NAS_FFMPEG_PATH`, `NAS_FFPROBE_PATH`, `NAS_MEDIA_PROBE_TIMEOUT_SECONDS`, `NAS_MEDIA_TRANSCODE_ENABLED`, `_MAX_HEIGHT`, `_CRF`, `_PRESET`, `_THREADS`, `_TIMEOUT_SECONDS`, `_CACHE_MAX_BYTES`, `_LEASE_SECONDS`.
- **Fixes found by the live tests:** ffprobe rejects `-nostdin` (the probe now omits it); the transcoder now closes ffmpeg's stdout pipe (a `ResourceWarning` the fake-process tests caught).
- **Postman collection** `docs/postman/nas-manager-phase8-9.postman_collection.json` (29 requests: token, gallery, sync, albums, expected failures, video upload/transcode/stream, cleanup).

### Verification (executed in this workspace, Python 3.12.3, Ubuntu 24.04, ffmpeg 6.1.1 from Ubuntu)

| Check | Result |
|-------|--------|
| `pytest` | 563 passed (515 after Phase 8; +48 in `test_video.py`, `test_ffmpeg_live.py`, plus additions to `test_migrations.py` and `test_dashboard.py`) |
| Coverage (`app/`) | 91 % overall; `ffmpeg` 96 %, `transcode` 98 %, `video` 100 %, `thumbnails` 96 %, `indexer` 94 %, `media` router 99 %, `worker/runner` 93 %. The live-ffmpeg tests ran (ffmpeg installed); without ffmpeg they are skipped |
| `ruff check` / `ruff format --check` / `mypy` (strict, 169 source files) | clean |
| Migrations | fresh `upgrade head` applies `0001`–`0009`; drift test clean; `downgrade 0008` drops `media_transcodes` and the two new columns; re-upgrade works |
| Dependencies | `pip check` clean; every new module imports with warnings as errors |
| Fake-ffmpeg tests | ffprobe JSON parsing (rotation from side data and tags, cover art, NaN/garbage numbers, malformed output); 12-case playback table; argument lists (`file:` absolute paths, a hostile `-i http:…` file name stays a file, preset/CRF/scale, no `-nostdin` for ffprobe); `_run` failure/timeout/missing binary; transcoder with a real child process: progress to 100 %, failure with stderr, empty output, kill on stop, kill on timeout; upload → probe → poster thumbnail → direct stream with Range (including a suffix range); rotated dimensions; poster failure; audio-only files; no ffmpeg → `unknown` then re-probed by a scan; 503 when disabled; full transcode lifecycle (409 → queued, idempotent → ready → stream + Range → stale after a source change → re-transcode, old file removed, audit events); failure + retry; stop → re-queue; lease reclaim; LRU eviction; deletion removes the transcode; orphan pruning; library pre-transcode; the worker runs transcodes on its own pool; other users get 404; `/videos` renders |
| Real-ffmpeg tests (`test_ffmpeg_live.py`) | synthetic `testsrc` clips: probe and classify H.264/AAC MP4 (`direct`) and MPEG-4 Matroska (`transcode`); real poster JPEG; real transcode whose output probes as H.264 and classifies `direct`; corrupt input fails cleanly; end to end through the API (upload, poster thumbnail, Range stream, 409, transcode, stream, seek) |
| Live run (app + worker + ffmpeg 6.1.1) | H.264/AAC MP4 → `direct`, 4.0 s, 640×360; Range 206 (first 100 bytes and the last 50); poster thumbnail WebP. MPEG-4/MP3 MKV → `transcode`; stream 409 `transcode_required`; request → worker transcoded it in about 1 s → `ready`, 100,487 bytes; streamed file probes as `h264 aac`; seek Range 206. `/media/status` reports ffmpeg and cache usage; `/info` lists `video_streaming` and `video_transcoding`; `/videos` 200; no ERROR/CRITICAL lines or tracebacks; the worker exited 0 on SIGTERM |
| Postman collection | all 29 requests replayed against a live app + worker (scratch data dir, real ffmpeg) with a stdlib runner that interprets the collection's own status checks and variable captures: all passed, twice in a row; no errors in the logs |
| systemd | `systemd-analyze verify` on both edited units reports only the missing `/opt/nas-manager/.venv` paths (expected here) and the world-writable/executable mode of `nas-manager.service` in this working tree (pre-existing file modes, not introduced by this phase) |

### Not verified

- **No real browser**: video playback, seeking in `<video>`, the *Prepare for playback* progress and the poster were checked through HTTP only. Browser codec support differs (e.g. some browsers play HEVC); the classification is deliberately conservative.
- ffprobe/ffmpeg **under the systemd sandboxes** (`MemoryDenyWriteExecute`, `SystemCallFilter`, `Nice=`) were not run on a real host.
- Long and large videos (hours, 4K, multi-gigabyte) were not transcoded; only synthetic few-second clips. Transcode timing and cache sizing on real hardware are unmeasured.
- Only ffmpeg 6.1.1 was exercised.

## Phase 10: completed

Delivered:

- **Monitoring package** (`app/services/monitoring/`, replacing `monitoring.py`; the existing imports are unchanged):
  - `processes`: top N by CPU or memory, **never command lines**.
  - `services`: unprivileged `systemctl show` for `NAS_MONITOR_SERVICES`, with validated unit names, `--`, no shell, a timeout, and `unknown` instead of errors.
  - `checks`: RAID from `/proc/mdstat`; SMART and helper liveness through the existing helper operations; one stored `monitor_checks` row per target.
  - `samples`: host samples every `NAS_MONITOR_SAMPLE_SECONDS`, downsampled history for 1h/6h/24h/7d, network rates and reboot-safe counters, pruning after `NAS_MONITOR_HISTORY_DAYS`.
  - `rules`: the pure alert engine (pending → firing after a duration, hysteresis on resolve, stale targets, `unknown` ignored).
  - `alerts`: rule CRUD, alert state, acknowledgement and outbox queueing.
  - `channels`: email and webhook validation, with Fernet-encrypted secrets.
  - `notify`: SMTP (STARTTLS/TLS, verified certificates) and webhooks (HTTPS, no redirects, HMAC-SHA256 `X-NAS-Signature`, optional `Authorization`), with retries and exponential backoff, and audited final failures.
- **Worker:** two new single-thread pools. *monitor* takes samples, runs checks and evaluates rules. *notify* delivers. Hourly pruning now also covers samples and resolved alerts.
- **Migration `0010`:** `metric_samples`, `monitor_checks`, `alert_rules` (10 built-in rules), `alerts`, `alert_channels` and `alert_notifications`. No new permissions: `system:read` (admin, operator) and `system:write` (admin) are reused.
- **API:**
  - `/monitoring/{overview,history,processes,services,raid,checks}`
  - `/alerts` (list, summary, acknowledge)
  - `/alert-rules` (CRUD; built-in rules cannot be deleted)
  - `/alert-channels` (CRUD + test; session only for create/update/test)
  - `/info` features `monitoring` and `alerting`
  - a synthetic `worker_offline` entry in the summary.
- **UI:** a **Monitoring** page (inline-SVG sparklines, services, RAID, SMART and helper, process table) and an **Alerts** page (active and resolved alerts, acknowledge, rule editor, email/webhook channel forms with test). The dashboard gains an "Active alerts" card for `system:read`. Navigation links are permission-gated, and everything stays CSP-compatible.
- **Config:** `NAS_MONITOR_*`, `NAS_SYSTEMCTL_PATH` and `NAS_ALERT_*`, in both env templates. The worker unit is annotated with the sandbox requirements (`AF_UNIX`; no `ProtectProc`).
- **Fixes:**
  - `parse_smartctl` (Phase 5) treated any non-zero smartctl exit as an error. smartctl's exit status is a bit mask, and bit 3 ("disk failing") comes with valid JSON, so a failing disk was reported as "error". It is now parsed, and the disk shows as `failed`.
  - Two bugs found by the live run were also fixed: the reserved `message` key in a log `extra`, and resolution messages being rewritten as "above the threshold".
- **Postman collection** `docs/postman/nas-manager-phase10.postman_collection.json` (25 requests), generated from the same definition as the live replay runner.

### Verification (executed in this workspace, Python 3.12.3, Ubuntu 24.04 on WSL2 with systemd running)

| Check | Result |
|-------|--------|
| `pytest` | 661 passed (563 after Phase 9; +98 in `test_alert_rules.py`, `test_alerting.py`, `test_monitoring_api.py`, plus additions to `test_monitoring.py`, `test_worker.py`, `test_dashboard.py`, `test_migrations.py`) |
| Coverage (`app/`) | 91 % overall; monitoring API routers 100 %, `rules` 99 %, `samples`/`processes` 100 %, `alerts` 96 %, `notify` 95 %, `services` 95 %, `checks` 94 %, `channels` 88 %, `worker/runner` 90 % |
| `ruff check` / `ruff format --check` / `mypy` (strict, 186 source files) | clean |
| Migrations | the CLI on a fresh directory applies `0001`–`0010`; `downgrade 0009` and back up; `alembic check` reports no drift; the built-in rules are seeded and re-seeded |
| Engine | pending/firing/drop/resolve timing, hysteresis in both directions, per-target tracking, a rule target narrowing, stale targets, `unknown` checks, condition metrics, and messages frozen on resolution |
| Outbox and transports | per-channel filters (`min_severity`, `send_resolved`, disabled), silent rules, retry → backoff → failed + audit; fake SMTP (STARTTLS order, login, implicit TLS, auth failure, unreachable host, plain refused, subject folding); webhooks over `MockTransport` (signature, `Authorization`, no redirect following, network error, plain HTTP refused) |
| API and permissions | anonymous 401, viewer 403, operator read-only (writes and acknowledge 403), admin full; secrets never in responses, the stored config or audit details; API tokens refused on channel create/update/test; validation errors |
| Live run (`python -m app` + `python -m app.worker` + a loopback webhook receiver, scratch data dir, 10 s sample/check interval) | the 25-request replay passed twice in a row: history filled by the worker, processes without command lines, services read through the real `systemctl` (here: not installed → `unknown`), a 1 % disk rule fired, signed `test`/`firing`/`resolved` webhooks were received with valid HMACs, acknowledge worked, raising the threshold resolved the alert and notified; `/`, `/monitoring` and `/alerts` returned 200; the signing secret was absent from the DB, WAL and logs; no ERROR/CRITICAL lines or tracebacks; the worker exited cleanly on SIGTERM. `unit_states` also parsed real units (`systemd-journald`, `dbus.socket` → active) |
| systemd | `systemd-analyze verify` on the worker unit reports only the missing `/opt/nas-manager/.venv` (expected here) and the pre-existing mode bits of `nas-manager.service` |

### Not verified

- **No real browser**: the Monitoring and Alerts pages and the dashboard card were checked over HTTP only (rendering, element IDs, no inline code). The JavaScript was not syntax-checked, because there is no Node runtime here.
- **No real SMTP server or provider**: email was tested against a fake `smtplib`. Webhooks went to a loopback receiver, not to ntfy, Gotify or Slack.
- `systemctl show` was run as root in WSL, **not as the unprivileged `nas-manager` user inside the sandboxed unit**. Run the check in `INSTALLATION.md` on the target host.
- **No real RAID array or SMART hardware**: mdstat and smartctl came from fixtures, and SMART from a mocked helper.
- PostgreSQL is still not exercised.


## Phase 11: completed

Delivered:

- **Backup package** (`app/services/backup/`): `crypto` (scrypt → HKDF → AES-256-GCM in authenticated 4 MiB segments, keyed blob names), `repository` (self-describing on-target format: `repo.json`, sealed snapshot manifests, content-addressed sealed blobs; explicit initialize/adopt), `sources` (file-root folders, media libraries, system state with a consistent SQLite copy + credentials key + `smb.conf`, and remote folders for pull backups), `engine` (backup with parent-snapshot reuse and dedup, full/quick verify that rebuilds the catalogue and blob cache, keep-last/daily/weekly/monthly retention with safe garbage collection, restore with temp file → checksum → atomic rename and `.nas-backup-replaced/`), `jobs` (CRUD, queueing, per-repository exclusive claiming, leases, scheduling, run processing).
- **Targets:** every Phase 7 provider (SFTP, FTP/FTPS, WebDAV, S3, Google Drive) plus a jailed `LocalProvider` for external storage under `NAS_BACKUP_LOCAL_ROOTS`. Providers gained `delete` (Drive: trash), used only by backup retention and GC.
- **Directions:** push (NAS → repository) and pull (remote server → local repository); restores to file roots, media libraries, remote connections or a staging area.
- **Worker:** a single-thread *backup* pool, scheduled backups, lease renewal/reclaim, hourly pruning of backup run history.
- **CLI:** `restore-system --run <id> --yes` (installs a staged system snapshot with the services stopped, keeping `*.before-restore-*` files, then migrates) and `backup-recover --repo-dir` (lists or restores snapshots straight from a repository directory; disaster recovery).
- **Migration `0011`:** `backup_repositories`, `backup_jobs`, `backup_runs`, `backup_snapshots`, `backup_blobs`; permissions `backup:read` (admin, operator), `backup:write`, `backup:restore` (admin).
- **API:** `/backup/status`, `/backup-repositories` (CRUD, `init`, `verify`, `prune`, `snapshots`), `/backup-jobs` (CRUD, `run`), `/backup-runs` (list, detail, cancel), `/backup-snapshots` (detail, `entries`, `restore`); `/info` feature `backup`. Passphrase endpoints are session-only; risky actions use `confirmation_required`.
- **UI:** a **Backups** page (repositories, jobs, snapshots with browse and restore dialog, recent runs) gated on `backup:read`, CSP-compatible.
- **Config/deploy:** `NAS_BACKUP_*` in both env templates; the worker unit documents `ReadWritePaths` for backup roots.
- **Docs:** `BACKUP_AND_RESTORE.md` rewritten (concepts, format, setup, retention, verification, restore, system restore, disaster recovery, limitations).

### Verification (executed in this workspace, Python 3.12.3, Ubuntu 24.04 on WSL2)

| Check | Result |
|-------|--------|
| `pytest` | 726 passed, 1 skipped (661 after Phase 10; +65 in `test_backup_crypto.py`, `test_backup_engine.py`, `test_backup_api.py`, plus additions to `test_worker.py` and `test_migrations.py`). The skip is the unreadable-file test, which cannot work as root. |
| `ruff check` / `ruff format --check` / `mypy` (strict, 199 source files) | clean |
| Migrations | head `0011`; downgrade to `0010` removes the tables and grants, re-upgrade restores them; model/migration drift check passes |
| Crypto | round trips at segment boundaries; wrong key, flipped byte, truncation, segment reordering, trailing data, bad header and hostile scrypt parameters are rejected |
| Engine | dedup across files and runs; unchanged files reused; partial runs; cancelled runs leave no snapshot; missing source, wrong passphrase and unmounted target fail cleanly; verify detects corrupted and missing blobs and rebuilds the catalogue; retention buckets; prune dry run; GC refuses with an unreadable snapshot; restores into new folders, selected paths, in place with replacement archiving, to a remote, and from a crafted manifest with `..`/absolute paths (refused); tampered blobs are not restored; claim exclusivity per repository; scheduling and lease reclaim; both recovery CLI commands |
| Live run (`python -m app` + `python -m app.worker`, scratch data dir, real scrypt cost, loopback SFTP server) | 30/30 checks: local and SFTP repositories, push backup with dedup, second run fully reused, encrypted repository content, browse, restore byte-identical, overwrite with confirmation and archiving, retention, system-state backup and staged restore, pull from SFTP, push to SFTP + full verify, restore to SFTP, corrupted blob detected, prune preview, API token refused. Then `restore-system` (refused without `--yes`, installed with it; the restarted app showed the backup-time jobs and the audit event) and `backup-recover` (listed snapshots, restored a pull snapshot identical to the SFTP source). No passphrase in the database, WAL, logs or repository; only INFO log lines. |

### Not verified

- **No real browser:** the Backups page was checked over HTTP only, and the JavaScript was not syntax-checked (no Node runtime).
- **No real cloud providers:** S3, WebDAV, FTP and Google Drive were not used as backup targets; `delete` for those providers is untested against real servers. SFTP was a loopback stub.
- **No real external disk, systemd sandbox or hardware loss:** `ReadWritePaths` and the `nas-manager` account's access were not exercised.
- Large data sets (many GB, hundreds of thousands of files) and scrypt cost on slow NAS CPUs are unmeasured.

## Phase 12: completed

Delivered:

- **Export** (`app/services/config_transfer/export.py`): settings, roles, groups, users, Samba shares, remote connections, sync jobs, shared media libraries, alert rules and channels, upload quotas, backup repositories and jobs, keyed by name. Secrets (password hashes, credentials, channel secrets, backup passphrases) are omitted, or sealed with a passphrase together with a digest of the sections.
- **Import** (`importer.py`): `preview` and `apply` run the same code in one transaction (preview always rolls back). Supports `merge` and `replace` modes. Every value is validated with the existing validators, all problems are reported at once and the import is all-or-nothing. Warnings need confirmation. Administrator protection is enforced. Deactivated users lose their sessions and tokens. Imported repositories start uninitialized, and shares with missing paths are imported disabled.
- **Rollback**: each applied import stores a Fernet-encrypted pre-import snapshot and an SQLite copy of the database (last 10 kept). Rolling back the latest import deletes what it created and re-applies the snapshot in one transaction.
- **Migration `0012`:** `config_import_runs`; permissions `config:export`, `config:import` (admin).
- **API:** `POST /config/export`, `POST /config/import/preview`, `POST /config/import`, `GET /config/imports`, `POST /config/imports/{id}/rollback`, all session-only. `/info` gains the feature `config_transfer`. New audit category `config`.
- **UI:** a **Configuration** page (export with an optional passphrase, file import with a preview table, warnings and errors, and import history with rollback).
- **Docs:** API, architecture, security, backup/restore (configuration section), master plan.

### Verification (executed in this workspace, Python 3.12.3, Ubuntu 24.04 on WSL2)

| Check | Result |
|-------|--------|
| `pytest` | 739 passed, 1 skipped (726 after Phase 11; +12 in `test_config_transfer.py`, +1 migration test) |
| `ruff check` / `ruff format --check` / `mypy` (strict, 206 source files) | clean |
| Migrations | head `0012`; downgrade to `0011` and re-upgrade, with grants checked; drift check passes |
| Tests | export without a passphrase has no secrets; sealed export round trip; full round trip into a second installation with identical resulting configuration (and the imported user can log in); idempotent re-import; wrong or missing passphrase; tampered sections; envelope and per-object validation (any error refuses everything and is audited); last-admin and own-admin protection; users without hashes are inactive; missing share paths are disabled; replace mode deletes only plain configuration and needs confirmation; rollback restores the exact prior state; only the latest import can be rolled back; operator, anonymous and API-token access refused |
| Live run (two `python -m app` instances: a source with the Phase 11 data and a fresh target) | 20/20 checks: export with and without a passphrase, wrong passphrase refused, preview, confirmation required, import applied, target configuration identical to the source, the imported user logs in, `smb.conf` not written, rollback restores the original target, history shows the rollback, page renders, no plaintext secrets anywhere in the target data dir. Logs: INFO lines only, no tracebacks. |

### Not verified

- **No real browser:** the Configuration page (file picker, download through a blob URL) was checked over HTTP only.
- **Cross-version imports** are untested (only format version 1 exists).
- **Imports into PostgreSQL** are untested.
- **Large documents** (thousands of users) are untested; they are capped at 10,000 objects per section.

## Phase 13: completed

Delivered:

- **Searchable audit log:** free text (`q`, also over details, wildcards escaped), time range, target, request id and IP filters; facets; streamed CSV/JSON Lines export (formula-safe CSV, capped, audited); a details modal and export buttons on the Audit page.
- **Comprehensive auditing:** `auth.permission_denied` (throttled), `system.started`/`stopped` for web and worker, `remote.oauth_started`, and a route-inventory test covering all 102 state-changing routes. A quota rejection during resumable completion no longer also records `upload.session_completed`.
- **Hash chain** (`app/services/audit_chain.py`): worker sealing, verification (API, Logging page and a daily worker check that records `audit.chain_broken`), and retention pruning (`NAS_AUDIT_RETENTION_DAYS`, default 90) that waits for sealing and forwarding.
- **Remote syslog** (`app/services/syslog/`): RFC 5424/3164 over UDP, TCP or verified TLS. Destinations are managed on a new **Logging** page and through `/api/v1/syslog/*`. The worker forwards audit events from a cursor with backoff and transition-only audit events. Every process forwards application logs best effort.
- **Local logging:** native journald output (`NAS_LOG_JOURNALD`, structured `NAS_*` fields, per-process `NAS_SYSLOG_IDENTIFIER`), daily rotation (`NAS_LOG_ROTATION`) and gzip compression (`NAS_LOG_COMPRESS`).
- **Migration `0013`:** `syslog_destinations`, `audit_events.chain_hash` and an `actor_username` index; no new permissions.
- **Docs:** API, architecture, security, installation (journalctl queries, rsyslog TLS receiver, retention upgrade note) and the master plan.

### Verification (executed in this workspace, Python 3.12.3, Ubuntu 24.04 on WSL2)

| Check | Result |
|-------|--------|
| `pytest` | 820 passed, 1 skipped (739 after Phase 12; +81 in `test_syslog.py`, `test_audit_chain.py`, `test_audit_search.py`, `test_audit_coverage.py`, `test_log_outputs.py`, plus additions to `test_worker.py`, `test_migrations.py`, `test_dashboard.py`, `test_resumable_uploads.py`). The worker and syslog modules were re-run (53 passed) after the last fix. The skip is the unreadable-file test, which cannot work as root. |
| `ruff check` / `ruff format --check` / `mypy` (strict, 223 source files) | clean |
| Migrations | head `0013`; downgrade to `0012` and re-upgrade keeps existing audit rows; drift check passes; `alembic upgrade` via the CLI on a fresh directory |
| Live run (`python -m app` + `python -m app.worker`, scratch data dir, loopback TCP/UDP collector) | 26/26 checks: plaintext confirmation, backfill, failures-only RFC 3164 over UDP, the test message, search and both exports, worker sealing, verify passes, then a raw SQL edit is detected. Collector stopped → destination failing with a backlog → restarted → backlog plus `forward_failed`/`forward_recovered` delivered. Application logs (INFO) forwarded from both processes under their own identifiers; `app.audit` records not duplicated as logs. The worker's `system.stopped` reached the collector after a fix found here: the final pass now ignores the stop flag. No ERROR lines or tracebacks in either log. |
| systemd | `systemd-analyze verify` on the worker unit: no directive errors |

### Not verified

- **No real browser:** the Logging page and the new Audit page were checked over HTTP only (rendering, element ids, no inline code). The JavaScript was not syntax-checked (no Node runtime).
- **No real collector software:** delivery was tested against loopback Python receivers (UDP, TCP, and TLS with a generated CA), not rsyslog, syslog-ng or Graylog. The rsyslog example in INSTALLATION.md is untested.
- **journald:** the native protocol was tested against a bound datagram socket. Entries were not checked in a real journal under the systemd units.
- The Postman collection planned for this phase was not produced: `docs/postman/` is empty in this workspace (the earlier phases' collections are missing too).
- PostgreSQL is still not exercised.

## Phase 14: completed

Delivered:

- **Drop-folder admission before the body is read** (`PublicUploadGuardMiddleware`): declared `Content-Length` above the per-file cap → 413 `payload_too_large`; per-uploader rate limit (`NAS_PUBLIC_UPLOAD_RATE_MAX_REQUESTS` / `_WINDOW_SECONDS`, default 30 per 5 minutes) → 429 with `Retry-After` and one `public_upload.rate_limited` audit event per window; no quota left → 413 before anything is written.
- **Throttles**: `SlidingWindowThrottle` (alias `IpLoginThrottle`) with a capped key table; `DenialThrottle` takes any hashable key.
- **Proxy and Host trust**: `NAS_FORWARDED_ALLOW_IPS` passed explicitly to uvicorn (empty disables proxy headers), `NAS_TRUSTED_HOSTS` (Starlette `TrustedHostMiddleware`), no `Server` header.
- **Headers**: `Permissions-Policy`, `Cross-Origin-Opener-Policy`, `Cross-Origin-Resource-Policy`, HSTS on HTTPS (`NAS_HSTS_MAX_AGE_SECONDS`).
- **Fixes found by review**: the CSRF middleware buffered whole multipart bodies in memory before refusing them (now only small urlencoded forms are read); logout cleared cookies without `Secure`/`SameSite`; the fallback `smb.conf` writer replaced the file with a `0600` one.
- **Production warnings** for debug mode, empty trusted hosts, `NAS_FORWARDED_ALLOW_IPS=*` and an unlimited drop folder; umask at least `027` in every entry point.
- **Fixes found by the live deployment** (none was visible to the mocked tests):
  1. `/run/nas-manager` was `0750 root:root`, so the service account could not reach the helper socket ("helper probe failed"). The helper also re-moded whatever directory held the socket. Now systemd creates it `0711`, and the helper never re-modes an existing directory.
  2. The helper's `SystemCallFilter=@system-service` excludes `@mount`, so mounting would have been killed. `@mount` was added.
  3. The helper mounted inside its private mount namespace; the mount was invisible to the host. The helper now runs in the host namespace (decision 69).
  4. `findmnt --target` matched the file system containing any existing directory, so every pre-existing mount point was refused as "already in use". The check now uses `--mountpoint`.
  5. The helper unit had no `WorkingDirectory`/`NAS_DATA_DIR`, so it would have used `/data`. It also wrote a log file into the web unit's log directory. Now it logs to the journal only.
  6. The documented `sudo -u nas-manager … app.cli bootstrap-admin` acted on a relative `data/` directory. It is replaced by `deploy/nas-manager-cli`.
  7. `http2 on;` in the nginx example needs nginx 1.25.1+; Ubuntu 24.04 ships 1.24.
  8. The smoke test's cookie jar dropped Secure cookies over loopback HTTP.
- **Deployment artifacts**:
  - `deploy/install.sh` (idempotent install/upgrade; never overwrites the env file) and `deploy/uninstall.sh` (keeps data; `--purge --yes` removes everything).
  - `deploy/nas-manager-cli` and `deploy/smoke_test.py` (stdlib; HTTP, headers, Host check, login, helper, unit users and capabilities, socket mode, journal).
  - `deploy/nginx/nas-manager.conf`.
  - Hardened units; both env templates now document every setting (test-enforced).
- **Tooling**: `pip-audit==2.10.1` (no known vulnerabilities in `requirements.txt` or `requirements-dev.txt`); coverage configuration; `deploy/smoke_test.py` under mypy strict.
- **Docs**: Installation rewritten for the installer, reverse proxy, firewall, smoke test, upgrade/rollback and uninstall. Security, architecture, API, backup/restore (CLI wrapper), README and master plan updated.

### Verification (executed in this workspace, Python 3.12.3, Ubuntu 24.04 container on WSL2 with systemd 255)

| Check | Result |
|-------|--------|
| `pytest` (with coverage) | 865 passed, 1 skipped (820 after Phase 13; +45 in `test_hardening.py`, `test_deploy.py`, `test_config.py` and `test_helper.py`); branch coverage of `app/` 89 %. The skip is the unreadable-file test, which cannot work as root. An earlier full run had 1 failure from a test edited mid-run; the final run is the one recorded. |
| `ruff check` / `ruff format --check` / `mypy` (strict, 226 source files incl. `deploy/smoke_test.py`) | clean |
| Migrations | no new migration; `alembic upgrade head` 0001–0013 applied by the live unit's `ExecStartPre` on an empty `/var/lib/nas-manager` |
| `pip-audit -r requirements.txt` / `-r requirements-dev.txt` | no known vulnerabilities |
| `systemd-analyze verify` | no directive errors for any unit (only the expected missing `/opt` binaries before installation) |
| `systemd-analyze security` | web 1.4, worker 1.4 (unchanged; the added directives are not scored), helper 4.9 → 4.3 |
| Live install (`install.sh --with-helper`, then two upgrade runs) | user/group created; `/opt` root-owned, `0644`/`0755`; env file `0640 root:nas-manager` created once and kept on upgrades; units stopped worker → web → helper and restarted helper → web → worker; migrations applied |
| Live smoke test (`smoke_test.py --login admin`, password from stdin) | **35/35**: health, 7 security headers, no `Server`, docs 404, anonymous 401, unknown Host 400, login, HttpOnly + Secure cookie, `/auth/me`, disk discovery through the helper, logout and revocation; web and worker uid `nas-manager`, `CapEff=0`, `NoNewPrivs=1`; helper root; socket `0660 root:nas-manager`; journal entries for all three |
| Live helper mount (64 MiB ext4 image on a fresh loop device) | confirm missing → 400, wrong intent → 400, preview → confirm → mounted and visible on the host (`findmnt`), unmount preview → confirm → gone; `disk.mounted`/`disk.unmounted` audited; loop device detached afterwards |
| nginx 1.24 in front (scratch prefix, self-signed certificate) | `nginx -t` ok; HTTP/2 + HSTS over TLS; HTTP → 301; spoofed `X-Forwarded-For` replaced by the real address in the audit log; `limit_req` → 429 after the burst; app limiter → 429 with `Retry-After` after 30 uploads; one `public_upload.rate_limited` event |
| Lifecycle | start/restart/stop cycles record `system.started`/`system.stopped` for web and worker; no ERROR lines or tracebacks in the journals or log files (warnings: expected rejections, `smartctl` not installed) |
| Uninstall | plain uninstall kept data, config and user; reinstall onto the kept data passed the smoke test (35/35); `--purge` without `--yes` refused; `--purge --yes` removed user, group, units, `/opt`, `/etc`, `/var/lib`, `/var/log` |
| `bootstrap-admin` through `nas-manager-cli` | admin created in `/var/lib/nas-manager/nas.db` (`0640 nas-manager`); interactive `--pty` mode checked with `systemd-run` separately |

### Not verified

- **No real browser.** The new headers (COOP, CORP, Permissions-Policy) were checked on responses only. That includes video playback and the OAuth redirect.
- **No JavaScript syntax check** (user decision): esprima 4.0.1 cannot parse the ES2020 syntax used, and there is no Node runtime.
- **Not a stock Ubuntu host.** The environment is a Docker Desktop container running systemd. The root mount's private propagation here is what exposed bug 3 above. Real disks, SMART (`smartctl` not installed), RAID scrubs, Samba reloads (`smbcontrol`) and ffmpeg under the worker sandbox were not exercised live.
- **TLS proxy test used a scratch nginx and certificate** on high ports, not the system nginx with a real certificate. The ufw rules are untested.
- PostgreSQL is still not exercised.

## Phase 15: completed

Explorer-style gallery (`/gallery`, `/videos`).

Delivered:

- **UI** (`gallery.html`, `js/gallery.js` + modules in `js/gallery/`):
  - Navigation pane: Quick access (All, Favorites, Recently added), a lazily loaded folder tree per library, and albums. Folders and albums accept dropped items.
  - Address bar with back/up/refresh, breadcrumbs and a search scoped to the current location (it searches subfolders).
  - Filter panel: type, orientation, date presets/range, camera (from facets), size presets, location. Active filters show as removable chips; an "Include subfolders" switch flattens a folder.
  - Sort by name, date taken, date modified, size, type or duration, ascending or descending (Details column headers sort too).
  - Six views: extra large, large, medium and small icons, list, details. The chosen view is kept in `localStorage`.
  - Location, filters and sort are kept in the URL hash, so back/forward and reload work.
  - Selection: click, Ctrl/Cmd, Shift ranges, Ctrl+A, rubber band, checkmark overlays. Keyboard: arrows, Enter, Space, F2, Delete, Backspace/Alt+Up, Alt+Left, `/`.
  - Context menus (right-click, long-press, Shift+F10) and a selection bar: add to/remove from album, album cover, favorite, move (folder picker with "New folder"), rename, ZIP download, delete.
  - Infinite scroll and a status bar (counts, total size, selection size).
  - Lightbox: filmstrip, keys and swipes, wheel/pinch/double-click zoom with pan (loads the original when zoomed and the browser can show it), slideshow with interval, full screen, details panel, Back closes it. Video playback and transcode flow unchanged.
  - Library administration moved into a "Libraries" dialog.
- **API** (see `docs/API.md`):
  - Folder listing, create, rename and delete.
  - Item move and rename, bulk delete, ZIP download (`NAS_MEDIA_ZIP_MAX_BYTES`).
  - Private favorites (`media_favorites`, migration 0014).
  - Facets.
  - New `/media/items` filters (`folder`, `recursive`, `favorites`, size, orientation, `indexed_after`) and seven new sort orders.
  - Moves and renames keep item ids and journal `deleted` + `created` changes.
- **Audit**: `media.items_downloaded`, `media.items_moved`, `media.item_renamed`, `media.folder_created/renamed/deleted`. Favorites are exempt (private, no files changed).

### Verification

| Check | Result |
|-------|--------|
| `pytest` | 853 passed, 1 skipped (with the 7 `caplog` tests and the audit-coverage test rerun separately: 58 passed). `tests/test_remote_providers.py` could not be collected: `boto3` is not installed in this interpreter. New: `tests/test_media_explorer.py` (10), 0014 round trip. |
| `ruff check` / `ruff format --check` | clean. `mypy`: no new errors in changed files (the environment's SQLAlchemy stubs report pre-existing errors elsewhere). |
| JavaScript | parsed with esprima 4.0.1 after mapping `?.`/`??`; named imports checked against exports. |
| Headless Chromium (Playwright, isolated server on port 8090, scratch data) | Exercised: browse/tree, all views, sort, selection (click/Shift/Ctrl/band/Ctrl+A), favorites, context menus, folder picker move, drag to tree folder, folder rename, item rename (F2), filters/chips, search, album add/remove, details header sort, lightbox (zoom, details, slideshow, Back closes), bulk delete, ZIP download, dark theme, 390 px width (no horizontal overflow), read-only user (no write controls). No console errors. |
