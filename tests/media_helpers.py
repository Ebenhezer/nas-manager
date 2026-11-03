"""Shared helpers for the media (Phase 8/9) tests: real image files and users."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import ExifTags, Image

from app.models import User

from .conftest import ADMIN_PASSWORD, login


def jpeg_bytes(
    size: tuple[int, int] = (64, 48),
    color: tuple[int, int, int] = (200, 30, 30),
    *,
    taken: str | None = "2021:07:04 12:30:00",
    make: str | None = "Canon",
    model: str | None = "EOS R6",
    orientation: int | None = None,
    gps: tuple[float, float] | None = None,
) -> bytes:
    """A real JPEG with the given EXIF fields."""
    image = Image.new("RGB", size, color)
    exif = Image.Exif()
    if make:
        exif[0x010F] = make
    if model:
        exif[0x0110] = model
    if orientation:
        exif[0x0112] = orientation
    if taken:
        exif.get_ifd(ExifTags.IFD.Exif)[0x9003] = taken
    if gps:
        lat, lon = gps
        info = exif.get_ifd(ExifTags.IFD.GPSInfo)
        info[1] = "S" if lat < 0 else "N"
        info[2] = _dms(abs(lat))
        info[3] = "W" if lon < 0 else "E"
        info[4] = _dms(abs(lon))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif)
    return buffer.getvalue()


def _dms(value: float) -> tuple[float, float, float]:
    degrees = int(value)
    minutes = int((value - degrees) * 60)
    seconds = round(((value - degrees) * 60 - minutes) * 60, 4)
    return float(degrees), float(minutes), seconds


def png_bytes(size: tuple[int, int] = (40, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, (0, 128, 255, 128)).save(buffer, "PNG")
    return buffer.getvalue()


def write_jpeg(path: Path, **kwargs: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(jpeg_bytes(**kwargs))  # type: ignore[arg-type]
    return path


def create_user(
    app: FastAPI,
    admin: User,
    username: str,
    *,
    roles: list[str] | None = None,
    groups: list[int] | None = None,
) -> User:
    from app.services.users_service import create_user as create

    with app.state.db.session_factory() as session:
        return create(
            session,
            username=username,
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=roles or ["viewer"],
            groups=groups or [],
            settings=app.state.settings,
            actor=admin,
        )


def create_group(app: FastAPI, admin: User, name: str, member_ids: list[int]) -> int:
    from app.services.groups_service import create_group as create
    from app.services.groups_service import replace_members

    with app.state.db.session_factory() as session:
        actor = session.merge(admin)
        group = create(session, name=name, description=None, actor=actor)
        replace_members(session, group, user_ids=member_ids, actor=actor)
        return group.id


@contextmanager
def user_client(app: FastAPI, username: str) -> Iterator[TestClient]:
    """A logged-in TestClient whose default headers carry the CSRF token."""
    with TestClient(app) as client:
        login(client, username, ADMIN_PASSWORD)
        client.headers["X-CSRF-Token"] = client.cookies.get("nas_csrf", "") or ""
        yield client


def upload(
    client: TestClient,
    name: str,
    data: bytes,
    *,
    path: str | None = None,
    taken_at: str | None = None,
) -> dict[str, Any]:
    form: dict[str, str] = {}
    if path is not None:
        form["path"] = path
    if taken_at is not None:
        form["taken_at"] = taken_at
    response = client.post(
        "/api/v1/media/upload", files={"file": (name, data, "application/octet-stream")}, data=form
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body
