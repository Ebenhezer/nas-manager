"""Phase 14: security hardening (rate limits, proxy/Host trust, headers,
production warnings, cookie clearing, CSRF buffering, file modes)."""

from __future__ import annotations

import logging
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings
from app.db.migrations import upgrade_to_head
from app.main import create_app, production_warnings
from app.models import AuditEvent, PublicUploadQuota
from app.services import public_uploads, samba
from app.services.throttle import IpLoginThrottle, SlidingWindowThrottle

from .conftest import ADMIN_PASSWORD, login


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _app_with(settings: Settings, **overrides: Any) -> FastAPI:
    custom = settings.model_copy(update=overrides)
    custom.ensure_directories()
    upgrade_to_head(custom)
    return create_app(custom)


@pytest.fixture
def make_client(settings: Settings) -> Iterator[Any]:
    clients: list[TestClient] = []

    def factory(*, base_url: str = "http://testserver", **overrides: Any) -> TestClient:
        client = TestClient(_app_with(settings, **overrides), base_url=base_url)
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


def _upload(client: TestClient, content: bytes = b"data", **kwargs: Any) -> Any:
    return client.post(
        "/api/v1/public/uploads",
        files={"file": ("f.txt", content, "text/plain")},
        **kwargs,
    )


def _events(app: FastAPI, event: str) -> list[AuditEvent]:
    with app.state.db.session_factory() as session:
        return list(session.scalars(select(AuditEvent).where(AuditEvent.event == event)))


def _leftovers(app: FastAPI) -> list[Path]:
    root = Path(app.state.settings.resolved_public_upload_dir)
    return [p for p in root.rglob("*") if p.is_file()]


# --- Throttle ---------------------------------------------------------------


def test_throttle_window_and_alias() -> None:
    clock = FakeClock()
    throttle = SlidingWindowThrottle(max_attempts=2, window_seconds=10, clock=clock)
    assert IpLoginThrottle is SlidingWindowThrottle
    assert throttle.check("a").allowed
    assert throttle.check("a").allowed
    refused = throttle.check("a")
    assert not refused.allowed
    assert refused.retry_after_seconds == 11
    clock.now += 11
    assert throttle.check("a").allowed


def test_throttle_caps_tracked_keys() -> None:
    clock = FakeClock()
    throttle = SlidingWindowThrottle(max_attempts=5, window_seconds=10, clock=clock, max_keys=3)
    for key in ("a", "b", "c"):
        throttle.check(key)
    throttle.check("a")  # most recently seen
    throttle.check("d")  # evicts the least recently seen live key ("b")
    assert len(throttle) == 3
    assert set(throttle._attempts) == {"a", "c", "d"}
    clock.now += 60  # every window expired: all are dropped before eviction
    throttle.check("e")
    assert set(throttle._attempts) == {"e"}


# --- Public drop folder admission (before the body is read) -----------------


def test_public_upload_rate_limit_returns_429_and_audits_once(make_client: Any) -> None:
    client = make_client(public_upload_rate_max_requests=2, public_upload_rate_window_seconds=60)
    assert _upload(client).status_code == 201
    assert _upload(client).status_code == 201
    for _ in range(3):
        refused = _upload(client)
        assert refused.status_code == 429
        assert int(refused.headers["Retry-After"]) >= 1
        assert refused.json()["error"]["code"] == "too_many_requests"
        assert refused.headers["X-Request-ID"]
    events = _events(client.app, "public_upload.rate_limited")
    assert len(events) == 1
    assert events[0].outcome == "failure"
    assert (events[0].details or {})["uploader_key"].startswith("ip:")
    # The policy endpoint is not limited.
    assert client.get("/api/v1/public/uploads").status_code == 200


def test_public_upload_rate_limit_can_be_disabled(make_client: Any) -> None:
    client = make_client(public_upload_rate_max_requests=0)
    assert client.app.state.public_upload_throttle is None
    for _ in range(5):
        assert _upload(client).status_code == 201


