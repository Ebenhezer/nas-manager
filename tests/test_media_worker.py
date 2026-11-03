"""Phase 8: media work in the worker - scheduled scans, leases, thumbnails, pruning."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.db.base import utcnow_naive
from app.db.session import Database
from app.models import MediaItem, MediaLibrary
from app.models.media import SCAN_IDLE, SCAN_QUEUED, SCAN_RUNNING, THUMB_READY
from app.services.bootstrap_service import bootstrap_admin
from app.services.media import jobs, libraries, thumbnails
from app.services.users_service import create_user
from app.worker.runner import Worker

from .conftest import ADMIN_PASSWORD
from .media_helpers import write_jpeg


@pytest.fixture
def worker(migrated_settings: Settings, db: Database) -> Iterator[Worker]:
    instance = Worker(migrated_settings, db, owner="media-test")
    yield instance
    instance.stop.set()
    instance.pool.shutdown(wait=True)
    instance.media_pool.shutdown(wait=True)


@pytest.fixture
def library_id(migrated_settings: Settings, db: Database) -> int:
    with db.session_factory() as session:
        admin = bootstrap_admin(
            session, username="admin", password=ADMIN_PASSWORD, settings=migrated_settings
        )
        user = create_user(
            session,
            username="alice",
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=["viewer"],
            groups=[],
            settings=migrated_settings,
            actor=admin,
        )
        library = libraries.ensure_personal_library(session, migrated_settings, user)
        return library.id


def _library(db: Database, library_id: int) -> MediaLibrary:
    with db.session_factory() as session:
        library = session.get(MediaLibrary, library_id)
        assert library is not None
        session.expunge(library)
        return library


def _photo(settings: Settings, name: str = "a.jpg") -> None:
    write_jpeg(settings.resolved_media_dir / "alice" / name)


def test_new_library_is_scanned_then_thumbnailed_by_ticks(
    worker: Worker, db: Database, migrated_settings: Settings, library_id: int
) -> None:
    _photo(migrated_settings)
    worker.tick()  # due (never scanned) -> queued -> claimed
    worker.wait_idle()
    library = _library(db, library_id)
    assert library.scan_state == SCAN_IDLE and library.last_scan_status == "succeeded"
    assert library.next_scan_at is not None
    worker.tick()  # nothing to scan: render the pending thumbnail
    worker.wait_idle()
    with db.session_factory() as session:
        item = session.execute(select(MediaItem)).scalar_one()
        assert item.thumb_state == THUMB_READY
        assert thumbnails.thumb_path(migrated_settings, item, "small").is_file()


def test_scans_are_not_scheduled_when_the_interval_is_zero(
    worker: Worker, db: Database, migrated_settings: Settings, library_id: int
) -> None:
    worker.settings = migrated_settings.model_copy(update={"media_scan_interval_minutes": 0})
    worker.tick()
    worker.wait_idle()
    assert _library(db, library_id).scan_state == SCAN_IDLE
    assert _library(db, library_id).last_scan_at is None


def test_next_scan_waits_for_the_interval(
    worker: Worker, db: Database, migrated_settings: Settings, library_id: int
) -> None:
    worker.tick()
    worker.wait_idle()
    first = _library(db, library_id).last_scan_at
    worker.tick()
    worker.wait_idle()
    assert _library(db, library_id).last_scan_at == first
    with db.session_factory() as session:
        row = session.get(MediaLibrary, library_id)
        assert row is not None
        row.next_scan_at = utcnow_naive() - timedelta(seconds=1)
        session.commit()
    worker.tick()
    worker.wait_idle()
    assert _library(db, library_id).last_scan_at != first


def test_claim_is_exclusive(db: Database, library_id: int) -> None:
    with db.session_factory() as session:
        library = session.get(MediaLibrary, library_id)
        assert library is not None
        library.scan_state = SCAN_QUEUED
        session.commit()
        first = jobs.claim_scan(session, owner="one", lease_seconds=60)
        second = jobs.claim_scan(session, owner="two", lease_seconds=60)
    assert first is not None and first.scan_lease_owner == "one"
    assert second is None


def test_expired_lease_is_requeued_and_own_leases_renewed(db: Database, library_id: int) -> None:
    with db.session_factory() as session:
        library = session.get(MediaLibrary, library_id)
        assert library is not None
        library.scan_state = SCAN_RUNNING
        library.scan_lease_owner = "dead-worker"
        library.scan_lease_expires_at = utcnow_naive() - timedelta(minutes=1)
        session.commit()
        jobs.renew_scan_leases(session, owner="someone-else", lease_seconds=60)
        assert jobs.reclaim_scans(session, utcnow_naive()) == 1
        session.refresh(library)
        assert library.scan_state == SCAN_QUEUED and library.scan_lease_owner is None

        library.scan_state = SCAN_RUNNING
        library.scan_lease_owner = "me"
        library.scan_lease_expires_at = utcnow_naive() + timedelta(seconds=5)
        session.commit()
        jobs.renew_scan_leases(session, owner="me", lease_seconds=600)
        session.refresh(library)
        assert library.scan_lease_expires_at is not None
        assert library.scan_lease_expires_at > utcnow_naive() + timedelta(minutes=5)
        assert jobs.reclaim_scans(session, utcnow_naive()) == 0


def test_stopped_scan_goes_back_to_the_queue(
    db: Database, migrated_settings: Settings, library_id: int
) -> None:
    _photo(migrated_settings)
    stop = threading.Event()
    stop.set()
    with db.session_factory() as session:
        library = session.get(MediaLibrary, library_id)
        assert library is not None
        library.scan_state = SCAN_QUEUED
        session.commit()
        claimed = jobs.claim_scan(session, owner="w", lease_seconds=60)
        assert claimed is not None
        jobs.run_scan(session, migrated_settings, claimed, stop=stop)
        session.refresh(claimed)
        assert claimed.scan_state == SCAN_QUEUED and claimed.last_scan_at is None


def test_failed_scan_is_recorded(
    db: Database, migrated_settings: Settings, library_id: int
) -> None:
    folder = migrated_settings.resolved_media_dir / "alice"
    folder.rmdir()
    with db.session_factory() as session:
        library = session.get(MediaLibrary, library_id)
        assert library is not None
        result = jobs.run_scan(session, migrated_settings, library)
        assert result is None
        assert library.last_scan_status == "failed" and "missing" in (library.last_scan_error or "")
        assert library.scan_state == SCAN_IDLE


def test_unexpected_scan_errors_are_contained(
    db: Database, migrated_settings: Settings, library_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("app.services.media.indexer.scan_library", boom)
    with db.session_factory() as session:
        library = session.get(MediaLibrary, library_id)
        assert library is not None
        assert jobs.run_scan(session, migrated_settings, library) is None
        assert library.last_scan_error == "unexpected error: disk on fire"


def test_media_work_does_not_block_sync_slots(
    worker: Worker, db: Database, migrated_settings: Settings, library_id: int
) -> None:
    release = threading.Event()
    started = threading.Event()

    def slow_scan(library_id: int) -> None:
        started.set()
        release.wait(5)

    worker._scan = slow_scan  # type: ignore[method-assign]
    worker.tick()
    assert started.wait(5)
    assert worker.media_future is not None and not worker.media_future.done()
    # A second tick neither starts another media task nor waits for this one.
    worker.tick()
    assert len(worker.running) == 0 and worker.pool._max_workers == 1
    release.set()
    worker.wait_idle()


def test_prune_removes_old_journal_rows(
    worker: Worker, db: Database, migrated_settings: Settings, library_id: int
) -> None:
    _photo(migrated_settings, "a.jpg")
    _photo(migrated_settings, "b.jpg")
    worker.tick()
    worker.wait_idle()
    from app.models import MediaChange

    with db.session_factory() as session:
        for row in session.execute(select(MediaChange)).scalars():
            row.created_at = utcnow_naive() - timedelta(days=400)
        session.commit()
    worker._last_prune = 0.0
    worker.tick()
    worker.wait_idle()
    with db.session_factory() as session:
        remaining = session.execute(select(MediaChange)).scalars().all()
    assert len(remaining) == 1  # the newest row is always kept
