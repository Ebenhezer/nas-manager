"""Phase 8: gallery API - libraries, access rules, uploads, search, albums, sync."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings
from app.models import AuditEvent, MediaLibrary, User
from app.services.media import jobs

from .media_helpers import (
    create_group,
    create_user,
    jpeg_bytes,
    png_bytes,
    upload,
    user_client,
    write_jpeg,
)

API = "/api/v1/media"


@pytest.fixture
def admin(app: FastAPI, admin_user: User) -> User:
    return admin_user


@pytest.fixture
def admin_client(app: FastAPI, admin: User) -> Any:
    with user_client(app, "admin") as client:
        yield client


def _run_queued_scans(app: FastAPI) -> None:
    with app.state.db.session_factory() as session:
        while (library := jobs.claim_scan(session, owner="test", lease_seconds=60)) is not None:
            jobs.run_scan(session, app.state.settings, library)


def _audit_events(app: FastAPI, prefix: str) -> list[AuditEvent]:
    with app.state.db.session_factory() as session:
        return list(
            session.execute(
                select(AuditEvent)
                .where(AuditEvent.event.like(f"{prefix}%"))
                .order_by(AuditEvent.id)
            )
            .scalars()
            .all()
        )


def _shared_folder(settings: Settings, name: str = "Photos") -> Path:
    folder = settings.resolved_file_roots[0] / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# ---------------------------------------------------------------------------
# Permissions and personal libraries
# ---------------------------------------------------------------------------


def test_anonymous_is_rejected(client: TestClient) -> None:
    for path in ("/libraries", "/items", "/albums", "/items/1/original", "/sync/cursor"):
        assert client.get(f"{API}{path}").status_code == 401


def test_viewer_gets_a_personal_library_but_no_admin_routes(app: FastAPI, admin: User) -> None:
    viewer = create_user(app, admin, "alice")
    with user_client(app, "alice") as client:
        libs = client.get(f"{API}/libraries").json()
        assert len(libs) == 1
        own = libs[0]
        assert own["is_own"] and own["writable"] and own["browsable"]
        assert own["kind"] == "personal" and own["owner_user_id"] == viewer.id
        assert (app.state.settings.resolved_media_dir / "alice").is_dir()
        assert client.get(f"{API}/status").status_code == 403
        denied = client.post(f"{API}/libraries", json={"name": "x", "root": "/", "path": ""})
        assert denied.status_code == 403
        assert client.patch(f"{API}/libraries/{own['id']}", json={}).status_code == 403
        assert client.delete(f"{API}/libraries/{own['id']}").status_code == 403


def test_user_without_media_permissions_is_forbidden(
    app: FastAPI, admin_client: TestClient
) -> None:
    role = admin_client.post(
        "/api/v1/roles", json={"name": "dash_only", "permissions": ["dashboard:read"]}
    )
    assert role.status_code == 201, role.text
    created = admin_client.post(
        "/api/v1/users",
        json={
            "username": "bob",
            "password": "correct horse battery staple",
            "roles": ["dash_only"],
        },
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]
    replaced = admin_client.post(f"/api/v1/users/{user_id}/roles", json={"roles": ["dash_only"]})
    assert replaced.status_code == 200, replaced.text
    with user_client(app, "bob") as client:
        assert client.get(f"{API}/libraries").status_code == 403
        assert client.get("/gallery").status_code == 401


def test_api_token_scopes_apply(app: FastAPI, admin_client: TestClient) -> None:
    read_only = admin_client.post(
        "/api/v1/tokens", json={"name": "ro", "scopes": ["media:read"]}
    ).json()["token"]
    backup = admin_client.post(
        "/api/v1/tokens", json={"name": "phone", "scopes": ["media:read", "media:write"]}
    ).json()["token"]
    other = admin_client.post(
        "/api/v1/tokens", json={"name": "files", "scopes": ["files:read"]}
    ).json()["token"]
    with TestClient(app) as anon:
        assert (
            anon.get(f"{API}/items", headers={"Authorization": f"Bearer {other}"}).status_code
            == 403
        )
        files = {"file": ("x.jpg", jpeg_bytes(), "image/jpeg")}
        no_write = anon.post(
            f"{API}/upload", files=files, headers={"Authorization": f"Bearer {read_only}"}
        )
        assert no_write.status_code == 403
        ok = anon.post(f"{API}/upload", files=files, headers={"Authorization": f"Bearer {backup}"})
        assert ok.status_code == 201, ok.text  # bearer requests need no CSRF token


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------


def test_upload_lands_in_dated_folder_with_metadata(app: FastAPI, admin_client: TestClient) -> None:
    data = jpeg_bytes(gps=(-33.925, 18.4166))
    item = upload(admin_client, "IMG_0001.JPG", data, taken_at="2019-05-06T07:08:09Z")
    assert item["rel_path"] == "2019/05/IMG_0001.JPG"
    assert item["kind"] == "image" and item["mime"] == "image/jpeg"
    assert item["sha256"] == hashlib.sha256(data).hexdigest()
    assert item["camera_make"] == "Canon"
    assert item["taken_at"].startswith("2021-07-04T12:30")  # EXIF beats the client date
    assert item["gps_lat"] == pytest.approx(-33.925, abs=1e-4)
    stored = app.state.settings.resolved_media_dir / "admin" / "2019" / "05" / "IMG_0001.JPG"
    assert stored.read_bytes() == data
    assert int(stored.stat().st_mtime) == 1557126489  # the client's taken_at

    second = upload(admin_client, "IMG_0001.JPG", jpeg_bytes(color=(1, 1, 1)), path="2019/05")
    assert second["rel_path"] == "2019/05/IMG_0001 (1).JPG"
    assert stored.read_bytes() == data  # never overwritten
    events = _audit_events(app, "media.item_uploaded")
    assert len(events) == 2
    assert events[0].details is not None and events[0].details["path"] == "2019/05/IMG_0001.JPG"


def test_upload_validation(
    app: FastAPI, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def post(name: str, data: bytes = b"x", **form: str) -> Any:
        return admin_client.post(
            f"{API}/upload", files={"file": (name, data, "image/jpeg")}, data=form
        )

    assert post("notes.txt").status_code == 400
    assert post("vector.svg").status_code == 400
    assert post(".hidden.jpg").status_code == 400
    assert post("ok.jpg", path="../escape").status_code == 400
    assert post("ok.jpg", path="a/.secret").status_code == 400
    assert post("ok.jpg", path="/".join(["d"] * 9)).status_code == 400
    monkeypatch.setattr(app.state.settings, "max_upload_bytes", 1024 * 1024)
    too_big = post("big.jpg", b"0" * (1024 * 1024 + 1))
    assert too_big.status_code == 400
    folder = app.state.settings.resolved_media_dir / "admin"
    assert not [p for p in folder.rglob("*") if p.is_file()]


def test_upload_of_non_image_bytes_is_kept_but_has_no_thumbnail(admin_client: TestClient) -> None:
    item = upload(admin_client, "fake.jpg", b"not really a jpeg")
    assert item["thumb_state"] == "failed"
    response = admin_client.get(f"{API}/items/{item['id']}/thumbnail")
    assert response.status_code == 404


def test_resumable_upload_into_media_library(app: FastAPI, admin_client: TestClient) -> None:
    data = jpeg_bytes(size=(120, 90))
    created = admin_client.post(
        "/api/v1/uploads",
        json={"target": "media", "filename": "big.jpg", "size": len(data), "path": "Trips/2024"},
    )
    assert created.status_code == 201, created.text
    upload_id = created.json()["id"]
    patched = admin_client.patch(
        f"/api/v1/uploads/{upload_id}",
        content=data,
        headers={"Upload-Offset": "0", "Content-Type": "application/offset+octet-stream"},
    )
    assert patched.status_code == 200, patched.text
    done = admin_client.post(
        f"/api/v1/uploads/{upload_id}/complete",
        json={"sha256": hashlib.sha256(data).hexdigest()},
    )
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["target"] == "media" and body["file"] is None
    assert body["media"]["rel_path"] == "Trips/2024/big.jpg"
    folder = app.state.settings.resolved_media_dir / "admin" / "Trips" / "2024"
    assert [p.name for p in folder.iterdir()] == ["big.jpg"]


def test_resumable_media_target_validation(app: FastAPI, admin_client: TestClient) -> None:
    bad_type = admin_client.post(
        "/api/v1/uploads", json={"target": "media", "filename": "x.exe", "size": 3}
    )
    assert bad_type.status_code == 400
    traversal = admin_client.post(
        "/api/v1/uploads",
        json={"target": "media", "filename": "x.jpg", "size": 3, "path": "../../etc"},
    )
    assert traversal.status_code == 400


def test_owner_deletes_an_item(app: FastAPI, admin_client: TestClient) -> None:
    item = upload(admin_client, "gone.jpg", jpeg_bytes())
    path = app.state.settings.resolved_media_dir / "admin" / str(item["rel_path"])
    assert path.is_file()
    assert admin_client.delete(f"{API}/items/{item['id']}").status_code == 204
    assert not path.exists()
    assert admin_client.get(f"{API}/items/{item['id']}").status_code == 404
    assert _audit_events(app, "media.item_deleted")


# ---------------------------------------------------------------------------
# Isolation between users
# ---------------------------------------------------------------------------


def test_personal_libraries_are_private(
    app: FastAPI, admin: User, admin_client: TestClient
) -> None:
    create_user(app, admin, "alice")
    create_user(app, admin, "carol")
    with user_client(app, "alice") as alice:
        item = upload(alice, "private.jpg", jpeg_bytes())
        album = alice.post(f"{API}/albums", json={"name": "Mine"}).json()
        alice_lib = alice.get(f"{API}/libraries").json()[0]
    item_id = item["id"]
    with user_client(app, "carol") as carol:
        for path in (
            f"/items/{item_id}",
            f"/items/{item_id}/thumbnail",
            f"/items/{item_id}/original",
            f"/albums/{album['id']}",
            f"/libraries/{alice_lib['id']}",
        ):
            assert carol.get(f"{API}{path}").status_code == 404, path
        assert carol.delete(f"{API}/items/{item_id}").status_code == 404
        assert carol.post(f"{API}/libraries/{alice_lib['id']}/scan").status_code == 404
        assert carol.get(f"{API}/items").json()["total"] == 0
        own_album = carol.post(f"{API}/albums", json={"name": "Hers"}).json()
        stolen = carol.post(f"{API}/albums/{own_album['id']}/items", json={"item_ids": [item_id]})
        assert stolen.status_code == 404
        assert carol.get(f"{API}/items", params={"album_id": album["id"]}).status_code == 404
        changes = carol.get(
            f"{API}/sync/changes", params={"library_id": alice_lib["id"], "cursor": "m0"}
        )
        assert changes.status_code == 404

    # Administrators manage the library record but cannot browse the photos.
    libs = {lib["id"]: lib for lib in admin_client.get(f"{API}/libraries").json()}
    assert libs[alice_lib["id"]]["browsable"] is False
    assert libs[alice_lib["id"]]["counts"]["image"] == 1
    assert admin_client.get(f"{API}/items/{item_id}").status_code == 404
    assert admin_client.get(f"{API}/items/{item_id}/original").status_code == 404
    assert admin_client.post(f"{API}/libraries/{alice_lib['id']}/scan").status_code == 202


# ---------------------------------------------------------------------------
# Shared libraries
# ---------------------------------------------------------------------------


def test_shared_library_grants_follow_groups(
    app: FastAPI, admin: User, admin_client: TestClient
) -> None:
    settings = app.state.settings
    folder = _shared_folder(settings)
    write_jpeg(folder / "beach.jpg")
    write_jpeg(folder / "sub" / "dune.jpg", color=(5, 5, 5))
    member = create_user(app, admin, "alice")
    create_user(app, admin, "carol")
    group_id = create_group(app, admin, "family", [member.id])

    created = admin_client.post(
        f"{API}/libraries",
        json={
            "name": "Family photos",
            "root": str(settings.resolved_file_roots[0]),
            "path": "Photos",
            "group_ids": [group_id],
        },
    )
    assert created.status_code == 201, created.text
    library = created.json()
    assert library["kind"] == "shared" and library["scan_state"] == "queued"
    assert library["group_ids"] == [group_id]
    _run_queued_scans(app)

    with user_client(app, "alice") as alice:
        page = alice.get(f"{API}/items", params={"library_id": library["id"]}).json()
        assert page["total"] == 2
        item_id = page["items"][0]["id"]
        assert alice.get(f"{API}/items/{item_id}/original").status_code == 200
        # Shared libraries are read-only through the gallery.
        assert alice.delete(f"{API}/items/{item_id}").status_code == 403
        assert alice.post(f"{API}/libraries/{library['id']}/scan").status_code == 403
    with user_client(app, "carol") as carol:
        assert carol.get(f"{API}/items/{item_id}").status_code == 404

    # Revoking the grant hides the library again.
    revoked = admin_client.put(f"{API}/libraries/{library['id']}/groups", json={"group_ids": []})
    assert revoked.status_code == 200 and revoked.json()["group_ids"] == []
    with user_client(app, "alice") as alice:
        assert alice.get(f"{API}/items/{item_id}").status_code == 404

    # Disabling hides it from administrators' browsing too; removing keeps files.
    assert admin_client.get(f"{API}/items/{item_id}").status_code == 200
    patched = admin_client.patch(f"{API}/libraries/{library['id']}", json={"enabled": False})
    assert patched.json()["enabled"] is False
    assert admin_client.get(f"{API}/items/{item_id}").status_code == 404
    assert admin_client.delete(f"{API}/libraries/{library['id']}").status_code == 204
    assert (folder / "beach.jpg").is_file() and (folder / "sub" / "dune.jpg").is_file()
    assert [e.event for e in _audit_events(app, "media.library")] == [
        "media.library_created",
        "media.library_groups_changed",
        "media.library_updated",
        "media.library_deleted",
    ]


def test_shared_library_validation(app: FastAPI, admin: User, admin_client: TestClient) -> None:
    settings = app.state.settings
    _shared_folder(settings)
    root = str(settings.resolved_file_roots[0])

    def create(**overrides: object) -> Any:
        body = {"name": "Lib", "root": root, "path": "Photos", **overrides}
        return admin_client.post(f"{API}/libraries", json=body)

    assert create(path="../..").status_code == 400
    assert create(path="Missing").status_code == 400
    assert create(root="/etc").status_code == 404
    assert create(name="~sneaky").status_code == 400
    assert create(group_ids=[999]).status_code == 400
    assert create().status_code == 201
    assert create(name="lib").status_code == 409  # case-insensitive
    own = next(lib for lib in admin_client.get(f"{API}/libraries").json() if lib["is_own"])
    assert admin_client.delete(f"{API}/libraries/{own['id']}").status_code == 409
    assert admin_client.patch(f"{API}/libraries/{own['id']}", json={"name": "x"}).status_code == 400
    groups = admin_client.put(f"{API}/libraries/{own['id']}/groups", json={"group_ids": []})
    assert groups.status_code == 400


def test_library_whose_root_left_the_configuration_fails_to_scan(
    app: FastAPI, admin_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = app.state.settings
    _shared_folder(settings)
    created = admin_client.post(
        f"{API}/libraries",
        json={"name": "Lib", "root": str(settings.resolved_file_roots[0]), "path": "Photos"},
    ).json()
    monkeypatch.setattr(settings, "file_roots", [tmp_path / "other"])
    _run_queued_scans(app)
    lib = admin_client.get(f"{API}/libraries/{created['id']}").json()
    assert lib["last_scan_status"] == "failed"
    assert "not available" in lib["last_scan_error"]
    assert lib["scan_state"] == "idle"


# ---------------------------------------------------------------------------
# Serving files
# ---------------------------------------------------------------------------


def test_original_supports_range_and_is_typed_by_allowlist(
    app: FastAPI, admin_client: TestClient
) -> None:
    data = jpeg_bytes()
    item = upload(admin_client, "photo.jpg", data)
    url = f"{API}/items/{item['id']}/original"
    full = admin_client.get(url)
    assert full.status_code == 200 and full.content == data
    assert full.headers["content-type"] == "image/jpeg"
    assert full.headers["x-content-type-options"] == "nosniff"
    assert full.headers["content-disposition"].startswith("inline")
    partial = admin_client.get(url, headers={"Range": "bytes=0-9"})
    assert partial.status_code == 206 and partial.content == data[:10]
    assert not _audit_events(app, "media.item_downloaded")
    download = admin_client.get(url, params={"download": "true"})
    assert download.headers["content-disposition"].startswith("attachment")
    assert len(_audit_events(app, "media.item_downloaded")) == 1


def test_thumbnail_is_rendered_on_demand(admin_client: TestClient) -> None:
    item = upload(admin_client, "photo.png", png_bytes((600, 300)))
    small = admin_client.get(f"{API}/items/{item['id']}/thumbnail")
    assert small.status_code == 200
    assert small.headers["content-type"] == "image/webp"
    assert "private" in small.headers["cache-control"]
    large = admin_client.get(f"{API}/items/{item['id']}/thumbnail", params={"size": "large"})
    assert large.status_code == 200 and large.content != small.content
    assert admin_client.get(f"{API}/items/{item['id']}").json()["thumb_state"] == "ready"
    bad = admin_client.get(f"{API}/items/{item['id']}/thumbnail", params={"size": "huge"})
    assert bad.status_code == 422


def test_original_missing_on_disk_is_404(app: FastAPI, admin_client: TestClient) -> None:
    item = upload(admin_client, "photo.jpg", jpeg_bytes())
    (app.state.settings.resolved_media_dir / "admin" / str(item["rel_path"])).unlink()
    assert admin_client.get(f"{API}/items/{item['id']}/original").status_code == 404


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_search_filters_and_sorting(admin_client: TestClient) -> None:
    upload(admin_client, "beach.jpg", jpeg_bytes(taken="2020:01:01 10:00:00", gps=(1.0, 2.0)))
    upload(
        admin_client,
        "party.jpg",
        jpeg_bytes(color=(1, 2, 3), taken="2022:06:01 10:00:00", make="Apple", model="iPhone"),
    )
    upload(admin_client, "100%_real.png", png_bytes())

    def names(**params: Any) -> list[str]:
        response = admin_client.get(f"{API}/items", params=params)
        assert response.status_code == 200, response.text
        return [item["name"] for item in response.json()["items"]]

    assert names(q="BEACH") == ["beach.jpg"]
    assert names(q="iphone") == ["party.jpg"]
    assert names(q="%") == ["100%_real.png"]  # wildcards are literal
    assert names(camera="canon") == ["beach.jpg"]
    assert names(has_gps="true") == ["beach.jpg"]
    assert names(date_from="2021-01-01T00:00:00", date_to="2023-01-01T00:00:00") == ["party.jpg"]
    assert names(sort="name") == ["100%_real.png", "beach.jpg", "party.jpg"]
    newest = names(sort="taken_desc")
    assert newest.index("party.jpg") < newest.index("beach.jpg")
    assert names(kind="video") == []
    page = admin_client.get(f"{API}/items", params={"limit": 1, "offset": 1}).json()
    assert page["total"] == 3 and len(page["items"]) == 1
    assert admin_client.get(f"{API}/items", params={"kind": "audio"}).status_code == 422


# ---------------------------------------------------------------------------
# Albums
# ---------------------------------------------------------------------------


def test_album_lifecycle(app: FastAPI, admin_client: TestClient) -> None:
    first = upload(admin_client, "a.jpg", jpeg_bytes())
    second = upload(admin_client, "b.jpg", jpeg_bytes(color=(9, 9, 9)))
    album = admin_client.post(f"{API}/albums", json={"name": "Holiday"}).json()
    assert album["item_count"] == 0 and album["cover_item_id"] is None
    assert admin_client.post(f"{API}/albums", json={"name": "holiday"}).status_code == 409

    added = admin_client.post(
        f"{API}/albums/{album['id']}/items", json={"item_ids": [first["id"], second["id"]]}
    )
    assert added.json() == {"changed": 2}
    again = admin_client.post(f"{API}/albums/{album['id']}/items", json={"item_ids": [first["id"]]})
    assert again.json() == {"changed": 0}
    missing = admin_client.post(f"{API}/albums/{album['id']}/items", json={"item_ids": [12345]})
    assert missing.status_code == 404

    detail = admin_client.get(f"{API}/albums/{album['id']}").json()
    assert detail["item_ids"] == [first["id"], second["id"]]
    assert detail["cover_item_id"] == first["id"]
    in_album = admin_client.get(f"{API}/items", params={"album_id": album["id"]}).json()
    assert in_album["total"] == 2

    patched = admin_client.patch(
        f"{API}/albums/{album['id']}",
        json={"name": "Holiday 2024", "cover_item_id": second["id"]},
    ).json()
    assert patched["name"] == "Holiday 2024" and patched["cover_item_id"] == second["id"]
    bad_cover = admin_client.patch(f"{API}/albums/{album['id']}", json={"cover_item_id": 999})
    assert bad_cover.status_code == 400

    removed = admin_client.request(
        "DELETE", f"{API}/albums/{album['id']}/items", json={"item_ids": [second["id"]]}
    )
    assert removed.json() == {"changed": 1}
    assert admin_client.get(f"{API}/albums/{album['id']}").json()["cover_item_id"] is None
    listing = admin_client.get(f"{API}/albums").json()
    assert [(a["name"], a["item_count"]) for a in listing] == [("Holiday 2024", 1)]
    assert admin_client.delete(f"{API}/albums/{album['id']}").status_code == 204
    assert admin_client.get(f"{API}/items/{first['id']}").status_code == 200  # photo kept
    assert [e.event for e in _audit_events(app, "media.album")] == [
        "media.album_created",
        "media.album_items_changed",
        "media.album_items_changed",
        "media.album_updated",
        "media.album_items_changed",
        "media.album_deleted",
    ]


def test_album_hides_items_after_access_is_revoked(
    app: FastAPI, admin: User, admin_client: TestClient
) -> None:
    settings = app.state.settings
    write_jpeg(_shared_folder(settings) / "shared.jpg")
    member = create_user(app, admin, "alice")
    group_id = create_group(app, admin, "family", [member.id])
    library = admin_client.post(
        f"{API}/libraries",
        json={
            "name": "Shared",
            "root": str(settings.resolved_file_roots[0]),
            "path": "Photos",
            "group_ids": [group_id],
        },
    ).json()
    _run_queued_scans(app)
    with user_client(app, "alice") as alice:
        item_id = alice.get(f"{API}/items").json()["items"][0]["id"]
        album = alice.post(f"{API}/albums", json={"name": "Faves"}).json()
        alice.post(f"{API}/albums/{album['id']}/items", json={"item_ids": [item_id]})
    admin_client.put(f"{API}/libraries/{library['id']}/groups", json={"group_ids": []})
    with user_client(app, "alice") as alice:
        detail = alice.get(f"{API}/albums/{album['id']}").json()
        assert detail["item_ids"] == [] and detail["item_count"] == 0
        assert alice.get(f"{API}/albums").json()[0]["item_count"] == 0


# ---------------------------------------------------------------------------
# Mobile sync
# ---------------------------------------------------------------------------


def test_hash_lookup_and_change_feed(app: FastAPI, admin_client: TestClient) -> None:
    cursor = admin_client.get(f"{API}/sync/cursor").json()["cursor"]
    data = jpeg_bytes()
    item = upload(admin_client, "a.jpg", data)
    have = hashlib.sha256(data).hexdigest()
    missing = "f" * 64
    result = admin_client.post(
        f"{API}/sync/lookup", json={"sha256": [have.upper(), missing, have]}
    ).json()
    assert result["present"] == {have: [item["id"]]}
    assert result["missing"] == [missing]
    bad = admin_client.post(f"{API}/sync/lookup", json={"sha256": ["xyz"]})
    assert bad.status_code == 400

    library_id = item["library_id"]
    feed = admin_client.get(
        f"{API}/sync/changes", params={"library_id": library_id, "cursor": cursor}
    ).json()
    assert [(c["kind"], c["rel_path"]) for c in feed["changes"]] == [("created", item["rel_path"])]
    assert feed["changes"][0]["sha256"] == have and not feed["reset"]
    admin_client.delete(f"{API}/items/{item['id']}")
    later = admin_client.get(
        f"{API}/sync/changes", params={"library_id": library_id, "cursor": feed["cursor"]}
    ).json()
    assert [c["kind"] for c in later["changes"]] == ["deleted"]
    bad_cursor = admin_client.get(
        f"{API}/sync/changes", params={"library_id": library_id, "cursor": "nope"}
    )
    assert bad_cursor.status_code == 400


# ---------------------------------------------------------------------------
# Scans and status
# ---------------------------------------------------------------------------


def test_scan_endpoint_and_status(app: FastAPI, admin_client: TestClient) -> None:
    own = admin_client.get(f"{API}/libraries").json()[0]
    write_jpeg(app.state.settings.resolved_media_dir / "admin" / "copied-in.jpg")
    queued = admin_client.post(f"{API}/libraries/{own['id']}/scan")
    assert queued.status_code == 202 and queued.json()["scan_state"] == "queued"
    status = admin_client.get(f"{API}/status").json()
    assert status["queued_scans"] == 1 and status["worker_online"] is False
    _run_queued_scans(app)
    lib = admin_client.get(f"{API}/libraries/{own['id']}").json()
    assert lib["scan_state"] == "idle" and lib["last_scan_status"] == "succeeded"
    assert lib["last_scan_summary"]["added"] == 1
    assert lib["counts"] == {"image": 1, "video": 0, "pending_thumbnails": 1}
    assert lib["next_scan_at"] is not None
    assert len(_audit_events(app, "media.scan_queued")) == 1


def test_info_advertises_the_gallery(client: TestClient) -> None:
    assert "media_gallery" in client.get("/api/v1/info").json()["features"]


def test_personal_library_record_survives_reload(app: FastAPI, admin_client: TestClient) -> None:
    admin_client.get(f"{API}/libraries")
    admin_client.get(f"{API}/libraries")
    with app.state.db.session_factory() as session:
        count = len(session.execute(select(MediaLibrary)).scalars().all())
    assert count == 1
