"""Resumable (chunked) uploads (Phase 6)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.requests import ClientDisconnect

from app.db.base import utcnow_naive
from app.models import AuditEvent, PublicUploadFile, PublicUploadQuota, UploadSession, User
from app.services import public_uploads, resumable_uploads
from tests.conftest import ADMIN_PASSWORD, login


def _root(client: TestClient) -> str:
    roots: list[str] = client.get("/api/v1/files/roots").json()
    return roots[0]


def _create(client: TestClient, headers: dict[str, str], **body: Any) -> Any:
    return client.post("/api/v1/uploads", json=body, headers=headers)


def _patch(
    client: TestClient, headers: dict[str, str], upload_id: str, offset: int, data: bytes
) -> Any:
    return client.patch(
        f"/api/v1/uploads/{upload_id}",
        content=data,
        headers={
            **headers,
            "Upload-Offset": str(offset),
            "Content-Type": "application/offset+octet-stream",
        },
    )


def _staged(app: FastAPI, upload_id: str) -> Path:
    with app.state.db.session_factory() as session:
        row = session.get(UploadSession, upload_id)
        assert row is not None
        return resumable_uploads.staged_path(app.state.settings, row)


def test_files_target_multi_chunk_roundtrip(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    root = _root(authed_client)
    data = b"0123456789" * 10
    created = _create(
        authed_client,
        csrf_header,
        target="files",
        root=root,
        filename="video.bin",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    assert created.status_code == 201, created.text
    upload = created.json()
    assert upload["offset"] == 0
    assert created.headers["upload-offset"] == "0"
    assert created.headers["location"].endswith(upload["id"])
    upload_id = upload["id"]

    # The staging file is invisible to listings while the upload is open.
    listing = authed_client.get("/api/v1/files/list", params={"root": root}).json()
    assert [e["name"] for e in listing["entries"]] == []
    assert _staged(app, upload_id).exists()

    for offset in range(0, len(data), 40):
        response = _patch(authed_client, csrf_header, upload_id, offset, data[offset : offset + 40])
        assert response.status_code == 200, response.text
        assert response.headers["upload-offset"] == str(min(offset + 40, len(data)))

    head = authed_client.head(f"/api/v1/uploads/{upload_id}")
    assert head.headers["upload-offset"] == str(len(data))
    assert head.headers["upload-length"] == str(len(data))

    done = authed_client.post(f"/api/v1/uploads/{upload_id}/complete", headers=csrf_header)
    assert done.status_code == 200, done.text
    assert done.json()["file"]["path"] == "video.bin"
    assert (Path(root) / "video.bin").read_bytes() == data
    assert authed_client.get(f"/api/v1/uploads/{upload_id}").status_code == 404
    with app.state.db.session_factory() as session:
        assert session.execute(select(UploadSession)).first() is None
        events = {row.event for row in session.execute(select(AuditEvent)).scalars()}
    assert {"upload.session_created", "upload.session_completed", "file.uploaded"} <= events


def test_resume_after_lost_ack_and_wrong_offset(
    authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    root = _root(authed_client)
    upload_id = _create(
        authed_client, csrf_header, target="files", root=root, filename="r.bin", size=6
    ).json()["id"]
    assert _patch(authed_client, csrf_header, upload_id, 0, b"abc").status_code == 200

    wrong = _patch(authed_client, csrf_header, upload_id, 0, b"abc")
    assert wrong.status_code == 409
    assert wrong.json()["error"]["details"]["offset"] == 3

    # The client asks where to continue and sends the rest.
    status = authed_client.get(f"/api/v1/uploads/{upload_id}").json()
    assert status["offset"] == 3
    assert _patch(authed_client, csrf_header, upload_id, 3, b"def").status_code == 200
    done = authed_client.post(f"/api/v1/uploads/{upload_id}/complete", headers=csrf_header)
    assert done.status_code == 200
    assert (Path(root) / "r.bin").read_bytes() == b"abcdef"


def test_disconnect_keeps_received_bytes(app: FastAPI, admin_user: User) -> None:
    root = str(app.state.settings.resolved_file_roots[0])
    with app.state.db.session_factory() as session:
        user = session.get(User, admin_user.id)
        assert user is not None
        row = resumable_uploads.create_upload(
            session,
            app.state.settings,
            user=user,
            token=None,
            target="files",
            root=root,
            filename="cut.bin",
            size=10,
        )

        async def cut_off() -> AsyncIterator[bytes]:
            yield b"1234"
            raise ClientDisconnect

        with pytest.raises(ClientDisconnect):
            asyncio.run(
                resumable_uploads.append_chunk(
                    session, app.state.settings, row, offset=0, chunks=cut_off()
                )
            )
        session.refresh(row)
        assert row.received_bytes == 4
        assert resumable_uploads.staged_path(app.state.settings, row).read_bytes() == b"1234"


def test_limits_are_enforced(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    root = _root(authed_client)
    app.state.settings.max_upload_bytes = 2 * 1024 * 1024
    too_big = _create(
        authed_client, csrf_header, target="files", root=root, filename="x", size=3 * 1024 * 1024
    )
    assert too_big.status_code == 400

    upload_id = _create(
        authed_client, csrf_header, target="files", root=root, filename="y", size=4
    ).json()["id"]
    overflow = _patch(authed_client, csrf_header, upload_id, 0, b"12345")
    assert overflow.status_code == 400
    # The rejected chunk is rolled back entirely.
    assert authed_client.get(f"/api/v1/uploads/{upload_id}").json()["offset"] == 0
    assert _staged(app, upload_id).stat().st_size == 0

    app.state.settings.resumable_chunk_max_bytes = 2
    assert _patch(authed_client, csrf_header, upload_id, 0, b"123").status_code == 400
    assert _patch(authed_client, csrf_header, upload_id, 0, b"12").status_code == 200

    incomplete = authed_client.post(f"/api/v1/uploads/{upload_id}/complete", headers=csrf_header)
    assert incomplete.status_code == 409
    assert incomplete.json()["error"]["details"]["offset"] == 2

    bad_offset = authed_client.patch(
        f"/api/v1/uploads/{upload_id}", content=b"34", headers={**csrf_header, "Upload-Offset": "x"}
    )
    assert bad_offset.status_code == 400


def test_checksum_mismatch_discards_upload(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    root = _root(authed_client)
    upload_id = _create(
        authed_client,
        csrf_header,
        target="files",
        root=root,
        filename="sum.bin",
        size=3,
        sha256=hashlib.sha256(b"abc").hexdigest(),
    ).json()["id"]
    staged = _staged(app, upload_id)
    _patch(authed_client, csrf_header, upload_id, 0, b"abd")
    response = authed_client.post(f"/api/v1/uploads/{upload_id}/complete", headers=csrf_header)
    assert response.status_code == 400
    assert not (Path(root) / "sum.bin").exists()
    assert not staged.exists()
    assert authed_client.get(f"/api/v1/uploads/{upload_id}").status_code == 404


def test_overwrite_conflict_and_path_jail(
    authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    root = _root(authed_client)
    (Path(root) / "taken.txt").write_bytes(b"old")
    conflict = _create(
        authed_client, csrf_header, target="files", root=root, filename="taken.txt", size=1
    )
    assert conflict.status_code == 409
    for body in (
        {"filename": "ok", "path": "../.."},
        {"filename": "../escape"},
        {"filename": "ok", "path": "/etc"},
    ):
        bad = _create(authed_client, csrf_header, target="files", root=root, size=1, **body)
        assert bad.status_code == 400, body
    unknown_root = _create(
        authed_client, csrf_header, target="files", root="/nope", filename="x", size=1
    )
    assert unknown_root.status_code == 404

    # A file that appears while the upload is in flight is not clobbered.
    upload_id = _create(
        authed_client, csrf_header, target="files", root=root, filename="race.txt", size=3
    ).json()["id"]
    _patch(authed_client, csrf_header, upload_id, 0, b"new")
    (Path(root) / "race.txt").write_bytes(b"first")
    late = authed_client.post(f"/api/v1/uploads/{upload_id}/complete", headers=csrf_header)
    assert late.status_code == 409
    assert (Path(root) / "race.txt").read_bytes() == b"first"
    # The bytes are kept so the client can decide; abort discards them.
    assert authed_client.get(f"/api/v1/uploads/{upload_id}").status_code == 200
    assert (
        authed_client.delete(f"/api/v1/uploads/{upload_id}", headers=csrf_header).status_code == 204
    )

    overwrite_id = _create(
        authed_client,
        csrf_header,
        target="files",
        root=root,
        filename="taken.txt",
        size=3,
        overwrite=True,
    ).json()["id"]
    _patch(authed_client, csrf_header, overwrite_id, 0, b"new")
    assert (
        authed_client.post(
            f"/api/v1/uploads/{overwrite_id}/complete", headers=csrf_header
        ).status_code
        == 200
    )
    assert (Path(root) / "taken.txt").read_bytes() == b"new"


def test_uploads_are_private_to_their_owner(app: FastAPI, admin_user: User) -> None:
    from app.services.users_service import create_user

    with app.state.db.session_factory() as session:
        create_user(
            session,
            username="other",
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=["admin"],
            groups=[],
            settings=app.state.settings,
            actor=admin_user,
        )
    with TestClient(app) as owner, TestClient(app) as other:
        login(owner, "admin", ADMIN_PASSWORD)
        login(other, "other", ADMIN_PASSWORD)
        owner_headers = {"X-CSRF-Token": owner.cookies["nas_csrf"]}
        other_headers = {"X-CSRF-Token": other.cookies["nas_csrf"]}
        upload_id = _create(
            owner, owner_headers, target="files", root=_root(owner), filename="mine", size=1
        ).json()["id"]
        assert other.get(f"/api/v1/uploads/{upload_id}").status_code == 404
        assert _patch(other, other_headers, upload_id, 0, b"x").status_code == 404
        assert (
            other.delete(f"/api/v1/uploads/{upload_id}", headers=other_headers).status_code == 404
        )
        assert owner.get(f"/api/v1/uploads/{upload_id}").status_code == 200


def test_files_target_requires_files_write(app: FastAPI, admin_user: User) -> None:
    from app.services.users_service import create_user

    with app.state.db.session_factory() as session:
        create_user(
            session,
            username="viewer1",
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=["viewer"],
            groups=[],
            settings=app.state.settings,
            actor=admin_user,
        )
    with TestClient(app) as client:
        login(client, "viewer1", ADMIN_PASSWORD)
        headers = {"X-CSRF-Token": client.cookies["nas_csrf"]}
        root = str(app.state.settings.resolved_file_roots[0])
        denied = _create(client, headers, target="files", root=root, filename="x", size=1)
        assert denied.status_code == 403
    with TestClient(app) as anon:
        assert anon.post("/api/v1/uploads", json={"target": "public"}).status_code in {401, 422}
        assert (
            anon.post(
                "/api/v1/uploads", json={"target": "public", "filename": "a", "size": 1}
            ).status_code
            == 401
        )


def test_open_upload_cap(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    app.state.settings.resumable_max_open_per_user = 2
    root = _root(authed_client)
    for name in ("a", "b"):
        assert (
            _create(
                authed_client, csrf_header, target="files", root=root, filename=name, size=1
            ).status_code
            == 201
        )
    third = _create(authed_client, csrf_header, target="files", root=root, filename="c", size=1)
    assert third.status_code == 409


def test_expired_uploads_are_swept(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    root = _root(authed_client)
    upload_id = _create(
        authed_client, csrf_header, target="files", root=root, filename="old", size=5
    ).json()["id"]
    staged = _staged(app, upload_id)
    with app.state.db.session_factory() as session:
        row = session.get(UploadSession, upload_id)
        assert row is not None
        row.expires_at = utcnow_naive() - timedelta(minutes=1)
        session.commit()
    assert authed_client.get(f"/api/v1/uploads/{upload_id}").status_code == 404
    swept = authed_client.post("/api/v1/uploads/cleanup", headers=csrf_header)
    assert swept.status_code == 200
    assert swept.json()["expired_uploads"] == 1
    assert not staged.exists()
    with app.state.db.session_factory() as session:
        assert session.get(UploadSession, upload_id) is None


def test_public_target_with_token_quota(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    token = authed_client.post(
        "/api/v1/tokens", json={"name": "cam", "scopes": ["dashboard:read"]}, headers=csrf_header
    ).json()
    bearer = {"Authorization": f"Bearer {token['token']}"}
    key = f"token:{token['id']}"
    app.state.settings.api_token_upload_quota_bytes = 10

    with TestClient(app) as phone:
        too_big = _create(phone, bearer, target="public", filename="big.jpg", size=11)
        assert too_big.status_code == 413

        upload_id = _create(phone, bearer, target="public", filename="pic.jpg", size=6).json()["id"]
        assert _staged(app, upload_id).parent == public_uploads.uploader_dir(
            app.state.settings, key
        )
        assert _patch(phone, bearer, upload_id, 0, b"abcdef").status_code == 200

        # Meanwhile another upload uses up most of the token's quota.
        other = _create(phone, bearer, target="public", filename="b.jpg", size=5).json()["id"]
        _patch(phone, bearer, other, 0, b"12345")
        first = phone.post(f"/api/v1/uploads/{other}/complete", headers=bearer)
        assert first.status_code == 200, first.text
        assert first.json()["public"]["uploader_key"] == key

        # The authoritative check at completion rejects the overflow.
        late = phone.post(f"/api/v1/uploads/{upload_id}/complete", headers=bearer)
        assert late.status_code == 413
        assert phone.get(f"/api/v1/uploads/{upload_id}", headers=bearer).status_code == 404

    with app.state.db.session_factory() as session:
        quota = session.get(PublicUploadQuota, key)
        assert quota is not None
        assert (quota.quota_bytes, quota.used_bytes) == (10, 5)
        names = [row.filename for row in session.execute(select(PublicUploadFile)).scalars()]
        assert names == ["b.jpg"]
        # Phase 13: the rejected completion is not also audited as "completed".
        completed = session.execute(
            select(AuditEvent.target_id).where(AuditEvent.event == "upload.session_completed")
        ).scalars()
        assert list(completed) == [other]
    leftovers = list(public_uploads.uploader_dir(app.state.settings, key).iterdir())
    assert [p.name for p in leftovers] == ["b.jpg"]


def test_public_cleanup_spares_resumable_staging(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    upload_id = _create(
        authed_client, csrf_header, target="public", filename="keep.bin", size=4
    ).json()["id"]
    _patch(authed_client, csrf_header, upload_id, 0, b"ab")
    staged = _staged(app, upload_id)
    cleanup = authed_client.post("/api/v1/public/uploads/cleanup", json={}, headers=csrf_header)
    assert cleanup.status_code == 200
    assert staged.exists()
    _patch(authed_client, csrf_header, upload_id, 2, b"cd")
    done = authed_client.post(f"/api/v1/uploads/{upload_id}/complete", headers=csrf_header)
    assert done.status_code == 200
    assert done.json()["public"]["uploader_key"] == "user:admin"


def test_concurrent_request_for_same_upload_is_refused(app: FastAPI, admin_user: User) -> None:
    root = str(app.state.settings.resolved_file_roots[0])
    with app.state.db.session_factory() as session:
        user = session.get(User, admin_user.id)
        assert user is not None
        row = resumable_uploads.create_upload(
            session,
            app.state.settings,
            user=user,
            token=None,
            target="files",
            root=root,
            filename="busy",
            size=1,
        )
        with resumable_uploads._upload_lock(row.id), pytest.raises(Exception) as excinfo:
            resumable_uploads.abort_upload(session, app.state.settings, row, actor=user)
        assert getattr(excinfo.value, "status_code", None) == 409
