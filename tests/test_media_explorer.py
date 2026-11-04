"""Phase 15: explorer-style gallery - folders, moves, renames, favorites, bulk actions."""

from __future__ import annotations

import io
import os
import zipfile
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AuditEvent, MediaLibrary, User
from app.services.media import jobs

from .media_helpers import create_group, create_user, jpeg_bytes, png_bytes, upload, user_client

API = "/api/v1/media"


@pytest.fixture
def admin(app: FastAPI, admin_user: User) -> User:
    return admin_user


@pytest.fixture
def admin_client(app: FastAPI, admin: User) -> Any:
    with user_client(app, "admin") as client:
        yield client


def _names(client: TestClient, **params: Any) -> list[str]:
    response = client.get(f"{API}/items", params=params)
    assert response.status_code == 200, response.text
    return [item["name"] for item in response.json()["items"]]


def _library_dir(app: FastAPI, library_id: int) -> Any:
    with app.state.db.session_factory() as session:
        library = session.get(MediaLibrary, library_id)
        assert library is not None
        return app.state.settings.resolved_media_dir / library.rel_path


def _events(app: FastAPI, prefix: str) -> list[str]:
    with app.state.db.session_factory() as session:
        return list(
            session.execute(
                select(AuditEvent.event)
                .where(AuditEvent.event.like(f"{prefix}%"))
                .order_by(AuditEvent.id)
            )
            .scalars()
            .all()
        )


def _seed(client: TestClient) -> dict[str, dict[str, Any]]:
    return {
        "top": upload(client, "top.jpg", jpeg_bytes(), path="/"),
        "beach": upload(client, "beach.jpg", jpeg_bytes(color=(1, 1, 1)), path="Trips/Beach"),
        "dune": upload(client, "dune.png", png_bytes((30, 60)), path="Trips/Beach/Dunes"),
        "city": upload(client, "city.jpg", jpeg_bytes(color=(2, 2, 2)), path="Trips/City"),
    }


# ---------------------------------------------------------------------------
# Folder listing and folder-scoped search
# ---------------------------------------------------------------------------


def test_folder_listing_merges_index_and_disk(app: FastAPI, admin_client: TestClient) -> None:
    items = _seed(admin_client)
    library_id = items["top"]["library_id"]
    (_library_dir(app, library_id) / "Empty").mkdir()
    (_library_dir(app, library_id) / ".hidden").mkdir()

    top = admin_client.get(f"{API}/libraries/{library_id}/folders").json()
    assert top["path"] == "" and top["writable"] is True
    assert [(f["name"], f["item_count"]) for f in top["folders"]] == [("Empty", 0), ("Trips", 3)]
    trips = admin_client.get(
        f"{API}/libraries/{library_id}/folders", params={"path": "Trips"}
    ).json()
    beach = next(f for f in trips["folders"] if f["name"] == "Beach")
    assert beach["path"] == "Trips/Beach" and beach["item_count"] == 2
    # The newest image is the cover (both are images; the PNG is "taken" now).
    assert beach["cover_item_id"] in {items["beach"]["id"], items["dune"]["id"]}
    bad = admin_client.get(f"{API}/libraries/{library_id}/folders", params={"path": "../x"})
    assert bad.status_code == 400


def test_folder_filter_direct_and_recursive(admin_client: TestClient) -> None:
    items = _seed(admin_client)
    library_id = items["top"]["library_id"]

    def names(**params: Any) -> list[str]:
        return sorted(_names(admin_client, library_id=library_id, **params))

    assert names(folder="") == ["top.jpg"]
    assert names(folder="Trips") == []
    assert names(folder="Trips/Beach") == ["beach.jpg"]
    assert names(folder="Trips/Beach", recursive="true") == ["beach.jpg", "dune.png"]
    assert names(folder="trips/beach") == []  # folder matching is case-sensitive
    no_library = admin_client.get(f"{API}/items", params={"folder": "Trips"})
    assert no_library.status_code == 400


