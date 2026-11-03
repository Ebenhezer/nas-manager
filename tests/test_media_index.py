"""Phase 8: metadata extraction, thumbnails and library scans (service level)."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import MediaChange, MediaItem, MediaLibrary, User
from app.models.media import THUMB_FAILED, THUMB_PENDING, THUMB_READY, THUMB_UNSUPPORTED
from app.services.media import changes, images, indexer, libraries, thumbnails

from .media_helpers import jpeg_bytes, png_bytes, write_jpeg


@pytest.fixture
def session(app: FastAPI) -> Iterator[Session]:
    with app.state.db.session_factory() as db_session:
        yield db_session


@pytest.fixture
def library(session: Session, app: FastAPI, admin_user: User) -> MediaLibrary:
    owner = session.merge(admin_user)
    return libraries.ensure_personal_library(session, app.state.settings, owner)


def _folder(settings: Settings, library: MediaLibrary) -> Path:
    return libraries.library_path(settings, library)


def _items(session: Session, library: MediaLibrary) -> dict[str, MediaItem]:
    rows = session.execute(select(MediaItem).where(MediaItem.library_id == library.id)).scalars()
    return {row.rel_path: row for row in rows}


def _journal(session: Session) -> list[tuple[str, str]]:
    rows = session.execute(select(MediaChange).order_by(MediaChange.id)).scalars()
    return [(row.kind, row.rel_path) for row in rows]


# ---------------------------------------------------------------------------
# images.py
# ---------------------------------------------------------------------------


def test_read_metadata_parses_exif_gps_and_orientation(tmp_path: Path) -> None:
    path = write_jpeg(tmp_path / "a.jpg", size=(60, 40), orientation=6, gps=(-33.925, 18.4166))
    meta = images.read_metadata(path, max_pixels=10**8)
    assert (meta.width, meta.height) == (40, 60)  # rotated by the orientation tag
    assert meta.taken_at == datetime(2021, 7, 4, 12, 30)
    assert (meta.camera_make, meta.camera_model) == ("Canon", "EOS R6")
    assert meta.orientation == 6
    assert meta.gps_lat == pytest.approx(-33.925, abs=1e-4)
    assert meta.gps_lon == pytest.approx(18.4166, abs=1e-4)


def test_read_metadata_without_exif(tmp_path: Path) -> None:
    path = tmp_path / "plain.png"
    path.write_bytes(png_bytes((30, 20)))
    meta = images.read_metadata(path, max_pixels=10**8)
    assert (meta.width, meta.height) == (30, 20)
    assert meta.taken_at is None and meta.camera_make is None and meta.gps_lat is None


def test_read_metadata_ignores_garbage_exif_values(tmp_path: Path) -> None:
    path = write_jpeg(tmp_path / "odd.jpg", taken="not a date", make="\x00\x01", orientation=0)
    meta = images.read_metadata(path, max_pixels=10**8)
    assert meta.taken_at is None
    assert meta.camera_make is None
    assert meta.orientation is None


def test_corrupt_file_raises_image_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0 definitely not a jpeg")
    with pytest.raises(images.ImageError):
        images.read_metadata(path, max_pixels=10**8)
    with pytest.raises(images.ImageError):
        images.make_thumbnails(path, {"small": tmp_path / "s.webp"}, max_pixels=10**8)
    assert not (tmp_path / "s.webp").exists()


def test_pixel_limit_refuses_large_images_before_decoding(tmp_path: Path) -> None:
    path = tmp_path / "big.png"
    Image.new("L", (1200, 1000)).save(path, "PNG")
    with pytest.raises(images.ImageError, match="pixel limit"):
        images.read_metadata(path, max_pixels=1_000_000)
    with pytest.raises(images.ImageError, match="pixel limit"):
        images.make_thumbnails(path, {"small": tmp_path / "s.webp"}, max_pixels=1_000_000)
    # Far above the limit (Pillow's hard error) is refused the same way.
    with pytest.raises(images.ImageError, match="pixel limit"):
        images.read_metadata(path, max_pixels=500_000)


def test_thumbnails_are_upright_webp_and_bounded(tmp_path: Path) -> None:
    path = write_jpeg(tmp_path / "wide.jpg", size=(2000, 1000), orientation=6)
    targets = {"small": tmp_path / "s.webp", "large": tmp_path / "l.webp"}
    images.make_thumbnails(path, targets, max_pixels=10**8)
    with Image.open(targets["small"]) as small, Image.open(targets["large"]) as large:
        assert small.format == large.format == "WEBP"
        assert small.size == (160, 320)  # portrait after applying orientation 6
        assert large.size == (640, 1280)
    assert not list(tmp_path.glob(".tmp-*"))


def test_thumbnails_keep_transparency(tmp_path: Path) -> None:
    path = tmp_path / "alpha.png"
    path.write_bytes(png_bytes((50, 50)))
    images.make_thumbnails(path, {"small": tmp_path / "s.webp"}, max_pixels=10**8)
    with Image.open(tmp_path / "s.webp") as thumb:
        assert thumb.mode == "RGBA"


# ---------------------------------------------------------------------------
# indexer.py
# ---------------------------------------------------------------------------


def test_classify_allowlist() -> None:
    assert indexer.classify("A.JPG") == ("image", "image/jpeg")
    assert indexer.classify("clip.mkv") == ("video", "video/x-matroska")
    assert indexer.classify("vector.svg") is None
    assert indexer.classify("page.html") is None
    assert indexer.classify("noext") is None


def test_scan_adds_updates_and_removes_with_journal(
    session: Session, app: FastAPI, library: MediaLibrary
) -> None:
    settings = app.state.settings
    folder = _folder(settings, library)
    write_jpeg(folder / "2021" / "a.jpg")
    (folder / "b.png").write_bytes(png_bytes())
    (folder / "notes.txt").write_text("not media")

    result = indexer.scan_library(session, settings, library)
    assert (result.added, result.updated, result.removed) == (2, 0, 0)
    items = _items(session, library)
    assert set(items) == {"2021/a.jpg", "b.png"}
    photo = items["2021/a.jpg"]
    assert photo.kind == "image" and photo.mime == "image/jpeg"
    assert photo.taken_at == datetime(2021, 7, 4, 12, 30)
    assert photo.camera_model == "EOS R6"
    assert photo.thumb_state == THUMB_PENDING
    assert photo.sha256 is not None and len(photo.sha256) == 64

    # Unchanged files are not re-read.
    again = indexer.scan_library(session, settings, library)
    assert (again.added, again.updated, again.unchanged) == (0, 0, 2)

    # Modify one file, delete the other.
    (folder / "2021" / "a.jpg").write_bytes(jpeg_bytes(size=(80, 80), taken="2020:01:02 03:04:05"))
    os.utime(folder / "2021" / "a.jpg", ns=(1, 2_000_000_000))
    (folder / "b.png").unlink()
    third = indexer.scan_library(session, settings, library)
    assert (third.added, third.updated, third.removed) == (0, 1, 1)
    items = _items(session, library)
    assert set(items) == {"2021/a.jpg"}
    assert items["2021/a.jpg"].taken_at == datetime(2020, 1, 2, 3, 4, 5)
    assert _journal(session) == [
        ("created", "b.png"),  # a directory's files come before its subdirectories
        ("created", "2021/a.jpg"),
        ("modified", "2021/a.jpg"),
        ("deleted", "b.png"),
    ]


def test_scan_skips_hidden_temp_symlinks_and_archive(
    session: Session, app: FastAPI, library: MediaLibrary, tmp_path: Path
) -> None:
    settings = app.state.settings
    folder = _folder(settings, library)
    write_jpeg(folder / "keep.jpg")
    write_jpeg(folder / ".hidden.jpg")
    write_jpeg(folder / ".nas-upload-123.part.jpg")
    write_jpeg(folder / ".nas-sync-archive" / "job-1" / "old.jpg")
    outside = write_jpeg(tmp_path / "outside" / "secret.jpg")
    (folder / "link.jpg").symlink_to(outside)
    (folder / "linkdir").symlink_to(outside.parent, target_is_directory=True)

    indexer.scan_library(session, settings, library)
    assert set(_items(session, library)) == {"keep.jpg"}


def test_scan_respects_include_videos(
    session: Session, app: FastAPI, library: MediaLibrary
) -> None:
    settings = app.state.settings
    folder = _folder(settings, library)
    (folder / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    indexer.scan_library(session, settings, library)
    assert _items(session, library)["clip.mp4"].kind == "video"
    library.include_videos = False
    session.commit()
    result = indexer.scan_library(session, settings, library)
    assert result.removed == 1
    assert _items(session, library) == {}


def test_corrupt_and_heic_files_are_indexed_without_thumbnails(
    session: Session, app: FastAPI, library: MediaLibrary
) -> None:
    settings = app.state.settings
    folder = _folder(settings, library)
    (folder / "broken.jpg").write_bytes(b"garbage")
    (folder / "phone.heic").write_bytes(b"\x00\x00\x00\x18ftypheic")
    indexer.scan_library(session, settings, library)
    items = _items(session, library)
    assert items["broken.jpg"].thumb_state == THUMB_FAILED
    assert items["broken.jpg"].thumb_error
    assert items["phone.heic"].thumb_state == THUMB_UNSUPPORTED


def test_missing_library_folder_fails_without_dropping_items(
    session: Session, app: FastAPI, library: MediaLibrary
) -> None:
    settings = app.state.settings
    folder = _folder(settings, library)
    write_jpeg(folder / "a.jpg")
    indexer.scan_library(session, settings, library)
    (folder / "a.jpg").unlink()
    folder.rmdir()
    with pytest.raises(indexer.ScanError, match="missing"):
        indexer.scan_library(session, settings, library)
    assert set(_items(session, library)) == {"a.jpg"}


def test_empty_folder_guard_keeps_a_large_index(
    session: Session, app: FastAPI, library: MediaLibrary
) -> None:
    settings = app.state.settings
    folder = _folder(settings, library)
    for index in range(12):
        write_jpeg(folder / f"p{index}.jpg", color=(index, 0, 0))
    indexer.scan_library(session, settings, library)
    for path in folder.glob("*.jpg"):
        path.unlink()
    with pytest.raises(indexer.ScanError, match="is the disk mounted"):
        indexer.scan_library(session, settings, library)
    assert len(_items(session, library)) == 12


def test_stopped_scan_does_not_remove_items(
    session: Session, app: FastAPI, library: MediaLibrary
) -> None:
    settings = app.state.settings
    folder = _folder(settings, library)
    write_jpeg(folder / "a.jpg")
    indexer.scan_library(session, settings, library)
    stop = threading.Event()
    stop.set()
    (folder / "a.jpg").unlink()
    write_jpeg(folder / "b.jpg")
    result = indexer.scan_library(session, settings, library, stop=stop)
    assert result.complete is False and result.removed == 0
    assert "a.jpg" in _items(session, library)


def test_unreadable_directory_makes_the_scan_partial(
    session: Session, app: FastAPI, library: MediaLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = app.state.settings
    folder = _folder(settings, library)
    write_jpeg(folder / "locked" / "a.jpg")
    indexer.scan_library(session, settings, library)
    real_walk = os.walk

    def walk_with_unreadable_dir(top: Path, **kwargs: object) -> object:
        on_error = kwargs["onerror"]
        assert callable(on_error)
        on_error(PermissionError(13, "Permission denied", str(folder / "locked")))
        return ((d, n, f) for d, n, f in real_walk(top, followlinks=False) if "locked" not in d)

    monkeypatch.setattr(os, "walk", walk_with_unreadable_dir)
    result = indexer.scan_library(session, settings, library)
    assert result.complete is False and result.removed == 0
    assert "locked/a.jpg" in _items(session, library)


# ---------------------------------------------------------------------------
# thumbnails.py and changes.py
# ---------------------------------------------------------------------------


def test_thumbnail_cache_regenerates_after_change_and_prunes_orphans(
    session: Session, app: FastAPI, library: MediaLibrary
) -> None:
    settings = app.state.settings
    folder = _folder(settings, library)
    write_jpeg(folder / "a.jpg")
    indexer.scan_library(session, settings, library)
    item = _items(session, library)["a.jpg"]

    assert thumbnails.process_pending(session, settings, limit=10) == 1
    assert item.thumb_state == THUMB_READY
    first = thumbnails.thumb_path(settings, item, "small")
    assert first.is_file()

    (folder / "a.jpg").write_bytes(jpeg_bytes(size=(90, 90)))
    os.utime(folder / "a.jpg", ns=(1, 3_000_000_000))
    indexer.scan_library(session, settings, library)
    assert not first.exists()  # the stale thumbnail went with the change
    path = thumbnails.ensure_thumbnail(session, settings, item, "large")
    assert path is not None and path.is_file() and path != first

    orphan = thumbnails.thumb_dir(settings, 99_999) / "99999-abcdef123456-small.webp"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"x")
    stale_tmp = path.parent / ".tmp-old.webp"
    stale_tmp.write_bytes(b"x")
    os.utime(stale_tmp, (1, 1))
    fresh_tmp = path.parent / ".tmp-new.webp"
    fresh_tmp.write_bytes(b"x")
    assert thumbnails.prune_orphans(session, settings) == 2
    assert path.is_file() and fresh_tmp.is_file()
    assert not orphan.exists() and not stale_tmp.exists()


def test_failed_thumbnail_is_not_retried_on_every_request(
    session: Session, app: FastAPI, library: MediaLibrary
) -> None:
    settings = app.state.settings
    folder = _folder(settings, library)
    (folder / "broken.png").write_bytes(b"not a png")
    indexer.scan_library(session, settings, library)
    item = _items(session, library)["broken.png"]
    assert thumbnails.ensure_thumbnail(session, settings, item, "small") is None


def test_change_journal_cursor_semantics(
    session: Session, app: FastAPI, library: MediaLibrary
) -> None:
    settings = app.state.settings
    folder = _folder(settings, library)
    start = changes.current_cursor(session)
    write_jpeg(folder / "a.jpg")
    write_jpeg(folder / "b.jpg", color=(1, 2, 3))
    indexer.scan_library(session, settings, library)

    page = changes.changes_since(session, library_id=library.id, cursor=start, limit=1)
    assert page.has_more and not page.reset and [c.rel_path for c in page.changes] == ["a.jpg"]
    rest = changes.changes_since(session, library_id=library.id, cursor=page.cursor, limit=10)
    assert [c.rel_path for c in rest.changes] == ["b.jpg"] and not rest.has_more

    future = changes.changes_since(session, library_id=library.id, cursor="m999999", limit=10)
    assert future.reset
    with pytest.raises(Exception, match="Invalid media sync cursor"):
        changes.changes_since(session, library_id=library.id, cursor="c1", limit=10)

    # Pruned history: a cursor older than the journal must re-list.
    for row in session.execute(select(MediaChange)).scalars():
        row.created_at = datetime(2000, 1, 1)
    session.commit()
    write_jpeg(folder / "c.jpg", color=(9, 9, 9))
    indexer.scan_library(session, settings, library)
    assert changes.prune(session, settings) == 2
    stale = changes.changes_since(session, library_id=library.id, cursor="m1", limit=10)
    assert stale.reset

    found = changes.lookup_hashes(
        session,
        library_id=library.id,
        hashes=[_items(session, library)["a.jpg"].sha256 or "", "0" * 64],
    )
    assert list(found.values()) == [[_items(session, library)["a.jpg"].id]]
