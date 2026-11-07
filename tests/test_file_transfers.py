"""Phase 16: USB drives on the Files page, access rules and copy/move transfers."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AuditEvent, FileChange, FileTransfer, User
from app.models.file_transfer import STATE_INTERRUPTED, STATE_RUNNING
from app.services import file_transfers, removable
from app.services.removable import RemovableDrive

from .media_helpers import create_user, user_client

API = "/api/v1/files"


@pytest.fixture
def usb(app: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A folder standing in for a plugged-in USB stick."""
    base = tmp_path / "usb-mounts"
    stick = base / "STICK"
    stick.mkdir(parents=True)
    object.__setattr__(app.state.settings, "removable_mount_dir", base)
    state: dict[str, Any] = {"root": stick, "plugged": True, "fstype": "vfat", "writable": True}

    def fake_list(settings: object, **_: object) -> list[RemovableDrive]:
        if not state["plugged"]:
            return []
        return [
            RemovableDrive(
                root=str(stick),
                label="STICK",
                device="/dev/sdz1",
                fstype=state["fstype"],
                total_bytes=8 * 1024**3,
                free_bytes=4 * 1024**3,
                writable=state["writable"],
            )
        ]

    monkeypatch.setattr(removable, "list_drives", fake_list)
    return state


@pytest.fixture
def nas_root(app: FastAPI) -> Path:
    root = app.state.settings.resolved_file_roots[0]
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def admin_client(app: FastAPI, admin_user: User) -> Any:
    with user_client(app, "admin") as client:
        yield client


def _wait(client: TestClient, transfer_id: int, timeout: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"{API}/transfers/{transfer_id}").json()
        if body["state"] not in ("queued", "running"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"transfer {transfer_id} did not finish: {body}")


def _start(client: TestClient, **payload: Any) -> dict[str, Any]:
    response = client.post(f"{API}/transfers", json=payload)
    assert response.status_code == 202, response.text
    return _wait(client, response.json()["id"])


def _tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): path.read_text()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ---------------------------------------------------------------------------
# Locations and access rules
# ---------------------------------------------------------------------------


def test_locations_list_nas_roots_and_usb_drives(
    admin_client: TestClient, usb: dict[str, Any], nas_root: Path
) -> None:
    locations = admin_client.get(f"{API}/locations").json()
    assert [(loc["kind"], loc["label"]) for loc in locations] == [
        ("nas", nas_root.name),
        ("removable", "STICK"),
    ]
    stick = locations[1]
    assert (
        stick["writable"] is True
        and stick["fstype"] == "vfat"
        and stick["free_bytes"] == 4 * 1024**3
    )
    usb["plugged"] = False
    assert [loc["kind"] for loc in admin_client.get(f"{API}/locations").json()] == ["nas"]
    gone = admin_client.get(f"{API}/list", params={"root": str(usb["root"])})
    assert gone.status_code == 404
    assert "no longer connected" in gone.json()["error"]["message"]


def test_permissions_split_nas_and_usb(
    app: FastAPI, admin_user: User, usb: dict[str, Any], nas_root: Path
) -> None:
    (nas_root / "secret.txt").write_text("nas")
    (usb["root"] / "photo.jpg").write_text("usb")
    create_user(app, admin_user, "olive", roles=["operator"])  # removable:* but no files:*
    create_user(app, admin_user, "vic", roles=["viewer"])  # neither
    stick = str(usb["root"])
    with user_client(app, "olive") as olive:
        assert [loc["kind"] for loc in olive.get(f"{API}/locations").json()] == ["removable"]
        listing = olive.get(f"{API}/list", params={"root": stick})
        assert [e["name"] for e in listing.json()["entries"]] == ["photo.jpg"]
        assert olive.get(f"{API}/list", params={"root": str(nas_root)}).status_code == 403
        assert (
            olive.get(
                f"{API}/content", params={"root": str(nas_root), "path": "secret.txt"}
            ).status_code
            == 403
        )
        assert olive.post(f"{API}/mkdir", json={"root": stick, "name": "New"}).status_code == 200
        # Copying NAS -> USB needs files:read on the NAS side.
        denied = olive.post(
            f"{API}/transfers",
            json={"sources": [{"root": str(nas_root), "path": "secret.txt"}], "dest_root": stick},
        )
        assert denied.status_code == 403
        assert olive.get(f"{API}/roots").status_code == 403
    with user_client(app, "vic") as vic:
        assert vic.get(f"{API}/locations").status_code == 403
        assert vic.get(f"{API}/list", params={"root": stick}).status_code == 403
        assert vic.get("/files").status_code in (401, 403)
    with user_client(app, "olive") as olive:
        assert olive.get("/files").status_code == 200