def test_new_filters_and_sort_orders(admin_client: TestClient) -> None:
    upload(admin_client, "wide.jpg", jpeg_bytes(size=(80, 40)))
    upload(admin_client, "tall.jpg", jpeg_bytes(size=(40, 80), color=(3, 3, 3)))
    upload(admin_client, "rotated.jpg", jpeg_bytes(size=(80, 40), color=(4, 4, 4), orientation=6))
    upload(admin_client, "square.png", png_bytes((50, 50)))

    assert sorted(_names(admin_client, orientation="landscape")) == ["wide.jpg"]
    assert sorted(_names(admin_client, orientation="portrait")) == ["rotated.jpg", "tall.jpg"]
    assert _names(admin_client, orientation="square") == ["square.png"]
    by_size = _names(admin_client, sort="size_asc")
    assert by_size == list(reversed(_names(admin_client, sort="size_desc")))
    assert _names(admin_client, sort="name_desc") == [
        "wide.jpg",
        "tall.jpg",
        "square.png",
        "rotated.jpg",
    ]
    smallest = admin_client.get(f"{API}/items", params={"sort": "size_asc"}).json()["items"][0]
    assert _names(admin_client, max_size=smallest["size"]) == [smallest["name"]]
    for sort in ("modified_desc", "modified_asc", "type", "duration_desc", "duration_asc"):
        assert len(_names(admin_client, sort=sort)) == 4
    assert admin_client.get(f"{API}/items", params={"orientation": "x"}).status_code == 422


def test_facets_list_cameras_and_totals(admin_client: TestClient) -> None:
    upload(admin_client, "a.jpg", jpeg_bytes(make="Canon", model="Canon EOS R6"))
    upload(admin_client, "b.jpg", jpeg_bytes(color=(5, 5, 5), make="Apple", model="iPhone 15"))
    upload(admin_client, "c.jpg", jpeg_bytes(color=(6, 6, 6), make="Apple", model="iPhone 15"))
    upload(admin_client, "d.png", png_bytes())

    facets = admin_client.get(f"{API}/facets").json()
    assert facets["total"] == 4 and facets["images"] == 4 and facets["videos"] == 0
    assert facets["total_bytes"] > 0 and facets["first_taken_at"] is not None
    assert facets["cameras"] == [
        {"name": "Apple iPhone 15", "count": 2},
        {"name": "Canon EOS R6", "count": 1},
    ]
    assert sorted(_names(admin_client, camera="Apple iPhone 15")) == ["b.jpg", "c.jpg"]


# ---------------------------------------------------------------------------
# Favorites
# ---------------------------------------------------------------------------


def test_favorites_are_private_and_filterable(app: FastAPI, admin: User) -> None:
    create_user(app, admin, "alice")
    create_user(app, admin, "bob")
    with user_client(app, "alice") as alice:
        first = upload(alice, "a.jpg", jpeg_bytes())
        second = upload(alice, "b.jpg", jpeg_bytes(color=(7, 7, 7)))
        changed = alice.post(
            f"{API}/favorites", json={"item_ids": [first["id"], first["id"]], "favorite": True}
        )
        assert changed.json() == {"changed": 1}
        again = alice.post(f"{API}/favorites", json={"item_ids": [first["id"]]})
        assert again.json() == {"changed": 0}
        assert _names(alice, favorites="true") == ["a.jpg"]
        assert alice.get(f"{API}/items/{first['id']}").json()["is_favorite"] is True
        assert alice.get(f"{API}/items/{second['id']}").json()["is_favorite"] is False
    with user_client(app, "bob") as bob:
        # Items bob cannot browse are skipped, not starred.
        blocked = bob.post(f"{API}/favorites", json={"item_ids": [first["id"]]})
        assert blocked.json() == {"changed": 0}
        assert _names(bob, favorites="true") == []
    with user_client(app, "alice") as alice:
        removed = alice.post(
            f"{API}/favorites", json={"item_ids": [first["id"]], "favorite": False}
        )
        assert removed.json() == {"changed": 1}
        assert _names(alice, favorites="true") == []


# ---------------------------------------------------------------------------
# Folder operations
# ---------------------------------------------------------------------------


