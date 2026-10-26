from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import PublicUploadFile, PublicUploadQuota, User
from app.services import public_uploads


def _upload_dir(app: FastAPI) -> Path:
    return Path(app.state.settings.resolved_public_upload_dir)


def _dir_for(app: FastAPI, key: str) -> Path:
    return public_uploads.uploader_dir(app.state.settings, key)


def _post_upload(client: TestClient, name: str, content: bytes) -> Any:
    """POST a multipart upload; echo the CSRF cookie when the client has one."""
    headers = {}
    if "nas_csrf" in client.cookies:
        headers["X-CSRF-Token"] = client.cookies["nas_csrf"]
    return client.post(
        "/api/v1/public/uploads",
        files={"file": (name, content, "application/octet-stream")},
        headers=headers,
    )


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["nas_csrf"]}


def test_policy_endpoint_is_public_and_upload_only(client: TestClient) -> None:
    response = client.get("/api/v1/public/uploads")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["max_file_bytes"] == 1024**3
    assert body["filename_max_length"] == 255
    # No listing or status is exposed without the admin permission.
    assert client.get("/api/v1/public/uploads/status").status_code == 401


def test_anonymous_upload_roundtrip(client: TestClient, app: FastAPI) -> None:
    response = _post_upload(client, "hello.txt", b"hello world")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["filename"] == "hello.txt"
    assert body["size_bytes"] == 11
    assert body["uploader_key"].startswith("ip:")
    assert body["id"] > 0

    path = _dir_for(app, body["uploader_key"]) / "hello.txt"
    assert path.read_bytes() == b"hello world"
    # The storage directory is a digest, never the raw key.
    assert body["uploader_key"] not in path.parent.name


def test_authenticated_upload_is_keyed_by_username(authed_client: TestClient, app: FastAPI) -> None:
    response = authed_client.post(
        "/api/v1/public/uploads",
        files={"file": ("mine.txt", b"data", "text/plain")},
        headers=_csrf(authed_client),
    )
    assert response.status_code == 201, response.text
    assert response.json()["uploader_key"] == "user:admin"


def test_drop_folder_never_overwrites_and_uniquifies_names(
    client: TestClient, app: FastAPI
) -> None:
    first = _post_upload(client, "photo.jpg", b"aaa")
    second = _post_upload(client, "photo.jpg", b"bbbb")
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["filename"] == "photo.jpg"
    assert second.json()["filename"] == "photo (1).jpg"
    key = first.json()["uploader_key"]
    own_dir = _dir_for(app, key)
    assert (own_dir / "photo.jpg").read_bytes() == b"aaa"
    assert (own_dir / "photo (1).jpg").read_bytes() == b"bbbb"


def test_filenames_are_sanitized_and_traversal_rejected(client: TestClient, app: FastAPI) -> None:
    for evil in ["../escape.txt", "a/b.txt", "/etc/passwd", "..", "~root/x"]:
        response = _post_upload(client, evil, b"x")
        assert response.status_code in (400, 422), evil
    surviving = list(_upload_dir(app).rglob("*"))
    assert all(path.name != "escape.txt" for path in surviving)


def test_quota_is_enforced_and_reservation_rolled_back(
    authed_client: TestClient, app: FastAPI
) -> None:
    app.state.settings.public_upload_default_quota_bytes = 30
    ok = authed_client.post(
        "/api/v1/public/uploads",
        files={"file": ("a.txt", b"a" * 20, "text/plain")},
        headers=_csrf(authed_client),
    )
    assert ok.status_code == 201, ok.text
    reject = authed_client.post(
        "/api/v1/public/uploads",
        files={"file": ("b.txt", b"b" * 15, "text/plain")},
        headers=_csrf(authed_client),
    )
    assert reject.status_code == 413
    assert reject.json()["error"]["code"] == "quota_exceeded"

    with app.state.db.session_factory() as session:
        quota = session.get(PublicUploadQuota, "user:admin")
        rows = (
            session.execute(
                select(PublicUploadFile).where(PublicUploadFile.uploader_key == "user:admin")
            )
            .scalars()
            .all()
        )
    assert quota is not None
    assert quota.used_bytes == 20  # the rejected upload reserved nothing
    assert len(rows) == 1
    assert not (_dir_for(app, "user:admin") / "b.txt").exists()

    # A file that fits exactly at the remaining quota succeeds.
    fits = authed_client.post(
        "/api/v1/public/uploads",
        files={"file": ("c.txt", b"c" * 10, "text/plain")},
        headers=_csrf(authed_client),
    )
    assert fits.status_code == 201, fits.text


