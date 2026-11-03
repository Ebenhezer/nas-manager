"""Backup repositories, jobs, runs, snapshots and restores over the API (Phase 11)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.secrets import CredentialCipher
from app.models import AuditEvent, BackupRepository, User
from app.services.backup import jobs
from tests.conftest import ADMIN_PASSWORD, login

PASSPHRASE = "a long backup passphrase"


def _user_client(app: FastAPI, admin_user: User, username: str, role: str) -> TestClient:
    from app.services.users_service import create_user

    with app.state.db.session_factory() as session:
        create_user(
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
    client = TestClient(app)
    login(client, username, ADMIN_PASSWORD)
    return client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("nas_csrf", "") or ""}


def _work(app: FastAPI) -> None:
    """Run every queued backup run, as the worker would."""
    settings = app.state.settings
    cipher = CredentialCipher.from_settings(settings)
    with app.state.db.session_factory() as session:
        while (run := jobs.claim_next(session, owner="test", lease_seconds=60)) is not None:
            jobs.process_run(session, settings, run, cipher=cipher)


def _root(app: FastAPI) -> Path:
    root: Path = app.state.settings.resolved_file_roots[0]
    return root


def _repository(client: TestClient, headers: dict[str, str], **values: Any) -> dict[str, Any]:
    body = {
        "name": "usb",
        "target_kind": "local",
        "local_root": str(client.app.state.settings.resolved_backup_local_roots[0]),  # type: ignore[attr-defined]
        "repo_path": "nas",
        "passphrase": PASSPHRASE,
    } | values
    response = client.post("/api/v1/backup-repositories", json=body, headers=headers)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    init = client.post(f"/api/v1/backup-repositories/{created['id']}/init", headers=headers)
    assert init.status_code == 200, init.text
    return created


def _job(client: TestClient, headers: dict[str, str], repository_id: int, **values: Any) -> int:
    body = {
        "name": "docs",
        "repository_id": repository_id,
        "source_kind": "files",
        "source_root": str(_root(client.app)),  # type: ignore[arg-type]
        "source_path": "docs",
        "keep_last": 0,
        "keep_daily": 0,
        "keep_weekly": 0,
        "keep_monthly": 0,
    } | values
    response = client.post("/api/v1/backup-jobs", json=body, headers=headers)
    assert response.status_code == 201, response.text
    job_id: int = response.json()["id"]
    return job_id


@pytest.fixture
def docs(app: FastAPI) -> Path:
    folder = _root(app) / "docs"
    folder.mkdir(parents=True)
    (folder / "report.txt").write_text("quarterly numbers")
    (folder / "sub").mkdir()
    (folder / "sub" / "notes.md").write_text("notes")
    return folder


def test_permissions(app: FastAPI, admin_user: User, client: TestClient) -> None:
    assert client.get("/api/v1/backup-repositories").status_code == 401
    viewer = _user_client(app, admin_user, "vic", "viewer")
    assert viewer.get("/api/v1/backup-repositories").status_code == 403
    assert viewer.get("/backups").status_code in (401, 403)

    operator = _user_client(app, admin_user, "olga", "operator")
    for path in ("/api/v1/backup-repositories", "/api/v1/backup-jobs", "/api/v1/backup-runs"):
        assert operator.get(path).status_code == 200, path
    assert operator.get("/api/v1/backup/status").status_code == 200
    body = {"name": "x", "target_kind": "local", "repo_path": "x", "passphrase": PASSPHRASE}
    assert (
        operator.post("/api/v1/backup-repositories", json=body, headers=_csrf(operator)).status_code
        == 403
    )
    page = operator.get("/backups")
    assert page.status_code == 200
    assert 'data-can-write="false"' in page.text and 'data-can-restore="false"' in page.text
    assert 'id="restore-modal"' not in page.text


def test_repository_lifecycle_never_exposes_the_passphrase(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    repo = _repository(authed_client, csrf_header)
    assert "passphrase" not in repo
    assert repo["initialized_at"] is None
    listed = authed_client.get("/api/v1/backup-repositories").json()
    assert listed[0]["initialized_at"] is not None
    assert PASSPHRASE not in authed_client.get("/api/v1/backup-repositories").text

    # Initializing again adopts the existing repository.
    again = authed_client.post(
        f"/api/v1/backup-repositories/{repo['id']}/init", headers=csrf_header
    )
    assert again.json()["created"] is False

    # A different passphrase does not unlock it; the repository is then
    # uninitialized until the right one is set.
    changed = authed_client.patch(
        f"/api/v1/backup-repositories/{repo['id']}",
        json={"passphrase": "another long passphrase"},
        headers=csrf_header,
    )
    assert changed.status_code == 200 and changed.json()["initialized_at"] is None
    wrong = authed_client.post(
        f"/api/v1/backup-repositories/{repo['id']}/init", headers=csrf_header
    )
    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "wrong_passphrase"

    with app.state.db.session_factory() as session:
        row = session.get(BackupRepository, repo["id"])
        assert row is not None and PASSPHRASE not in row.passphrase_encrypted
        details = " ".join(str(e.details) for e in session.execute(select(AuditEvent)).scalars())
        assert PASSPHRASE not in details and "another long passphrase" not in details

    assert (
        authed_client.delete(
            f"/api/v1/backup-repositories/{repo['id']}", headers=csrf_header
        ).status_code
        == 204
    )
    backup_root = app.state.settings.resolved_backup_local_roots[0]
    assert (backup_root / "nas" / "repo.json").is_file()  # the target is left alone


def test_repository_validation(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str], tmp_path: Path
) -> None:
    def post(**values: Any) -> Any:
        body = {
            "name": "r",
            "target_kind": "local",
            "local_root": str(app.state.settings.resolved_backup_local_roots[0]),
            "repo_path": "r",
            "passphrase": PASSPHRASE,
        } | values
        return authed_client.post("/api/v1/backup-repositories", json=body, headers=csrf_header)

    assert post(local_root=str(tmp_path)).status_code == 400  # not an allowed root
    assert post(passphrase="short").status_code == 422
    assert post(target_kind="tape").status_code == 400
    assert post(target_kind="remote").status_code == 400  # no connection
    assert post(target_kind="remote", connection_id=999).status_code == 404
    assert post(repo_path="").status_code == 400
    assert post(repo_path="../escape").status_code == 400
    assert post().status_code == 201
    assert post(repo_path="other").status_code == 409  # duplicate name

    # A non-empty folder that is not a repository is refused.
    foreign = app.state.settings.resolved_backup_local_roots[0] / "photos"
    foreign.mkdir()
    (foreign / "x.jpg").write_text("x")
    repo = post(name="foreign", repo_path="photos").json()
    init = authed_client.post(f"/api/v1/backup-repositories/{repo['id']}/init", headers=csrf_header)
    assert init.status_code == 409


def test_backup_browse_restore_flow(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str], docs: Path
) -> None:
    repo = _repository(authed_client, csrf_header)
    job_id = _job(authed_client, csrf_header, repo["id"])

    run = authed_client.post(f"/api/v1/backup-jobs/{job_id}/run", headers=csrf_header)
    assert run.status_code == 202 and run.json()["status"] == "queued"
    duplicate = authed_client.post(f"/api/v1/backup-jobs/{job_id}/run", headers=csrf_header)
    assert duplicate.status_code == 409
    _work(app)
    detail = authed_client.get(f"/api/v1/backup-runs/{run.json()['id']}").json()
    assert detail["status"] == "succeeded" and detail["files"] == 2

    snapshots = authed_client.get(f"/api/v1/backup-repositories/{repo['id']}/snapshots").json()
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    entries = authed_client.get(f"/api/v1/backup-snapshots/{snapshot['id']}/entries").json()
    assert {e["path"] for e in entries["entries"]} == {"report.txt", "sub/notes.md"}
    sub = authed_client.get(
        f"/api/v1/backup-snapshots/{snapshot['id']}/entries", params={"prefix": "sub"}
    ).json()
    assert [e["path"] for e in sub["entries"]] == ["sub/notes.md"]

    target = {"kind": "files", "root": str(_root(app)), "path": "docs"}
    restore_url = f"/api/v1/backup-snapshots/{snapshot['id']}/restore"
    refused = authed_client.post(
        restore_url, json={"target": target, "overwrite": True}, headers=csrf_header
    )
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "confirmation_required"
    assert refused.json()["error"]["details"]["warnings"]

    fresh = {"kind": "files", "root": str(_root(app)), "path": "restored"}
    queued = authed_client.post(restore_url, json={"target": fresh}, headers=csrf_header)
    assert queued.status_code == 202, queued.text
    _work(app)
    assert (_root(app) / "restored" / "report.txt").read_text() == "quarterly numbers"
    assert (_root(app) / "restored" / "sub" / "notes.md").read_text() == "notes"

    (docs / "report.txt").write_text("overwritten by mistake")
    confirmed = authed_client.post(
        restore_url,
        json={"target": target, "overwrite": True, "paths": ["report.txt"], "confirm": True},
        headers=csrf_header,
    )
    assert confirmed.status_code == 202
    _work(app)
    assert (docs / "report.txt").read_text() == "quarterly numbers"
    finished = authed_client.get(f"/api/v1/backup-runs/{confirmed.json()['id']}").json()
    assert finished["summary"]["replaced"] == ["report.txt"]

    bad = authed_client.post(
        restore_url,
        json={"target": {"kind": "files", "root": str(_root(app)), "path": "../../etc"}},
        headers=csrf_header,
    )
    assert bad.status_code == 400
    assert (
        authed_client.post(
            restore_url, json={"target": {"kind": "nowhere"}}, headers=csrf_header
        ).status_code
        == 400
    )


def test_verify_and_prune_endpoints(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str], docs: Path
) -> None:
    repo = _repository(authed_client, csrf_header)
    job_id = _job(authed_client, csrf_header, repo["id"], keep_last=1)
    authed_client.post(f"/api/v1/backup-jobs/{job_id}/run", headers=csrf_header)
    _work(app)

    verify = authed_client.post(
        f"/api/v1/backup-repositories/{repo['id']}/verify", json={}, headers=csrf_header
    )
    assert verify.status_code == 202
    _work(app)
    assert authed_client.get(f"/api/v1/backup-runs/{verify.json()['id']}").json()["status"] == (
        "succeeded"
    )
    snapshots = authed_client.get(f"/api/v1/backup-repositories/{repo['id']}/snapshots").json()
    assert snapshots[0]["verify_status"] == "ok"
    assert (
        authed_client.post(
            f"/api/v1/backup-repositories/{repo['id']}/verify",
            json={"snapshot_id": 9999},
            headers=csrf_header,
        ).status_code
        == 404
    )

    prune_url = f"/api/v1/backup-repositories/{repo['id']}/prune"
    preview = authed_client.post(prune_url, json={}, headers=csrf_header)
    assert preview.status_code == 202 and preview.json()["dry_run"] is True
    _work(app)
    real = authed_client.post(prune_url, json={"dry_run": False}, headers=csrf_header)
    assert real.status_code == 400 and real.json()["error"]["code"] == "confirmation_required"
    real = authed_client.post(
        prune_url, json={"dry_run": False, "confirm": True}, headers=csrf_header
    )
    assert real.status_code == 202

    runs = authed_client.get("/api/v1/backup-runs", params={"repository_id": repo["id"]}).json()
    assert {r["kind"] for r in runs} >= {"backup", "verify", "prune"}


def test_job_validation_and_confirmation(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str], docs: Path
) -> None:
    repo = _repository(authed_client, csrf_header)

    def post(**values: Any) -> Any:
        body = {
            "name": "j",
            "repository_id": repo["id"],
            "source_kind": "files",
            "source_root": str(_root(app)),
            "source_path": "docs",
        } | values
        return authed_client.post("/api/v1/backup-jobs", json=body, headers=csrf_header)

    assert post(source_path="missing").status_code == 400
    assert post(source_path="../..").status_code == 400
    assert post(source_kind="tape").status_code == 400
    assert post(source_root="/etc").status_code == 404
    assert post(source_kind="remote").status_code == 400  # no connection
    assert post(keep_last=-1).status_code == 422
    assert post(repository_id=999).status_code == 404

    risky = post(enabled=True, schedule_minutes=60)
    assert risky.status_code == 400
    warnings = risky.json()["error"]["details"]["warnings"]
    assert any("schedule" in w for w in warnings) and any("retention" in w for w in warnings)
    created = post(enabled=True, schedule_minutes=60, confirm=True)
    assert created.status_code == 201
    job = created.json()
    assert job["direction"] == "push" and job["next_run_at"] is not None

    shrink = authed_client.patch(
        f"/api/v1/backup-jobs/{job['id']}", json={"keep_last": 1}, headers=csrf_header
    )
    assert shrink.status_code == 400
    assert (
        authed_client.patch(
            f"/api/v1/backup-jobs/{job['id']}",
            json={"keep_last": 1, "confirm": True},
            headers=csrf_header,
        ).status_code
        == 200
    )
    # A repository used by a job cannot be forgotten.
    assert (
        authed_client.delete(
            f"/api/v1/backup-repositories/{repo['id']}", headers=csrf_header
        ).status_code
        == 409
    )
    assert (
        authed_client.delete(f"/api/v1/backup-jobs/{job['id']}", headers=csrf_header).status_code
        == 204
    )


def test_system_job_and_staged_restore(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    repo = _repository(authed_client, csrf_header)
    job_id = _job(authed_client, csrf_header, repo["id"], source_kind="system", source_path="")
    authed_client.post(f"/api/v1/backup-jobs/{job_id}/run", headers=csrf_header)
    _work(app)
    [snapshot] = authed_client.get(f"/api/v1/backup-repositories/{repo['id']}/snapshots").json()
    assert snapshot["source_kind"] == "system"
    url = f"/api/v1/backup-snapshots/{snapshot['id']}/restore"
    assert (
        authed_client.post(
            url, json={"target": {"kind": "staging"}, "overwrite": True}, headers=csrf_header
        ).status_code
        == 400
    )
    staged = authed_client.post(url, json={"target": {"kind": "staging"}}, headers=csrf_header)
    assert staged.status_code == 202
    _work(app)
    run = authed_client.get(f"/api/v1/backup-runs/{staged.json()['id']}").json()
    assert run["status"] == "succeeded", run
    staging = app.state.settings.resolved_backup_staging_dir / "restore" / str(run["id"])
    assert (staging / "nas.db").is_file() and (staging / "restore.json").is_file()


def test_operator_cannot_restore_or_cancel_restores(
    app: FastAPI,
    admin_user: User,
    authed_client: TestClient,
    csrf_header: dict[str, str],
    docs: Path,
) -> None:
    repo = _repository(authed_client, csrf_header)
    job_id = _job(authed_client, csrf_header, repo["id"])
    authed_client.post(f"/api/v1/backup-jobs/{job_id}/run", headers=csrf_header)
    _work(app)
    [snapshot] = authed_client.get(f"/api/v1/backup-repositories/{repo['id']}/snapshots").json()
    restore = authed_client.post(
        f"/api/v1/backup-snapshots/{snapshot['id']}/restore",
        json={"target": {"kind": "files", "root": str(_root(app)), "path": "out"}},
        headers=csrf_header,
    ).json()
    operator = _user_client(app, admin_user, "olga", "operator")
    assert operator.get(f"/api/v1/backup-snapshots/{snapshot['id']}").status_code == 200
    assert (
        operator.post(
            f"/api/v1/backup-snapshots/{snapshot['id']}/restore",
            json={"target": {"kind": "staging"}},
            headers=_csrf(operator),
        ).status_code
        == 403
    )
    assert (
        operator.post(
            f"/api/v1/backup-runs/{restore['id']}/cancel", headers=_csrf(operator)
        ).status_code
        == 403
    )
    cancelled = authed_client.post(
        f"/api/v1/backup-runs/{restore['id']}/cancel", headers=csrf_header
    )
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"


def test_passphrase_endpoints_refuse_api_tokens(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    repo = _repository(authed_client, csrf_header)
    token = authed_client.post(
        "/api/v1/tokens",
        json={"name": "script", "scopes": ["backup:read", "backup:write"]},
        headers=csrf_header,
    ).json()["token"]
    with TestClient(app) as bearer:
        bearer.headers["Authorization"] = f"Bearer {token}"
        assert bearer.get("/api/v1/backup-repositories").status_code == 200
        body = {
            "name": "x",
            "target_kind": "local",
            "local_root": str(app.state.settings.resolved_backup_local_roots[0]),
            "repo_path": "x",
            "passphrase": PASSPHRASE,
        }
        assert bearer.post("/api/v1/backup-repositories", json=body).status_code == 403
        assert (
            bearer.patch(
                f"/api/v1/backup-repositories/{repo['id']}", json={"passphrase": PASSPHRASE}
            ).status_code
            == 403
        )
        # Scoped tokens may still trigger verification.
        assert (
            bearer.post(f"/api/v1/backup-repositories/{repo['id']}/verify", json={}).status_code
            == 202
        )


def test_info_and_page(authed_client: TestClient) -> None:
    assert "backup" in authed_client.get("/api/v1/info").json()["features"]
    page = authed_client.get("/backups")
    assert page.status_code == 200
    assert 'id="restore-modal"' in page.text and "js/backups.js" in page.text
    assert "<script>" not in page.text  # CSP: no inline scripts
    assert 'href="/backups"' in authed_client.get("/").text
