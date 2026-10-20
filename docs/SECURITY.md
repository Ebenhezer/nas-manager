# Security

Status after Phase 2: sessions, RBAC, CSRF and audit logging are in place. The interactive dashboard and the management API require authentication.

## Controls in place

| Control | Detail |
|---------|--------|
| Authentication | Argon2id password hashing (RFC 9106 low-memory profile, costs configurable). Server-side sessions; the database stores only SHA-256 of the session and CSRF tokens. Passwords are never stored, logged or echoed (tests assert it). |
| Login abuse controls | Per-account lockout after N failures (persistent) and a per-IP sliding window (`429` + audit event). Tested. |
| Sessions | `HttpOnly` cookie, `SameSite=Lax` (configurable), optional `Secure` (required when TLS terminates in front). Hard TTL plus idle ceiling; revocation on logout, password reset and deactivation. |
| CSRF | Double-submit cookie + session-bound token digest. Form field `_csrf` and `X-CSRF-Token` header both supported; first-page-load regression is tested. Cookie-less API clients are not blocked (not CSRF-attackable). |
| Authorization | Permission tokens (`dashboard:read` … `samba:write`) granted through roles; effective permissions = direct roles + the roles of the user's groups. API routes deny by `require_permission`, web pages by permission checks. Tested per role. |
| Last-administrator safety | Cannot delete or deactivate your own account; cannot demote the final administrator; deleting a group that is the only vehicle of admin power is refused. Tested. |
| Audit trail | Append-only `audit_events`: actor (survives user deletion by username snapshot), outcome, target, IP, user agent and request id for every auth/user/group/role change. |
| Unprivileged process | systemd unit runs as `nas-manager`, empty capability set, `NoNewPrivileges`, `ProtectSystem=strict`, private `/tmp` and devices, syscall filter. |
| No secrets in code or repo | `.env.example` and `deploy/nas-manager.env.example` hold no real values; `.env` is git-ignored; the bootstrap password is only ever read from a prompt or stdin. |
| Log redaction | Sensitive extra-field names are replaced with `***` in all handlers (tested). Query strings are never logged. |
| Request-id validation | Client ids restricted to `[A-Za-z0-9-]{1,64}` to prevent log injection (tested). |
| Security headers | `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`, and `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; object-src 'none'` (tested). |
| CSP-compatible UI | No inline script or style; DOM is built with `textContent` (no `innerHTML`). A test fails if inline code appears. |
| Static file serving | Path traversal attempts against `/static` return 400/404 (tested). |
| Error hygiene | Unhandled exceptions return a generic 500 envelope with a request id and no internals (tested). |
| API docs | Swagger/OpenAPI disabled unless `NAS_DEBUG=true`. |
| Dependencies | Fully pinned runtime set in `requirements.txt` including `argon2-cffi` and `python-multipart`. |

## Operational guidance

- Run the web service behind a TLS-terminating reverse proxy and set `NAS_SESSION_COOKIE_SECURE=true` (the deploy template does). The service warns at startup when `environment=production` and the flag is false.
- Create the first administrator with `sudo -u nas-manager python -m app.cli bootstrap-admin` after install; there is no web endpoint that creates accounts before login.
- Guard `/etc/nas-manager/nas-manager.env` (root:nas-manager, 0640) — it is sensitive once `NAS_SESSION_COOKIE_SECURE` and other production overrides are set.

## Not yet implemented

The privileged helper (Phase 5), API tokens and mobile clients (6), secret encryption at rest, TLS termination inside the app, dependency vulnerability scanning, and the syslog/journald forwarding of audit events (13).

## Rules for later phases (from the project instructions)

- The web process must never run as root; privileged actions go through an allowlisted helper that never executes client-supplied commands.
- No automatic format, partition, destructive mount, erase or RAID rebuild. Read-only discovery by default; dry-run and explicit confirmation for destructive operations.
- Validate Samba configuration before reload; back up and preserve unmanaged settings.
- Prevent path traversal, public folder browsing, and quota bypass through concurrent uploads.
- Never delete data silently during two-way sync.
- No plaintext passwords, API secrets or cloud credentials; exports must protect secrets; imports must be validated and reversible.

## Reporting

Record findings in the issue tracker of the deployment. Track hardening work in `IMPLEMENTATION_PLAN.md` (Phase 14).
