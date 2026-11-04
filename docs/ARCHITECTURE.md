# Architecture

## Layers

```
HTTP request
   │
   ▼
RequestContextMiddleware   request id, access log, security headers (+ HSTS on HTTPS)
   ▼
TrustedHostMiddleware      Host header allowlist (only when NAS_TRUSTED_HOSTS is set)
   ▼
CSRFProtectMiddleware      double-submit + session-bound token check
   ▼
PublicUploadGuard          POST /public/uploads admission before the body is read
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
| `app/cli.py` | `python -m app.cli bootstrap-admin` (first-run administrator); `restore-system` (install a staged system restore) and `backup-recover` (read a repository directly) |
| `app/worker/` | `python -m app.worker`: the worker loop (`runner.py`: heartbeat, lease renewal/reclaim, scheduling, atomic claiming, run-history pruning; a sync pool, a single-thread media pool for scans and thumbnails, a single-thread transcode pool, single-thread monitor and notify pools, and a single-thread backup pool) |
| `app/core/config.py` | `Settings` from `NAS_*` variables (incl. sessions, throttling, Argon2id costs) |
| `app/core/logging.py` | JSON/text formatters, redaction, rotating file handler |
| `app/core/middleware.py` | Request id, access log (path only, never query string), security headers, HSTS |
| `app/core/csrf.py` | CSRF protection middleware (double-submit + session-bound) |
| `app/core/errors.py` | `AppError` hierarchy and the JSON error envelope |
| `app/core/permissions.py` | Permission-token registry and default role grants |
| `app/core/secrets.py` | Fernet key file (`NAS_CREDENTIALS_KEY_FILE`, created 0600) and `CredentialCipher` for remote credentials at rest |
| `app/db/` | Naming conventions, engine/session factory, migration helpers, `get_db` |
| `app/api/v1/deps.py` | `CurrentUser` (session or API token), `get_current_user`, `require_permission(...)`, `require_session_auth` |
| `app/api/v1/` | `auth`, `users`, `groups`, `roles`, `audit`, `health`, `system`, `files`, `samba`, `settings`, `public_uploads`, `disks`, `api_tokens`, `uploads` (resumable), `sync` (+ `/info`), `remotes`, `remote_sync` (jobs, runs, conflicts, status), `media` (libraries, items, thumbnails, streaming, transcodes, albums, mobile sync), `monitoring` (overview, history, processes, services, RAID, checks), `alerts` (alerts, rules, channels), `backups` (repositories, jobs, runs, snapshots, restore), `config_transfer` (export, import preview/apply, rollback) |
| `app/services/` | `passwords`, `auth_service`, `users_service`, `groups_service`, `roles_service`, `security`, `audit`, `throttle`, `bootstrap_service`, `health`, `settings_service`, `files`, `samba`, `public_uploads`, `api_tokens`, `resumable_uploads`, `sync`, `remotes`, `remote_sync`, `sync_engine` |
| `app/services/media/` | Photo gallery and video (Phases 8–9): `libraries` (records, access rules, path jail), `images` (Pillow EXIF + thumbnails), `indexer` (scans), `thumbnails` (cache), `changes` (mobile journal + hash lookup), `search`, `albums`, `uploads`, `jobs` (worker side), `ffmpeg` (the only module that runs ffprobe/ffmpeg), `video` (playback classification), `transcode` (cached MP4 renditions) |
| `app/services/monitoring/` | Monitoring and alerting (Phases 1 + 10): `host` (live psutil summary), `processes` (top N, never command lines), `services` (`systemctl show`), `checks` (RAID from `/proc/mdstat`, SMART + helper via the helper, stored snapshots), `samples` (history + downsampling), `rules` (pure evaluation engine), `alerts` (rules CRUD, alert state, outbox queueing), `channels` (email/webhook config + encrypted secrets), `notify` (SMTP/webhook delivery, retries) |
| `app/services/remote/` | Remote store providers behind one interface (`base.py`): `sftp` (paramiko, pinned host key), `ftp` (FTP/FTPS, `ftplib`), `webdav` and `gdrive` (httpx2), `s3` (boto3), `local` (backup repositories on external storage, Phase 11); `registry.py` holds the field specs, validation and factory |
| `app/services/config_transfer/` | Configuration export/import (Phase 12): `export` (projection by natural keys, sealed secrets), `importer` (validated single-transaction import, preview, replace mode, rollback) |
| `app/services/backup/` | Backup and restore (Phase 11): `crypto` (scrypt, HKDF, segmented AES-256-GCM), `repository` (on-target format), `sources` (files, media, system state, remote pull), `engine` (backup, verify, retention + GC, restore), `jobs` (CRUD, queueing, worker primitives) |
| `app/services/helper_client.py` | Thin `AF_UNIX` client for the privileged helper; maps helper errors to typed `AppError`s (503 unavailable, 400 validation/intent/operation) |
| `app/services/disks.py` | Disk/volume/RAID discovery parsing (`lsblk`, `findmnt`, `/proc/mdstat`, `smartctl`), preview + confirmed actions, audit |
| `app/helper/` | Privileged helper (root process): `operations.py` fixed allowlist, `server.py` Unix socket + `SO_PEERCRED`, `protocol.py` framing/intent digest, `__main__.py` entry |
| `app/models/` | `user`, `group`, `role`, `session`, `audit_event`, `app_setting`, `samba_share`, `public_upload`, `api_token`, `upload_session`, `file_change`, `remote` (connections, sync jobs, baselines, runs, conflicts), `media` (libraries, group grants, items, albums, change journal, transcodes), `monitoring` (metric samples, checks, alert rules, alerts, channels, notification outbox), `backup` (repositories, jobs, runs, snapshot catalogue, blob cache), `config_import` (import runs with encrypted pre-import snapshots) |
| `app/web/` | Jinja2 templates + modular JS (login, dashboard, users, groups, audit, profile + API tokens, files, samba, settings, uploads, public_upload, disks, api_tokens admin, remote_sync, gallery — also served as `/videos`, monitoring, alerts, backups, config) |

## Authentication and authorization

1. `POST /api/v1/auth/login` (or the `/login` form) verifies the password with Argon2id after a per-IP throttle check, then creates a server-side `auth_sessions` row. The database stores only SHA-256 of the token and of the CSRF token; the browser keeps the raw session token in an `HttpOnly` cookie.
2. Every request passes through `CSRFProtectMiddleware`: state-changing requests that carry the `nas_csrf` cookie must echo it (`X-CSRF-Token` header or `_csrf` form field), and authenticated requests must additionally match the digest bound to their session.
3. `get_current_user` resolves an `Authorization: Bearer` API token when the header is present (authoritative: a bad value is 401), otherwise the session cookie; it rejects revoked/expired sessions or tokens and deactivated users, and slides the idle expiry window. A token's permissions are its scopes ∩ the owner's effective permissions; `require_session_auth` keeps account-control endpoints (token management, password change, logout) session-only. `require_permission("permission")` guards each route: 401 anonymous, 403 without the permission.
4. Effective permissions = union of the permissions of the user's direct roles and of the roles granted to the user's groups (`app/services/security.py`).
5. Login abuse controls: persistent per-account lockout (threshold and cooldown in settings) plus an in-process per-IP sliding window (429). All outcomes are written to `audit_events`.

## Database

SQLAlchemy 2 with a naming convention so constraints are stable across SQLite and PostgreSQL. The backend is selected purely by `NAS_DATABASE_URL`; SQLite gets WAL, `foreign_keys=ON` and a busy timeout on every connection. Alembic runs in batch mode for SQLite `ALTER` support. Models are registered in `app/models/__init__.py`, and a test asserts that models and migrations have no drift. Timestamps stored by auth code are naive UTC wall-clock (SQLite serialises `DateTime` without an offset); `app/db/base.py` exposes both `utcnow()` and `utcnow_naive()`.

## Request lifecycle and observability

Every response carries `X-Request-ID`. A client-supplied id is honoured only if it matches `[A-Za-z0-9-]{1,64}`; otherwise a new one is generated. The id is in each log line and in the error envelope, and it is recorded on every audit event. Logs go to stdout (or natively to journald under systemd, Phase 13), to a rotated file and, best effort, to remote syslog (see Phase 13 below). Extra fields whose names look sensitive (`password`, `token`, `secret`, `authorization`, `api_key`, `credential`) are replaced with `***`.

## Deployment topology

```
nginx (TLS, X-Forwarded-For overwritten, limit_req)  deploy/nginx/nas-manager.conf
   └── 127.0.0.1:8080
