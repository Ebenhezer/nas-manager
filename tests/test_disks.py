"""Tests for disk discovery, storage and RAID management (Phase 5)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.migrations import upgrade_to_head
from app.main import create_app
from app.models import User
from app.services import disks as disks_service
from app.services.helper_client import HelperUnavailableError

from .conftest import ADMIN_PASSWORD, login

LSBLK_JSON = json.dumps(
    {
        "blockdevices": [
            {
                "name": "sda",
                "path": "/dev/sda",
                "type": "disk",
                "size": 1000204886016,
                "rota": 1,
                "tran": "sata",
                "model": "Samsung SSD 870",
                "serial": "S3Z1NX0M000001",
                "wwn": "0x5002538d",
                "mountpoint": None,
                "fstype": None,
                "label": None,
                "partlabel": None,
                "uuid": None,
                "children": [
                    {
                        "name": "sda1",
                        "path": "/dev/sda1",
                        "type": "part",
                        "size": 500107862016,
                        "rota": 1,
                        "tran": None,
                        "model": None,
                        "serial": None,
                        "wwn": None,
                        "mountpoint": "/mnt/media",
                        "fstype": "ext4",
                        "label": None,
                        "partlabel": "primary",
                        "uuid": "aaaa-bbbb",
                        "children": [],
                    }
                ],
            },
            {"name": "loop0", "path": "/dev/loop0", "type": "loop", "size": 100000},
        ]
    }
)

DISK_DETAIL_JSON = json.dumps(
    {
        "blockdevices": [
            {
                "name": "sda1",
                "path": "/dev/sda1",
                "type": "part",
                "size": 500107862016,
                "rota": 1,
                "tran": None,
                "model": None,
                "serial": None,
                "wwn": None,
                "mountpoint": "/mnt/media",
                "fstype": "ext4",
                "label": None,
                "partlabel": "primary",
                "uuid": "aaaa-bbbb",
                "children": [],
            }
        ]
    }
)

FINDMNT_JSON = json.dumps(
    {
        "filesystems": [
            {"target": "/", "source": "/dev/sda2", "fstype": "ext4", "options": "rw,relatime"},
            {
                "target": "/mnt/media",
                "source": "/dev/sda1",
                "fstype": "ext4",
                "options": "rw,noatime",
                "used": 1000,
                "avail": 999000,
                "size": 1000000,
                "children": [],
            },
        ]
    }
)

MDSTAT_ACTIVE = """Personalities : [raid1] [raid6] [raid5] [raid4]
md0 : active raid1 sdb1[1] sdc1[0]
      41934848 blocks super 1.2 [2/2] [UU]

unused devices: <none>
"""

MDSTAT_DEGRADED = """Personalities : [raid1] [raid6] [raid5] [raid4]
md1 : active raid5 sdd1[0] sde1[1] sdf1[3](S)
      8388608 blocks super 1.2 [2/3] [U_U]
      [>....................]  resync =  12.5% (1048576/8388608)

md2 : inactive sdh1[0]

