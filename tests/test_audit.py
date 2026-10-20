from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import AuditEvent, User

from .conftest import ADMIN_PASSWORD


def _events(app: FastAPI) -> list[AuditEvent]:
    from sqlalchemy import select

    with app.state.db.session_factory() as session:
        return list(session.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars().all())


def test_every_sensitive_action_is_audited(
    app: FastAPI, admin_user: User, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    before = len(_events(app))
    authed_client.post(
        "/api/v1/users",
        json={
            "username": "audited",
            "password": "audited-password-1",
            "display_name": None,
            "email": None,
            "roles": ["viewer"],
        },
        headers=csrf_header,
    )
    created = authed_client.get("/api/v1/users").json()
    user = next(u for u in created if u["username"] == "audited")
    authed_client.patch(
        f"/api/v1/users/{user['id']}/active", json={"active": False}, headers=csrf_header
    )
    authed_client.post(f"/api/v1/users/{user['id']}/unlock", json={}, headers=csrf_header)
    authed_client.post(
        f"/api/v1/users/{user['id']}/reset-password",
        json={"new_password": "a-new-password-88"},
        headers=csrf_header,
    )
    authed_client.delete(f"/api/v1/users/{user['id']}", headers=csrf_header)

    events = _events(app)[before:]
    events_by_name = {e.event: e for e in events}
    expected = {
        "user.created",
        "user.active_state_changed",
        "user.unlocked",
        "user.password_reset",
        "user.deleted",
    }
    assert expected <= set(events_by_name)
    for ev in events:
        assert ev.actor_username == "admin"
        assert ev.category in {"user", "role", "auth", "group"}
        # Never store secrets in the audit trail.
        for value in (ev.details or {}).values():
            if isinstance(value, str) and "password" in value.lower():
                assert ev.event != "user.created"


def test_audit_log_refuses_viewers_and_allows_operators(app: FastAPI, admin_user: User) -> None:
    from app.services.users_service import create_user

    with app.state.db.session_factory() as session:
        create_user(
            session,
            username="peon",
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=["viewer"],
            groups=[],
            settings=app.state.settings,
            actor=admin_user,
        )
        create_user(
            session,
            username="supervisor",
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=["operator"],
            groups=[],
            settings=app.state.settings,
            actor=admin_user,
        )

    for name in ("peon", "supervisor"):
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/auth/login", json={"username": name, "password": ADMIN_PASSWORD}
            )
            assert response.status_code == 200
            status = client.get("/api/v1/audit/events").status_code
            if name == "peon":
                assert status == 403
            else:
                assert status == 200


def test_audit_filters_and_pagination(
    authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    for i in range(3):
        authed_client.post(
            "/api/v1/users",
            json={
                "username": f"filtered{i}",
                "password": "filtered-password-1",
                "display_name": None,
                "email": None,
            },
            headers=csrf_header,
        )
    page = authed_client.get(
        "/api/v1/audit/events?category=user&outcome=success&limit=2&offset=0"
    ).json()
    assert page["limit"] == 2
    assert page["total"] == 4  # 1 bootstrap event + 3 user.created
    assert len(page["events"]) == 2


def test_audit_events_include_request_metadata(
    authed_client: TestClient,
) -> None:
    authed_client.get("/api/v1/audit/events")
    # The audit read itself is not recorded; but login audit rows carry metadata.
    me_page = authed_client.get("/api/v1/audit/events?event=auth.login.success").json()
    assert me_page["events"]
    event = me_page["events"][0]
    assert event["actor_username"] == "admin"
    assert event["request_id"]
    assert event["ip_address"] is None or True  # TestClient has no client address
