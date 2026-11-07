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


def test_public_upload_page_is_public(client: TestClient) -> None:
    response = client.get("/public/upload")
    assert response.status_code == 200
    assert "public_upload.js" in response.text
    assert "Public upload" in response.text
    # Upload-only: no file list is ever rendered or fetched.
    assert "files?uploader_key" not in response.text


def test_first_login_page_embeds_matching_csrf_token(client: TestClient) -> None:
    """Regression: form token and Set-Cookie must match on the very first load."""
    page = client.get("/login")
    cookie = page.cookies["nas_csrf"]
    assert cookie
    assert f'name="_csrf" value="{cookie}"' in page.text


def test_management_pages_require_authentication(client: TestClient) -> None:
    for path in [
        "/users",
        "/groups",
        "/audit",
        "/profile",
        "/files",
        "/samba",
        "/settings",
        "/uploads",
        "/tokens",
        "/remote-sync",
        "/gallery",
        "/videos",
        "/monitoring",
        "/alerts",
        "/logging",
    ]:
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


@pytest.mark.parametrize(
    "path",
    [
        "/files",
        "/samba",
        "/settings",
        "/uploads",
        "/public/upload",
        "/tokens",
        "/profile",
        "/remote-sync",
        "/gallery",
        "/videos",
        "/monitoring",
        "/alerts",
        "/logging",
        "/audit",
    ],
)
def test_phase3_pages_have_no_inline_script_or_style(page: TestClient, path: str) -> None:
    """Phase 3/4 pages must stay within the same CSP (no inline code)."""
    html = page.get(path).text.lower()
    assert "<style" not in html
    assert " style=" not in html
    assert "onclick=" not in html
    inline_scripts = [s for s in html.split("<script")[1:] if "src=" not in s.split(">")[0]]
    assert inline_scripts == []


def test_change_password_fields_match_profile_js(page: TestClient) -> None:
    """profile.js reads FormData("current"/"new"); the inputs must carry those names."""
    html = page.get("/profile").text
    script = (STATIC_DIR / "js" / "profile.js").read_text()
    for name in ("current", "new"):
        assert f'name="{name}"' in html
        assert f'data.get("{name}")' in script


def test_remote_sync_page_has_every_element_its_script_uses(page: TestClient) -> None:
    """remote_sync.js looks elements up by id; an admin page must render all of them."""
    import re

    html = page.get("/remote-sync").text
    script = (STATIC_DIR / "js" / "remote_sync.js").read_text()
    ids = set(re.findall(r'getElementById\("([a-z0-9-]+)"\)', script))
    assert ids, "expected getElementById lookups"
    # cf-* are the connection form fields the script generates itself.
    missing = sorted(i for i in ids if f'id="{i}"' not in html and not i.startswith("cf-"))
    assert missing == []


def test_gallery_page_has_every_element_its_script_uses(page: TestClient) -> None:
    """gallery.js and its modules look elements up by id; the admin view must
    render all of them."""
    import re

    response = page.get("/gallery")
    assert response.status_code == 200
    html = response.text
    modules = sorted((STATIC_DIR / "js" / "gallery").glob("*.js"))
    sources = [STATIC_DIR / "js" / "gallery.js", *modules]
    script = "\n".join(path.read_text() for path in sources)
    ids = set(re.findall(r'getElementById\("([a-z0-9-]+)"\)', script))
    assert ids, "expected getElementById lookups"
    missing = sorted(i for i in ids if f'id="{i}"' not in html)
    assert missing == []
    assert 'href="/gallery"' in html  # nav link


def test_files_page_has_every_element_its_scripts_use(page: TestClient) -> None:
    """files.js and the gallery modules it imports look elements up by id."""
    import re

    response = page.get("/files")
    assert response.status_code == 200
    html = response.text
    sources = [STATIC_DIR / "js" / "files.js"] + [
        STATIC_DIR / "js" / "gallery" / name for name in ("util.js", "menu.js", "dialogs.js")
    ]
    script = "\n".join(path.read_text() for path in sources)
    ids = set(re.findall(r'getElementById\("([a-z0-9-]+)"\)', script))
    assert ids, "expected getElementById lookups"
    missing = sorted(i for i in ids if f'id="{i}"' not in html)
    assert missing == []


