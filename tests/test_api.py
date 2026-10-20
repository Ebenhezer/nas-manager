from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app import __version__
from app.core.config import Settings
from app.main import create_app
from app.services import health as health_service
from app.services.settings_service import INSTANCE_ID_KEY, get_setting


def test_health_ok_when_migrated(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["environment"] == "test"
    assert body["database"]["reachable"] is True
    assert body["database"]["schema_current"] is True
    assert body["database"]["revision"] == body["database"]["head_revision"]


def test_health_degraded_without_migrations(unmigrated_client: TestClient) -> None:
    response = unmigrated_client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"]["schema_current"] is False
    assert body["database"]["revision"] is None


def test_health_unhealthy_returns_503_when_database_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_engine: Any) -> str | None:
        raise OperationalError("SELECT 1", {}, Exception("disk I/O error"))

    monkeypatch.setattr(health_service, "get_current_revision", boom)
    response = client.get("/api/v1/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unhealthy"
    assert body["database"]["reachable"] is False


def test_startup_creates_instance_id_when_schema_current(app: FastAPI) -> None:
    with TestClient(app):
        with app.state.db.session_factory() as session:
            stored = get_setting(session, INSTANCE_ID_KEY)
        assert stored is not None
        assert app.state.instance_id == stored


def test_startup_skips_instance_id_when_schema_outdated(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(settings)
    with caplog.at_level(logging.WARNING), TestClient(app):
        assert app.state.instance_id is None
    assert "alembic upgrade head" in caplog.text


def test_auto_migrate_brings_schema_to_head(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"auto_migrate": True}))
    with TestClient(app) as client:
        assert client.get("/api/v1/health").json()["status"] == "ok"
        assert app.state.instance_id is not None


def test_request_id_generated_and_returned(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    request_id = response.headers["X-Request-ID"]
    assert len(request_id) == 32


def test_valid_incoming_request_id_is_preserved(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-ID": "trace-42"})
    assert response.headers["X-Request-ID"] == "trace-42"


def test_malicious_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-ID": 'x"; <script>'})
    assert response.headers["X-Request-ID"] != 'x"; <script>'
    assert len(response.headers["X-Request-ID"]) == 32


def test_security_headers_present(client: TestClient) -> None:
    headers = client.get("/login").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_unknown_route_uses_error_envelope(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "http_error"
    assert error["request_id"] == response.headers["X-Request-ID"]


def test_method_not_allowed_uses_error_envelope(client: TestClient) -> None:
    response = client.post("/api/v1/health")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "http_error"


def test_validation_error_envelope(app: FastAPI) -> None:
    @app.get("/api/v1/_echo/{count}")
    def echo(count: int) -> dict[str, int]:
        return {"count": count}

    with TestClient(app) as client:
        response = client.get("/api/v1/_echo/notanumber")
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"][0]["loc"][-1] == "count"


def test_app_error_envelope(app: FastAPI) -> None:
    from app.core.errors import ConflictError

    @app.get("/api/v1/_conflict")
    def conflict() -> None:
        raise ConflictError("already exists", details={"name": "x"})

    with TestClient(app) as client:
        response = client.get("/api/v1/_conflict")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "conflict"
    assert error["details"] == {"name": "x"}


def test_unhandled_exception_returns_generic_500_without_leaking(app: FastAPI) -> None:
    @app.get("/api/v1/_crash")
    def crash() -> None:
        raise RuntimeError("secret internal detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/_crash")
    assert response.status_code == 500
    assert "secret internal detail" not in response.text
    assert response.json()["error"]["code"] == "internal_error"
    assert response.headers["X-Request-ID"]


def test_api_docs_disabled_outside_debug(client: TestClient) -> None:
    assert client.get("/api/docs").status_code == 404
    assert client.get("/api/openapi.json").status_code == 404


def test_api_docs_enabled_in_debug(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"debug": True}))
    with TestClient(app) as client:
        assert client.get("/api/openapi.json").status_code == 200


def test_system_summary_shape(authed_client: TestClient) -> None:
    response = authed_client.get("/api/v1/system/summary")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"host", "cpu", "memory", "disks", "network"}
    assert body["memory"]["total"] > 0
    assert any(disk["path"] == "/" for disk in body["disks"])
    assert "nas_session" in authed_client.cookies


def test_access_log_omits_query_string(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        client.get("/api/v1/health?token=abc123")
    # Scope to the application's own access log: the test HTTP client logs the URL itself.
    access = [r for r in caplog.records if r.name == "app.access"]
    assert access
    assert all("abc123" not in r.getMessage() for r in access)
    assert all("abc123" not in str(r.__dict__) for r in access)
    assert access[-1].__dict__["path"] == "/api/v1/health"


def test_access_log_carries_the_response_request_id(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        response = client.get("/api/v1/health")
    access = [r for r in caplog.records if r.name == "app.access"]
    assert access[-1].__dict__["request_id"] == response.headers["X-Request-ID"]
