from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.migrations import upgrade_to_head
from app.main import create_app
from app.models import User

from .conftest import ADMIN_PASSWORD, login


@pytest.fixture
def samba_settings(tmp_path: Path, settings: Settings) -> Settings:
    overridden = settings.model_copy(
        update={
            "samba_config_path": tmp_path / "smb" / "smb.conf",
            "samba_backup_dir": tmp_path / "smb" / "backups",
        }
    )
    overridden.ensure_directories()
    upgrade_to_head(overridden)
    return overridden


@pytest.fixture
def samba_app(samba_settings: Settings) -> FastAPI:
    return create_app(samba_settings)


@pytest.fixture
def samba_admin(samba_app: FastAPI) -> User:
    from app.services.bootstrap_service import bootstrap_admin

    with samba_app.state.db.session_factory() as session:
        return bootstrap_admin(
            session,
            username="admin",
            password=ADMIN_PASSWORD,
            settings=samba_app.state.settings,
        )


@pytest.fixture
def samba_client(samba_app: FastAPI, samba_admin: User) -> Iterator[TestClient]:
    with TestClient(samba_app) as client:
        login(client, "admin", ADMIN_PASSWORD)
        yield client


@pytest.fixture
def csrf_header(samba_client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": samba_client.cookies["nas_csrf"]}


@pytest.fixture
def ok_testparm(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _payload(name: str, path: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": name,
        "path": path,
        "comment": "",
        "read_only": False,
        "browseable": True,
        "guest_ok": False,
        "valid_users": "",
        "write_list": "",
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def _share_dir(tmp_path: Path, name: str = "media") -> str:
    target = tmp_path / "shares" / name
    target.mkdir(parents=True)
    return str(target)


def test_share_crud(samba_client: TestClient, csrf_header: dict[str, str], tmp_path: Path) -> None:
    path = _share_dir(tmp_path)
    created = samba_client.post(
        "/api/v1/samba/shares", json=_payload("media", path), headers=csrf_header
    )
    assert created.status_code == 201
    share_id = created.json()["id"]

    got = samba_client.get(f"/api/v1/samba/shares/{share_id}").json()
    assert got["name"] == "media"
    assert got["path"] == path
    assert got["browseable"] is True

    listed = samba_client.get("/api/v1/samba/shares").json()
    assert [share["name"] for share in listed] == ["media"]

    updated = samba_client.patch(
        f"/api/v1/samba/shares/{share_id}",
        json={"read_only": True, "comment": "Movies"},
        headers=csrf_header,
    )
    assert updated.status_code == 200
    assert updated.json()["read_only"] is True
    assert updated.json()["comment"] == "Movies"

    deleted = samba_client.delete(f"/api/v1/samba/shares/{share_id}", headers=csrf_header)
    assert deleted.status_code == 204
    assert samba_client.get("/api/v1/samba/shares").json() == []


def test_duplicate_share_name_conflicts(
    samba_client: TestClient, csrf_header: dict[str, str], tmp_path: Path
) -> None:
    first = _share_dir(tmp_path, "a")
    second = _share_dir(tmp_path, "b")
    assert (
        samba_client.post(
            "/api/v1/samba/shares", json=_payload("dup", first), headers=csrf_header
        ).status_code
        == 201
    )
    response = samba_client.post(
        "/api/v1/samba/shares", json=_payload("dup", second), headers=csrf_header
    )
    assert response.status_code == 409


def test_nonexistent_share_path_rejected(
    samba_client: TestClient, csrf_header: dict[str, str], tmp_path: Path
) -> None:
    missing = tmp_path / "does" / "not" / "exist"
    response = samba_client.post(
        "/api/v1/samba/shares", json=_payload("bad", str(missing)), headers=csrf_header
    )
    assert response.status_code == 400


def test_invalid_share_name_rejected(
    samba_client: TestClient, csrf_header: dict[str, str], tmp_path: Path
) -> None:
    path = _share_dir(tmp_path)
    for name in ["Bad Name", "a/b"]:
        response = samba_client.post(
            "/api/v1/samba/shares", json=_payload(name, path), headers=csrf_header
        )
        assert response.status_code == 400, name
    # An empty name fails Pydantic schema validation instead (422).
    empty = samba_client.post("/api/v1/samba/shares", json=_payload("", path), headers=csrf_header)
    assert empty.status_code == 422


def test_share_name_is_normalised_to_lowercase(
    samba_client: TestClient, csrf_header: dict[str, str], tmp_path: Path
) -> None:
    path = _share_dir(tmp_path)
    response = samba_client.post(
        "/api/v1/samba/shares", json=_payload("UPPER", path), headers=csrf_header
    )
    assert response.status_code == 201
    assert response.json()["name"] == "upper"


def test_config_injection_rejected(
    samba_client: TestClient, csrf_header: dict[str, str], tmp_path: Path
) -> None:
    path = _share_dir(tmp_path)
    response = samba_client.post(
        "/api/v1/samba/shares",
        json=_payload("safe", path, valid_users="alice\n    path = /etc"),
        headers=csrf_header,
    )
    assert response.status_code == 400


def test_apply_writes_validated_config_and_backs_up(
    samba_client: TestClient,
    csrf_header: dict[str, str],
    ok_testparm: list[list[str]],
    tmp_path: Path,
) -> None:
    path = _share_dir(tmp_path)
    samba_client.post("/api/v1/samba/shares", json=_payload("media", path), headers=csrf_header)

    config_path = tmp_path / "smb" / "smb.conf"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("[global]\n    workgroup = WORKGROUP\n")

    result = samba_client.post("/api/v1/samba/apply", headers=csrf_header)
    assert result.status_code == 200
    body = result.json()
    assert body["reload_required"] is True
    assert body["backup_path"] is not None

    text = config_path.read_text()
    assert "[global]\n    workgroup = WORKGROUP" in text  # unmanaged content preserved
    assert "# ==== NAS-MANAGER MANAGED BEGIN ====" in text
    assert "[media]" in text
    assert f"    path = {path}" in text

    backups = list((tmp_path / "smb" / "backups").glob("smb.conf.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text() == "[global]\n    workgroup = WORKGROUP\n"


def test_apply_appends_managed_section_when_absent(
    samba_client: TestClient,
    csrf_header: dict[str, str],
    ok_testparm: list[list[str]],
    tmp_path: Path,
) -> None:
    path = _share_dir(tmp_path)
    samba_client.post("/api/v1/samba/shares", json=_payload("media", path), headers=csrf_header)
    config_path = tmp_path / "smb" / "smb.conf"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("    unmanaged = yes\n")

    result = samba_client.post("/api/v1/samba/apply", headers=csrf_header)
    assert result.status_code == 200
    text = config_path.read_text()
    assert "    unmanaged = yes" in text
    assert text.count("# ==== NAS-MANAGER MANAGED BEGIN ====") == 1
    assert text.count("# ==== NAS-MANAGER MANAGED END ====") == 1


def test_apply_replaces_previous_managed_section(
    samba_client: TestClient,
    csrf_header: dict[str, str],
    ok_testparm: list[list[str]],
    tmp_path: Path,
) -> None:
    path = _share_dir(tmp_path)
    samba_client.post("/api/v1/samba/shares", json=_payload("media", path), headers=csrf_header)
    config_path = tmp_path / "smb" / "smb.conf"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("[global]\n    workgroup = WORKGROUP\n")

    assert samba_client.post("/api/v1/samba/apply", headers=csrf_header).status_code == 200
    # Stale managed content (simulating an older run) must be replaced, not duplicated.
    config_path.write_text(config_path.read_text().replace("[media]", "[stale]"))
    assert samba_client.post("/api/v1/samba/apply", headers=csrf_header).status_code == 200
    text = config_path.read_text()
    assert text.count("[global]") == 1
    assert "[media]" in text
    assert "[stale]" not in text
    assert text.count("# ==== NAS-MANAGER MANAGED BEGIN ====") == 1
    backups = list((tmp_path / "smb" / "backups").glob("smb.conf.*.bak"))
    assert len(backups) == 2  # one backup per apply once the config exists


def test_apply_refuses_invalid_config(
    samba_client: TestClient,
    csrf_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def bad_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="Error loading services.")

    monkeypatch.setattr(subprocess, "run", bad_run)
    path = _share_dir(tmp_path)
    samba_client.post("/api/v1/samba/shares", json=_payload("media", path), headers=csrf_header)
    config_path = tmp_path / "smb" / "smb.conf"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("[global]\n    workgroup = WORKGROUP\n")

    response = samba_client.post("/api/v1/samba/apply", headers=csrf_header)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    # Nothing was written or backed up because validation failed first.
    assert config_path.read_text() == "[global]\n    workgroup = WORKGROUP\n"
    assert not (tmp_path / "smb" / "backups").exists()


def test_apply_500_when_testparm_missing(
    samba_client: TestClient,
    csrf_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def missing(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("testparm")

    monkeypatch.setattr(subprocess, "run", missing)
    path = _share_dir(tmp_path)
    samba_client.post("/api/v1/samba/shares", json=_payload("media", path), headers=csrf_header)

    response = samba_client.post("/api/v1/samba/apply", headers=csrf_header)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "configuration_error"


def test_apply_never_invokes_privileged_reload(
    samba_client: TestClient,
    csrf_header: dict[str, str],
    ok_testparm: list[list[str]],
    tmp_path: Path,
) -> None:
    path = _share_dir(tmp_path)
    samba_client.post("/api/v1/samba/shares", json=_payload("media", path), headers=csrf_header)
    result = samba_client.post("/api/v1/samba/apply", headers=csrf_header)
    assert result.status_code == 200
    assert result.json()["reload_required"] is True
    assert ok_testparm  # testparm was invoked
    assert all(call and call[0] == "testparm" for call in ok_testparm)
    assert not any(call[0] in {"systemctl", "smbcontrol", "service"} for call in ok_testparm)


def test_status_reports_pending_then_applied(
    samba_client: TestClient,
    csrf_header: dict[str, str],
    ok_testparm: list[list[str]],
    tmp_path: Path,
) -> None:
    path = _share_dir(tmp_path)
    samba_client.post("/api/v1/samba/shares", json=_payload("media", path), headers=csrf_header)

    status = samba_client.get("/api/v1/samba/status").json()
    assert status["managed_share_count"] == 1
    assert status["pending_changes"] is True
    assert status["reload_required"] is True
    assert status["config_exists"] is False

    assert samba_client.post("/api/v1/samba/apply", headers=csrf_header).status_code == 200
    status = samba_client.get("/api/v1/samba/status").json()
    assert status["pending_changes"] is False
    assert status["config_exists"] is True
    assert status["managed_markers_present"] is True


def test_operator_locked_out_by_default(samba_app: FastAPI, samba_admin: User) -> None:
    from app.services.users_service import create_user

    with samba_app.state.db.session_factory() as session:
        create_user(
            session,
            username="op1",
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=["operator"],
            groups=[],
            settings=samba_app.state.settings,
            actor=samba_admin,
        )
    with TestClient(samba_app) as client:
        response = client.post(
            "/api/v1/auth/login", json={"username": "op1", "password": ADMIN_PASSWORD}
        )
        assert response.status_code == 200
        headers = {"X-CSRF-Token": client.cookies["nas_csrf"]}
        assert client.get("/api/v1/samba/shares").status_code == 403
        assert client.get("/samba", follow_redirects=False).status_code == 401
        assert (
            client.post(
                "/api/v1/samba/shares",
                json={"name": "x", "path": "/tmp"},  # noqa: S108 - rejected before any filesystem access
                headers=headers,
            ).status_code
            == 403
        )


def test_anonymous_gets_401(samba_app: FastAPI) -> None:
    with TestClient(samba_app) as client:
        assert client.get("/api/v1/samba/shares").status_code == 401


def test_audit_events_recorded(
    samba_client: TestClient,
    csrf_header: dict[str, str],
    ok_testparm: list[list[str]],
    tmp_path: Path,
) -> None:
    path = _share_dir(tmp_path)
    samba_client.post("/api/v1/samba/shares", json=_payload("media", path), headers=csrf_header)
    samba_client.post("/api/v1/samba/apply", headers=csrf_header)

    page = samba_client.get("/api/v1/audit/events?category=samba")
    assert page.status_code == 200
    events = [event["event"] for event in page.json()["events"]]
    assert "samba.share.created" in events
    assert "samba.config.applied" in events