@pytest.mark.parametrize(
    ("path", "script"),
    [("/monitoring", "monitoring.js"), ("/alerts", "alerts.js"), ("/", "dashboard.js")],
)
def test_phase10_pages_have_every_element_their_script_uses(
    page: TestClient, path: str, script: str
) -> None:
    """The admin view renders every element the page script looks up by id."""
    import re

    response = page.get(path)
    assert response.status_code == 200
    html = response.text
    source = (STATIC_DIR / "js" / script).read_text()
    ids = set(re.findall(r'getElementById\("([a-z0-9-]+)"\)', source))
    assert ids, "expected getElementById lookups"
    missing = sorted(i for i in ids if f'id="{i}"' not in html)
    assert missing == []
    assert 'href="/monitoring"' in html and 'href="/alerts"' in html  # nav links


@pytest.mark.parametrize(("path", "script"), [("/logging", "logging.js"), ("/audit", "audit.js")])
def test_phase13_pages_have_every_element_their_script_uses(
    page: TestClient, path: str, script: str
) -> None:
    import re

    response = page.get(path)
    assert response.status_code == 200
    html = response.text
    source = (STATIC_DIR / "js" / script).read_text()
    ids = set(re.findall(r'(?:getElementById|field)\("([a-z0-9-]+)"\)', source))
    ids |= {f"export-{fmt}" for fmt in ("csv", "jsonl")} if script == "audit.js" else set()
    assert ids
    missing = sorted(i for i in ids if f'id="{i}"' not in html)
    assert missing == []
    assert 'href="/logging"' in html


def test_logging_page_needs_system_read(app: object, admin_user: object) -> None:
    from fastapi import FastAPI

    from app.services.users_service import create_user
    from tests.conftest import ADMIN_PASSWORD, login

    assert isinstance(app, FastAPI)
    with app.state.db.session_factory() as session:
        create_user(
            session,
            username="vera",
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=["viewer"],
            groups=[],
            settings=app.state.settings,
            actor=admin_user,  # type: ignore[arg-type]
        )
    with TestClient(app) as viewer:
        login(viewer, "vera", ADMIN_PASSWORD)
        assert 'href="/logging"' not in viewer.get("/").text
        assert viewer.get("/logging").status_code in (401, 403)


def test_viewer_dashboard_has_no_alert_card(app: object, admin_user: object) -> None:
    """Without system:read the dashboard neither shows nor polls alerts."""
    from fastapi import FastAPI

    from app.services.users_service import create_user
    from tests.conftest import ADMIN_PASSWORD, login

    assert isinstance(app, FastAPI)
    with app.state.db.session_factory() as session:
        create_user(
            session,
            username="vera",
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=["viewer"],
            groups=[],
            settings=app.state.settings,
            actor=admin_user,  # type: ignore[arg-type]
        )
    with TestClient(app) as viewer:
        login(viewer, "vera", ADMIN_PASSWORD)
        html = viewer.get("/").text
    assert 'id="alerts-list"' not in html
    assert 'href="/monitoring"' not in html


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
        "/static/js/api_tokens.js",
        "/static/js/api_tokens_admin.js",
        "/static/js/files.js",
        "/static/js/samba.js",
        "/static/js/settings.js",
        "/static/js/uploads.js",
        "/static/js/public_upload.js",
        "/static/js/remote_sync.js",
        "/static/js/gallery.js",
        "/static/js/gallery/actions.js",
        "/static/js/gallery/admin.js",
        "/static/js/gallery/browser.js",
        "/static/js/gallery/dialogs.js",
        "/static/js/gallery/menu.js",
        "/static/js/gallery/state.js",
        "/static/js/gallery/tree.js",
        "/static/js/gallery/uploads.js",
        "/static/js/gallery/util.js",
        "/static/js/gallery/viewer.js",
        "/static/js/monitoring.js",
        "/static/js/alerts.js",
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
