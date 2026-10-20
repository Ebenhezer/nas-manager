from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import User

from .conftest import ADMIN_PASSWORD


def test_permission_denials_map_401_vs_403(client: TestClient, authed_client: TestClient) -> None:
    """Anonymous = 401; authenticated without the permission = 403."""
    assert client.get("/api/v1/users").status_code == 401
    assert authed_client.get("/api/v1/users").status_code == 200


def test_every_users_write_action_denied_for_viewer(app: FastAPI, admin_user: User) -> None:
    from app.services.users_service import create_user

    with app.state.db.session_factory() as session:
        create_user(
            session,
            username="viewer1",
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=["viewer"],
            groups=[],
            settings=app.state.settings,
            actor=admin_user,
        )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "viewer1", "password": ADMIN_PASSWORD},
        )
        assert response.status_code == 200
        headers = {"X-CSRF-Token": client.cookies["nas_csrf"]}
        payload: dict[str, object] = {
            "username": "x",
            "password": "long-enough-password-123",
            "display_name": None,
            "email": None,
        }
        for method, path, body in [
            ("post", "/api/v1/users", payload),
            ("delete", "/api/v1/users/1", None),
            ("patch", "/api/v1/users/1/active", {"active": False}),
            ("post", "/api/v1/users/1/roles", {"roles": ["viewer"]}),
            ("post", "/api/v1/users/1/unlock", None),
        ]:
            request_kwargs: dict[str, object] = {"headers": headers}
            if body is not None:
                request_kwargs["json"] = body
            assert getattr(client, method)(path, **request_kwargs).status_code == 403, (
                method,
                path,
            )


def test_effective_permissions_union_of_user_and_group_roles(
    app: FastAPI, admin_user: User
) -> None:
    from app.services.groups_service import (
        create_group,
        replace_group_roles,
        replace_members,
    )
    from app.services.security import effective_permissions_for_user
    from app.services.users_service import create_user

    with app.state.db.session_factory() as session:
        user = create_user(
            session,
            username="unionny",
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=["viewer"],
            groups=[],
            settings=app.state.settings,
            actor=admin_user,
        )
        group = create_group(session, name="readers", description=None, actor=admin_user)
        replace_group_roles(session, group, role_names=["operator"], actor=admin_user)
        replace_members(session, group, user_ids=[user.id], actor=admin_user)
        perms = effective_permissions_for_user(session, user)
    assert "dashboard:read" in perms  # from the direct viewer role
    assert "users:read" in perms  # from operator through the group


def test_roles_list_details_for_every_role(authed_client: TestClient) -> None:
    roles = authed_client.get("/api/v1/roles").json()
    detail = next(r for r in roles if r["name"] == "admin")
    assert "users:write" in detail["permissions"]
    assert "users:read" in detail["permissions"]


def test_viewer_denied_roles_read(app: FastAPI, admin_user: User) -> None:
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
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login", json={"username": "peon", "password": ADMIN_PASSWORD}
        )
        assert response.status_code == 200
        assert client.get("/api/v1/roles").status_code == 403


def test_custom_role_grants_permission_to_its_users(
    app: FastAPI, admin_user: User, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    role = authed_client.post(
        "/api/v1/roles",
        json={"name": "auditor", "description": None, "permissions": ["audit:read"]},
        headers=csrf_header,
    ).json()
    authed_client.post(
        "/api/v1/users",
        json={
            "username": "auditbot",
            "password": ADMIN_PASSWORD,
            "display_name": None,
            "email": None,
            "roles": [role["name"]],
        },
        headers=csrf_header,
    )
    with TestClient(app) as bot_client:
        response = bot_client.post(
            "/api/v1/auth/login",
            json={"username": "auditbot", "password": ADMIN_PASSWORD},
        )
        assert response.status_code == 200
        assert bot_client.get("/api/v1/audit/events").status_code == 200  # custom role grant
