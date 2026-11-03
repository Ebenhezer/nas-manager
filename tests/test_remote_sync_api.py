"""Remote connections and sync jobs over the API (Phase 7)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.secrets import CredentialCipher
from app.db.base import utcnow_naive
from app.models import AuditEvent, RemoteConnection, SyncRun, User
from app.services import remote_sync, remotes
from app.services.remote import gdrive
from tests.conftest import ADMIN_PASSWORD, login

DAV = {
    "name": "Nextcloud",
    "provider": "webdav",
    "config": {"url": "https://cloud.example/dav/", "username": "me"},
    "secrets": {"password": "app-password-123"},
}


def _make_user(app: FastAPI, admin_user: User, username: str, role: str) -> User:
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


def _user_client(app: FastAPI, admin_user: User, username: str, role: str) -> TestClient:
    _make_user(app, admin_user, username, role)
    client = TestClient(app)
    login(client, username, ADMIN_PASSWORD)
    return client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("nas_csrf", "") or ""}


def _create_connection(
    client: TestClient, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    response = client.post("/api/v1/remotes", json={**DAV, **overrides}, headers=headers)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.fixture
def job_dir(app: FastAPI) -> Path:
    root: Path = app.state.settings.resolved_file_roots[0]
    (root / "photos").mkdir()
    return root


def _create_job(
    client: TestClient, headers: dict[str, str], connection_id: int, root: Path, **extra: Any
) -> Any:
    return client.post(
        "/api/v1/sync-jobs",
        json={
            "name": extra.pop("name", "photos"),
            "connection_id": connection_id,
            "local_root": str(root),
            "local_path": "photos",
            "remote_path": "/backup/photos/",
            "direction": "push",
            **extra,
        },
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


def test_connection_crud_never_returns_secrets(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    created = _create_connection(authed_client, csrf_header)
    assert created["secrets_set"] == ["password"]
    assert created["credentials_readable"] is True
    assert "app-password-123" not in authed_client.get("/api/v1/remotes").text

    with app.state.db.session_factory() as session:
        row = session.execute(select(RemoteConnection)).scalar_one()
        assert "app-password-123" not in (row.secrets_encrypted or "")
        assert "app-password-123" not in row.config_json
        events = list(
            session.execute(
                select(AuditEvent).where(AuditEvent.category == "remote_sync")
            ).scalars()
        )
        assert events and all("app-password-123" not in str(e.details) for e in events)

    # PATCH without secrets keeps them; renaming works.
    patched = authed_client.patch(
        f"/api/v1/remotes/{created['id']}",
        json={"name": "Cloud", "config": {"username": "other"}},
        headers=csrf_header,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["config"]["username"] == "other"
    assert patched.json()["secrets_set"] == ["password"]
    cleared = authed_client.patch(
        f"/api/v1/remotes/{created['id']}",
        json={"secrets": {"password": None}},
        headers=csrf_header,
    )
    assert cleared.json()["secrets_set"] == []

    assert (
        authed_client.delete(f"/api/v1/remotes/{created['id']}", headers=csrf_header).status_code
        == 204
    )
    assert authed_client.get(f"/api/v1/remotes/{created['id']}").status_code == 404


def test_connection_validation(authed_client: TestClient, csrf_header: dict[str, str]) -> None:
    def post(**overrides: Any) -> Any:
        return authed_client.post("/api/v1/remotes", json={**DAV, **overrides}, headers=csrf_header)

    assert post(provider="nfs").status_code == 400
    assert post(config={"url": "http://plain/"}).status_code == 400
    assert post(config={"url": "https://x/", "password": "inline"}).status_code == 400
    assert post(name=" ").status_code in (400, 422)
    assert post().status_code == 201
    assert post().status_code == 409  # duplicate name


def test_providers_catalogue(authed_client: TestClient) -> None:
    names = {p["name"] for p in authed_client.get("/api/v1/remotes/providers").json()}
    assert names == {"sftp", "ftp", "ftps", "webdav", "s3", "gdrive"}


def test_connection_test_records_outcome(
    app: FastAPI,
    authed_client: TestClient,
    csrf_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.remote_fakes import LocalDirProvider

    monkeypatch.setattr(
        remotes,
        "build_provider",
        lambda row, cipher, settings: LocalDirProvider(Path("/nonexistent")),
    )
    created = _create_connection(authed_client, csrf_header)
    response = authed_client.post(f"/api/v1/remotes/{created['id']}/test", headers=csrf_header)
    # Unreachable remote: a clean failure, recorded and audited, never a 500.
    assert response.status_code == 200
    assert response.json()["ok"] is False
    listed = authed_client.get(f"/api/v1/remotes/{created['id']}").json()
    assert listed["last_test_ok"] is False
    with app.state.db.session_factory() as session:
        event = session.execute(
            select(AuditEvent).where(AuditEvent.event == "remote.tested")
        ).scalar_one()
        assert event.outcome == "failure"


def test_browse_uses_provider(
    authed_client: TestClient,
    csrf_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from tests.remote_fakes import LocalDirProvider

    (tmp_path / "remote" / "folder").mkdir(parents=True)
    monkeypatch.setattr(
        remotes,
        "build_provider",
        lambda row, cipher, settings: LocalDirProvider(tmp_path / "remote"),
    )
    created = _create_connection(authed_client, csrf_header)
    body = authed_client.get(f"/api/v1/remotes/{created['id']}/browse").json()
    assert [(e["name"], e["is_dir"]) for e in body["entries"]] == [("folder", True)]
    assert (
        authed_client.get(
            f"/api/v1/remotes/{created['id']}/browse", params={"path": "../x"}
        ).status_code
        == 400
    )


def test_credential_endpoints_refuse_api_tokens(
    authed_client: TestClient, csrf_header: dict[str, str], app: FastAPI
) -> None:
    token = authed_client.post(
        "/api/v1/tokens",
        json={"name": "script", "scopes": ["remotes:read", "remotes:write"]},
        headers=csrf_header,
    ).json()["token"]
    with TestClient(app) as bearer:
        bearer.headers["Authorization"] = f"Bearer {token}"
        assert bearer.get("/api/v1/remotes").status_code == 200
        assert bearer.post("/api/v1/remotes", json=DAV).status_code == 403
        assert bearer.post("/api/v1/remotes/sftp/host-key", json={"host": "h"}).status_code == 403


def test_permissions(app: FastAPI, admin_user: User, client: TestClient) -> None:
    assert client.get("/api/v1/remotes").status_code == 401
    viewer = _user_client(app, admin_user, "vic", "viewer")
    assert viewer.get("/api/v1/remotes").status_code == 403
    assert viewer.get("/api/v1/sync-jobs").status_code == 403
    operator = _user_client(app, admin_user, "olga", "operator")
    assert operator.get("/api/v1/remotes").status_code == 200
    assert operator.get("/api/v1/sync-jobs").status_code == 200
    assert operator.get("/api/v1/remote-sync/status").status_code == 200
    assert operator.post("/api/v1/remotes", json=DAV, headers=_csrf(operator)).status_code == 403
    assert operator.get("/remote-sync").status_code == 200
    assert viewer.get("/remote-sync").status_code in (401, 403)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def test_job_lifecycle(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str], job_dir: Path
) -> None:
    conn = _create_connection(authed_client, csrf_header)
    created = _create_job(authed_client, csrf_header, conn["id"], job_dir)
    assert created.status_code == 201, created.text
    job = created.json()
    assert (job["remote_path"], job["enabled"], job["local_path"]) == (
        "backup/photos",
        False,
        "photos",
    )

    # Connection is in use.
    assert (
        authed_client.delete(f"/api/v1/remotes/{conn['id']}", headers=csrf_header).status_code
        == 409
    )

    queued = authed_client.post(
        f"/api/v1/sync-jobs/{job['id']}/run", json={"dry_run": True}, headers=csrf_header
    )
    assert queued.status_code == 202
    run = queued.json()
    assert (run["status"], run["dry_run"]) == ("queued", True)
    assert (
        authed_client.post(
            f"/api/v1/sync-jobs/{job['id']}/run", json={}, headers=csrf_header
        ).status_code
        == 409
    )
    assert (
        authed_client.delete(f"/api/v1/sync-jobs/{job['id']}", headers=csrf_header).status_code
        == 409
    )

    cancelled = authed_client.post(f"/api/v1/sync-runs/{run['id']}/cancel", headers=csrf_header)
    assert cancelled.json()["status"] == "cancelled"
    runs = authed_client.get(f"/api/v1/sync-jobs/{job['id']}/runs").json()
    assert [r["status"] for r in runs] == ["cancelled"]
    assert authed_client.get(f"/api/v1/sync-runs/{run['id']}").json()["summary"] is not None

    assert (
        authed_client.delete(f"/api/v1/sync-jobs/{job['id']}", headers=csrf_header).status_code
        == 204
    )
    assert (job_dir / "photos").is_dir()  # deleting a job never touches files


def test_job_validation(
    authed_client: TestClient, csrf_header: dict[str, str], job_dir: Path
) -> None:
    conn = _create_connection(authed_client, csrf_header)
    cid = conn["id"]
    assert (
        _create_job(authed_client, csrf_header, cid, job_dir, local_path="../..").status_code == 400
    )
    assert (
        _create_job(authed_client, csrf_header, cid, job_dir, local_path="missing").status_code
        == 400
    )
    assert _create_job(authed_client, csrf_header, cid, Path("/etc")).status_code == 404
    assert (
        _create_job(authed_client, csrf_header, cid, job_dir, remote_path="a/../../b").status_code
        == 400
    )
    assert (
        _create_job(authed_client, csrf_header, cid, job_dir, direction="sideways").status_code
        == 400
    )
    assert _create_job(authed_client, csrf_header, 999, job_dir).status_code == 404
    assert (
        _create_job(authed_client, csrf_header, cid, job_dir, exclude_patterns=["a\nb"]).status_code
        == 400
    )


def test_risky_settings_need_confirmation(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str], job_dir: Path
) -> None:
    conn = _create_connection(authed_client, csrf_header)
    refused = _create_job(
        authed_client,
        csrf_header,
        conn["id"],
        job_dir,
        direction="bidirectional",
        delete_policy="archive",
    )
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "confirmation_required"
    assert len(refused.json()["error"]["details"]["warnings"]) == 2
    accepted = _create_job(
        authed_client,
        csrf_header,
        conn["id"],
        job_dir,
        direction="bidirectional",
        delete_policy="archive",
        confirm=True,
    )
    assert accepted.status_code == 201
    job_id = accepted.json()["id"]

    # Enabling a schedule needs confirmation too; then the job is due now.
    patch = {"schedule_minutes": 60, "enabled": True}
    assert (
        authed_client.patch(
            f"/api/v1/sync-jobs/{job_id}", json=patch, headers=csrf_header
        ).status_code
        == 400
    )
    enabled = authed_client.patch(
        f"/api/v1/sync-jobs/{job_id}", json={**patch, "confirm": True}, headers=csrf_header
    )
    assert enabled.status_code == 200
    assert enabled.json()["next_run_at"] is not None
    # Already-accepted risks are not asked again.
    renamed = authed_client.patch(
        f"/api/v1/sync-jobs/{job_id}", json={"name": "renamed"}, headers=csrf_header
    )
    assert renamed.status_code == 200

    # Overriding the deletion guard needs confirmation as well.
    guard = authed_client.post(
        f"/api/v1/sync-jobs/{job_id}/run",
        json={"override_deletion_guard": True},
        headers=csrf_header,
    )
    assert guard.json()["error"]["code"] == "confirmation_required"
    with app.state.db.session_factory() as session:
        details = [
            e.details
            for e in session.execute(
                select(AuditEvent).where(AuditEvent.event == "sync_job.updated")
            ).scalars()
        ]
    assert any(d and d.get("confirmed") for d in details)


def test_changing_paths_resets_the_baseline(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str], job_dir: Path
) -> None:
    from app.models import SyncJobEntry

    conn = _create_connection(authed_client, csrf_header)
    job = _create_job(authed_client, csrf_header, conn["id"], job_dir).json()
    with app.state.db.session_factory() as session:
        session.add(
            SyncJobEntry(job_id=job["id"], path="a", local_size=1, local_mtime_ns=1, remote_size=1)
        )
        session.commit()
    response = authed_client.patch(
        f"/api/v1/sync-jobs/{job['id']}", json={"remote_path": "elsewhere"}, headers=csrf_header
    )
    assert response.status_code == 200
    with app.state.db.session_factory() as session:
        assert session.execute(select(SyncJobEntry)).first() is None


def test_status_reports_worker_heartbeat(app: FastAPI, authed_client: TestClient) -> None:
    status = authed_client.get("/api/v1/remote-sync/status").json()
    assert status["worker_online"] is False
    with app.state.db.session_factory() as session:
        remote_sync.write_heartbeat(session)
    status = authed_client.get("/api/v1/remote-sync/status").json()
    assert status["worker_online"] is True
    assert status["file_roots"] == [str(app.state.settings.resolved_file_roots[0])]


def test_conflict_acknowledge(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str], job_dir: Path
) -> None:
    from app.models import SyncConflict

    conn = _create_connection(authed_client, csrf_header)
    job = _create_job(authed_client, csrf_header, conn["id"], job_dir).json()
    with app.state.db.session_factory() as session:
        row = SyncConflict(
            job_id=job["id"], path="a.txt", kind="both_modified", resolution="kept_both"
        )
        session.add(row)
        session.commit()
        conflict_id = row.id
    assert len(authed_client.get(f"/api/v1/sync-jobs/{job['id']}/conflicts").json()) == 1
    ack = authed_client.post(
        f"/api/v1/sync-conflicts/{conflict_id}/acknowledge", headers=csrf_header
    )
    assert ack.json()["acknowledged"] is True
    assert authed_client.get(f"/api/v1/sync-jobs/{job['id']}/conflicts").json() == []
    assert (
        len(
            authed_client.get(
                f"/api/v1/sync-jobs/{job['id']}/conflicts", params={"include_acknowledged": True}
            ).json()
        )
        == 1
    )


# ---------------------------------------------------------------------------
# Google Drive OAuth
# ---------------------------------------------------------------------------


@pytest.fixture
def drive_client(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str]
) -> Iterator[tuple[TestClient, int]]:
    app.state.settings.public_base_url = "https://nas.example"
    created = _create_connection(
        authed_client,
        csrf_header,
        name="Drive",
        provider="gdrive",
        config={"client_id": "cid.apps.googleusercontent.com"},
        secrets={"client_secret": "csecret"},
    )
    assert created["oauth_connected"] is False
    yield authed_client, created["id"]


def test_oauth_flow_stores_refresh_token(
    app: FastAPI,
    drive_client: tuple[TestClient, int],
    csrf_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, conn_id = drive_client
    exchanged: dict[str, Any] = {}

    def fake_exchange(**kwargs: Any) -> str:
        exchanged.update(kwargs)
        return "refresh-xyz"

    monkeypatch.setattr(gdrive, "exchange_code", fake_exchange)
    start = client.post(f"/api/v1/remotes/{conn_id}/oauth/start", headers=csrf_header)
    assert start.status_code == 200, start.text
    assert start.json()["redirect_uri"] == "https://nas.example/api/v1/remotes/oauth/callback"
    from urllib.parse import parse_qs, urlsplit

    state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
    callback = client.get(
        "/api/v1/remotes/oauth/callback",
        params={"state": state, "code": "abc"},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert "oauth=connected" in callback.headers["location"]
    assert exchanged["code"] == "abc" and exchanged["client_secret"] == "csecret"
    body = client.get(f"/api/v1/remotes/{conn_id}").json()
    assert body["oauth_connected"] is True
    with app.state.db.session_factory() as session:
        row = session.get(RemoteConnection, conn_id)
        assert row is not None and "refresh-xyz" not in (row.secrets_encrypted or "")
        cipher = CredentialCipher.from_settings(app.state.settings)
        assert remotes.stored_secrets(row, cipher)["refresh_token"] == "refresh-xyz"
    # The state is single-use.
    replay = client.get(
        "/api/v1/remotes/oauth/callback",
        params={"state": state, "code": "abc"},
        follow_redirects=False,
    )
    assert "oauth=error" in replay.headers["location"]


def test_oauth_rejects_forged_and_expired_state(
    app: FastAPI, drive_client: tuple[TestClient, int], csrf_header: dict[str, str]
) -> None:
    client, conn_id = drive_client
    forged = client.get(
        "/api/v1/remotes/oauth/callback",
        params={"state": f"{conn_id}.forged", "code": "abc"},
        follow_redirects=False,
    )
    assert "oauth=error" in forged.headers["location"]

    client.post(f"/api/v1/remotes/{conn_id}/oauth/start", headers=csrf_header)
    with app.state.db.session_factory() as session:
        row = session.get(RemoteConnection, conn_id)
        assert row is not None
        cipher = CredentialCipher.from_settings(app.state.settings)
        pending = cipher.decrypt_json(row.oauth_pending_encrypted)
        pending["expires_at"] = (utcnow_naive() - timedelta(minutes=1)).isoformat()
        pending["state_digest"] = remotes._digest(f"{conn_id}.known")
        row.oauth_pending_encrypted = cipher.encrypt_json(pending)
        session.commit()
    expired = client.get(
        "/api/v1/remotes/oauth/callback",
        params={"state": f"{conn_id}.known", "code": "abc"},
        follow_redirects=False,
    )
    assert "expired" in expired.headers["location"]


def test_oauth_needs_public_base_url(
    app: FastAPI, drive_client: tuple[TestClient, int], csrf_header: dict[str, str]
) -> None:
    client, conn_id = drive_client
    app.state.settings.public_base_url = None
    response = client.post(f"/api/v1/remotes/{conn_id}/oauth/start", headers=csrf_header)
    assert response.status_code == 500
    assert "NAS_PUBLIC_BASE_URL" in response.json()["error"]["message"]


def test_info_advertises_remote_sync(client: TestClient) -> None:
    assert "remote_sync" in client.get("/api/v1/info").json()["features"]


def test_queued_run_visible_to_worker(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str], job_dir: Path
) -> None:
    conn = _create_connection(authed_client, csrf_header)
    job = _create_job(authed_client, csrf_header, conn["id"], job_dir).json()
    authed_client.post(f"/api/v1/sync-jobs/{job['id']}/run", json={}, headers=csrf_header)
    with app.state.db.session_factory() as session:
        claimed = remote_sync.claim_next(session, owner="w1", lease_seconds=60, busy_job_ids=set())
        assert claimed is not None and claimed.status == "running"
        assert (
            remote_sync.claim_next(session, owner="w2", lease_seconds=60, busy_job_ids=set())
            is None
        )
        assert session.execute(select(SyncRun)).scalar_one().lease_owner == "w1"