def test_folder_create_rename_delete(app: FastAPI, admin_client: TestClient) -> None:
    item = upload(admin_client, "beach.jpg", jpeg_bytes(), path="Trips/Beach")
    library_id = item["library_id"]
    base = _library_dir(app, library_id)
    url = f"{API}/libraries/{library_id}/folders"

    created = admin_client.post(url, json={"path": "Trips/New"})
    assert created.status_code == 201 and created.json()["path"] == "Trips/New"
    assert (base / "Trips" / "New").is_dir()
    assert admin_client.post(url, json={"path": "Trips/New"}).status_code == 409
    assert admin_client.post(url, json={"path": "Trips/.secret"}).status_code == 400

    cursor = admin_client.get(f"{API}/sync/cursor").json()["cursor"]
    renamed = admin_client.patch(url, json={"path": "Trips", "new_name": "Travel"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["path"] == "Travel" and renamed.json()["item_count"] == 1
    moved = admin_client.get(f"{API}/items/{item['id']}").json()
    assert moved["rel_path"] == "Travel/Beach/beach.jpg"
    assert (base / "Travel" / "Beach" / "beach.jpg").is_file() and not (base / "Trips").exists()
    assert admin_client.get(f"{API}/items/{item['id']}/original").status_code == 200
    feed = admin_client.get(
        f"{API}/sync/changes", params={"library_id": library_id, "cursor": cursor}
    ).json()
    assert [(c["kind"], c["rel_path"]) for c in feed["changes"]] == [
        ("deleted", "Trips/Beach/beach.jpg"),
        ("created", "Travel/Beach/beach.jpg"),
    ]
    clash = admin_client.patch(url, json={"path": "Travel/New", "new_name": "Beach"})
    assert clash.status_code == 409

    not_empty = admin_client.delete(url, params={"path": "Travel/Beach"})
    assert not_empty.status_code == 409
    assert admin_client.delete(url, params={"path": "Travel/New"}).status_code == 204
    assert not (base / "Travel" / "New").exists()
    assert admin_client.delete(url, params={"path": "Nope"}).status_code == 404
    assert admin_client.delete(url, params={"path": "/"}).status_code == 400
    assert _events(app, "media.folder") == [
        "media.folder_created",
        "media.folder_renamed",
        "media.folder_deleted",
    ]


def test_move_and_rename_items(app: FastAPI, admin_client: TestClient) -> None:
    first = upload(admin_client, "photo.jpg", jpeg_bytes(), path="A")
    second = upload(admin_client, "photo.jpg", jpeg_bytes(color=(8, 8, 8)), path="B")
    library_id = first["library_id"]
    base = _library_dir(app, library_id)
    album = admin_client.post(f"{API}/albums", json={"name": "Keep"}).json()
    admin_client.post(f"{API}/albums/{album['id']}/items", json={"item_ids": [first["id"]]})

    moved = admin_client.post(
        f"{API}/items/move",
        json={"item_ids": [first["id"]], "library_id": library_id, "folder": "B"},
    )
    assert moved.json() == {"changed": 1}
    after = admin_client.get(f"{API}/items/{first['id']}").json()
    assert after["rel_path"] == "B/photo (1).jpg" and after["name"] == "photo (1).jpg"
    assert (base / "B" / "photo (1).jpg").is_file() and not (base / "A" / "photo.jpg").exists()
    assert (base / "B" / "photo.jpg").is_file()  # the existing file was not overwritten
    detail = admin_client.get(f"{API}/albums/{album['id']}").json()
    assert detail["item_ids"] == [first["id"]]  # albums follow the item
    again = admin_client.post(
        f"{API}/items/move",
        json={"item_ids": [first["id"]], "library_id": library_id, "folder": "B"},
    )
    assert again.json() == {"changed": 0}

    renamed = admin_client.patch(f"{API}/items/{first['id']}", json={"name": "sunset.JPG"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["rel_path"] == "B/sunset.JPG"
    assert (base / "B" / "sunset.JPG").is_file()
    clash = admin_client.patch(f"{API}/items/{first['id']}", json={"name": "photo.jpg"})
    assert clash.status_code == 409
    other_type = admin_client.patch(f"{API}/items/{second['id']}", json={"name": "photo.png"})
    assert other_type.status_code == 400
    bad = admin_client.patch(f"{API}/items/{second['id']}", json={"name": "../x.jpg"})
    assert bad.status_code == 400
    assert _events(app, "media.item_renamed") == ["media.item_renamed"]
    assert _events(app, "media.items_moved") == ["media.items_moved"]


def test_shared_libraries_stay_read_only(app: FastAPI, admin: User) -> None:
    settings = app.state.settings
    folder = settings.resolved_file_roots[0] / "Photos"
    (folder / "sub").mkdir(parents=True)
    (folder / "sub" / "a.jpg").write_bytes(jpeg_bytes())
    member = create_user(app, admin, "alice")
    group_id = create_group(app, admin, "family", [member.id])
    with user_client(app, "admin") as admin_client:
        library = admin_client.post(
            f"{API}/libraries",
            json={
                "name": "Family",
                "root": str(settings.resolved_file_roots[0]),
                "path": "Photos",
                "group_ids": [group_id],
            },
        ).json()
    with app.state.db.session_factory() as session:
        while (row := jobs.claim_scan(session, owner="test", lease_seconds=60)) is not None:
            jobs.run_scan(session, settings, row)
    url = f"{API}/libraries/{library['id']}/folders"
    with user_client(app, "alice") as alice:
        listing = alice.get(url).json()
        assert listing["writable"] is False
        assert [f["name"] for f in listing["folders"]] == ["sub"]
        item = alice.get(f"{API}/items", params={"library_id": library["id"]}).json()["items"][0]
        assert alice.post(url, json={"path": "x"}).status_code == 403
        assert alice.patch(url, json={"path": "sub", "new_name": "y"}).status_code == 403
        assert alice.delete(url, params={"path": "sub"}).status_code == 403
        move = alice.post(
            f"{API}/items/move",
            json={"item_ids": [item["id"]], "library_id": library["id"], "folder": ""},
        )
        assert move.status_code == 403
        assert alice.patch(f"{API}/items/{item['id']}", json={"name": "b.jpg"}).status_code == 403
        bulk = alice.post(f"{API}/items/delete", json={"item_ids": [item["id"]]}).json()
        assert bulk["changed"] == 0 and bulk["failed"][0]["item_id"] == item["id"]
    assert (folder / "sub" / "a.jpg").is_file()
    with user_client(app, "admin") as admin_client:
        # Another user's personal library is not browsable, so not movable into.
        own = upload(admin_client, "mine.jpg", jpeg_bytes())
    with user_client(app, "alice") as alice:
        hidden = alice.post(
            f"{API}/items/move",
            json={"item_ids": [own["id"]], "library_id": own["library_id"], "folder": ""},
        )
        assert hidden.status_code == 404


# ---------------------------------------------------------------------------
# Bulk delete and ZIP download
# ---------------------------------------------------------------------------


def test_bulk_delete_reports_failures(app: FastAPI, admin_client: TestClient) -> None:
    first = upload(admin_client, "a.jpg", jpeg_bytes())
    second = upload(admin_client, "b.jpg", jpeg_bytes(color=(9, 9, 9)))
    result = admin_client.post(
        f"{API}/items/delete", json={"item_ids": [first["id"], 999999, second["id"]]}
    ).json()
    assert result["changed"] == 2
    assert [f["item_id"] for f in result["failed"]] == [999999]
    assert admin_client.get(f"{API}/items").json()["total"] == 0
    assert _events(app, "media.item_deleted") == ["media.item_deleted"] * 2


def test_zip_download(app: FastAPI, admin_client: TestClient) -> None:
    first = upload(admin_client, "same.jpg", jpeg_bytes(), path="A")
    second = upload(admin_client, "same.jpg", jpeg_bytes(color=(4, 4, 4)), path="B")
    response = admin_client.get(f"{API}/items/zip", params={"ids": f"{first['id']},{second['id']}"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == ["same.jpg", "same (1).jpg"]
    assert list((app.state.settings.resolved_media_cache_dir / "zip").iterdir()) == []
    assert _events(app, "media.items_downloaded") == ["media.items_downloaded"]

    assert admin_client.get(f"{API}/items/zip", params={"ids": "1,x"}).status_code == 422
    assert admin_client.get(f"{API}/items/zip", params={"ids": "999999"}).status_code == 404
    object.__setattr__(app.state.settings, "media_zip_max_bytes", 1024 * 1024)
    big = upload(admin_client, "big.jpg", os.urandom(1024 * 1024 + 1))
    too_big = admin_client.get(f"{API}/items/zip", params={"ids": big["id"]})
    assert too_big.status_code == 400