unused devices: <none>
"""

SMART_JSON = json.dumps(
    {
        "smartctl": {"version": [7, 3]},
        "device": {"name": "/dev/sda", "type": "sat"},
        "model_name": "WDC WD40EFRX",
        "serial_number": "WD-WCC4E6",
        "firmware_version": "82.00A82",
        "smart_status": {"passed": True},
        "temperature": {"current": 41},
        "user_capacity": {"bytes": 4000787030016},
        "ata_smart_attributes": {
            "table": [
                {
                    "id": 5,
                    "name": "Reallocated_Sector_Ct",
                    "value": 100,
                    "worst": 100,
                    "thresh": 36,
                    "raw": {"value": 0, "string": "0"},
                }
            ]
        },
        "messages": [{"string": "SMART overall-health self-assessment test result: PASSED"}],
    }
)
# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_parse_lsblk_returns_only_physical_disks() -> None:
    disks = disks_service.parse_lsblk(LSBLK_JSON)
    assert [disk.name for disk in disks] == ["sda"]
    disk = disks[0]
    assert disk.path == "/dev/sda"
    assert disk.size_bytes == 1000204886016
    assert disk.rotational is True
    assert disk.transport == "sata"
    assert disk.model == "Samsung SSD 870"
    assert disk.partitions[0].name == "sda1"
    assert disk.partitions[0].mountpoints == ["/mnt/media"]
    assert disk.mountpoints == ["/mnt/media"]


def test_parse_lsblk_detail_uses_root_node() -> None:
    detail = disks_service.parse_lsblk_detail(DISK_DETAIL_JSON)
    assert detail.name == "sda1"
    assert detail.mountpoints == ["/mnt/media"]


def test_parse_findmnt_returns_volumes() -> None:
    volumes = disks_service.parse_findmnt(FINDMNT_JSON)
    by_target = {volume.target: volume for volume in volumes}
    assert set(by_target) == {"/", "/mnt/media"}
    assert by_target["/mnt/media"].fs_type == "ext4"
    assert by_target["/mnt/media"].used_bytes == 1000
    assert by_target["/mnt/media"].avail_bytes == 999000


def test_parse_mdstat_active_array() -> None:
    arrays = disks_service.parse_mdstat(MDSTAT_ACTIVE)
    assert len(arrays) == 1
    array = arrays[0]
    assert array.name == "md0"
    assert array.level == "raid1"
    assert array.state == "active"
    assert array.size_bytes == 41934848 * 1024
    assert array.active_disks == 2
    assert array.total_disks == 2
    assert array.degraded is False
    assert {m.device for m in array.members} == {"sdb1", "sdc1"}
    assert all(member.state == "active sync" for member in array.members)


def test_parse_mdstat_degraded_array_with_spare() -> None:
    arrays = disks_service.parse_mdstat(MDSTAT_DEGRADED)
    md1 = next(a for a in arrays if a.name == "md1")
    assert md1.degraded is True
    assert md1.active_disks == 2
    assert md1.total_disks == 3
    assert md1.sync_action == "resync"
    by_device = {m.device: m.state for m in md1.members}
    assert by_device["sdf1"] == "spare"
    assert by_device["sde1"] == "failed"
    md2 = next(a for a in arrays if a.name == "md2")
    assert md2.level is None
    assert md2.state == "inactive"


def test_parse_smartctl_health_and_attributes() -> None:
    smart = disks_service.parse_smartctl(SMART_JSON, device="/dev/sda", returncode=0)
    assert smart.available is True
    assert smart.health == "passed"
    assert smart.model == "WDC WD40EFRX"
    assert smart.temperature == 41
    assert smart.capacity_bytes == 4000787030016
    assert smart.attributes[0].name == "Reallocated_Sector_Ct"
    assert smart.messages


def test_parse_smartctl_tolerates_missing_smart() -> None:
    smart = disks_service.parse_smartctl("", device="/dev/sda", returncode=2)
    assert smart.available is False
    assert "exited with status 2" in (smart.error or "")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def helper_settings(tmp_path: Path, settings: Settings) -> Settings:
    overridden = settings.model_copy(
        update={
            "helper_enabled": True,
            "helper_socket": tmp_path / "hk" / "helper.sock",
            "samba_config_path": tmp_path / "smb" / "smb.conf",
            "samba_backup_dir": tmp_path / "smb" / "backups",
        }
    )
    overridden.ensure_directories()
    upgrade_to_head(overridden)
    return overridden


@pytest.fixture
def disks_app(helper_settings: Settings) -> FastAPI:
    return create_app(helper_settings)


@pytest.fixture
def disks_admin(disks_app: FastAPI) -> User:
    from app.services.bootstrap_service import bootstrap_admin

    with disks_app.state.db.session_factory() as session:
        return bootstrap_admin(
            session,
            username="admin",
            password=ADMIN_PASSWORD,
            settings=disks_app.state.settings,
        )


@pytest.fixture
def disks_client(disks_app: FastAPI, disks_admin: User) -> Iterator[TestClient]:
    with TestClient(disks_app) as client:
        login(client, "admin", ADMIN_PASSWORD)
        yield client


@pytest.fixture
def csrf_header(disks_client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": disks_client.cookies["nas_csrf"]}


@pytest.fixture
def fake_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Substitute the helper client with canned discovery/action responses."""

    def call_helper(settings: Settings, op: str, params: dict[str, object]) -> dict[str, object]:
        if op == "ping":
            return {"pong": True}
        if op == "disk.list":
            return {"stdout": LSBLK_JSON}
        if op == "disk.detail":
            return {"stdout": DISK_DETAIL_JSON}
        if op == "disk.smart":
            return {"stdout": SMART_JSON, "returncode": 0}
        if op == "volume.list":
            return {"stdout": FINDMNT_JSON}
        if op == "raid.list":
            return {"mdstat": MDSTAT_ACTIVE, "detail_scan": ""}
        if op == "mount":
            return {
                "device": f"/dev/{params['name']}",
                "mountpoint": str(params["mountpoint"]),
                "fstype": str(params["fstype"]),
                "options": str(params["options"]),
            }
        if op == "unmount":
            return {"device": f"/dev/{params['name']}", "mountpoint": str(params["mountpoint"])}
        if op == "raid.scrub":
            return {"name": str(params["name"]), "sync_action": "check", "previous_action": "idle"}
        raise AssertionError(f"fake_helper got unexpected op {op!r}")

    monkeypatch.setattr(disks_service, "call_helper", call_helper)
    monkeypatch.setattr(disks_service, "helper_ping", lambda settings: {"pong": True})