def test_usb_changes_are_not_journaled_for_mobile_sync(
    app: FastAPI, admin_client: TestClient, usb: dict[str, Any]
) -> None:
    stick = str(usb["root"])
    assert (
        admin_client.post(f"{API}/mkdir", json={"root": stick, "name": "Docs"}).status_code == 200
    )
    upload = admin_client.post(
        f"{API}/upload", data={"root": stick, "path": "Docs"}, files={"file": ("a.txt", b"hi")}
    )
    assert upload.status_code == 200, upload.text
    with app.state.db.session_factory() as session:
        assert session.execute(select(FileChange).where(FileChange.root == stick)).first() is None


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------


def test_copy_folder_tree_to_usb_and_back(
    app: FastAPI, admin_client: TestClient, usb: dict[str, Any], nas_root: Path
) -> None:
    (nas_root / "Holiday" / "Day 1").mkdir(parents=True)
    (nas_root / "Holiday" / "Day 1" / "a.jpg").write_text("A" * 3000)
    (nas_root / "Holiday" / "b.jpg").write_text("B")
    (nas_root / "note.txt").write_text("note")
    stick = str(usb["root"])
    done = _start(
        admin_client,
        sources=[
            {"root": str(nas_root), "path": "Holiday"},
            {"root": str(nas_root), "path": "note.txt"},
        ],
        dest_root=stick,
    )
    assert done["state"] == "done", done
    assert done["total_files"] == 3 and done["done_files"] == 3 and done["errors"] == []
    assert done["done_bytes"] == done["total_bytes"] == 3005
    assert _tree(usb["root"]) == {
        "Holiday/Day 1/a.jpg": "A" * 3000,
        "Holiday/b.jpg": "B",
        "note.txt": "note",
    }
    assert not any(".nas-part-" in p.name for p in usb["root"].rglob("*"))
    # Sources are untouched by a copy.
    assert (nas_root / "note.txt").read_text() == "note"

    # Copy back into the same NAS folder: "keep both" makes "note (1).txt".
    (nas_root / "In").mkdir()
    back = _start(
        admin_client,
        sources=[{"root": stick, "path": "note.txt"}],
        dest_root=str(nas_root),
        dest_path="In",
    )
    assert back["state"] == "done"
    assert (nas_root / "In" / "note.txt").read_text() == "note"
    again = _start(
        admin_client,
        sources=[{"root": stick, "path": "note.txt"}],
        dest_root=str(nas_root),
        dest_path="In",
    )
    assert again["state"] == "done"
    assert sorted(p.name for p in (nas_root / "In").iterdir()) == ["note (1).txt", "note.txt"]
    with app.state.db.session_factory() as session:
        journaled = (
            session.execute(
                select(FileChange.path).where(FileChange.root == str(nas_root.resolve()))
            )
            .scalars()
            .all()
        )
    assert "In/note.txt" in journaled and "In/note (1).txt" in journaled
    events = _audit(app, "file_transfer.")
    assert (
        events.count("file_transfer.started") == 3 and events.count("file_transfer.finished") == 3
    )


def _audit(app: FastAPI, prefix: str) -> list[str]:
    with app.state.db.session_factory() as session:
        return list(
            session.execute(
                select(AuditEvent.event)
                .where(AuditEvent.event.like(f"{prefix}%"))
                .order_by(AuditEvent.id)
            ).scalars()
        )