def test_quota_rejection_is_audited_as_failure(authed_client: TestClient, app: FastAPI) -> None:
    app.state.settings.public_upload_default_quota_bytes = 5
    _ = authed_client.post(
        "/api/v1/public/uploads",
        files={"file": ("x.txt", b"123456", "text/plain")},
        headers=_csrf(authed_client),
    )
    page = authed_client.get("/api/v1/audit/events?category=public_upload")
    assert page.status_code == 200
    events = page.json()["events"]
    assert any(e["event"] == "public_upload.rejected" for e in events)
    rejected = next(e for e in events if e["event"] == "public_upload.rejected")
    assert rejected["outcome"] == "failure"
    assert rejected["details"]["reason"] == "quota"


def test_max_file_size_is_enforced_and_no_temp_leftover(client: TestClient, app: FastAPI) -> None:
    app.state.settings.public_upload_max_file_bytes = 10
    response = _post_upload(client, "big.bin", b"x" * 11)
    assert response.status_code == 400
    surviving = [p for p in _upload_dir(app).rglob("*") if p.is_file()]
    assert surviving == []
    assert not list(_upload_dir(app).rglob(".nas-upload-*"))


def test_runtime_toggle_disables_uploads(
    authed_client: TestClient, client: TestClient, app: FastAPI
) -> None:
    # Default from config: enabled.
    assert authed_client.get("/api/v1/settings").json()["public_upload_enabled"] is True
    off = authed_client.put(
        "/api/v1/settings",
        json={"public_upload_enabled": False},
        headers=_csrf(authed_client),
    )
    assert off.status_code == 200
    assert off.json()["public_upload_enabled"] is False

    assert client.get("/api/v1/public/uploads").json()["enabled"] is False
    blocked = _post_upload(client, "nope.txt", b"x")
    assert blocked.status_code == 403
    assert (_upload_dir(app) / "nope.txt").exists() is False

    back_on = authed_client.put(
        "/api/v1/settings",
        json={"public_upload_enabled": True},
        headers=_csrf(authed_client),
    )
    assert back_on.status_code == 200
    assert _post_upload(client, "yes.txt", b"ok").status_code == 201


def test_admin_status_reports_usage_and_over_quota(
    authed_client: TestClient, client: TestClient, app: FastAPI
) -> None:
    _post_upload(client, "one.bin", b"a" * 100)
    _post_upload(client, "two.bin", b"b" * 50)

    status = authed_client.get("/api/v1/public/uploads/status")
    assert status.status_code == 200
    body = status.json()
    assert body["total_files"] == 2
    assert body["total_used_bytes"] == 150
    assert len(body["uploaders"]) == 1
    uploader = body["uploaders"][0]
    assert uploader["uploader_key"].startswith("ip:")
    assert uploader["used_bytes"] == 150
    assert uploader["file_count"] == 2
    assert uploader["over_quota"] is False
    assert uploader["quota_bytes"] == 1024**3

    # Lower the quota below use: status flags it without deleting anything.
    key = uploader["uploader_key"]
    adjusted = authed_client.patch(
        f"/api/v1/public/uploads/quotas/{key}",
        json={"quota_bytes": 100},
        headers=_csrf(authed_client),
    )
    assert adjusted.status_code == 200
    assert adjusted.json()["over_quota"] is True
    assert adjusted.json()["used_bytes"] == 150
    assert _post_upload(client, "three.bin", b"c").status_code == 413