def _mount_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "mountpoint": "/mnt/data",
        "fstype": "ext4",
        "options": "defaults",
        "confirm": False,
        "intent": "",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# API: permissions and discovery
# --------------------------------------------------------------------------


def test_disks_api_anonymous_gets_401(disks_app: FastAPI) -> None:
    with TestClient(disks_app) as client:
        assert client.get("/api/v1/disks").status_code == 401


def test_disks_api_viewer_is_denied(disks_app: FastAPI, disks_admin: User) -> None:
    from app.services.users_service import create_user

    with disks_app.state.db.session_factory() as session:
        create_user(
            session,
            username="peon",
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=["viewer"],
            groups=[],
            settings=disks_app.state.settings,
            actor=disks_admin,
        )
    with TestClient(disks_app) as client:
        response = client.post(
            "/api/v1/auth/login", json={"username": "peon", "password": ADMIN_PASSWORD}
        )
        assert response.status_code == 200
        assert client.get("/api/v1/disks").status_code == 403
        assert client.get("/disks", follow_redirects=False).status_code == 401


def test_operator_can_read_but_not_write(
    disks_app: FastAPI, disks_admin: User, fake_helper: None
) -> None:
    from app.services.users_service import create_user

    with disks_app.state.db.session_factory() as session:
        create_user(
            session,
            username="op1",
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=["operator"],
            groups=[],
            settings=disks_app.state.settings,
            actor=disks_admin,
        )
    with TestClient(disks_app) as client:
        response = client.post(
            "/api/v1/auth/login", json={"username": "op1", "password": ADMIN_PASSWORD}
        )
        assert response.status_code == 200
        headers = {"X-CSRF-Token": client.cookies["nas_csrf"]}
        assert client.get("/api/v1/disks").status_code == 200
        assert client.get("/api/v1/storage/raid").status_code == 200
        assert (
            client.post(
                "/api/v1/disks/sda1/mount", json=_mount_payload(), headers=headers
            ).status_code
            == 403
        )