systemd: nas-manager.service        (user nas-manager, no capabilities, sandboxed)
   └── uvicorn (proxy headers from NAS_FORWARDED_ALLOW_IPS only) → FastAPI app
       → SQLite in /var/lib/nas-manager
systemd: nas-manager-worker.service (user nas-manager, same sandbox, Nice=10)
   └── python -m app.worker → claims sync runs, media scans and transcodes
       from the same database, talks to remote stores, writes inside the
       managed file roots and the media cache, runs ffprobe/ffmpeg
systemd: nas-manager-helper.service (root, allowlisted operations only,
                                     host mount namespace)
   └── Unix socket at /run/nas-manager/helper.sock (0660 root:<group> in a
       0711 directory, SO_PEERCRED uid allowlist) ← the helper_client talks to this
```

`deploy/install.sh` installs this layout (code in `/opt/nas-manager`, configuration in `/etc/nas-manager`), `deploy/uninstall.sh` removes it, `deploy/nas-manager-cli` runs the CLI with the units' environment through `systemd-run`, and `deploy/smoke_test.py` (standard library only) checks a running installation.

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

## Photo gallery and media libraries (Phase 8)

- **Libraries.** A `media_libraries` row is either *personal* (one per user, folder `<NAS_MEDIA_DIR>/<username>`, created the first time the user opens the gallery or uploads) or *shared* (registered by an administrator over an existing folder inside a managed file root and granted to groups through `media_library_groups`). Paths are recomputed and re-validated through `files.resolve_root`/`resolve_in_root` on every use, so a root removed from configuration or a folder swapped for an outside symlink is refused, never followed.
- **Access rules** (`app/services/media/libraries.py::browsable_clause`). A user browses their own personal library and the shared libraries granted to one of their groups; `media:admin` additionally browses every shared library and sees every library's *record* (counts, scan state) but not the photos in other users' personal libraries. Disabled libraries are hidden. Only the owner (with `media:write`) uploads to or deletes from a personal library; shared libraries are read-only through the gallery. Every item, thumbnail, stream and album lookup is filtered by the same clause, so anything else is a 404.
- **Index.** `app/services/media/indexer.py` walks a library without following symlinks, skips hidden entries (which includes the application's `.nas-*` temp files and `.nas-sync-archive/`) and anything outside the image/video extension allowlists, and only re-reads files whose size or mtime changed. New and changed files get a SHA-256, Pillow metadata (dimensions, EXIF date, camera, orientation, GPS) and, for videos, ffprobe metadata (Phase 9); vanished files lose their row. Guards: a missing library folder fails the scan; a walk that was stopped, hit an unreadable directory or found nothing where ≥ 10 items were indexed (an unmounted disk) removes nothing. Every change appends a `media_changes` row (the mobile cursor).
- **Thumbnails.** `app/services/media/thumbnails.py` renders upright (EXIF-orientation-aware) 320 px and 1280 px WebP files into `<NAS_MEDIA_CACHE_DIR>/thumbs/<id % 256>/<id>-<fingerprint>-<size>.webp`, where the fingerprint hashes the source's size and mtime; a stale thumbnail therefore can never be served. Images above `NAS_MEDIA_MAX_IMAGE_PIXELS` are never decoded (Pillow's decompression-bomb check is promoted to an error). The worker renders pending thumbnails in batches; the thumbnail endpoint renders a missing one on demand.
- **Uploads and mobile sync.** `app/services/media/uploads.py` writes into the caller's personal library (`YYYY/MM` folders by default) through a temp file and a hard link (falling back to rename), so a name is never overwritten even by concurrent uploads; the resumable upload protocol gained a `media` target that ends in the same commit helper. `POST /media/sync/lookup` tells a phone which SHA-256 digests the server already has; `GET /media/sync/changes` pages the journal per library with the same cursor/reset semantics as the Phase 6 file journal.
- **Worker.** Scans use the sync-run pattern: a conditional `UPDATE … WHERE scan_state='queued'` claims a library, the worker renews the lease every tick, and an expired lease re-queues the scan (scans are idempotent). Libraries are rescanned every `NAS_MEDIA_SCAN_INTERVAL_MINUTES`; scans, thumbnails and transcodes run on their own single-thread pools so they never take a sync slot. Hourly pruning trims the media journal and orphaned cache files.
- Migration `0008` adds `media_libraries`, `media_library_groups`, `media_items`, `media_albums`, `media_album_items`, `media_changes` and the `media:read`/`media:write`/`media:admin` permissions (admin all three; operator and viewer read + write).

## Video playback and streaming (Phase 9)

- **ffmpeg isolation.** `app/services/media/ffmpeg.py` is the only code that runs ffprobe/ffmpeg: fixed argument lists, no shell, inputs and outputs passed as `file:<absolute path>` (so a file name can never become an option or another protocol), a minimal environment, `stdin=/dev/null`, timeouts, and a kill on worker shutdown. Binaries come from `NAS_FFMPEG_PATH`/`NAS_FFPROBE_PATH` or a `PATH` lookup; without them videos are still indexed (`playback: unknown`) and downloadable, and the next scan after ffmpeg appears probes them.
- **Playback modes** (`app/services/media/video.py`). H.264 with AAC/MP3 (or no audio) in MP4/M4V/MOV, and VP8/VP9/AV1 with Opus/Vorbis (or no audio) in WebM, are `direct`: `GET /media/items/{id}/stream` serves the original with HTTP Range, so the browser's `<video>` element seeks without any server-side work. Any other video stream is `transcode`; a file without a video stream (cover art does not count) is `unsupported`.
- **Transcodes** (`app/services/media/transcode.py`, table `media_transcodes`). A transcode is requested on demand (anyone who can watch the video) or for a whole library (`media:admin`) and executed by the worker: libx264 (`NAS_MEDIA_TRANSCODE_PRESET`, `_CRF`), height capped at `NAS_MEDIA_TRANSCODE_MAX_HEIGHT`, AAC 128k stereo, `+faststart`, progress parsed from `-progress pipe:1`. Output goes to a temp file and is renamed to `<NAS_MEDIA_CACHE_DIR>/transcodes/<id % 256>/<id>-<fingerprint>.mp4`; a source change makes the rendition stale (never served) and a new request re-transcodes. Claims, lease renewal and reclaim mirror scans; a transcode interrupted by shutdown is re-queued, a failed one records the ffmpeg error and can be retried. The cache is evicted least recently streamed first above `NAS_MEDIA_TRANSCODE_CACHE_MAX_BYTES`. No HLS/adaptive streaming: one progressive MP4 per video.
- Migration `0009` adds `media_items.playback`/`rotation` and `media_transcodes`; no new permissions.

## System monitoring and alerting (Phase 10)

- **The worker observes, the web reads.** Each monitor pass runs on the worker's single-thread *monitor* pool. A host sample (`metric_samples`: CPU, load per core, memory, swap, network counters and per-filesystem usage for the data dir and file roots) is taken every `NAS_MONITOR_SAMPLE_SECONDS`. Checks run every `NAS_MONITOR_CHECK_SECONDS`, and SMART runs every `NAS_MONITOR_SMART_INTERVAL_MINUTES`. Check results are upserted into `monitor_checks` (one row per service, array, disk and the helper). Pages read those snapshots, so a page view never runs `systemctl` or reaches the helper; only the live summary, processes and `/proc/mdstat` are read on request, because they are cheap and unprivileged.
- **Sources.**
  - Services: `systemctl show` with a fixed argv, unit names validated against `UNIT_NAME_RE` plus `--`, no shell and a timeout. No systemctl or D-Bus means `unknown`, never an error.
  - RAID: `/proc/mdstat`, through the Phase 5 parser.
  - SMART: the helper's existing `disk.list`/`disk.smart` operations; nothing new is added to the root allowlist.
  - Processes: psutil, without command lines.
- **Rules engine** (`rules.py`, pure). Observations are threshold values (CPU, load per core, memory, swap, disk usage per path) or conditions (RAID degraded, RAID member failed, SMART failed, service down, helper unavailable). An active observation opens a `pending` alert, which becomes `firing` after the rule's `duration_seconds`. A pending alert that clears is dropped. A firing threshold alert resolves only past a 5 % hysteresis margin. A target that is no longer observed keeps its alert for an hour before it is resolved. `unknown` checks never open or resolve anything.
- **Notifications.** When an alert fires or resolves, `alerts.py` queues one `alert_notifications` row per enabled channel whose `min_severity` matches (resolutions only to channels with `send_resolved`). The worker's *notify* pool delivers them with exponential backoff (1, 2, 4, … minutes, at most 1 h) for up to `NAS_ALERT_NOTIFY_MAX_ATTEMPTS`. After that the row is marked failed and the failure is audited. Channels are email (SMTP with STARTTLS or TLS) or webhook (JSON POST, optional HMAC-SHA256 signature). Their secrets are one Fernet token, using the same key file as remote credentials.
- **Who sees what.** `system:read` (admin, operator) covers the monitoring pages, alerts, rules and channel metadata. `system:write` (admin) is needed to acknowledge alerts and change rules and channels. Channel create/update/test also need a session (API tokens are refused). The worker's own liveness is reported by the web process as a synthetic `worker_offline` entry in `/alerts/summary`, since a dead worker cannot notify.
- Migration `0010` adds `metric_samples`, `monitor_checks`, `alert_rules` (seeded with 10 built-in rules), `alerts`, `alert_channels` and `alert_notifications`. No new permissions.

## Backup and restore (Phase 11)

- **Repository format** (`app/services/backup/repository.py`). A repository on any target is self-describing: `repo.json` (format, scrypt parameters, a sealed key-check value), `snapshots/<id>.snap` (sealed, zlib-compressed JSON manifests) and `blobs/<aa>/<name>` (sealed file content). Blob names are `HMAC-SHA256(name key, SHA-256 of the content)`, so content is stored once per repository and the target never sees file names or content hashes. A snapshot is written after all its blobs exist.
- **Crypto** (`crypto.py`). scrypt → master key → HKDF-SHA256 → content key + naming key. AES-256-GCM in 4 MiB segments, each with a random nonce and its index and final-segment flag as associated data, so large files stream in constant memory and truncation, reordering and tampering are detected. The repository passphrase is stored Fernet-encrypted (`backup_repositories.passphrase_encrypted`), like remote credentials.
- **Targets.** Remote repositories reuse the Phase 7 providers through `remotes.build_provider`. External storage uses `app/services/remote/local.py` (`LocalProvider`), jailed to `NAS_BACKUP_LOCAL_ROOTS` with no symlink following and fsync'ed atomic writes; it refuses to connect when the directory is missing (an unmounted disk), and `Repository.open` refuses a target without `repo.json` instead of creating one, so a backup never lands on the root filesystem by accident. The provider interface gained `delete(path)` (Drive: move to trash), used **only** for retention and garbage collection inside a repository; sync still never deletes.
- **Sources** (`sources.py`). `files` and `media` reuse the sync scanner (no symlinks, temp files skipped, exclude patterns); `system` stages a consistent SQLite copy (`sqlite3` backup API + `PRAGMA integrity_check`), the credentials key and `smb.conf`; `remote` (pull) walks a connection and downloads each file to staging on demand.
- **Engine** (`engine.py`). `run_backup` reuses unchanged entries of the job's previous snapshot (same size and mtime, blob known), encrypts the rest into a staging temp file while hashing, uploads only unknown blobs and records them in `backup_blobs` (a cache), then writes the manifest and the `backup_snapshots` catalogue row. `run_verify` rebuilds the catalogue and blob cache from the target and checks presence or (full) decrypts and hashes every referenced blob. `run_prune` applies keep-last/daily/weekly/monthly per job (`select_retained`, pure) and garbage-collects unreferenced blobs, refusing if any snapshot is unreadable. `run_restore` writes through temp file → SHA-256 check → atomic rename, skips existing files unless `overwrite`, and moves replaced files to `.nas-backup-replaced/<stamp>/`.
- **Jobs and runs** (`jobs.py`). Mirrors Phase 7: the web process records repositories, jobs and queued runs; the worker's single-thread *backup* pool claims them with a conditional `UPDATE` that also requires no other running run on the same repository, renews leases each tick, reclaims dead runs and queues scheduled backups. Initializing a repository and listing a snapshot's entries are the only synchronous target accesses from the web process.
- **System restore** is staged by the worker (`<staging>/restore/<run>/` + `restore.json` with checksums) and installed by `python -m app.cli restore-system` with the services stopped; `python -m app.cli backup-recover` reads a repository directly for disaster recovery.
- Migration `0011` adds `backup_repositories`, `backup_jobs`, `backup_runs`, `backup_snapshots`, `backup_blobs` and the `backup:read` (admin, operator), `backup:write` and `backup:restore` (admin) permissions.

## Configuration export and import (Phase 12)

- **Export** (`app/services/config_transfer/export.py`). `collect` projects every configuration table onto plain objects keyed by natural keys (role/group/connection/repository names, usernames, uploader keys), with references by name, and separates the secrets (password hashes, decrypted remote credentials, channel secrets, backup passphrases). With a passphrase the secrets and a SHA-256 of the canonical `sections` are sealed with the Phase 11 crypto (scrypt + AES-256-GCM); without one they are dropped. Runtime state (sessions, tokens, audit, journals, indexes, run history, counters, timestamps, `instance_id`) is never exported.
- **Import** (`importer.py`). One handler per section, run in dependency order inside a single transaction with flushes. Handlers reuse the services' validators (`validate_group_name`, `ROLE_NAME_RE`, `validate_permissions`, `validate_username`, Samba `validate_share_*`, the remote registry's `validate_config`, `channels.validate`, alert `_validate_rule`, sync and backup job validators, the file-root and media jails) but write rows directly, because the services commit per object. Problems are collected per object; any problem rolls the whole transaction back. `preview` runs the same code and always rolls back. After the sections, the importer checks that an administrator remains and that the importing user is still an active administrator.
- **Replace mode** deletes, per section present in the document, objects missing from it, limited to plain configuration (`_REPLACEABLE`); deletions check active runs and references first.
- **Rollback.** Before committing, `apply` stores a `config_import_runs` row with the plan (what was created, updated, deleted), the warnings, a Fernet-encrypted pre-import snapshot (`snapshot_document`: the same projection with plaintext secrets) and, for SQLite, a file copy of the database made with the SQLite backup API. `rollback` (latest applied import only) deletes the created objects in reverse dependency order and re-applies the snapshot in merge mode, again in one transaction.
- **Not automatic:** `smb.conf` is not rewritten (the Samba page applies it with `testparm` and a backup), and imported backup repositories start uninitialized.
- Migration `0012` adds `config_import_runs` and the `config:export` / `config:import` permissions (admin).

## Syslog and comprehensive auditing (Phase 13)

```
record_audit_event ──► audit_events row ──commit──► "app.audit" log record ──► file / journald / console
                              │                                       (rolled back ⇒ nothing logged)
                              ▼
        worker audit pool: seal (hash chain) ─► forward (per-destination cursor) ─► remote syslog
                           daily verify, hourly prune (sealed + forwarded + older than retention)