def test_rate_limit_is_per_uploader(make_client: Any, settings: Settings) -> None:
    client = make_client(public_upload_rate_max_requests=1)
    from app.services.bootstrap_service import bootstrap_admin

    with client.app.state.db.session_factory() as session:
        bootstrap_admin(
            session, username="admin", password=ADMIN_PASSWORD, settings=client.app.state.settings
        )
    assert _upload(client).status_code == 201
    assert _upload(client).status_code == 429  # ip:<testclient>
    login(client, "admin", ADMIN_PASSWORD)
    headers = {"X-CSRF-Token": client.cookies["nas_csrf"]}
    accepted = _upload(client, headers=headers)
    assert accepted.status_code == 201  # user:admin has its own window
    assert accepted.json()["uploader_key"] == "user:admin"


def test_oversized_declared_body_is_refused_before_reading(make_client: Any) -> None:
    client = make_client(public_upload_max_file_bytes=1024)
    refused = client.post(
        "/api/v1/public/uploads",
        content=b"x" * 10,
        headers={
            "Content-Type": "multipart/form-data; boundary=x",
            "Content-Length": str(1024 + 65 * 1024),
        },
    )
    assert refused.status_code == 413
    assert refused.json()["error"]["code"] == "payload_too_large"
    assert refused.json()["error"]["details"] == {"max_file_bytes": 1024}
    assert _leftovers(client.app) == []
    # Within the allowance the route's own streaming cap still applies.
    capped = _upload(client, b"y" * 2048)
    assert capped.status_code == 400
    assert _leftovers(client.app) == []


def test_exhausted_quota_is_refused_before_reading(make_client: Any) -> None:
    client = make_client()
    key = public_uploads.uploader_key_for_ip("testclient")
    with client.app.state.db.session_factory() as session:
        session.add(PublicUploadQuota(uploader_key=key, quota_bytes=4, used_bytes=4))
        session.commit()
    refused = _upload(client, b"z")
    assert refused.status_code == 413
    assert refused.json()["error"]["code"] == "quota_exceeded"
    assert _leftovers(client.app) == []
    rejected = _events(client.app, "public_upload.rejected")
    assert rejected
    assert (rejected[-1].details or {})["stage"] == "admission"


def test_invalid_bearer_on_drop_endpoint_is_401_once(make_client: Any) -> None:
    client = make_client()
    response = _upload(client, headers={"Authorization": "Bearer nas_invalid"})
    assert response.status_code == 401
    assert len(_events(client.app, "api_token.auth_failed")) == 1


def test_disabled_folder_still_answers_403(make_client: Any) -> None:
    client = make_client(public_upload_enabled=False)
    assert _upload(client).status_code == 403


# --- Headers, HSTS and Host validation --------------------------------------


def test_phase14_security_headers(client: TestClient) -> None:
    headers = client.get("/api/v1/health").headers
    assert headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "camera=()" in headers["Permissions-Policy"]
    assert "Strict-Transport-Security" not in headers


def test_hsts_only_on_https_when_configured(make_client: Any) -> None:
    plain = make_client(hsts_max_age_seconds=31536000)
    assert "Strict-Transport-Security" not in plain.get("/api/v1/health").headers
    tls = make_client(base_url="https://testserver", hsts_max_age_seconds=31536000)
    assert tls.get("/api/v1/health").headers["Strict-Transport-Security"] == "max-age=31536000"
    off = make_client(base_url="https://testserver")
    assert "Strict-Transport-Security" not in off.get("/api/v1/health").headers


def test_trusted_hosts_reject_unknown_host(make_client: Any) -> None:
    client = make_client(trusted_hosts="testserver,*.example.org")
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/health", headers={"Host": "nas.example.org"}).status_code == 200
    refused = client.get("/api/v1/health", headers={"Host": "evil.test"})
    assert refused.status_code == 400
    assert refused.headers["X-Frame-Options"] == "DENY"
    assert refused.headers["X-Request-ID"]


def test_any_host_accepted_without_trusted_hosts(client: TestClient) -> None:
    assert client.get("/api/v1/health", headers={"Host": "whatever.test"}).status_code == 200


# --- Production warnings ----------------------------------------------------