def test_list_disks_and_volumes_and_raid(
    disks_client: TestClient, csrf_header: dict[str, str], fake_helper: None
) -> None:
    disks = disks_client.get("/api/v1/disks")
    assert disks.status_code == 200
    assert [disk["name"] for disk in disks.json()] == ["sda"]

    page = disks_client.get("/disks")
    assert page.status_code == 200

    volumes = disks_client.get("/api/v1/storage/volumes")
    assert volumes.status_code == 200
    assert {v["target"] for v in volumes.json()} == {"/", "/mnt/media"}

    raid = disks_client.get("/api/v1/storage/raid")
    assert raid.status_code == 200
    arrays = raid.json()["arrays"]
    assert arrays[0]["name"] == "md0"
    assert arrays[0]["level"] == "raid1"

    smart = disks_client.get("/api/v1/disks/sda/smart")
    assert smart.status_code == 200
    assert smart.json()["health"] == "passed"


def test_disks_status_reports_helper_unavailable(
    disks_client: TestClient, fake_helper: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_settings: Settings) -> dict[str, object]:
        raise HelperUnavailableError("socket gone")

    monkeypatch.setattr(disks_service, "helper_ping", boom)
    status = disks_client.get("/api/v1/disks/status")
    assert status.status_code == 200
    body = status.json()
    assert body["available"] is False
    assert "socket" in body["error"]


# --------------------------------------------------------------------------
# API: mount / unmount / scrub confirm-then-execute flows
# --------------------------------------------------------------------------


def test_mount_requires_explicit_confirmation(
    disks_client: TestClient, csrf_header: dict[str, str], fake_helper: None
) -> None:
    response = disks_client.post(
        "/api/v1/disks/sda1/mount",
        json=_mount_payload(confirm=False),
        headers=csrf_header,
    )
    assert response.status_code == 400
    assert "confirmation" in response.json()["error"]["message"]


def test_mount_requires_matching_intent(
    disks_client: TestClient, csrf_header: dict[str, str], fake_helper: None
) -> None:
    response = disks_client.post(
        "/api/v1/disks/sda1/mount",
        json=_mount_payload(confirm=True, intent="not-the-preview-intent"),
        headers=csrf_header,
    )
    assert response.status_code == 400
    assert "intent" in response.json()["error"]["message"]


def test_mount_preview_then_confirm_succeeds_and_is_audited(
    disks_client: TestClient,
    csrf_header: dict[str, str],
    fake_helper: None,
) -> None:
    preview = disks_client.post(
        "/api/v1/disks/sda1/mount/preview",
        json={"mountpoint": "/mnt/data", "fstype": "ext4", "options": "defaults"},
        headers=csrf_header,
    )
    assert preview.status_code == 200
    body = preview.json()
    assert body["action"] == "mount"
    assert body["device"] == "/dev/sda1"
    assert body["intent"]

    mounted = disks_client.post(
        "/api/v1/disks/sda1/mount",
        json=_mount_payload(confirm=True, intent=body["intent"]),
        headers=csrf_header,
    )
    assert mounted.status_code == 200
    assert mounted.json()["message"].startswith("Mounted /dev/sda1")

    events = disks_client.get("/api/v1/audit/events?category=disk").json()["events"]
    assert "disk.mounted" in [e["event"] for e in events]


def test_unmount_preview_then_confirm_succeeds_and_is_audited(
    disks_client: TestClient,
    csrf_header: dict[str, str],
    fake_helper: None,
) -> None:
    preview = disks_client.post(
        "/api/v1/disks/sda1/unmount/preview",
        json={"mountpoint": None},
        headers=csrf_header,
    )
    assert preview.status_code == 200
    body = preview.json()
    assert body["mountpoint"] == "/mnt/media"  # resolved from the discovered mount point
    assert body["intent"]

    unmounted = disks_client.post(
        "/api/v1/disks/sda1/unmount",
        json={"mountpoint": "/mnt/media", "confirm": True, "intent": body["intent"]},
        headers=csrf_header,
    )
    assert unmounted.status_code == 200

    events = disks_client.get("/api/v1/audit/events?category=disk").json()["events"]
    assert "disk.unmounted" in [e["event"] for e in events]


