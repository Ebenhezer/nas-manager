from __future__ import annotations

from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.base import utcnow_naive
from app.models import AuditEvent, AuthSession, User
from app.services.auth_service import token_digest

from .conftest import ADMIN_PASSWORD


def _audit_events(app: FastAPI, event: str) -> list[AuditEvent]:
    from sqlalchemy import select

    with app.state.db.session_factory() as session:
        return list(
            session.execute(
                select(AuditEvent).where(AuditEvent.event == event).order_by(AuditEvent.id)
            )
            .scalars()
            .all()
        )


def _users(app: FastAPI) -> list[User]:
    from sqlalchemy import select

    with app.state.db.session_factory() as session:
        return list(session.execute(select(User)).scalars().all())


def test_login_success_sets_cookies_and_audits(app: FastAPI, admin_user: User) -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": ADMIN_PASSWORD},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["username"] == "admin"
    assert "admin" in body["roles"]
    assert "users:write" in body["permissions"]
    assert body["csrf_token"]
    assert "nas_session" in response.cookies
    assert "nas_csrf" in response.cookies
    success = _audit_events(app, "auth.login.success")
    assert len(success) == 1
    assert success[0].actor_username == "admin"


def test_login_failure_increments_counter_and_audits(app: FastAPI, admin_user: User) -> None:
    with TestClient(app) as client:
        for _ in range(2):
            response = client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "wrong password"},
            )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    admin = _users(app)[0]
    assert admin.failed_login_count == 2
    assert len(_audit_events(app, "auth.login.failure")) == 2
    # No plaintext password anywhere in the audit log.
    for event in _audit_events(app, "auth.login.failure"):
        assert "wrong password" not in str(event.details)


def test_unknown_username_is_unauthorized_and_recorded(app: FastAPI) -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login", json={"username": "ghost", "password": "whatever123"}
        )
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Invalid username or password"
    assert len(_audit_events(app, "auth.login.failure")) == 1


def test_lockout_after_threshold(app: FastAPI, admin_user: User) -> None:
    threshold = app.state.settings.login_lockout_threshold
    with TestClient(app) as client:
        responses = [
            client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "wrong password"}
            )
            for _ in range(threshold)
        ]
    assert responses[-1].status_code == 401
    message = responses[-1].json()["error"]["message"]
    assert "locked" in message.lower()
    admin = _users(app)[0]
    assert admin.failed_login_count == 0  # reset after the lockout tripped
    assert admin.locked_until is not None
    assert len(_audit_events(app, "auth.login.locked")) == 1


def test_locked_account_rejected_until_expiry(app: FastAPI, admin_user: User) -> None:
    from app.services.users_service import get_user_or_404

    with app.state.db.session_factory() as session:
        user = get_user_or_404(session, admin_user.id)
        user.locked_until = utcnow_naive() + timedelta(minutes=30)
        session.commit()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}
        )
    assert response.status_code == 401
    assert "locked" in response.json()["error"]["message"].lower()


def test_lockout_expiry_allows_login_again(app: FastAPI, admin_user: User) -> None:
    from app.services.users_service import get_user_or_404

    with app.state.db.session_factory() as session:
        user = get_user_or_404(session, admin_user.id)
        user.locked_until = utcnow_naive() - timedelta(seconds=1)
        user.failed_login_count = 0
        session.commit()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}
        )
    assert response.status_code == 200


def test_ip_throttling_returns_429(migrated_settings: Settings) -> None:
    """Exhaust the per-IP sliding window, then check the 429 and the audit row."""
    from app.main import create_app

    tight = migrated_settings.model_copy(update={"login_ip_max_attempts": 3, "environment": "test"})
    app = create_app(tight)
    with TestClient(app) as client:
        for _ in range(3):
            client.post(
                "/api/v1/auth/login", json={"username": "nobody", "password": "nope-nope-nope"}
            )
        response = client.post(
            "/api/v1/auth/login", json={"username": "nobody", "password": "nope-nope-nope"}
        )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "too_many_requests"
    assert len(_audit_events(app, "auth.login.rate_limited")) == 1


def test_inactive_user_cannot_log_in(
    app: FastAPI, admin_user: User, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    from app.services.users_service import create_user

    with app.state.db.session_factory() as session:
        created = create_user(
            session,
            username="dormant",
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=["viewer"],
            groups=[],
            settings=app.state.settings,
            actor=admin_user,
        )
        dormant_id = created.id
    response = authed_client.patch(
        f"/api/v1/users/{dormant_id}/active",
        json={"active": False},
        headers=csrf_header,
    )
    assert response.status_code == 200
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login", json={"username": "dormant", "password": ADMIN_PASSWORD}
        )
    assert login.status_code == 401


def test_me_requires_authentication(app: FastAPI) -> None:
    with TestClient(app) as client:
        assert client.get("/api/v1/auth/me").status_code == 401


def test_me_returns_identity(authed_client: TestClient) -> None:
    response = authed_client.get("/api/v1/auth/me")
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["username"] == "admin"
    assert "admin" in body["roles"]
    assert "users:write" in body["permissions"]
    assert body["session_expires_at"]


def test_logout_revokes_session(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    response = authed_client.post("/api/v1/auth/logout", headers=csrf_header)
    assert response.status_code == 204
    assert authed_client.get("/api/v1/auth/me").status_code == 401
    with app.state.db.session_factory() as session:
        from sqlalchemy import select

        row = session.execute(select(AuthSession)).scalar_one()
        assert row.revoked_at is not None
    assert len(_audit_events(app, "auth.logout")) == 1


def test_expired_session_is_rejected(app: FastAPI, authed_client: TestClient) -> None:
    token = authed_client.cookies["nas_session"]
    with app.state.db.session_factory() as session:
        from sqlalchemy import select

        row = session.execute(
            select(AuthSession).where(AuthSession.token_hash == token_digest(token))
        ).scalar_one()
        row.expires_at = utcnow_naive() - timedelta(seconds=1)
        session.commit()
    assert authed_client.get("/api/v1/auth/me").status_code == 401


def test_deleted_user_loses_access_immediately(
    app: FastAPI, admin_user: User, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    from app.services.users_service import create_user

    with app.state.db.session_factory() as session:
        victim = create_user(
            session,
            username="victim",
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=["viewer"],
            groups=[],
            settings=app.state.settings,
            actor=admin_user,
        )
        victim_id = victim.id
    with TestClient(app) as victim_client:
        response = victim_client.post(
            "/api/v1/auth/login", json={"username": "victim", "password": ADMIN_PASSWORD}
        )
        assert response.status_code == 200
        assert victim_client.get("/api/v1/auth/me").status_code == 200
    assert (
        authed_client.delete(f"/api/v1/users/{victim_id}", headers=csrf_header).status_code == 204
    )
    with app.state.db.session_factory() as session:
        from sqlalchemy import select

        remaining = (
            session.execute(select(AuthSession).where(AuthSession.user_id == victim_id))
            .scalars()
            .all()
        )
        assert remaining == []
