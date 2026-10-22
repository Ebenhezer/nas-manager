from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.web.routes import STATIC_DIR


@pytest.fixture
def page(authed_client: TestClient) -> TestClient:
    return authed_client


def test_dashboard_renders_when_authenticated(page: TestClient) -> None:
    response = page.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Dashboard" in response.text
    assert "NAS Manager" in response.text
    assert "/static/js/dashboard.js" in response.text


def test_dashboard_redirects_anonymous_users_to_login(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_login_page_is_public(client: TestClient) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    assert "Log in" in response.text
    assert 'name="_csrf"' in response.text


def test_login_page_sets_csrf_cookie(client: TestClient) -> None:
    response = client.get("/login")
    assert "nas_csrf" in response.cookies


def test_first_login_page_embeds_matching_csrf_token(client: TestClient) -> None:
    """Regression: form token and Set-Cookie must match on the very first load."""
    page = client.get("/login")
    cookie = page.cookies["nas_csrf"]
    assert cookie
    assert f'name="_csrf" value="{cookie}"' in page.text


def test_management_pages_require_authentication(client: TestClient) -> None:
    for path in ["/users", "/groups", "/audit", "/profile", "/files", "/samba", "/settings"]:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 302, path
        assert response.headers["location"] == "/login", path


def test_dashboard_has_no_inline_script_or_style(page: TestClient) -> None:
    """The CSP forbids inline code; templates must not rely on it."""
    html = page.get("/").text.lower()
    assert "<style" not in html
    assert " style=" not in html
    assert "onclick=" not in html
    inline_scripts = [s for s in html.split("<script")[1:] if "src=" not in s.split(">")[0]]
    assert inline_scripts == []


@pytest.mark.parametrize("path", ["/files", "/samba", "/settings"])
def test_phase3_pages_have_no_inline_script_or_style(page: TestClient, path: str) -> None:
    """Phase 3 pages must stay within the same CSP (no inline code)."""
    html = page.get(path).text.lower()
    assert "<style" not in html
    assert " style=" not in html
    assert "onclick=" not in html
    inline_scripts = [s for s in html.split("<script")[1:] if "src=" not in s.split(">")[0]]
    assert inline_scripts == []


@pytest.mark.parametrize(
    "path",
    [
        "/static/css/app.css",
        "/static/js/dashboard.js",
        "/static/js/api.js",
        "/static/js/format.js",
        "/static/js/theme.js",
        "/static/js/users.js",
        "/static/js/groups.js",
        "/static/js/audit.js",
        "/static/js/profile.js",
        "/static/js/files.js",
        "/static/js/samba.js",
        "/static/js/settings.js",
        "/static/vendor/bootstrap/bootstrap.min.css",
        "/static/vendor/bootstrap/bootstrap.bundle.min.js",
    ],
)
def test_static_assets_served(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 200


def test_static_path_traversal_blocked(client: TestClient, tmp_path: Path) -> None:
    secret = STATIC_DIR.parent / "routes.py"
    assert secret.exists()
    for attempt in ("/static/../routes.py", "/static/%2e%2e/routes.py", "/static/..%2froutes.py"):
        response = client.get(attempt)
        assert response.status_code in (400, 404)
        assert "APIRouter" not in response.text


def test_vendored_bootstrap_is_unmodified_5_3() -> None:
    css = (STATIC_DIR / "vendor/bootstrap/bootstrap.min.css").read_text()
    assert "Bootstrap  v5.3" in css[:200]