def test_scrub_preview_requires_known_array(
    disks_client: TestClient, csrf_header: dict[str, str], fake_helper: None
) -> None:
    response = disks_client.post(
        "/api/v1/storage/raid/md9/scrub/preview", json={}, headers=csrf_header
    )
    assert response.status_code == 400


def test_scrub_confirm_succeeds_and_is_audited(
    disks_client: TestClient, csrf_header: dict[str, str], fake_helper: None
) -> None:
    preview = disks_client.post(
        "/api/v1/storage/raid/md0/scrub/preview", json={}, headers=csrf_header
    )
    assert preview.status_code == 200
    intent = preview.json()["intent"]

    scrubbed = disks_client.post(
        "/api/v1/storage/raid/md0/scrub",
        json={"confirm": True, "intent": intent},
        headers=csrf_header,
    )
    assert scrubbed.status_code == 200
    assert scrubbed.json()["details"]["sync_action"] == "check"

    events = disks_client.get("/api/v1/audit/events?category=raid").json()["events"]
    assert "raid.scrub_started" in [e["event"] for e in events]


def test_mount_with_unavailable_helper_returns_503_and_audits_failure(
    disks_client: TestClient,
    csrf_header: dict[str, str],
    fake_helper: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.helper.protocol import intent_digest

    def boom(settings: Settings, op: str, params: dict[str, object]) -> dict[str, object]:
        if op == "mount":
            raise HelperUnavailableError("helper is not reachable")
        raise AssertionError(f"unexpected op {op!r}")

    monkeypatch.setattr(disks_service, "call_helper", boom)
    params = {"name": "sda1", "mountpoint": "/mnt/data", "fstype": "ext4", "options": "defaults"}
    intent = intent_digest("mount", params)
    response = disks_client.post(
        "/api/v1/disks/sda1/mount",
        json=_mount_payload(confirm=True, intent=intent),
        headers=csrf_header,
    )
    assert response.status_code == 503
    events = disks_client.get("/api/v1/audit/events?category=disk").json()["events"]
    failures = [e for e in events if e["event"] == "disk.action_failed"]
    assert len(failures) == 1
    assert failures[0]["outcome"] == "failure"


# --------------------------------------------------------------------------
# Samba apply delegates to the helper when enabled
# --------------------------------------------------------------------------


def test_samba_apply_via_helper_writes_and_reloads(
    disks_client: TestClient,
    csrf_header: dict[str, str],
    disks_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    from app.services import samba as samba_service

    share_dir = tmp_path / "shares" / "media"
    share_dir.mkdir(parents=True)

    creation = disks_client.post(
        "/api/v1/samba/shares",
        json={
            "name": "media",
            "path": str(share_dir),
            "comment": "",
            "read_only": False,
            "browseable": True,
            "guest_ok": False,
            "valid_users": "",
            "write_list": "",
            "enabled": True,
        },
        headers=csrf_header,
    )
    assert creation.status_code == 201

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    helper_calls: list[tuple[str, dict[str, object]]] = []

    def fake_apply(settings: Settings, op: str, params: dict[str, object]) -> dict[str, object]:
        assert op == "samba.apply"
        helper_calls.append((op, params))
        # Mimic the real helper: validate, back up, atomically write, reload.
        config_path = Path(str(params["config_path"]))
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(str(params["content"]), encoding="utf-8")
        return {
            "config_path": str(params["config_path"]),
            "backup_path": None,
            "validated": True,
            "reload_performed": True,
            "reload_message": None,
        }

    monkeypatch.setattr(samba_service, "call_helper", fake_apply)
    monkeypatch.setattr(samba_service, "helper_ping", lambda settings: {"pong": True})
    result = disks_client.post("/api/v1/samba/apply", headers=csrf_header)
    assert result.status_code == 200
    assert result.json()["reload_required"] is False
    assert result.json()["helper_available"] is True
    assert helper_calls
    assert "MANAGED BEGIN" in str(helper_calls[0][1]["content"])

    status = disks_client.get("/api/v1/samba/status").json()
    assert status["reload_required"] is False
