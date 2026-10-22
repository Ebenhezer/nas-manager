from __future__ import annotations

import os
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import User

from .conftest import ADMIN_PASSWORD


def _root(app: FastAPI) -> str:
    return str(app.state.settings.resolved_file_roots[0])


def _root_path(app: FastAPI) -> Path:
    return cast(Path, app.state.settings.resolved_file_roots[0])


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["nas_csrf"]}


def _upload(client: TestClient, app: FastAPI, name: str, content: bytes, *, path: str = "") -> None:
    client.post(
        "/api/v1/files/upload",
        files={"file": (name, content, "application/octet-stream")},
        data={"root": _root(app), "path": path},
        headers=_csrf(client),
    )


def test_list_empty_root(authed_client: TestClient, app: FastAPI) -> None:
    response = authed_client.get(f"/api/v1/files/list?root={_root(app)}")
    assert response.status_code == 200
    assert response.json()["entries"] == []


def test_list_sorted_dirs_first_with_metadata(authed_client: TestClient, app: FastAPI) -> None:
    root = _root_path(app)
    (root / "docs").mkdir()
    (root / "a.txt").write_bytes(b"hello")
    (root / "b").write_bytes(b"12345")
    response = authed_client.get(f"/api/v1/files/list?root={_root(app)}")
    assert response.status_code == 200
    entries = response.json()["entries"]
    assert [entry["name"] for entry in entries] == ["docs", "a.txt", "b"]
    assert entries[0]["is_dir"] is True
    assert entries[1]["is_dir"] is False
    assert entries[1]["size"] == 5
    assert entries[1]["mode"].startswith("-")
    assert entries[0]["path"] == "docs"


def test_list_subdirectory(authed_client: TestClient, app: FastAPI) -> None:
    root = _root_path(app)
    (root / "docs" / "nested").mkdir(parents=True)
    (root / "docs" / "nested" / "x.txt").write_bytes(b"x")
    response = authed_client.get(f"/api/v1/files/list?root={_root(app)}&path=docs/nested")
    assert response.status_code == 200
    assert response.json()["path"] == "docs/nested"
    assert [entry["name"] for entry in response.json()["entries"]] == ["x.txt"]


def test_upload_download_roundtrip(authed_client: TestClient, app: FastAPI) -> None:
    response = authed_client.post(
        "/api/v1/files/upload",
        files={"file": ("hello.txt", b"hello world", "text/plain")},
        data={"root": _root(app), "path": ""},
        headers=_csrf(authed_client),
    )
    assert response.status_code == 200
    assert response.json()["name"] == "hello.txt"
    assert (_root_path(app) / "hello.txt").read_bytes() == b"hello world"

    download = authed_client.get(f"/api/v1/files/content?root={_root(app)}&path=hello.txt")
    assert download.status_code == 200
    assert download.content == b"hello world"
    assert "attachment" in download.headers["content-disposition"]


def test_upload_into_subdirectory(authed_client: TestClient, app: FastAPI) -> None:
    (_root_path(app) / "docs").mkdir()
    response = authed_client.post(
        "/api/v1/files/upload",
        files={"file": ("inside.txt", b"data", "text/plain")},
        data={"root": _root(app), "path": "docs"},
        headers=_csrf(authed_client),
    )
    assert response.status_code == 200
    assert (_root_path(app) / "docs" / "inside.txt").read_bytes() == b"data"


def test_upload_refuses_overwrite_then_allows_with_flag(
    authed_client: TestClient, app: FastAPI
) -> None:
    _upload(authed_client, app, "dup.txt", b"first")
    response = authed_client.post(
        "/api/v1/files/upload",
        files={"file": ("dup.txt", b"second", "text/plain")},
        data={"root": _root(app), "path": ""},
        headers=_csrf(authed_client),
    )
    assert response.status_code == 409
    assert (_root_path(app) / "dup.txt").read_bytes() == b"first"

    response = authed_client.post(
        "/api/v1/files/upload",
        files={"file": ("dup.txt", b"second", "text/plain")},
        data={"root": _root(app), "path": "", "overwrite": "true"},
        headers=_csrf(authed_client),
    )
    assert response.status_code == 200
    assert (_root_path(app) / "dup.txt").read_bytes() == b"second"


def test_upload_enforces_size_limit_and_leaves_no_temp(
    authed_client: TestClient, app: FastAPI
) -> None:
    app.state.settings.max_upload_bytes = 16
    response = authed_client.post(
        "/api/v1/files/upload",
        files={"file": ("big.bin", b"x" * 64, "application/octet-stream")},
        data={"root": _root(app), "path": ""},
        headers=_csrf(authed_client),
    )
    assert response.status_code == 400
    assert not (_root_path(app) / "big.bin").exists()
    assert not any(name.startswith(".nas-upload-") for name in os.listdir(_root_path(app)))


def test_upload_traversal_filename_rejected(authed_client: TestClient, app: FastAPI) -> None:
    response = authed_client.post(
        "/api/v1/files/upload",
        files={"file": ("../evil.txt", b"x", "text/plain")},
        data={"root": _root(app), "path": ""},
        headers=_csrf(authed_client),
    )
    assert response.status_code == 400


def test_path_traversal_rejected(authed_client: TestClient, app: FastAPI) -> None:
    for path in ["../", "..", "../../etc", "/etc", "..%2fetc", "%2e%2e/etc"]:
        response = authed_client.get(f"/api/v1/files/list?root={_root(app)}&path={path}")
        assert response.status_code == 400, path


