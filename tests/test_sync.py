"""Sync cursors over the file change journal, /info and range downloads (Phase 6)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.db.base import utcnow_naive
from app.models import FileChange, User
from app.services import sync
from tests.conftest import ADMIN_PASSWORD, login


def _root(client: TestClient) -> str:
    roots: list[str] = client.get("/api/v1/files/roots").json()
    return roots[0]


def _changes(client: TestClient, root: str, cursor: str, **params: Any) -> Any:
    response = client.get("/api/v1/sync/changes", params={"root": root, "cursor": cursor, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_every_mutation_is_journaled(
    authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    root = _root(authed_client)
    cursor = authed_client.get("/api/v1/sync/cursor").json()["cursor"]
    assert cursor == "c0"

    authed_client.post(
        "/api/v1/files/mkdir", json={"root": root, "name": "docs"}, headers=csrf_header
    )
    authed_client.post(
        "/api/v1/files/upload",
        data={"root": root, "path": "docs"},
        files={"file": ("a.txt", b"hello", "text/plain")},
        headers=csrf_header,
    )
    authed_client.post(
        "/api/v1/files/upload",
        data={"root": root, "path": "docs", "overwrite": "true"},
        files={"file": ("a.txt", b"hello again", "text/plain")},
        headers=csrf_header,
    )
    authed_client.patch(
        "/api/v1/files/rename",
        json={"root": root, "path": "docs/a.txt", "new_name": "b.txt"},
        headers=csrf_header,
    )
    authed_client.request(
        "DELETE",
        "/api/v1/files",
        json={"root": root, "path": "docs", "recursive": True},
        headers=csrf_header,
    )

    page = _changes(authed_client, root, cursor)
    assert page["reset"] is False
    assert page["has_more"] is False
    summary = [(c["kind"], c["path"], c["old_path"], c["is_dir"]) for c in page["changes"]]
    assert summary == [
        ("created", "docs", None, True),
        ("created", "docs/a.txt", None, False),
        ("modified", "docs/a.txt", None, False),
        ("renamed", "docs/b.txt", "docs/a.txt", False),
        ("deleted", "docs", None, True),
    ]
    assert page["changes"][2]["size_bytes"] == len(b"hello again")
    # Polling again from the returned cursor yields nothing new.
    again = _changes(authed_client, root, page["cursor"])
    assert again["changes"] == []
    assert again["cursor"] == page["cursor"]


def test_pagination_and_head_cursor(authed_client: TestClient, csrf_header: dict[str, str]) -> None:
    root = _root(authed_client)
    for index in range(5):
        authed_client.post(
            "/api/v1/files/mkdir", json={"root": root, "name": f"d{index}"}, headers=csrf_header
        )
    first = _changes(authed_client, root, "c0", limit=2)
    assert [c["path"] for c in first["changes"]] == ["d0", "d1"]
    assert first["has_more"] is True
    second = _changes(authed_client, root, first["cursor"], limit=2)
    third = _changes(authed_client, root, second["cursor"], limit=2)
    assert [c["path"] for c in second["changes"] + third["changes"]] == ["d2", "d3", "d4"]
    assert third["has_more"] is False
    assert third["cursor"] == authed_client.get("/api/v1/sync/cursor").json()["cursor"]


def test_reset_for_stale_future_and_malformed_cursors(
    app: FastAPI, authed_client: TestClient, csrf_header: dict[str, str]
) -> None:
    root = _root(authed_client)
    for name in ("x", "y", "z"):
        authed_client.post(
            "/api/v1/files/mkdir", json={"root": root, "name": name}, headers=csrf_header
        )

    assert _changes(authed_client, root, "c999")["reset"] is True
    assert (
        authed_client.get(
            "/api/v1/sync/changes", params={"root": root, "cursor": "nope"}
        ).status_code
        == 400
    )
    assert (
        authed_client.get(
            "/api/v1/sync/changes", params={"root": "/elsewhere", "cursor": "c0"}
        ).status_code
        == 404
    )

    with app.state.db.session_factory() as session:
        session.execute(update(FileChange).values(created_at=utcnow_naive() - timedelta(days=365)))
        session.commit()
        assert sync.prune(session, app.state.settings) == 2  # the head row is kept
    stale = _changes(authed_client, root, "c1")
    assert stale["reset"] is True
    assert stale["cursor"] == "c3"
    assert _changes(authed_client, root, "c0")["reset"] is True
    assert _changes(authed_client, root, "c2")["reset"] is False  # saw everything pruned


def test_sync_requires_files_read(app: FastAPI, admin_user: User) -> None:
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
        assert client.get("/api/v1/sync/cursor").status_code == 401
        login(client, "viewer1", ADMIN_PASSWORD)
        assert client.get("/api/v1/sync/cursor").status_code == 403


def test_info_is_public_and_harmless(client: TestClient) -> None:
    response = client.get("/api/v1/info")
    assert response.status_code == 200
    body = response.json()
    assert body["api_version"] == "1"
    assert {"api_tokens", "resumable_uploads", "sync_cursors"} <= set(body["features"])
    assert body["auth_schemes"] == ["session", "bearer"]
    assert body["limits"]["resumable_chunk_max_bytes"] == 64 * 1024 * 1024
    assert "data_dir" not in str(body)


def test_range_download_returns_partial_content(authed_client: TestClient) -> None:
    root = _root(authed_client)
    (Path(root) / "big.bin").write_bytes(bytes(range(100)))
    response = authed_client.get(
        "/api/v1/files/content",
        params={"root": root, "path": "big.bin"},
        headers={"Range": "bytes=10-19"},
    )
    assert response.status_code == 206
    assert response.content == bytes(range(10, 20))
    assert response.headers["content-range"] == "bytes 10-19/100"
