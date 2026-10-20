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

Codes in use: `http_error` (404, 405, ...), `validation_error` (422), `conflict` (409), `not_found` (404), `bad_request` (400), `unauthorized` (401), `forbidden` (403), `too_many_requests` (429), `csrf_failure` (403), `internal_error` (500).

- **Authentication**: except `GET /health` and the static assets, every endpoint requires a session cookie (`nas_session`, `HttpOnly`) obtained from `POST /auth/login` (JSON) or the `/login` form. Anonymous calls return `401`; an authenticated caller without the required permission returns `403`.
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
  "database": {"reachable": true, "schema_current": true, "revision": "0002", "head_revision": "0002"}
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

## Planned

Files and shares (Phase 3), upload quotas (4), tokens, resumable transfers and sync cursors (6). Each phase will extend this document.