def test_symlink_escaping_root_rejected(authed_client: TestClient, app: FastAPI) -> None:
    outside = _root_path(app).parents[1]  # tmp_path, outside the managed root
    (outside / "secret.txt").write_text("top secret")
    os.symlink(outside, _root_path(app) / "escape")
    response = authed_client.get(f"/api/v1/files/list?root={_root(app)}&path=escape")
    assert response.status_code == 400
    response = authed_client.get(f"/api/v1/files/content?root={_root(app)}&path=escape/secret.txt")
    assert response.status_code == 400


def test_unknown_root_rejected(authed_client: TestClient) -> None:
    response = authed_client.get("/api/v1/files/list?root=/does/not/exist")
    assert response.status_code == 404


def test_mkdir_rename_delete_flow(authed_client: TestClient, app: FastAPI) -> None:
    headers = _csrf(authed_client)
    created = authed_client.post(
        "/api/v1/files/mkdir",
        json={"root": _root(app), "path": "", "name": "docs"},
        headers=headers,
    )
    assert created.status_code == 200
    assert created.json()["is_dir"] is True

    renamed = authed_client.patch(
        "/api/v1/files/rename",
        json={"root": _root(app), "path": "docs", "new_name": "docs2"},
        headers=headers,
    )
    assert renamed.status_code == 200
    assert (_root_path(app) / "docs2").is_dir()
    assert not (_root_path(app) / "docs").exists()

    # Empty directories may be deleted without the recursive flag.
    deleted = authed_client.request(
        "DELETE", "/api/v1/files", json={"root": _root(app), "path": "docs2"}, headers=headers
    )
    assert deleted.status_code == 204
    assert not (_root_path(app) / "docs2").exists()

    # Renames must not contain separators (no cross-directory moves).
    (_root_path(app) / "again").mkdir()
    bad = authed_client.patch(
        "/api/v1/files/rename",
        json={"root": _root(app), "path": "again", "new_name": "sub/moved"},
        headers=headers,
    )
    assert bad.status_code == 400


def test_delete_nonempty_directory_requires_recursive(
    authed_client: TestClient, app: FastAPI
) -> None:
    headers = _csrf(authed_client)
    (_root_path(app) / "full").mkdir()
    (_root_path(app) / "full" / "f.txt").write_bytes(b"x")
    refused = authed_client.request(
        "DELETE", "/api/v1/files", json={"root": _root(app), "path": "full"}, headers=headers
    )
    assert refused.status_code == 400
    accepted = authed_client.request(
        "DELETE",
        "/api/v1/files",
        json={"root": _root(app), "path": "full", "recursive": True},
        headers=headers,
    )
    assert accepted.status_code == 204
    assert not (_root_path(app) / "full").exists()


def test_cannot_delete_or_rename_root(authed_client: TestClient, app: FastAPI) -> None:
    headers = _csrf(authed_client)
    deleted = authed_client.request(
        "DELETE", "/api/v1/files", json={"root": _root(app), "path": ""}, headers=headers
    )
    assert deleted.status_code == 400
    renamed = authed_client.patch(
        "/api/v1/files/rename",
        json={"root": _root(app), "path": "", "new_name": "root2"},
        headers=headers,
    )
    assert renamed.status_code == 400


def test_search_is_case_insensitive_and_bounded(authed_client: TestClient, app: FastAPI) -> None:
    root = _root_path(app)
    (root / "report.txt").write_bytes(b"a")
    (root / "docs").mkdir()
    (root / "docs" / "REPORT_2024.csv").write_bytes(b"b")
    (root / "other.txt").write_bytes(b"c")

    response = authed_client.get(f"/api/v1/files/search?root={_root(app)}&q=report")
    assert response.status_code == 200
    names = {entry["name"] for entry in response.json()["matches"]}
    assert names == {"report.txt", "REPORT_2024.csv"}

    limited = authed_client.get(f"/api/v1/files/search?root={_root(app)}&q=report&limit=1")
    assert len(limited.json()["matches"]) == 1

    empty = authed_client.get(f"/api/v1/files/search?root={_root(app)}&q=zzzz")
    assert empty.json()["matches"] == []


def test_operator_locked_out_by_default(app: FastAPI, admin_user: User) -> None:
    from app.services.users_service import create_user

    with app.state.db.session_factory() as session:
        create_user(
            session,
            username="op1",
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=["operator"],
            groups=[],
            settings=app.state.settings,
            actor=admin_user,
        )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login", json={"username": "op1", "password": ADMIN_PASSWORD}
        )
        assert response.status_code == 200
        headers = _csrf(client)
        assert client.get(f"/api/v1/files/list?root={_root(app)}").status_code == 403
        assert client.get("/files", follow_redirects=False).status_code == 401
        assert (
            client.post(
                "/api/v1/files/upload",
                files={"file": ("x.txt", b"x", "text/plain")},
                data={"root": _root(app), "path": ""},
                headers=headers,
            ).status_code
            == 403
        )


def test_anonymous_gets_401(client: TestClient, app: FastAPI) -> None:
    assert client.get(f"/api/v1/files/list?root={_root(app)}").status_code == 401


def test_audit_events_recorded(authed_client: TestClient, app: FastAPI) -> None:
    _upload(authed_client, app, "logged.txt", b"payload")
    authed_client.request(
        "DELETE",
        "/api/v1/files",
        json={"root": _root(app), "path": "logged.txt"},
        headers=_csrf(authed_client),
    )
    page = authed_client.get("/api/v1/audit/events?category=file")
    assert page.status_code == 200
    events = [event["event"] for event in page.json()["events"]]
    assert "file.uploaded" in events
    assert "file.deleted" in events