def test_production_warnings(settings: Settings) -> None:
    risky = settings.model_copy(
        update={
            "environment": "production",
            "debug": True,
            "forwarded_allow_ips": "*",
            "public_upload_rate_max_requests": 0,
        }
    )
    messages = production_warnings(risky)
    assert any("NAS_SESSION_COOKIE_SECURE" in m for m in messages)
    assert any("NAS_DEBUG" in m for m in messages)
    assert any("NAS_TRUSTED_HOSTS" in m for m in messages)
    assert any("NAS_FORWARDED_ALLOW_IPS" in m for m in messages)
    assert any("rate limit" in m for m in messages)
    safe = risky.model_copy(
        update={
            "debug": False,
            "session_cookie_secure": True,
            "trusted_hosts": "nas.example.org",
            "forwarded_allow_ips": "127.0.0.1",
            "public_upload_rate_max_requests": 30,
        }
    )
    assert production_warnings(safe) == []
    assert production_warnings(settings) == []  # environment=test


def test_production_warnings_are_logged_at_startup(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    app = _app_with(settings, environment="production", debug=True)
    with caplog.at_level(logging.WARNING, logger="app.main"), TestClient(app):
        pass
    assert any("NAS_DEBUG is true" in r.getMessage() for r in caplog.records)


# --- Cookies and CSRF -------------------------------------------------------


def test_logout_clears_cookies_with_their_attributes(make_client: Any) -> None:
    client = make_client(session_cookie_secure=True, base_url="https://testserver")
    from app.services.bootstrap_service import bootstrap_admin

    with client.app.state.db.session_factory() as session:
        bootstrap_admin(
            session, username="admin", password=ADMIN_PASSWORD, settings=client.app.state.settings
        )
    login(client, "admin", ADMIN_PASSWORD)
    response = client.post(
        "/api/v1/auth/logout", headers={"X-CSRF-Token": client.cookies["nas_csrf"]}
    )
    assert response.status_code == 204
    cleared = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in cleared if c.startswith("nas_session="))
    csrf_cookie = next(c for c in cleared if c.startswith("nas_csrf="))
    for cookie in (session_cookie, csrf_cookie):
        assert "Max-Age=0" in cookie
        assert "Secure" in cookie
        assert "SameSite=lax" in cookie
    assert "HttpOnly" in session_cookie
    assert "HttpOnly" not in csrf_cookie


def test_multipart_without_csrf_header_is_refused_without_buffering(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.requests import Request

    async def forbidden_body(self: Request) -> bytes:
        raise AssertionError("the CSRF check must not buffer a multipart body")

    monkeypatch.setattr(Request, "body", forbidden_body)
    response = authed_client.post(
        "/api/v1/public/uploads", files={"file": ("f.txt", b"payload", "text/plain")}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failure"


def test_large_urlencoded_form_is_not_buffered(client: TestClient) -> None:
    client.get("/login")  # obtain the CSRF cookie
    response = client.post(
        "/login",
        content=b"username=a&password=b&_csrf=" + b"x" * (70 * 1024),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 403


# --- Process entry points and file modes ------------------------------------


def test_uvicorn_receives_proxy_settings(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    import uvicorn

    import app.__main__ as entry

    captured: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
    monkeypatch.setattr(entry, "create_app", lambda s: object())

    for value, proxy_headers, allow in (
        ("10.0.0.5,127.0.0.1", True, "10.0.0.5,127.0.0.1"),
        ("", False, None),
    ):
        custom = settings.model_copy(update={"forwarded_allow_ips": value})
        monkeypatch.setattr(entry, "get_settings", lambda custom=custom: custom)
        entry.main()
        assert captured["proxy_headers"] is proxy_headers
        assert captured["forwarded_allow_ips"] == allow
        assert captured["server_header"] is False


def test_samba_fallback_write_keeps_the_config_mode(tmp_path: Path) -> None:
    config = tmp_path / "smb.conf"
    config.write_text("[global]\n")
    config.chmod(0o644)
    samba._atomic_write(config, "[global]\n# new\n")
    assert config.read_text().endswith("# new\n")
    assert stat.S_IMODE(config.stat().st_mode) == 0o644
    assert [p.name for p in tmp_path.iterdir()] == ["smb.conf"]


def test_helper_socket_dir_is_created_traversable_and_never_remoded(tmp_path: Path) -> None:
    from app.helper.server import prepare_socket_dir

    created = tmp_path / "run" / "nas-manager"
    prepare_socket_dir(created)
    assert stat.S_IMODE(created.stat().st_mode) == 0o711
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o1777)
    prepare_socket_dir(shared)
    assert stat.S_IMODE(shared.stat().st_mode) == 0o1777
