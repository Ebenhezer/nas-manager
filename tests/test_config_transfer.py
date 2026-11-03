"""Configuration export, import and rollback (Phase 12).

Two installations ("source" and "target") share the same file roots and
backup root, so a document exported from one can be imported into the other.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings
from app.core.secrets import CredentialCipher
from app.db.migrations import upgrade_to_head
from app.main import create_app
from app.models import (
    AlertChannel,
    AlertRule,
    AuditEvent,
    BackupRepository,
    Group,
    SambaShare,
    User,
)
from app.services import remotes, samba
from app.services.backup import jobs as backup_jobs
from app.services.bootstrap_service import bootstrap_admin
from app.services.config_transfer import export
from app.services.groups_service import create_group, replace_members
from app.services.media import libraries
from app.services.monitoring import alerts as alert_rules
from app.services.monitoring import channels
from app.services.users_service import create_user
from tests.conftest import ADMIN_PASSWORD, login
from tests.sftp_stub_server import generate_ed25519

PASSPHRASE = "export passphrase 123"
WEBHOOK_SECRET = "hook-signing-secret-xyz"
SFTP_PASSWORD = "sftp-password-abc"
REPO_PASSPHRASE = "repository passphrase 1"


def _settings(data_dir: Path, shared: Path) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        data_dir=data_dir,
        file_roots=[shared / "files"],
        backup_local_roots=[shared / "backups"],
        log_to_console=False,
        log_to_file=False,
        argon2_time_cost=1,
        argon2_memory_cost=64,
        argon2_parallelism=1,
        backup_scrypt_n=1024,
    )


def _app(settings: Settings) -> FastAPI:
    settings.ensure_directories()
    upgrade_to_head(settings)
    app = create_app(settings)
    with app.state.db.session_factory() as session:
        bootstrap_admin(session, username="admin", password=ADMIN_PASSWORD, settings=settings)
    return app


@pytest.fixture
def shared(tmp_path: Path) -> Path:
    root = tmp_path / "shared"
    for sub in ("files/docs", "files/photos", "backups", "smb/public"):
        (root / sub).mkdir(parents=True)
    return root


@pytest.fixture
def source(tmp_path: Path, shared: Path) -> FastAPI:
    app = _app(_settings(tmp_path / "source", shared))
    _populate(app, shared)
    return app


@pytest.fixture
def target(tmp_path: Path, shared: Path) -> FastAPI:
    return _app(_settings(tmp_path / "target", shared))


def _client(app: FastAPI, username: str = "admin") -> TestClient:
    client = TestClient(app)
    login(client, username, ADMIN_PASSWORD)
    return client


@pytest.fixture
def source_client(source: FastAPI) -> Iterator[TestClient]:
    with _client(source) as client:
        yield client


@pytest.fixture
def target_client(target: FastAPI) -> Iterator[TestClient]:
    with _client(target) as client:
        yield client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("nas_csrf", "") or ""}


def _populate(app: FastAPI, shared: Path) -> None:
    settings: Settings = app.state.settings
    cipher = CredentialCipher.from_settings(settings)
    with app.state.db.session_factory() as session:
        admin = session.execute(select(User).where(User.username == "admin")).scalar_one()
        family = create_group(session, name="family", description="The family", actor=admin)
        alice = create_user(
            session,
            username="alice",
            password=ADMIN_PASSWORD,
            display_name="Alice",
            email="alice@example.org",
            role_names=["operator"],
            groups=[family.id],
            settings=settings,
            actor=admin,
        )
        replace_members(session, family, user_ids=[alice.id], actor=admin)
        samba.create_share(
            session,
            name="public",
            path=str(shared / "smb" / "public"),
            comment="Shared stuff",
            read_only=False,
            browseable=True,
            guest_ok=False,
            valid_users="@family",
            write_list="",
            enabled=True,
            actor=admin,
        )
        conn = remotes.create_connection(
            session,
            name="offsite",
            provider="sftp",
            config={
                "host": "backup.example.org",
                "port": 22,
                "username": "nas",
                "host_key": "ssh-ed25519 " + _host_key_b64(),
            },
            secrets={"password": SFTP_PASSWORD},
            cipher=cipher,
            actor=admin,
        )
        libraries.create_shared_library(
            session,
            settings,
            name="Family photos",
            root=str(shared / "files"),
            path="photos",
            include_videos=True,
            enabled=True,
            group_ids=[family.id],
            actor=admin,
        )
        alert_rules.create_rule(
            session,
            values={"name": "CPU hot", "metric": "cpu_percent", "threshold": 95.0},
            actor=admin,
        )
        builtin = session.execute(
            select(AlertRule).where(AlertRule.name == "Disk almost full")
        ).scalar_one()
        builtin.threshold = 85.0
        session.commit()
        channels.create_channel(
            session,
            values={
                "name": "ntfy",
                "kind": "webhook",
                "config": {"url": "https://ntfy.example/nas"},
                "secrets": {"signing_secret": WEBHOOK_SECRET},
            },
            cipher=cipher,
            actor=admin,
        )
        repo = backup_jobs.create_repository(
            session,
            settings,
            cipher,
            values={
                "name": "usb",
                "target_kind": "local",
                "local_root": str(shared / "backups"),
                "repo_path": "nas",
                "passphrase": REPO_PASSPHRASE,
            },
            actor=admin,
        )
        backup_jobs.create_job(
            session,
            settings,
            values={
                "name": "docs",
                "repository_id": repo.id,
                "source_kind": "files",
                "source_root": str(shared / "files"),
                "source_path": "docs",
            },
            confirm=True,
            actor=admin,
        )
        assert conn.id


def _host_key_b64() -> str:
    key, _ = generate_ed25519()
    return key.get_base64()


def _export(client: TestClient, passphrase: str | None = PASSPHRASE) -> dict[str, Any]:
    response = client.post(
        "/api/v1/config/export", json={"passphrase": passphrase}, headers=_csrf(client)
    )
    assert response.status_code == 200, response.text
    assert "attachment" in response.headers["content-disposition"]
    document: dict[str, Any] = response.json()
    return document


def _import(client: TestClient, document: dict[str, Any], **values: Any) -> Any:
    body = {"document": document, "passphrase": PASSPHRASE, "mode": "merge"} | values
    return client.post("/api/v1/config/import", json=body, headers=_csrf(client))


def _comparable(app: FastAPI) -> dict[str, Any]:
    settings = app.state.settings
    with app.state.db.session_factory() as session:
        sections, secrets = export.collect(
            session, settings, CredentialCipher.from_settings(settings)
        )
    secrets["users"] = {}  # hashes differ per install for the bootstrap admin
    return {"sections": sections, "secrets": secrets}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_without_passphrase_contains_no_secrets(source_client: TestClient) -> None:
    document = _export(source_client, passphrase=None)
    assert document["format"] == "nas-manager-config" and document["version"] == 1
    assert document["secrets"] is None
    text = json.dumps(document)
    for secret in (SFTP_PASSWORD, WEBHOOK_SECRET, REPO_PASSPHRASE, "$argon2"):
        assert secret not in text
    sections = document["sections"]
    assert set(sections) == set(export.SECTIONS)
    [conn] = sections["remote_connections"]
    assert conn["secret_fields"] == ["password"] and "password" not in conn["config"]
    assert [u["username"] for u in sections["users"]] == ["admin", "alice"]
    assert sections["users"][1]["groups"] == ["family"]
    assert sections["media_libraries"][0]["groups"] == ["family"]
    assert "instance_id" not in sections["settings"]


def test_export_with_passphrase_seals_secrets(source_client: TestClient) -> None:
    document = _export(source_client)
    text = json.dumps(document)
    for secret in (SFTP_PASSWORD, WEBHOOK_SECRET, REPO_PASSPHRASE, "$argon2"):
        assert secret not in text
    unsealed = export.unseal_secrets(document["secrets"], document["sections"], PASSPHRASE)
    assert unsealed["remote_connections"]["offsite"]["password"] == SFTP_PASSWORD
    assert unsealed["backup_repositories"]["usb"]["passphrase"] == REPO_PASSPHRASE
    assert unsealed["users"]["alice"]["password_hash"].startswith("$argon2")
    assert (
        source_client.post(
            "/api/v1/config/export", json={"passphrase": "short"}, headers=_csrf(source_client)
        ).status_code
        == 422
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def test_round_trip_into_a_fresh_installation(
    source: FastAPI, target: FastAPI, source_client: TestClient, target_client: TestClient
) -> None:
    document = _export(source_client)
    preview = target_client.post(
        "/api/v1/config/import/preview",
        json={"document": document, "passphrase": PASSPHRASE},
        headers=_csrf(target_client),
    )
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["errors"] == [] and plan["secrets_included"] is True
    assert plan["sections"]["users"]["create"] == ["alice"]
    assert plan["samba_apply_required"] is True
    # The preview changed nothing.
    with target.state.db.session_factory() as session:
        assert session.execute(select(User).where(User.username == "alice")).first() is None

    refused = _import(target_client, document)
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "confirmation_required"
    applied = _import(target_client, document, confirm=True, source_name="source.json")
    assert applied.status_code == 200, applied.text
    run = applied.json()["run"]
    assert run["status"] == "applied" and run["source_name"] == "source.json"
    assert run["database_copy"] and Path(run["database_copy"]).is_file()

    assert _comparable(target) == _comparable(source)
    with target.state.db.session_factory() as session:
        repo = session.execute(select(BackupRepository)).scalar_one()
        assert repo.initialized_at is None  # must be initialized (adopted) explicitly
        channel = session.execute(select(AlertChannel)).scalar_one()
        assert WEBHOOK_SECRET not in (channel.secrets_encrypted or "")
        event = session.execute(
            select(AuditEvent).where(AuditEvent.event == "config.imported")
        ).scalar_one()
        assert PASSPHRASE not in json.dumps(event.details)
    # alice can log in on the target with her original password.
    with TestClient(target) as alice:
        login(alice, "alice", ADMIN_PASSWORD)

    # Importing the same document again changes nothing.
    again = target_client.post(
        "/api/v1/config/import/preview",
        json={"document": document, "passphrase": PASSPHRASE},
        headers=_csrf(target_client),
    ).json()
    assert all(
        not s["create"] and not s["update"] and not s["delete"] for s in again["sections"].values()
    ), again["sections"]


def test_wrong_passphrase_and_tampering_are_refused(
    source_client: TestClient, target_client: TestClient
) -> None:
    document = _export(source_client)
    wrong = _import(target_client, document, passphrase="not the passphrase", confirm=True)
    assert wrong.status_code == 400 and "passphrase" in wrong.json()["error"]["message"]
    missing = _import(target_client, document, passphrase=None, confirm=True)
    assert missing.status_code == 400
    tampered = json.loads(json.dumps(document))
    tampered["sections"]["users"][1]["roles"] = ["admin"]
    changed = _import(target_client, tampered, confirm=True)
    assert changed.status_code == 400 and "modified" in changed.json()["error"]["message"]


def test_document_envelope_validation(target_client: TestClient) -> None:
    def post(document: Any) -> Any:
        return target_client.post(
            "/api/v1/config/import/preview",
            json={"document": document},
            headers=_csrf(target_client),
        )

    assert post({"format": "other"}).status_code == 400
    assert post({"format": "nas-manager-config", "version": 99, "sections": {}}).status_code == 400
    assert (
        post({"format": "nas-manager-config", "version": 1, "sections": {"nope": []}}).status_code
        == 400
    )
    assert (
        post(
            {"format": "nas-manager-config", "version": 1, "sections": {"groups": "x"}}
        ).status_code
        == 400
    )
    mode = target_client.post(
        "/api/v1/config/import/preview",
        json={
            "document": {"format": "nas-manager-config", "version": 1, "sections": {}},
            "mode": "x",
        },
        headers=_csrf(target_client),
    )
    assert mode.status_code == 400


def _doc(**sections: Any) -> dict[str, Any]:
    return {"format": "nas-manager-config", "version": 1, "sections": sections, "secrets": None}


def test_any_invalid_object_refuses_the_whole_import(
    target: FastAPI, target_client: TestClient
) -> None:
    document = _doc(
        groups=[{"name": "ok-group", "roles": []}, {"name": "bad", "roles": ["no-such-role"]}],
        roles=[{"name": "custom", "permissions": ["files:read", "made:up"]}],
        samba_shares=[{"name": "Bad Name!", "path": "/srv"}],
        alert_rules=[{"name": "r", "metric": "cpu_percent", "threshold": 500}],
    )
    response = _import(target_client, document, passphrase=None, confirm=True)
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "import_invalid"
    keys = {(e["section"], e["key"]) for e in body["details"]["errors"]}
    assert {
        ("groups", "bad"),
        ("roles", "custom"),
        ("samba_shares", "Bad Name!"),
        ("alert_rules", "r"),
    } <= keys
    with target.state.db.session_factory() as session:
        assert session.execute(select(Group).where(Group.name == "ok-group")).first() is None
        refused = session.execute(
            select(AuditEvent).where(AuditEvent.event == "config.import_refused")
        ).scalar_one()
        assert refused.outcome == "failure"


def test_import_cannot_remove_the_last_or_own_admin_access(target_client: TestClient) -> None:
    demote = _doc(users=[{"username": "admin", "roles": ["viewer"], "is_active": True}])
    response = _import(target_client, demote, passphrase=None, confirm=True)
    assert response.status_code == 400
    assert "administrator" in json.dumps(response.json())
    deactivate = _doc(users=[{"username": "admin", "roles": ["admin"], "is_active": False}])
    assert _import(target_client, deactivate, passphrase=None, confirm=True).status_code == 400


def test_users_without_a_hash_are_imported_inactive(
    target: FastAPI, target_client: TestClient
) -> None:
    document = _doc(users=[{"username": "bob", "roles": ["viewer"], "is_active": True}])
    preview = target_client.post(
        "/api/v1/config/import/preview",
        json={"document": document},
        headers=_csrf(target_client),
    ).json()
    assert any("bob" in w for w in preview["warnings"])
    assert _import(target_client, document, passphrase=None, confirm=True).status_code == 200
    with target.state.db.session_factory() as session:
        bob = session.execute(select(User).where(User.username == "bob")).scalar_one()
        assert bob.is_active is False


def test_missing_share_path_is_imported_disabled(
    target: FastAPI, target_client: TestClient, tmp_path: Path
) -> None:
    document = _doc(samba_shares=[{"name": "gone", "path": str(tmp_path / "nowhere")}])
    assert _import(target_client, document, passphrase=None, confirm=True).status_code == 200
    with target.state.db.session_factory() as session:
        share = session.execute(select(SambaShare)).scalar_one()
        assert share.enabled is False


def test_replace_mode_deletes_missing_objects_and_rollback_restores(
    source: FastAPI, source_client: TestClient
) -> None:
    before = _comparable(source)
    document = _export(source_client)
    secrets = export.unseal_secrets(document["secrets"], document["sections"], PASSPHRASE)
    sections = document["sections"]
    # Drop the share and the custom rule, change the channel, add a user.
    sections["samba_shares"] = []
    sections["alert_rules"] = [r for r in sections["alert_rules"] if r["name"] != "CPU hot"]
    sections["alert_channels"][0]["min_severity"] = "critical"
    sections["users"].append({"username": "carol", "roles": ["viewer"], "groups": ["family"]})
    document["secrets"] = export.seal_secrets(sections, secrets, PASSPHRASE, scrypt_n=1024)

    merge = source_client.post(
        "/api/v1/config/import/preview",
        json={"document": document, "passphrase": PASSPHRASE, "mode": "merge"},
        headers=_csrf(source_client),
    ).json()
    assert merge["sections"]["samba_shares"]["delete"] == []
    replace = _import(source_client, document, mode="replace")
    assert replace.status_code == 400
    warnings = replace.json()["error"]["details"]["warnings"]
    assert "will be deleted" in warnings[0]
    applied = _import(source_client, document, mode="replace", confirm=True)
    assert applied.status_code == 200, applied.text
    run_id = applied.json()["run"]["id"]
    with source.state.db.session_factory() as session:
        assert session.execute(select(SambaShare)).first() is None
        assert session.execute(select(AlertRule).where(AlertRule.name == "CPU hot")).first() is None
        assert session.execute(select(User).where(User.username == "carol")).scalar_one()
        # Built-in rules are never deleted.
        assert session.execute(
            select(AlertRule).where(AlertRule.name == "Disk almost full")
        ).scalar_one()

    url = f"/api/v1/config/imports/{run_id}/rollback"
    unconfirmed = source_client.post(url, json={}, headers=_csrf(source_client))
    assert unconfirmed.status_code == 400
    rolled = source_client.post(url, json={"confirm": True}, headers=_csrf(source_client))
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["run"]["status"] == "rolled_back"
    assert _comparable(source) == before
    with source.state.db.session_factory() as session:
        assert session.execute(select(User).where(User.username == "carol")).first() is None
    again = source_client.post(url, json={"confirm": True}, headers=_csrf(source_client))
    assert again.status_code == 409
    history = source_client.get("/api/v1/config/imports").json()
    assert history[0]["status"] == "rolled_back"


def test_only_the_latest_import_can_be_rolled_back(
    target: FastAPI, target_client: TestClient
) -> None:
    first = _import(target_client, _doc(groups=[{"name": "one"}]), passphrase=None, confirm=True)
    second = _import(target_client, _doc(groups=[{"name": "two"}]), passphrase=None, confirm=True)
    assert first.status_code == second.status_code == 200
    older = target_client.post(
        f"/api/v1/config/imports/{first.json()['run']['id']}/rollback",
        json={"confirm": True},
        headers=_csrf(target_client),
    )
    assert older.status_code == 409
    for response in (second, first):
        rolled = target_client.post(
            f"/api/v1/config/imports/{response.json()['run']['id']}/rollback",
            json={"confirm": True},
            headers=_csrf(target_client),
        )
        assert rolled.status_code == 200, rolled.text
    with target.state.db.session_factory() as session:
        assert {g.name for g in session.execute(select(Group)).scalars()} == set()


def test_permissions_and_api_tokens(source: FastAPI, source_client: TestClient) -> None:
    with TestClient(source) as anonymous:
        assert anonymous.post("/api/v1/config/export", json={}).status_code in (401, 403)
    with _client(source, "alice") as operator:
        assert (
            operator.post("/api/v1/config/export", json={}, headers=_csrf(operator)).status_code
            == 403
        )
        assert operator.get("/api/v1/config/imports").status_code == 403
        assert operator.get("/config").status_code in (401, 403)
    token = source_client.post(
        "/api/v1/tokens",
        json={"name": "script", "scopes": ["config:export", "config:import"]},
        headers=_csrf(source_client),
    ).json()["token"]
    with TestClient(source) as bearer:
        bearer.headers["Authorization"] = f"Bearer {token}"
        assert bearer.post("/api/v1/config/export", json={}).status_code == 403
        assert bearer.get("/api/v1/config/imports").status_code == 403
    page = source_client.get("/config")
    assert page.status_code == 200 and "js/config.js" in page.text
    assert 'href="/config"' in source_client.get("/").text
    with source.state.db.session_factory() as session:
        exported = (
            session.execute(select(AuditEvent).where(AuditEvent.event == "config.exported"))
            .scalars()
            .all()
        )
        assert exported == []  # nothing was exported in this test