def test_conflict_rules_skip_replace_and_merge(
    admin_client: TestClient, usb: dict[str, Any], nas_root: Path
) -> None:
    (nas_root / "Docs").mkdir()
    (nas_root / "Docs" / "a.txt").write_text("new")
    (nas_root / "Docs" / "b.txt").write_text("b")
    (usb["root"] / "Docs").mkdir()
    (usb["root"] / "Docs" / "a.txt").write_text("old")
    stick = str(usb["root"])
    source = [{"root": str(nas_root), "path": "Docs"}]
    skipped = _start(admin_client, sources=source, dest_root=stick, conflict="skip")
    assert skipped["state"] == "done" and skipped["skipped_files"] == 1
    assert _tree(usb["root"]) == {"Docs/a.txt": "old", "Docs/b.txt": "b"}
    replaced = _start(admin_client, sources=source, dest_root=stick, conflict="replace")
    assert replaced["state"] == "done"
    assert _tree(usb["root"]) == {"Docs/a.txt": "new", "Docs/b.txt": "b"}
    both = _start(admin_client, sources=source, dest_root=stick, conflict="keep_both")
    assert both["state"] == "done"
    assert sorted(p.name for p in usb["root"].iterdir()) == ["Docs", "Docs (1)"]


def test_move_within_and_across_locations(
    app: FastAPI, admin_client: TestClient, usb: dict[str, Any], nas_root: Path
) -> None:
    (nas_root / "A").mkdir()
    (nas_root / "B").mkdir()
    (nas_root / "A" / "x.txt").write_text("x")
    (nas_root / "A" / "Sub").mkdir()
    (nas_root / "A" / "Sub" / "y.txt").write_text("y")
    within = _start(
        admin_client,
        mode="move",
        sources=[{"root": str(nas_root), "path": "A/x.txt"}],
        dest_root=str(nas_root),
        dest_path="B",
    )
    assert within["state"] == "done"
    assert (nas_root / "B" / "x.txt").read_text() == "x" and not (nas_root / "A" / "x.txt").exists()
    across = _start(
        admin_client,
        mode="move",
        sources=[{"root": str(nas_root), "path": "A/Sub"}],
        dest_root=str(usb["root"]),
    )
    assert across["state"] == "done", across
    assert _tree(usb["root"]) == {"Sub/y.txt": "y"}
    assert not (nas_root / "A" / "Sub").exists()
    with app.state.db.session_factory() as session:
        deleted = session.execute(
            select(FileChange.path, FileChange.kind).where(FileChange.kind == "deleted")
        ).all()
    assert ("A/x.txt", "deleted") in deleted and ("A/Sub", "deleted") in deleted
    same = admin_client.post(
        f"{API}/transfers",
        json={
            "mode": "move",
            "sources": [{"root": str(nas_root), "path": "B/x.txt"}],
            "dest_root": str(nas_root),
            "dest_path": "B",
        },
    )
    assert same.status_code == 400


