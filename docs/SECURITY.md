# Security

Status after Phase 6: sessions, scoped personal API tokens, RBAC, CSRF and audit logging are in place; the file manager (path-jailed, with resumable uploads and a change journal for sync), Samba share management (validated, merged config), the public upload folder (upload-only, atomic per-uploader and per-token quotas) and disk/storage/RAID management (read-only discovery; confirmed, intent-bound actions through a root helper) are implemented. All management pages and the interactive dashboard require authentication.

## Controls in place

| Control | Detail |
|---------|--------|
| Authentication | Argon2id password hashing (RFC 9106 low-memory profile, costs configurable). Server-side sessions; the database stores only SHA-256 of the session and CSRF tokens. Passwords are never stored, logged or echoed (tests assert it). |
| Login abuse controls | Per-account lockout after N failures (persistent) and a per-IP sliding window (`429` + audit event). Tested. |
| Sessions | `HttpOnly` cookie, `SameSite=Lax` (configurable), optional `Secure` (required when TLS terminates in front). Hard TTL plus idle ceiling; revocation on logout, password reset and deactivation. |
| CSRF | Double-submit cookie + session-bound token digest. Form field `_csrf` and `X-CSRF-Token` header both supported; first-page-load regression is tested. Cookie-less API clients and `Authorization: Bearer` requests are not CSRF-checked: they are authenticated by the header alone (cookies are ignored), and a browser cannot attach that header cross-site without a CORS grant, which the app never issues. Tested. |
| API tokens | `nas_` + 256 bits of randomness; only SHA-256 is stored and the raw value is shown once. Effective access = token scopes ∩ the owner's current permissions (re-evaluated per request), so a token never exceeds its owner and shrinks when the owner loses a role. Scopes outside the creator's permissions are refused. Mandatory expiry by default (`NAS_API_TOKEN_MAX_LIFETIME_DAYS`), per-user cap, revocation by owner or admin, automatic revocation on admin password reset and deactivation. Tokens cannot mint/revoke tokens, change the password or log out (session-only endpoints). An `Authorization` header is authoritative: a bad value is 401, never a fallback to the cookie. Failed lookups are rate limited per IP (429) and audited up to the limit. Web pages stay cookie-only. Tested. |
| Resumable uploads | Destination validated with the same jail and name rules at creation and re-checked at completion; size caps enforced at creation and per chunk; chunks are bounded, offset-checked (409 on mismatch), fsync'ed before the offset advances and rolled back on any limit violation; optional SHA-256 verification discards mismatching data; uploads are private to their owner (404 otherwise); `files:write` is re-checked on every step; expired uploads are swept. Public-target quotas are reserved under the same row lock as single-shot uploads at completion. Tested. |
| Authorization | Permission tokens (`dashboard:read` … `api_tokens:write`) granted through roles; effective permissions = direct roles + the roles of the user's groups. API routes deny by `require_permission`, web pages by permission checks. Tested per role. |
| Last-administrator safety | Cannot delete or deactivate your own account; cannot demote the final administrator; deleting a group that is the only vehicle of admin power is refused. Tested. |
| Audit trail | Append-only `audit_events`: actor (survives user deletion by username snapshot), outcome, target, IP, user agent and request id for every auth/user/group/role change. |
| Unprivileged process | systemd unit runs as `nas-manager`, empty capability set, `NoNewPrivileges`, `ProtectSystem=strict`, private `/tmp` and devices, syscall filter. |
| No secrets in code or repo | `.env.example` and `deploy/nas-manager.env.example` hold no real values; `.env` is git-ignored; the bootstrap password is only ever read from a prompt or stdin. |
| Log redaction | Sensitive extra-field names are replaced with `***` in all handlers (tested). Query strings are never logged. |
| Request-id validation | Client ids restricted to `[A-Za-z0-9-]{1,64}` to prevent log injection (tested). |
| Security headers | `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`, and `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; object-src 'none'` (tested). |
| CSP-compatible UI | No inline script or style; DOM is built with `textContent` (no `innerHTML`). A test fails if inline code appears. |
| File manager path jail | Every requested path is resolved against a configured managed root and rejected when it escapes (`..`, absolute paths, null bytes, symlinks resolving outside the root). Listing/search never follow symlinks; uploads stream to a temp file and are atomically renamed, and the per-file cap (`NAS_MAX_UPLOAD_BYTES`) is enforced mid-stream so concurrent uploads cannot bypass it. Tested, including symlink escape. |
| Public upload folder | Upload-only: there is no public listing, download or search endpoint; the public policy endpoint exposes only the enabled flag and the per-file cap. Filenames are sanitized (no separators/`..`/null bytes, ≤ 255 bytes) and auto-uniquified — the folder never overwrites. Files land in digest-named per-uploader directories; API-token clients are charged to their own `token:<id>` quota; per-file and per-uploader quotas are enforced atomically inside the transaction that records the file (the uploader's quota row is locked first), so concurrent uploads cannot bypass a quota and a rejected upload consumes nothing and leaves no temp file. Deletion paths are re-verified against the upload root. Tested. |
| Samba config safety | The DB is the source of truth; `smb.conf` is only touched inside the managed markers, validated with `testparm -s` against a temp copy before writing, backed up first, written atomically. Free-text share fields reject newlines (no config injection); share names are restricted to `[a-z0-9][a-z0-9._-]*`. When the privileged helper is enabled, Apply delegates the write + `smbd` reload to it (which re-validates with `testparm`, backs up and writes atomically before reloading). Tested. |
| Privileged helper isolation | The web process never runs as root and holds no capabilities. All privileged work happens in a separate helper bound to a fixed allowlist of 10 validated operations over a Unix socket (`SO_PEERCRED` UID check + `0660 root:<group>` socket; responses and requests are length-prefixed JSON, max 1 MiB). The helper never executes client-supplied commands or option flags: device names cannot start with `-`, mount points must be absolute and normalised, filesystem types and option tokens are allowlisted (options `loop`/`bind`/`rbind`/`remount` refused), RAID devices must exist under `/sys/block/md*`, and `samba.apply` only ever writes the configured `smb.conf` path after requiring the managed markers. Tested. |
| Confirmed destructive operations | Nothing is ever mounted/unmounted or scrubbed without the API user explicitly confirming (`confirm: true` on the action call), after a `…/preview` endpoint has shown the exact plan and warnings. The action is bound to the previewed parameters by a deterministic intent digest that the helper re-derives from its own re-validated parameters; mismatches are refused. RAID scrub only ever writes `check` (never `repair`) to `md/sync_action`. Tested. |
| Least privilege / operator toggle | `operator`/`viewer` cannot open Files/Samba by default. Admin may flip `operator_view_files_samba` in Settings to grant operators read-only access; the toggle never grants write permissions and is audited. Operators receive `disks:read` (read-only discovery) only; mounting/unmounting/scrubbing requires `disks:write` (admin by default). Tested. |
| Static file serving | Path traversal attempts against `/static` return 400/404 (tested). |
| Error hygiene | Unhandled exceptions return a generic 500 envelope with a request id and no internals (tested). |
| API docs | Swagger/OpenAPI disabled unless `NAS_DEBUG=true`. |
| Dependencies | Fully pinned runtime set in `requirements.txt` including `argon2-cffi` and `python-multipart`. |

## Operational guidance

- Run the web service behind a TLS-terminating reverse proxy and set `NAS_SESSION_COOKIE_SECURE=true` (the deploy template does). The service warns at startup when `environment=production` and the flag is false.
- Create the first administrator with `sudo -u nas-manager python -m app.cli bootstrap-admin` after install; there is no web endpoint that creates accounts before login.
- Treat API tokens like passwords: create one per device with only the scopes it needs, keep the default expiry, and revoke lost devices from Profile → My API tokens (admins: the API tokens page). Tokens travel in a header, so serve the API over TLS only.
- Guard `/etc/nas-manager/nas-manager.env` (root:nas-manager, 0640) — it is sensitive once `NAS_SESSION_COOKIE_SECURE` and other production overrides are set.

- Open the Disks page only with `NAS_HELPER_ENABLED=true` and the helper service running; otherwise discovery returns 503 and the page shows a "helper offline" badge. The helper socket lives at `/run/nas-manager/helper.sock` (`0660 root:nas-manager`, `SO_PEERCRED`-enforced); the service account UID must be listed in `NAS_HELPER_ALLOWED_UIDS`.

## Not yet implemented

Secret encryption at rest, TLS termination inside the app, dependency vulnerability scanning, rate limiting of the anonymous drop endpoint, and the syslog/journald forwarding of audit events (13). The resumable-upload lock and the bearer-failure throttle are in-process (single web process). fstab persistence / auto-mount of disks is intentionally out of scope.

## Rules for later phases (from the project instructions)

- The web process must never run as root; privileged actions (e.g. reloading `smbd`, disk administration) go through the allowlisted helper, which never executes client-supplied commands. The Samba apply step delegates to the helper when enabled and otherwise stops at the boundary and reports `reload_required`.
- No automatic format, partition, destructive mount, erase or RAID rebuild. Read-only discovery by default; dry-run and explicit confirmation for destructive operations (Phase 5 implements both).
- Never delete data silently during two-way sync.
- Public-upload quotas are keyed by client IP for anonymous clients and by token for API clients (Phase 6); rate limiting of the anonymous drop endpoint is follow-up hardening (Phase 14) — a public deployment should terminate TLS, may additionally rate-limit `/public/upload`, and should keep `NAS_PUBLIC_UPLOAD_ENABLED` auditable through the Settings page.
- No plaintext passwords, API secrets or cloud credentials; exports must protect secrets; imports must be validated and reversible.

## Reporting

Record findings in the issue tracker of the deployment. Track hardening work in `IMPLEMENTATION_PLAN.md` (Phase 14).