any log record ≥ level ──► RemoteLogHandler queue (bounded, non-blocking) ──► remote syslog (best effort)
```

- **Search** (`app/services/audit.py`). One `AuditFilters` value drives the list, the count and the export, so they never disagree. The export streams keyset-paginated chunks from its own session.
- **Log stream.** `record_audit_event` remembers the row on the session; SQLAlchemy `after_flush` captures the id and `after_commit` logs it on `app.audit` (INFO success, WARNING failure, `audit_*` extras), `after_rollback` discards it. Existing `commit=False` callers are unchanged.
- **Hash chain** (`app/services/audit_chain.py`). The worker seals events in id order: `chain_hash = sha256(previous + canonical JSON)`. The canonical form excludes `actor_user_id` (it is `ON DELETE SET NULL`). `app_settings` holds the anchor (the newest pruned event, so the chain survives retention) and the head (so truncating the tail is detected). Verification reads with `populate_existing`, so it compares the database, not cached objects.
- **Retention.** `prune` removes a contiguous id prefix of events that are past `NAS_AUDIT_RETENTION_DAYS`, sealed and at or below every enabled destination's cursor, always keeps the newest event (SQLite would otherwise reuse ids below the anchor and the cursors), moves the anchor in the same transaction and records `audit.pruned`.
- **Remote syslog** (`app/services/syslog/`). `format` renders RFC 5424/3164; `transport` frames and sends over UDP, TCP or verified TLS; `destinations` validates and versions the configuration (`syslog.config_version` in `app_settings`); `forwarder` (worker) sends audit events above each destination's cursor and moves it after every accepted batch (at-least-once), with exponential backoff and transition-only audit events; `log_handler` is a root-logger handler in every process that queues formatted records for destinations with `forward_logs` and sends them from a daemon thread. It excludes `app.audit` (forwarded reliably by the worker) and its own package (no feedback loop). The web process reloads it after each change; the worker on the next tick when the version moved.
- **Journald** (`app/core/journald.py`). The native datagram protocol (stdlib only) with `SYSLOG_IDENTIFIER`, `PRIORITY`, `CODE_*`, `REQUEST_ID` and every extra as an upper-case `NAS_*` field. `NAS_LOG_JOURNALD=auto` turns it on when systemd sets `JOURNAL_STREAM`, and then the console handler is skipped.
- **Rotation.** Size (`RotatingFileHandler`) or daily at midnight UTC (`TimedRotatingFileHandler`), optional gzip through `namer`/`rotator`. Each process keeps its own file.
- **Coverage.** `require_permission` audits denials (`auth.permission_denied`, throttled per user, permission and route template). `tests/test_audit_coverage.py` lists every state-changing route with its event or an exemption, so a new unaudited route fails the build.
- Migration `0013` adds `syslog_destinations`, `audit_events.chain_hash` and an index on `actor_username`. No new permissions.

## Security hardening and deployment (Phase 14)

- **Pre-body admission** (`app/api/v1/public_uploads.py`, `PublicUploadGuardMiddleware`). FastAPI parses a multipart body before any dependency or route code runs, so the drop endpoint's limits are a `BaseHTTPMiddleware` that sees only the headers: the declared `Content-Length`, the uploader identity (the same `_identity` as the route, resolved in a thread with its own session), a `SlidingWindowThrottle` in `app.state.public_upload_throttle` and an advisory quota check. Rejections use the normal error envelope. It sits inside CSRF, and the route's checks remain authoritative.
- **Throttles** (`app/services/throttle.py`). `SlidingWindowThrottle` (formerly `IpLoginThrottle`, kept as an alias) keys by any string and caps tracked keys, evicting expired then least recently seen keys. One instance each for logins, bearer failures and the drop endpoint; `DenialThrottle` (generic keys) limits how often rejections are audited.
- **Proxy and Host trust.** `app/__main__.py` passes `proxy_headers`/`forwarded_allow_ips` from `NAS_FORWARDED_ALLOW_IPS` (empty disables proxy headers, since uvicorn would otherwise fall back to its own default) and turns off the `Server` header. `NAS_TRUSTED_HOSTS` adds Starlette's `TrustedHostMiddleware` inside the request-context middleware, so refusals still get a request id and the security headers. `RequestContextMiddleware` adds HSTS only when the effective scheme is HTTPS.
- **Startup checks.** `production_warnings(settings)` in `app/main.py` lists unsafe production choices and is logged at startup; `tighten_umask()` (`app/core/config.py`) runs first in all three entry points.
- **Units.** The helper runs in the host mount namespace: a disk mounted inside a private namespace is invisible to Samba, the web unit and the shell when propagation does not reach the host (the live test hit exactly that). It therefore carries only directives that do not create a mount namespace. `prepare_socket_dir` creates a missing socket directory `0711` and never re-modes an existing one (it used to `chmod 0750` whatever directory the socket was configured in).
- **Tests.** `tests/test_hardening.py` covers the code changes. `tests/test_deploy.py` checks the unit files' sandbox invariants, runs `systemd-analyze verify` and `bash -n` on the scripts, and runs the smoke test against a real uvicorn server. `tests/test_config.py` requires every setting to be documented in both env templates.