def test_refusals_before_queueing(
    admin_client: TestClient, usb: dict[str, Any], nas_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (nas_root / "Tree" / "Inner").mkdir(parents=True)
    root = str(nas_root)

    def post(**payload: Any) -> Any:
        return admin_client.post(f"{API}/transfers", json=payload)

    assert (
        post(
            sources=[{"root": root, "path": "Tree"}], dest_root=root, dest_path="Tree/Inner"
        ).status_code
        == 400
    )
    assert post(sources=[{"root": root, "path": "missing"}], dest_root=root).status_code == 400
    assert (
        post(sources=[{"root": root, "path": "Tree"}], dest_root=root, dest_path="nope").status_code
        == 400
    )
    assert post(sources=[{"root": root, "path": "../etc"}], dest_root=root).status_code == 400
    assert post(sources=[{"root": "/etc", "path": "passwd"}], dest_root=root).status_code == 404
    assert post(sources=[], dest_root=root).status_code == 422
    (nas_root / "big.bin").write_bytes(b"x" * 1000)
    usb["writable"] = True
    monkeypatch.setattr(
        file_transfers.shutil,
        "disk_usage",
        lambda path: os.statvfs_result((0,) * 10) and type("U", (), {"free": 10})(),
    )
    full = post(sources=[{"root": root, "path": "big.bin"}], dest_root=str(usb["root"]))
    assert full.status_code == 409
    assert "not enough free space" in full.json()["error"]["message"]


def test_symlinks_are_skipped_and_reported(
    admin_client: TestClient, usb: dict[str, Any], nas_root: Path
) -> None:
    (nas_root / "Pics").mkdir()
    (nas_root / "Pics" / "real.txt").write_text("r")
    (nas_root / "Pics" / "link.txt").symlink_to("/etc/hostname")
    done = _start(
        admin_client, sources=[{"root": str(nas_root), "path": "Pics"}], dest_root=str(usb["root"])
    )
    assert done["state"] == "done"
    assert done["message"] == "Finished with 1 problem(s)"
    assert done["errors"] == [{"path": "Pics/link.txt", "message": "symbolic links are not copied"}]
    assert _tree(usb["root"]) == {"Pics/real.txt": "r"}


def test_large_file_on_fat_is_reported(
    admin_client: TestClient, usb: dict[str, Any], nas_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (nas_root / "movie.mkv").write_text("m" * 10)
    monkeypatch.setattr(file_transfers, "_FAT_LIMIT", 5)
    done = _start(
        admin_client,
        sources=[{"root": str(nas_root), "path": "movie.mkv"}],
        dest_root=str(usb["root"]),
    )
    assert done["state"] == "done" and done["skipped_files"] == 1
    assert "4 GB" in done["errors"][0]["message"]
    assert _tree(usb["root"]) == {}


def test_drive_removed_mid_copy_fails_the_transfer(
    admin_client: TestClient, usb: dict[str, Any], nas_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (nas_root / "a.txt").write_text("a")
    real_copy = file_transfers._Run.copy_file

    def unplug_then_fail(self: Any, source: Path, target: Path, rel: str) -> bool:
        usb["plugged"] = False
        raise_oserror = OSError(5, "Input/output error")
        try:
            raise raise_oserror
        except OSError:
            self.check_destination()
        return real_copy(self, source, target, rel)

    monkeypatch.setattr(file_transfers._Run, "copy_file", unplug_then_fail)
    done = _start(
        admin_client, sources=[{"root": str(nas_root), "path": "a.txt"}], dest_root=str(usb["root"])
    )
    assert done["state"] == "failed"
    assert done["message"] == "The USB drive was removed during the transfer"


def test_cancel_and_privacy(
    app: FastAPI,
    admin_user: User,
    admin_client: TestClient,
    usb: dict[str, Any],
    nas_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (nas_root / "a.txt").write_text("a" * 10)
    monkeypatch.setattr(app.state.transfers, "submit", lambda transfer_id: None)  # keep it queued
    queued = admin_client.post(
        f"{API}/transfers",
        json={"sources": [{"root": str(nas_root), "path": "a.txt"}], "dest_root": str(usb["root"])},
    ).json()
    assert queued["state"] == "queued"
    create_user(app, admin_user, "olive", roles=["operator"])
    with user_client(app, "olive") as olive:
        assert olive.get(f"{API}/transfers/{queued['id']}").status_code == 404
        assert olive.post(f"{API}/transfers/{queued['id']}/cancel").status_code == 404
        assert olive.get(f"{API}/transfers").json() == []
    cancelled = admin_client.post(f"{API}/transfers/{queued['id']}/cancel").json()
    assert cancelled["state"] == "cancelled"
    assert [t["id"] for t in admin_client.get(f"{API}/transfers").json()] == [queued["id"]]
    assert not (usb["root"] / "a.txt").exists()


def test_restart_marks_unfinished_transfers_interrupted(app: FastAPI, admin_user: User) -> None:
    with app.state.db.session_factory() as session:
        row = FileTransfer(
            user_id=admin_user.id,
            mode="copy",
            sources="[]",
            dest_root="/x",
            dest_path="",
            conflict="skip",
            state=STATE_RUNNING,
        )
        session.add(row)
        session.commit()
        transfer_id = row.id
    assert app.state.transfers.recover() == 1
    with app.state.db.session_factory() as session:
        assert session.get(FileTransfer, transfer_id).state == STATE_INTERRUPTED
