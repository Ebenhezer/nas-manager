"""Personal API tokens (Phase 6): creation, bearer auth, scopes, revocation."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.base import utcnow_naive
from app.models import ApiToken, AuditEvent, User
from app.services.auth_service import token_digest
from tests.conftest import ADMIN_PASSWORD, login


def _create(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str = "phone",
    scopes: list[str] | None = None,
    **extra: object,
) -> dict[str, object]:
    response = client.post(
        "/api/v1/tokens",
        json={"name": name, "scopes": scopes or ["files:read"], **extra},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


def _bearer(raw: object) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


@pytest.fixture
def anon(app: FastAPI) -> Iterator[TestClient]:
    """A cookie-less client, like a mobile app or a script."""
    with TestClient(app) as test_client:
        yield test_client


def _make_user(app: FastAPI, admin_user: User, username: str, role: str) -> User:
    from app.services.users_service import create_user

    with app.state.db.session_factory() as session:
        return create_user(
            session,
            username=username,
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=[role],
            groups=[],
            settings=app.state.settings,
            actor=admin_user,
        )


def test_create_returns_raw_token_once_and_stores_only_digest(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    created = _create(authed_client, csrf_header, scopes=["files:read", "dashboard:read"])
    raw = str(created["token"])
    assert raw.startswith("nas_")
    assert created["token_prefix"] == raw[:12]
    assert created["scopes"] == ["dashboard:read", "files:read"]
    assert created["active"] is True
    assert created["expires_at"] is not None

    with app.state.db.session_factory() as session:
        row = session.execute(select(ApiToken)).scalar_one()
        assert row.token_hash == token_digest(raw)
        assert raw not in {row.token_hash, row.scopes_json}

    listing = authed_client.get("/api/v1/tokens").json()
    assert len(listing) == 1
    assert "token" not in listing[0]


def test_bearer_auth_without_cookies_or_csrf(
    authed_client: TestClient, csrf_header: dict[str, str], anon: TestClient
) -> None:
    raw = _create(authed_client, csrf_header, scopes=["files:read", "files:write"])["token"]
    me = anon.get("/api/v1/auth/me", headers=_bearer(raw))
    assert me.status_code == 200
    body = me.json()
    assert body["auth_method"] == "token"
    assert body["token_name"] == "phone"
    assert body["session_expires_at"] is None
    assert body["permissions"] == ["files:read", "files:write"]

    roots = anon.get("/api/v1/files/roots", headers=_bearer(raw)).json()
    # A state-changing call with no CSRF token at all.
    mkdir = anon.post(
        "/api/v1/files/mkdir", json={"root": roots[0], "name": "from-token"}, headers=_bearer(raw)
    )
    assert mkdir.status_code == 200, mkdir.text


def test_bearer_request_skips_csrf_even_with_browser_cookies(
    authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    raw = _create(authed_client, csrf_header, scopes=["files:read", "files:write"])["token"]
    roots = authed_client.get("/api/v1/files/roots").json()
    # authed_client still carries the session + CSRF cookies; no X-CSRF-Token.
    response = authed_client.post(
        "/api/v1/files/mkdir", json={"root": roots[0], "name": "bearer-wins"}, headers=_bearer(raw)
    )
    assert response.status_code == 200
    # Without the bearer header the same request is CSRF-checked.
    plain = authed_client.post("/api/v1/files/mkdir", json={"root": roots[0], "name": "nope"})
    assert plain.status_code == 403
    assert plain.json()["error"]["code"] == "csrf_failure"


def test_scopes_limit_token_access(
    authed_client: TestClient, csrf_header: dict[str, str], anon: TestClient
) -> None:
    raw = _create(authed_client, csrf_header, scopes=["files:read"])["token"]
    roots = anon.get("/api/v1/files/roots", headers=_bearer(raw))
    assert roots.status_code == 200
    mkdir = anon.post(
        "/api/v1/files/mkdir",
        json={"root": roots.json()[0], "name": "x"},
        headers=_bearer(raw),
    )
    assert mkdir.status_code == 403
    assert anon.get("/api/v1/users", headers=_bearer(raw)).status_code == 403


def test_token_access_shrinks_when_owner_loses_role(app: FastAPI, admin_user: User) -> None:
    _make_user(app, admin_user, "ops", "operator")
    with TestClient(app) as client:
        login(client, "ops", ADMIN_PASSWORD)
        headers = {"X-CSRF-Token": client.cookies["nas_csrf"]}
        raw = _create(client, headers, scopes=["users:read"])["token"]
    with TestClient(app) as anon:
        assert anon.get("/api/v1/users", headers=_bearer(raw)).status_code == 200
        from app.services.users_service import assign_user_roles

        with app.state.db.session_factory() as session:
            user = session.execute(select(User).where(User.username == "ops")).scalar_one()
            actor = session.get(User, admin_user.id)
            assert actor is not None
            assign_user_roles(session, user, role_names=["viewer"], actor=actor)
        assert anon.get("/api/v1/users", headers=_bearer(raw)).status_code == 403


def test_cannot_grant_scopes_beyond_own_permissions(app: FastAPI, admin_user: User) -> None:
    _make_user(app, admin_user, "viewer1", "viewer")
    with TestClient(app) as client:
        login(client, "viewer1", ADMIN_PASSWORD)
        headers = {"X-CSRF-Token": client.cookies["nas_csrf"]}
        assert client.get("/api/v1/tokens/scopes").json()["scopes"] == ["dashboard:read"]
        response = client.post(
            "/api/v1/tokens", json={"name": "x", "scopes": ["users:write"]}, headers=headers
        )
        assert response.status_code == 403
        assert response.json()["error"]["details"]["scopes"] == ["users:write"]
        unknown = client.post(
            "/api/v1/tokens", json={"name": "x", "scopes": ["bogus:read"]}, headers=headers
        )
        assert unknown.status_code == 400


def test_invalid_expired_revoked_tokens_are_401(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str], anon: TestClient
) -> None:
    created = _create(authed_client, csrf_header)
    raw = created["token"]
    bad = anon.get("/api/v1/auth/me", headers=_bearer("nas_" + "x" * 43))
    assert bad.status_code == 401
    assert bad.headers["www-authenticate"].startswith("Bearer")

    with app.state.db.session_factory() as session:
        row = session.get(ApiToken, created["id"])
        assert row is not None
        row.expires_at = utcnow_naive() - timedelta(seconds=1)
        session.commit()
    assert anon.get("/api/v1/auth/me", headers=_bearer(raw)).status_code == 401

    second = _create(authed_client, csrf_header, name="laptop")
    assert anon.get("/api/v1/auth/me", headers=_bearer(second["token"])).status_code == 200
    revoke = authed_client.delete(f"/api/v1/tokens/{second['id']}", headers=csrf_header)
    assert revoke.status_code == 204
    assert anon.get("/api/v1/auth/me", headers=_bearer(second["token"])).status_code == 401
    listing = {t["name"]: t for t in authed_client.get("/api/v1/tokens").json()}
    assert listing["laptop"]["active"] is False
    assert listing["laptop"]["revoked_at"] is not None


def test_malformed_authorization_header_never_falls_back_to_cookie(
    authed_client: TestClient,
) -> None:
    assert authed_client.get("/api/v1/auth/me").status_code == 200
    for header in ("Basic abc", "Bearer", "Bearer not-a-nas-token"):
        response = authed_client.get("/api/v1/auth/me", headers={"Authorization": header})
        assert response.status_code == 401, header


def test_failed_bearer_lookups_are_throttled_and_audited(app: FastAPI, anon: TestClient) -> None:
    app.state.token_throttle.max_attempts = 3
    codes = [
        anon.get("/api/v1/auth/me", headers=_bearer("nas_" + "y" * 43)).status_code
        for _ in range(5)
    ]
    assert codes == [401, 401, 401, 429, 429]
    limited = anon.get("/api/v1/auth/me", headers=_bearer("nas_" + "y" * 43))
    assert int(limited.headers["retry-after"]) >= 1
    with app.state.db.session_factory() as session:
        events = (
            session.execute(select(AuditEvent).where(AuditEvent.event == "api_token.auth_failed"))
            .scalars()
            .all()
        )
        assert len(events) == 3  # rate-limited attempts do not flood the log


def test_tokens_cannot_manage_tokens_or_the_account(
    authed_client: TestClient, csrf_header: dict[str, str], anon: TestClient
) -> None:
    created = _create(authed_client, csrf_header, scopes=["users:read"])
    headers = _bearer(created["token"])
    mint = anon.post(
        "/api/v1/tokens", json={"name": "y", "scopes": ["users:read"]}, headers=headers
    )
    assert mint.status_code == 403
    assert anon.delete(f"/api/v1/tokens/{created['id']}", headers=headers).status_code == 403
    password = anon.post(
        "/api/v1/auth/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "another long password"},
        headers=headers,
    )
    assert password.status_code == 403
    assert anon.post("/api/v1/auth/logout", headers=headers).status_code == 403
    # Listing its own owner's tokens is fine (no secrets are exposed).
    assert anon.get("/api/v1/tokens", headers=headers).status_code == 200


def test_name_uniqueness_cap_and_lifetime_limits(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    _create(authed_client, csrf_header, name="dup")
    again = authed_client.post(
        "/api/v1/tokens", json={"name": "DUP", "scopes": ["files:read"]}, headers=csrf_header
    )
    assert again.status_code == 409

    too_long = authed_client.post(
        "/api/v1/tokens",
        json={"name": "long", "scopes": ["files:read"], "expires_in_days": 400},
        headers=csrf_header,
    )
    assert too_long.status_code == 400
    never = authed_client.post(
        "/api/v1/tokens",
        json={"name": "never", "scopes": ["files:read"], "expires_in_days": 0},
        headers=csrf_header,
    )
    assert never.status_code == 400

    app.state.settings.api_token_max_lifetime_days = 0
    unlimited = _create(authed_client, csrf_header, name="forever", expires_in_days=0)
    assert unlimited["expires_at"] is None

    app.state.settings.api_token_max_per_user = 2
    capped = authed_client.post(
        "/api/v1/tokens", json={"name": "third", "scopes": ["files:read"]}, headers=csrf_header
    )
    assert capped.status_code == 409


def test_admin_oversight_and_operator_read_only(
    app: FastAPI, admin_user: User, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    _make_user(app, admin_user, "ops", "operator")
    with TestClient(app) as ops:
        login(ops, "ops", ADMIN_PASSWORD)
        ops_headers = {"X-CSRF-Token": ops.cookies["nas_csrf"]}
        ops_token = _create(ops, ops_headers, name="ops-cli", scopes=["users:read"])

        listing = ops.get("/api/v1/admin/tokens")
        assert listing.status_code == 200
        assert [t["username"] for t in listing.json()] == ["ops"]
        assert "token" not in listing.json()[0]
        denied = ops.delete(f"/api/v1/admin/tokens/{ops_token['id']}", headers=ops_headers)
        assert denied.status_code == 403

    assert authed_client.get("/api/v1/admin/tokens", params={"user_id": 999}).json() == []
    revoked = authed_client.delete(f"/api/v1/admin/tokens/{ops_token['id']}", headers=csrf_header)
    assert revoked.status_code == 204
    with TestClient(app) as anon:
        assert anon.get("/api/v1/auth/me", headers=_bearer(ops_token["token"])).status_code == 401
    # An owner cannot reach other users' tokens through /tokens.
    assert (
        authed_client.delete(f"/api/v1/tokens/{ops_token['id']}", headers=csrf_header).status_code
        == 404
    )

    with app.state.db.session_factory() as session:
        events = [
            row.event
            for row in session.execute(
                select(AuditEvent).where(AuditEvent.category == "api_token")
            ).scalars()
        ]
        assert events.count("api_token.created") == 1
        assert events.count("api_token.revoked") == 1


def test_password_reset_and_deactivation_revoke_tokens(app: FastAPI, admin_user: User) -> None:
    target = _make_user(app, admin_user, "mobile", "operator")
    with TestClient(app) as client:
        login(client, "mobile", ADMIN_PASSWORD)
        headers = {"X-CSRF-Token": client.cookies["nas_csrf"]}
        first = _create(client, headers, name="a", scopes=["users:read"])["token"]
        second = _create(client, headers, name="b", scopes=["users:read"])["token"]

    from app.services.users_service import reset_password, set_user_active

    with app.state.db.session_factory() as session:
        user = session.get(User, target.id)
        actor = session.get(User, admin_user.id)
        assert user is not None and actor is not None
        reset_password(
            session,
            user,
            new_password="a different long password",
            settings=app.state.settings,
            actor=actor,
        )
    with TestClient(app) as anon:
        assert anon.get("/api/v1/auth/me", headers=_bearer(first)).status_code == 401
        assert anon.get("/api/v1/auth/me", headers=_bearer(second)).status_code == 401

    with TestClient(app) as client:
        login(client, "mobile", "a different long password")
        headers = {"X-CSRF-Token": client.cookies["nas_csrf"]}
        third = _create(client, headers, name="c", scopes=["users:read"])["token"]
    with app.state.db.session_factory() as session:
        user = session.get(User, target.id)
        actor = session.get(User, admin_user.id)
        assert user is not None and actor is not None
        set_user_active(session, user, active=False, actor=actor)
    with TestClient(app) as anon:
        assert anon.get("/api/v1/auth/me", headers=_bearer(third)).status_code == 401


def test_last_used_is_recorded(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str], anon: TestClient
) -> None:
    created = _create(authed_client, csrf_header)
    assert created["last_used_at"] is None
    anon.get("/api/v1/auth/me", headers=_bearer(created["token"]))
    with app.state.db.session_factory() as session:
        row = session.get(ApiToken, created["id"])
        assert row is not None
        assert row.last_used_at is not None
        assert row.last_used_ip == "testclient"