def test_admin_lists_and_filters_files(
    authed_client: TestClient, client: TestClient, app: FastAPI
) -> None:
    first = _post_upload(client, "alpha.txt", b"a")
    _post_upload(client, "beta.txt", b"b")
    page = authed_client.get("/api/v1/public/uploads/files")
    assert page.status_code == 200
    body = page.json()
    assert body["total"] == 2
    assert {f["filename"] for f in body["files"]} == {"alpha.txt", "beta.txt"}

    key = first.json()["uploader_key"]
    filtered = authed_client.get(f"/api/v1/public/uploads/files?uploader_key={key}")
    assert filtered.json()["total"] == 2
    unknown = authed_client.get("/api/v1/public/uploads/files?uploader_key=user:nobody")
    assert unknown.json()["total"] == 0


def test_admin_delete_file_removes_disk_and_decrements_usage(
    authed_client: TestClient, client: TestClient, app: FastAPI
) -> None:
    result = _post_upload(client, "victim.bin", b"v" * 40)
    key = result.json()["uploader_key"]
    stored = _dir_for(app, key) / "victim.bin"
    assert stored.exists()

    delete = authed_client.request(
        "DELETE",
        f"/api/v1/public/uploads/files/{result.json()['id']}",
        headers=_csrf(authed_client),
    )
    assert delete.status_code == 204
    assert not stored.exists()
    status = authed_client.get("/api/v1/public/uploads/status").json()
    assert status["total_files"] == 0
    assert status["total_used_bytes"] == 0
    assert status["uploaders"][0]["used_bytes"] == 0

    missing = authed_client.request(
        "DELETE", "/api/v1/public/uploads/files/999999", headers=_csrf(authed_client)
    )
    assert missing.status_code == 404


def test_cleanup_removes_temps_purges_old_files_and_reconciles(
    authed_client: TestClient, app: FastAPI
) -> None:
    from datetime import UTC, datetime, timedelta

    app.state.settings.public_upload_default_quota_bytes = 10000
    fresh = authed_client.post(
        "/api/v1/public/uploads",
        files={"file": ("fresh.txt", b"f" * 10, "text/plain")},
        headers=_csrf(authed_client),
    )
    assert fresh.status_code == 201
    key = fresh.json()["uploader_key"]

    # A stale temp file and an old tracked file (row + disk, created 40 days ago).
    (Path(app.state.settings.resolved_public_upload_dir) / ".nas-upload-stale.part").write_text(
        "junk"
    )
    old_dir = _dir_for(app, key)
    old_file = old_dir / "old.bin"
    old_file.write_bytes(b"o" * 60)
    old_created = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=40)
    with app.state.db.session_factory() as session:
        row = PublicUploadFile(uploader_key=key, filename="old.bin", size_bytes=60, ip_address=None)
        row.created_at = old_created
        session.add(row)
        session.commit()

    result = authed_client.post(
        "/api/v1/public/uploads/cleanup",
        json={"older_than_days": 30},
        headers=_csrf(authed_client),
    )
    assert result.status_code == 200
    body = result.json()
    assert body["removed_temp_files"] == 1
    assert body["removed_files"] == 1
    assert body["removed_bytes"] == 60
    assert body["reconciled_uploaders"] == 1

    status = authed_client.get("/api/v1/public/uploads/status").json()
    assert status["total_files"] == 1
    assert status["total_used_bytes"] == 10
    assert status["uploaders"][0]["used_bytes"] == 10
    assert not old_file.exists()
    upload_root = Path(app.state.settings.resolved_public_upload_dir)
    assert not (upload_root / ".nas-upload-stale.part").exists()


def test_cleanup_default_only_removes_temps(
    authed_client: TestClient, client: TestClient, app: FastAPI
) -> None:
    _post_upload(client, "keep.txt", b"keep")
    (Path(app.state.settings.resolved_public_upload_dir) / ".nas-upload-dead.part").write_text("x")
    result = authed_client.post(
        "/api/v1/public/uploads/cleanup",
        json={},
        headers=_csrf(authed_client),
    )
    assert result.status_code == 200
    body = result.json()
    assert body["removed_temp_files"] == 1
    assert body["removed_files"] == 0
    assert authed_client.get("/api/v1/public/uploads/status").json()["total_files"] == 1


