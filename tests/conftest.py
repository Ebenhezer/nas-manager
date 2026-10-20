from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.migrations import upgrade_to_head
from app.db.session import Database
from app.main import create_app

ADMIN_PASSWORD = "correct horse battery staple"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Isolated settings: temp data dir, no .env file, no console/file logging,
    and deliberately cheap Argon2id costs so hashing stays fast in tests."""
    return Settings(
        _env_file=None,
        environment="test",
        data_dir=tmp_path / "data",
        log_to_console=False,
        log_to_file=False,
        argon2_time_cost=1,
        argon2_memory_cost=64,
        argon2_parallelism=1,
    )


@pytest.fixture
def migrated_settings(settings: Settings) -> Settings:
    settings.ensure_directories()
    upgrade_to_head(settings)
    return settings


@pytest.fixture
def db(migrated_settings: Settings) -> Iterator[Database]:
    database = Database(migrated_settings.resolved_database_url)
    yield database
    database.dispose()


@pytest.fixture
def app(migrated_settings: Settings) -> FastAPI:
    return create_app(migrated_settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def unmigrated_client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def admin_user(app: FastAPI) -> object:
    """Bootstrapped administrator (the CLI's default username)."""
    from app.services.bootstrap_service import bootstrap_admin

    with app.state.db.session_factory() as session:
        return bootstrap_admin(
            session,
            username="admin",
            password=ADMIN_PASSWORD,
            settings=app.state.settings,
        )


def login(client: TestClient, username: str, password: str) -> None:
    """Log in through the JSON API and assert success (cookies are stored)."""
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text


@pytest.fixture
def authed_client(app: FastAPI, admin_user: object) -> Iterator[TestClient]:
    """A TestClient whose cookie jar already holds an admin session."""
    with TestClient(app) as test_client:
        login(test_client, "admin", ADMIN_PASSWORD)
        yield test_client


@pytest.fixture
def csrf_header(authed_client: TestClient) -> dict[str, str]:
    """Header carrying the session-bound CSRF token for state-changing calls."""
    token: str = authed_client.cookies.get("nas_csrf", "") or ""
    return {"X-CSRF-Token": token}
