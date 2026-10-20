from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import User

from .conftest import ADMIN_PASSWORD
from .conftest import login as do_login


def _make_user(app: FastAPI, admin_user: User, username: str) -> int:
    from app.services.users_service import create_user

    with app.state.db.session_factory() as session:
        user = create_user(
            session,
            username=username,
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=["viewer"],
            groups=[],
            settings=app.state.settings,
            actor=admin_user,
        )
        return user.id


def test_group_lifecycle_and_membership(
    app: FastAPI, admin_user: User, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    member_id = _make_user(app, admin_user, "member1")

    created = authed_client.post(
        "/api/v1/groups",
        json={"name": "family", "description": "Family share"},
        headers=csrf_header,
    )
    assert created.status_code == 201
    group = created.json()
    group_id = group["id"]
    assert group["name"] == "family"
    assert group["members"] == []

    members = authed_client.patch(
        f"/api/v1/groups/{group_id}/members", json={"user_ids": [member_id]}, headers=csrf_header
    )
    assert members.status_code == 200
    assert [m["id"] for m in members.json()["members"]] == [member_id]

    listed = authed_client.get("/api/v1/groups")
    assert listed.status_code == 200
    assert any(g["name"] == "family" for g in listed.json())

    deleted = authed_client.delete(f"/api/v1/groups/{group_id}", headers=csrf_header)
    assert deleted.status_code == 204
    assert authed_client.get(f"/api/v1/groups/{group_id}").status_code == 404


def test_group_roles_give_members_permissions(
    app: FastAPI, admin_user: User, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    member_id = _make_user(app, admin_user, "auditor")

    group = authed_client.post("/api/v1/groups", json={"name": "ops"}, headers=csrf_header).json()
    group_id = group["id"]
    authed_client.patch(
        f"/api/v1/groups/{group_id}/members", json={"user_ids": [member_id]}, headers=csrf_header
    )
    updated = authed_client.patch(
        f"/api/v1/groups/{group_id}/roles", json={"roles": ["operator"]}, headers=csrf_header
    )
    assert updated.status_code == 200
    assert [r["name"] for r in updated.json()["roles"]] == ["operator"]

    with TestClient(app) as member_client:
        do_login(member_client, "auditor", ADMIN_PASSWORD)
        assert member_client.get("/api/v1/users").status_code == 200  # operator:read


def test_operator_cannot_manage_groups(app: FastAPI, admin_user: User) -> None:
    from app.services.users_service import create_user

    with app.state.db.session_factory() as session:
        create_user(
            session,
            username="op",
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=["operator"],
            groups=[],
            settings=app.state.settings,
            actor=admin_user,
        )
    with TestClient(app) as client:
        do_login(client, "op", ADMIN_PASSWORD)
        headers = {"X-CSRF-Token": client.cookies["nas_csrf"]}
        assert (
            client.post("/api/v1/groups", json={"name": "sneaky"}, headers=headers).status_code
            == 403
        )
        listing = client.get("/api/v1/groups")
        assert listing.status_code == 200  # groups:read is allowed
        assert listing.json() == []


def test_group_deletion_blocked_when_it_is_sole_admin_vehicle(
    app: FastAPI, admin_user: User, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    """Deleting a group that confers the only remaining admin power must fail.

    Scenario: a user holds admin only through a group. The direct admin
    demotes itself (allowed while another admin remains), and that group
    becomes the last vehicle of admin power — deleting it must be refused.
    """
    member_id = _make_user(app, admin_user, "onlyadmin")

    group = authed_client.post("/api/v1/groups", json={"name": "power"}, headers=csrf_header).json()
    group_id = group["id"]
    assert (
        authed_client.patch(
            f"/api/v1/groups/{group_id}/members",
            json={"user_ids": [member_id]},
            headers=csrf_header,
        ).status_code
        == 200
    )
    assert (
        authed_client.patch(
            f"/api/v1/groups/{group_id}/roles",
            json={"roles": ["admin"]},
            headers=csrf_header,
        ).status_code
        == 200
    )

    # The direct admin demotes itself; onlyadmin is now the sole administrator.
    demoted = authed_client.post(
        f"/api/v1/users/{admin_user.id}/roles",
        json={"roles": ["viewer"]},
        headers=csrf_header,
    )
    assert demoted.status_code == 200

    with TestClient(app) as client:
        do_login(client, "onlyadmin", ADMIN_PASSWORD)
        headers = {"X-CSRF-Token": client.cookies["nas_csrf"]}
        response = client.delete(f"/api/v1/groups/{group_id}", headers=headers)
        assert response.status_code == 409
        assert "without an administrator" in response.json()["error"]["message"]


def test_role_crud_and_permission_updates(
    authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    roles = authed_client.get("/api/v1/roles")
    assert roles.status_code == 200
    names = {r["name"] for r in roles.json()}
    assert {"admin", "operator", "viewer"} <= names

    created = authed_client.post(
        "/api/v1/roles",
        json={"name": "media", "description": "Media consumers", "permissions": ["dashboard:read"]},
        headers=csrf_header,
    )
    assert created.status_code == 201
    role_id = created.json()["id"]

    updated = authed_client.put(
        f"/api/v1/roles/{role_id}/permissions",
        json={"permissions": ["dashboard:read", "files:read"]},
        headers=csrf_header,
    )
    assert updated.status_code == 200
    assert updated.json()["permissions"] == ["dashboard:read", "files:read"]

    assert authed_client.delete(f"/api/v1/roles/{role_id}", headers=csrf_header).status_code == 204


def test_system_roles_are_immutable(authed_client: TestClient, csrf_header: dict[str, str]) -> None:
    roles = authed_client.get("/api/v1/roles").json()
    admin = next(r for r in roles if r["name"] == "admin")
    assert admin["system"] is True
    assert (
        authed_client.delete(f"/api/v1/roles/{admin['id']}", headers=csrf_header).status_code == 403
    )
    assert (
        authed_client.patch(
            f"/api/v1/roles/{admin['id']}", json={"description": "nope"}, headers=csrf_header
        ).status_code
        == 403
    )
    assert (
        authed_client.put(
            f"/api/v1/roles/{admin['id']}/permissions",
            json={"permissions": ["dashboard:read"]},
            headers=csrf_header,
        ).status_code
        == 403
    )


def test_unknown_permission_tokens_rejected(
    authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    created = authed_client.post(
        "/api/v1/roles",
        json={"name": "wildcard", "description": None, "permissions": ["everything:*"]},
        headers=csrf_header,
    )
    assert created.status_code in (400, 422)