def test_permission_boundaries(
    authed_client: TestClient, client: TestClient, app: FastAPI, admin_user: User
) -> None:
    from app.services.users_service import create_user

    with app.state.db.session_factory() as session:
        create_user(
            session,
            username="op1",
            password="correct horse battery staple",
            display_name=None,
            email=None,
            role_names=["operator"],
            groups=[],
            settings=app.state.settings,
            actor=admin_user,
        )
    with TestClient(app) as operator:
        login = operator.post(
            "/api/v1/auth/login",
            json={"username": "op1", "password": "correct horse battery staple"},
        )
        assert login.status_code == 200
        # Operators default to uploads:read: they may view status/files.
        assert operator.get("/api/v1/public/uploads/status").status_code == 200
        assert operator.get("/api/v1/public/uploads/files").status_code == 200
        # ...but never change quotas, delete files or run cleanup.
        assert (
            operator.patch(
                "/api/v1/public/uploads/quotas/user:nobody",
                json={"quota_bytes": 1},
                headers=_csrf(operator),
            ).status_code
            == 403
        )
        assert (
            operator.request(
                "DELETE", "/api/v1/public/uploads/files/1", headers=_csrf(operator)
            ).status_code
            == 403
        )
        assert (
            operator.post(
                "/api/v1/public/uploads/cleanup", json={}, headers=_csrf(operator)
            ).status_code
            == 403
        )
        # The Uploads page is reachable read-only.
        assert operator.get("/uploads", follow_redirects=False).status_code == 200

    # Anonymous callers get 401 on every admin route.
    for method, path in [
        ("GET", "/api/v1/public/uploads/status"),
        ("PATCH", "/api/v1/public/uploads/quotas/user:nobody"),
        ("DELETE", "/api/v1/public/uploads/files/1"),
        ("POST", "/api/v1/public/uploads/cleanup"),
    ]:
        response = client.request(method, path, json={} if method != "GET" else None)
        assert response.status_code == 401, (method, path)


def test_upload_events_are_audited(authed_client: TestClient, app: FastAPI) -> None:
    authed_client.post(
        "/api/v1/public/uploads",
        files={"file": ("logged.bin", b"payload", "text/plain")},
        headers=_csrf(authed_client),
    )
    page = authed_client.get("/api/v1/audit/events?category=public_upload")
    assert page.status_code == 200
    events = [e["event"] for e in page.json()["events"]]
    assert "public_upload.uploaded" in events


def test_service_level_key_derivation_and_dirs(app: FastAPI) -> None:
    assert public_uploads.uploader_key_for_user("alice") == "user:alice"
    assert public_uploads.uploader_key_for_ip("10.0.0.7") == "ip:10.0.0.7"
    assert public_uploads.uploader_key_for_ip(None) == "ip:unknown"
    # Different keys always map to different directories.
    a = public_uploads.uploader_dir(app.state.settings, "user:alice")
    b = public_uploads.uploader_dir(app.state.settings, "ip:10.0.0.7")
    assert a != b
    assert a.is_relative_to(_upload_dir(app))


def test_quota_row_created_on_first_upload_and_shared_across_uploads(
    client: TestClient, app: FastAPI
) -> None:
    app.state.settings.public_upload_default_quota_bytes = 25
    assert _post_upload(client, "p1.bin", b"a" * 10).status_code == 201
    assert _post_upload(client, "p2.bin", b"b" * 10).status_code == 201
    assert _post_upload(client, "p3.bin", b"c" * 10).status_code == 413
    with app.state.db.session_factory() as session:
        quotas = session.execute(select(PublicUploadQuota)).scalars().all()
        files = session.execute(select(PublicUploadFile)).scalars().all()
    assert len(quotas) == 1
    assert quotas[0].used_bytes == 20
    assert len(files) == 2
