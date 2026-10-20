from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import ADMIN_PASSWORD


def test_login_form_without_csrf_is_rejected(client: TestClient) -> None:
    # GET /login sets the csrf cookie; a POST without echoing it must fail.
    client.get("/login")
    response = client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failure"


def test_login_form_with_csrf_succeeds(client: TestClient) -> None:
    page = client.get("/login")
    assert "nas_csrf" in page.cookies
    token = client.cookies["nas_csrf"]
    response = client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD, "_csrf": token},
        follow_redirects=False,
    )
    # There is no admin yet in this database, so expect a 401, not a redirect.
    assert response.status_code == 401


def test_authenticated_post_without_csrf_is_rejected(authed_client: TestClient) -> None:
    response = authed_client.post("/api/v1/auth/logout")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failure"


def test_authenticated_post_with_wrong_csrf_is_rejected(
    authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    response = authed_client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "not-the-token"})
    assert response.status_code == 403


def test_authenticated_post_with_csrf_succeeds(
    authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    response = authed_client.post("/api/v1/auth/logout", headers=csrf_header)
    assert response.status_code == 204


def test_api_login_without_cookies_is_not_blocked(client: TestClient) -> None:
    """A cookie-less API client is not CSRF-attackable and must not be blocked."""
    response = client.post(
        "/api/v1/auth/login", json={"username": "nobody", "password": "whatever-1234"}
    )
    assert response.status_code == 401  # credential failure, not 403


def test_forms_are_parsed_after_csrf_middleware_reads_body(
    authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    """The middleware re-injects buffered form bodies so routes can parse them."""
    response = authed_client.post(
        "/api/v1/auth/logout",
        data={"_csrf": csrf_header["X-CSRF-Token"]},
        follow_redirects=False,
    )
    assert response.status_code == 204  # the form token satisfied the double-submit check


def test_safe_methods_never_require_csrf(authed_client: TestClient) -> None:
    assert authed_client.get("/api/v1/auth/me").status_code == 200


def test_csrf_failure_response_carries_request_id(client: TestClient) -> None:
    client.get("/login")
    response = client.post(
        "/login", data={"username": "x", "password": "y"}, follow_redirects=False
    )
    assert response.status_code == 403
    assert response.headers["X-Request-ID"]
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
