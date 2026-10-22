from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import User

from .conftest import ADMIN_PASSWORD


def _create_user(app: FastAPI, admin_user: User, username: str, role: str) -> User:
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


def _login(app: FastAPI, username: str) -> TestClient:
    client = TestClient(app)
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200
    return client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["nas_csrf"]}


def test_settings_default_to_off(authed_client: TestClient) -> None:
    body = authed_client.get("/api/v1/settings").json()
    assert body["operator_view_files_samba"] is False


def test_operator_locked_out_by_default(app: FastAPI, admin_user: User) -> None:
    _create_user(app, admin_user, "op1", "operator")
    client = _login(app, "op1")
    assert client.get("/api/v1/files/roots").status_code == 403
    assert client.get("/api/v1/samba/shares").status_code == 403
    assert client.get("/api/v1/samba/status").status_code == 403
    # Web pages: authenticated but not permitted -> 401 (existing convention).
    assert client.get("/files", follow_redirects=False).status_code == 401
    assert client.get("/samba", follow_redirects=False).status_code == 401


def test_viewer_cannot_flip_the_toggle(
    app: FastAPI, admin_user: User, authed_client: TestClient
) -> None:
    _create_user(app, admin_user, "viewer1", "viewer")
    client = _login(app, "viewer1")
    # The viewer role grants neither settings:read nor settings:write.
    assert client.get("/api/v1/settings").status_code == 403
    assert (
        client.put(
            "/api/v1/settings",
            json={"operator_view_files_samba": True},
            headers=_csrf(client),
        ).status_code
        == 403
    )


def test_admin_toggle_grants_read_only_view(
    app: FastAPI, admin_user: User, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    _create_user(app, admin_user, "op1", "operator")
    response = authed_client.put(
        "/api/v1/settings", json={"operator_view_files_samba": True}, headers=csrf_header
    )
    assert response.status_code == 200
    assert response.json()["operator_view_files_samba"] is True

    client = _login(app, "op1")
    assert client.get("/api/v1/files/roots").status_code == 200
    assert client.get("/api/v1/samba/shares").status_code == 200
    assert client.get("/api/v1/samba/status").status_code == 200
    assert client.get("/files", follow_redirects=False).status_code == 200
    assert client.get("/samba", follow_redirects=False).status_code == 200

    # Read-only: every write action stays forbidden.
    headers = _csrf(client)
    assert (
        client.post(
            "/api/v1/files/mkdir",
            json={"root": "/", "path": "", "name": "x"},
            headers=headers,
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/samba/shares",
            json={"name": "media", "path": "/tmp"},  # noqa: S108 - rejected before any filesystem access
            headers=headers,
        ).status_code
        == 403
    )
    assert client.post("/api/v1/samba/apply", headers=headers).status_code == 403
    assert (
        client.patch(
            "/api/v1/samba/shares/1",
            json={"comment": "x"},
            headers=headers,
        ).status_code
        == 403
    )


def test_toggle_off_restores_lockout(
    app: FastAPI, admin_user: User, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    _create_user(app, admin_user, "op1", "operator")
    authed_client.put(
        "/api/v1/settings", json={"operator_view_files_samba": True}, headers=csrf_header
    )
    client = _login(app, "op1")
    assert client.get("/api/v1/files/roots").status_code == 200
    authed_client.put(
        "/api/v1/settings", json={"operator_view_files_samba": False}, headers=csrf_header
    )
    assert client.get("/api/v1/files/roots").status_code == 403


def test_toggle_applies_via_group_carried_operator_role(
    app: FastAPI, admin_user: User, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    from app.services.groups_service import (
        create_group,
        replace_group_roles,
        replace_members,
    )

    user = _create_user(app, admin_user, "grpops", "viewer")
    with app.state.db.session_factory() as session:
        group = create_group(session, name="ops", description=None, actor=admin_user)
        replace_group_roles(session, group, role_names=["operator"], actor=admin_user)
        replace_members(session, group, user_ids=[user.id], actor=admin_user)

    authed_client.put(
        "/api/v1/settings", json={"operator_view_files_samba": True}, headers=csrf_header
    )
    client = _login(app, "grpops")
    assert client.get("/api/v1/files/roots").status_code == 200


def test_toggle_change_is_audited(authed_client: TestClient, csrf_header: dict[str, str]) -> None:
    authed_client.put(
        "/api/v1/settings", json={"operator_view_files_samba": True}, headers=csrf_header
    )
    page = authed_client.get("/api/v1/audit/events?event=settings.updated")
    assert page.status_code == 200
    events = page.json()["events"]
    assert any(
        e["event"] == "settings.updated" and e["details"]["operator_view_files_samba"] is True
        for e in events
    )
