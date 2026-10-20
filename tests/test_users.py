from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import User

from .conftest import ADMIN_PASSWORD


def test_admin_can_create_list_and_fetch_users(
    authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    response = authed_client.post(
        "/api/v1/users",
        json={
            "username": "alice",
            "password": "alice-strong-password",
            "display_name": "Alice",
            "email": "alice@example.com",
            "roles": ["operator"],
        },
        headers=csrf_header,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["username"] == "alice"
    assert [r["name"] for r in body["roles"]] == ["operator"]
    assert body["is_active"] is True
    assert body["is_locked"] is False

    listing = authed_client.get("/api/v1/users")
    assert listing.status_code == 200
    assert {u["username"] for u in listing.json()} == {"admin", "alice"}

    fetched = authed_client.get(f"/api/v1/users/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["email"] == "alice@example.com"


def test_create_user_defaults_role_to_viewer(
    authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    response = authed_client.post(
        "/api/v1/users",
        json={
            "username": "bob",
            "password": "bob-strong-password",
            "display_name": None,
            "email": None,
        },
        headers=csrf_header,
    )
    assert response.status_code == 201
    assert response.json()["roles"][0]["name"] == "viewer"


def test_duplicate_username_conflicts(
    authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    payload = {
        "username": "carol",
        "password": "carol-strong-password",
        "display_name": None,
        "email": None,
    }
    assert authed_client.post("/api/v1/users", json=payload, headers=csrf_header).status_code == 201
    conflict = authed_client.post("/api/v1/users", json=payload, headers=csrf_header)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "conflict"
    # Case-insensitive uniqueness: "CAROL" collides with "carol".
    upper = authed_client.post(
        "/api/v1/users", json={**payload, "username": "CAROL"}, headers=csrf_header
    )
    assert upper.status_code == 409


def test_invalid_usernames_rejected(authed_client: TestClient, csrf_header: dict[str, str]) -> None:
    for name in ["with space", "bad!", "-leading", "@at", "has%"]:
        response = authed_client.post(
            "/api/v1/users",
            json={
                "username": name,
                "password": "valid-password-123",
                "display_name": None,
                "email": None,
            },
            headers=csrf_header,
        )
        assert response.status_code in (400, 422), name


def test_uppercase_username_is_normalised_to_lowercase(
    authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    response = authed_client.post(
        "/api/v1/users",
        json={
            "username": "UPPER-Case",
            "password": "valid-password-123",
            "display_name": None,
            "email": None,
        },
        headers=csrf_header,
    )
    assert response.status_code == 201
    assert response.json()["username"] == "upper-case"
    # The same account cannot be created twice, case-insensitively.
    second = authed_client.post(
        "/api/v1/users",
        json={
            "username": "Upper-Case",
            "password": "valid-password-123",
            "display_name": None,
            "email": None,
        },
        headers=csrf_header,
    )
    assert second.status_code == 409


def test_weak_password_rejected(authed_client: TestClient, csrf_header: dict[str, str]) -> None:
    response = authed_client.post(
        "/api/v1/users",
        json={
            "username": "weak",
            "password": "short",
            "display_name": None,
            "email": None,
            "roles": ["viewer"],
        },
        headers=csrf_header,
    )
    assert response.status_code in (400, 422)


def test_unknown_role_rejected(authed_client: TestClient, csrf_header: dict[str, str]) -> None:
    response = authed_client.post(
        "/api/v1/users",
        json={
            "username": "rogue",
            "password": "rogue-strong-password",
            "display_name": None,
            "email": None,
            "roles": ["superuser"],
        },
        headers=csrf_header,
    )
    assert response.status_code == 404


def test_operator_cannot_create_or_delete_users(app: FastAPI, admin_user: User) -> None:
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
        response = client.post(
            "/api/v1/auth/login", json={"username": "op", "password": ADMIN_PASSWORD}
        )
        assert response.status_code == 200
        headers = {"X-CSRF-Token": client.cookies["nas_csrf"]}
        create = client.post(
            "/api/v1/users",
            json={
                "username": "intruder",
                "password": "intruder-password",
                "display_name": None,
                "email": None,
            },
            headers=headers,
        )
        assert create.status_code == 403
        listing = client.get("/api/v1/users")
        assert listing.status_code == 200  # operator has users:read
        delete = client.delete(f"/api/v1/users/{admin_user.id}", headers=headers)
        assert delete.status_code == 403


def test_viewer_cannot_even_list_users(app: FastAPI, admin_user: User) -> None:
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
        assert client.get("/api/v1/users").status_code == 403
        assert client.get("/api/v1/groups").status_code == 403
        assert client.get("/api/v1/audit/events").status_code == 403


def test_cannot_delete_own_account(
    authed_client: TestClient, csrf_header: dict[str, str], admin_user: User
) -> None:
    response = authed_client.delete(f"/api/v1/users/{admin_user.id}", headers=csrf_header)
    assert response.status_code == 409
    assert "your own" in response.json()["error"]["message"]


def test_cannot_deactivate_own_account(
    authed_client: TestClient, csrf_header: dict[str, str], admin_user: User
) -> None:
    response = authed_client.patch(
        f"/api/v1/users/{admin_user.id}/active", json={"active": False}, headers=csrf_header
    )
    assert response.status_code == 409


def test_last_admin_cannot_lose_admin_role(
    authed_client: TestClient, csrf_header: dict[str, str], admin_user: User
) -> None:
    response = authed_client.post(
        f"/api/v1/users/{admin_user.id}/roles", json={"roles": ["viewer"]}, headers=csrf_header
    )
    assert response.status_code == 409
    assert "without an administrator" in response.json()["error"]["message"]


def test_delete_second_admin_is_allowed_when_other_admins_remain(
    app: FastAPI, admin_user: User, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    from app.services.users_service import create_user

    with app.state.db.session_factory() as session:
        second = create_user(
            session,
            username="second",
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=["admin"],
            groups=[],
            settings=app.state.settings,
            actor=admin_user,
        )
        second_id = second.id
    assert (
        authed_client.delete(f"/api/v1/users/{second_id}", headers=csrf_header).status_code == 204
    )


def test_reset_password_revokes_sessions(
    app: FastAPI, admin_user: User, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    from app.services.users_service import create_user

    with app.state.db.session_factory() as session:
        target = create_user(
            session,
            username="target",
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=["viewer"],
            groups=[],
            settings=app.state.settings,
            actor=admin_user,
        )
        target_id = target.id
    with TestClient(app) as other_client:
        response = other_client.post(
            "/api/v1/auth/login", json={"username": "target", "password": ADMIN_PASSWORD}
        )
        assert response.status_code == 200
        assert other_client.get("/api/v1/auth/me").status_code == 200
        reset = authed_client.post(
            f"/api/v1/users/{target_id}/reset-password",
            json={"new_password": "a-brand-new-password-92"},
            headers=csrf_header,
        )
        assert reset.status_code == 200
        assert other_client.get("/api/v1/auth/me").status_code == 401  # session revoked
    with TestClient(app) as login_client:
        old = login_client.post(
            "/api/v1/auth/login", json={"username": "target", "password": ADMIN_PASSWORD}
        )
        assert old.status_code == 401
        new = login_client.post(
            "/api/v1/auth/login", json={"username": "target", "password": "a-brand-new-password-92"}
        )
        assert new.status_code == 200


def test_change_own_password_requires_current_password(
    authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    change = authed_client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "not-my-password", "new_password": "another-password-99"},
        headers=csrf_header,
    )
    assert change.status_code == 400


def test_unlock_clears_lockout(
    app: FastAPI, admin_user: User, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    from datetime import timedelta

    from app.db.base import utcnow_naive
    from app.services.users_service import get_user_or_404

    with app.state.db.session_factory() as session:
        user = get_user_or_404(session, admin_user.id)
        user.locked_until = utcnow_naive() + timedelta(minutes=30)
        user.failed_login_count = 4
        session.commit()
    response = authed_client.post(
        f"/api/v1/users/{admin_user.id}/unlock", json={}, headers=csrf_header
    )
    assert response.status_code == 200
    assert response.json()["is_locked"] is False
