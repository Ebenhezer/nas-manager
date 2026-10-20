# Implementation Plan

Status legend: **Done** (implemented and verified), **Next**, **Planned**.

## Phases

| # | Phase | Status |
|---|-------|--------|
| 1 | Foundation, database, dashboard, configuration, logging, systemd deployment | **Done** |
| 2 | Authentication, users, groups, permissions, audit events | **Done** |
| 3 | File manager and Samba integration | **Next** |
| 4 | Public upload folder and quotas | Planned |
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

## Known limitations

- The management UI was exercised via TestClient and curl, not a real browser.
- Only SQLite has been exercised; PostgreSQL support is configuration-only so far.
- `instance_id` is stored but not yet used by any feature.
- The per-IP login limiter is in-process (single process; resets on restart). The account lockout is persistent in the database.
- Sessions are not yet exposed through the API for mobile clients (that is Phase 6 with tokens/resumable transfers).
- Monitoring is limited to CPU, memory, swap, two disk-usage entries and network byte counters. No processes, services, RAID or per-disk data (Phase 5 and 10).
- No background job framework yet (needed from Phase 3/7 onward).

## Next: Phase 3 (outline)

File manager and Samba integration: safe path handling (no traversal, share-root jail), upload/download/browse/search/rename/delete with audit, a Samba share model and service that renders and validates `smb.conf` (back up before writing, refuse invalid configs, only touch managed ranges), and tests against a disposable file tree (never real shares).
