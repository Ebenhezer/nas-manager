"""Backup engine (Phase 11): backups, dedup, verify, retention, GC and restores.

Runs execute in-process through ``jobs.process_run`` against a local
repository under the default backup root, a ``LocalDirProvider`` "remote"
for pull backups and remote restores, and the real SQLite database for
system-state snapshots.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError, RemoteError
from app.core.secrets import CredentialCipher
from app.db.session import Database
from app.models import (
    AuditEvent,
    BackupBlob,
    BackupJob,
    BackupRepository,
    BackupRun,
    BackupSnapshot,
    RemoteConnection,
    User,
)
from app.models.backup import (
    RUN_BACKUP,
    RUN_PRUNE,
    RUN_RESTORE,
    RUN_VERIFY,
    SOURCE_FILES,
    SOURCE_REMOTE,
    SOURCE_SYSTEM,
    VERIFY_FAILED,
    VERIFY_OK,
)
from app.models.remote import (
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_PARTIAL,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
)
from app.services.backup import engine, jobs
from app.services.backup.repository import (
    Repository,
    WrongPassphraseError,
    new_snapshot_id,
)
from app.services.bootstrap_service import bootstrap_admin
from app.services.remote.local import LocalProvider
from tests.remote_fakes import LocalDirProvider

PASSPHRASE = "backup passphrase one"
T0 = datetime(2026, 1, 1, 12, 0)


class Env:
    def __init__(self, settings: Settings, db: Database, tmp_path: Path) -> None:
        self.settings = settings
        self.db = db
        self.cipher = CredentialCipher.from_settings(settings)
        self.root = settings.resolved_file_roots[0]
        self.source = self.root / "docs"
        self.source.mkdir(parents=True)
        self.backup_root = settings.resolved_backup_local_roots[0]
        self.repo_dir = self.backup_root / "repo"
        self.remote = tmp_path / "remote"
        self.remote.mkdir()
        with db.session_factory() as session:
            admin = bootstrap_admin(session, username="admin", password="x" * 12, settings=settings)
            self.admin_id = admin.id
            conn = RemoteConnection(name="fake", provider="sftp")
            session.add(conn)
            session.commit()
            self.connection_id = conn.id
            repo = jobs.create_repository(
                session,
                settings,
                self.cipher,
                values={
                    "name": "usb",
                    "target_kind": "local",
                    "local_root": str(self.backup_root),
                    "repo_path": "repo",
                    "passphrase": PASSPHRASE,
                },
                actor=admin,
            )
            jobs.initialize_repository(session, settings, self.cipher, repo, actor=admin)
            self.repository_id = repo.id

    def admin(self, session: Session) -> User:
        user = session.get(User, self.admin_id)
        assert user is not None
        return user

    def job(self, source_kind: str = SOURCE_FILES, **values: Any) -> int:
        with self.db.session_factory() as session:
            defaults: dict[str, Any] = {
                "name": f"job-{source_kind}-{len(values)}",
                "repository_id": self.repository_id,
                "source_kind": source_kind,
                "source_root": str(self.root),
                "source_path": "docs",
                "keep_last": 0,
                "keep_daily": 0,
                "keep_weekly": 0,
                "keep_monthly": 0,
            }
            if source_kind == SOURCE_REMOTE:
                defaults |= {"source_connection_id": self.connection_id, "source_path": ""}
            job = jobs.create_job(
                session,
                self.settings,
                values=defaults | values,
                confirm=True,
                actor=self.admin(session),
            )
            return job.id

    def run(
        self,
        kind: str,
        *,
        job_id: int | None = None,
        params: dict[str, Any] | None = None,
        dry_run: bool = False,
        cancel: bool = False,
        other: LocalDirProvider | None = None,
    ) -> BackupRun:
        with self.db.session_factory() as session:
            run = BackupRun(
                repository_id=self.repository_id,
                job_id=job_id,
                kind=kind,
                status=RUN_RUNNING,
                trigger="manual",
                dry_run=dry_run,
                cancel_requested=cancel,
            )
            run.params = params or {}
            session.add(run)
            session.commit()
            jobs.process_run(session, self.settings, run, cipher=self.cipher, other_provider=other)
            session.refresh(run)
            session.expunge(run)
            return run

    def backup(self, job_id: int, **kwargs: Any) -> BackupRun:
        return self.run(RUN_BACKUP, job_id=job_id, **kwargs)

    def snapshots(self) -> list[BackupSnapshot]:
        with self.db.session_factory() as session:
            rows = session.execute(
                select(BackupSnapshot).order_by(BackupSnapshot.taken_at, BackupSnapshot.id)
            ).scalars()
            return list(rows)

    def repo(self) -> Repository:
        return Repository.open(
            LocalProvider(self.backup_root),
            "repo",
            PASSPHRASE,
            staging=self.settings.resolved_backup_staging_dir / "tmp",
        )

    def blob_files(self) -> list[Path]:
        return sorted(p for p in (self.repo_dir / "blobs").rglob("*") if p.is_file())

    def events(self, event: str) -> list[AuditEvent]:
        with self.db.session_factory() as session:
            return list(
                session.execute(select(AuditEvent).where(AuditEvent.event == event)).scalars()
            )


@pytest.fixture
def env(migrated_settings: Settings, db: Database, tmp_path: Path) -> Iterator[Env]:
    yield Env(migrated_settings, db, tmp_path)


def write(path: Path, content: str | bytes, mtime_ns: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content)
    else:
        path.write_bytes(content)
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))


def tree(base: Path) -> dict[str, bytes]:
    return {
        p.relative_to(base).as_posix(): p.read_bytes()
        for p in sorted(base.rglob("*"))
        if p.is_file() and ".nas-backup-replaced" not in p.parts
    }


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


def test_initialize_creates_encrypted_layout(env: Env) -> None:
    info = json.loads((env.repo_dir / "repo.json").read_text())
    assert info["format"] == "nas-manager-backup"
    assert info["kdf"]["name"] == "scrypt"
    assert PASSPHRASE not in (env.repo_dir / "repo.json").read_text()
    assert (env.repo_dir / "snapshots").is_dir()
    assert (env.repo_dir / "blobs").is_dir()


def test_initialize_adopts_existing_repository_and_rejects_wrong_passphrase(env: Env) -> None:
    provider = LocalProvider(env.backup_root)
    staging = env.settings.resolved_backup_staging_dir
    _, created = Repository.initialize(provider, "repo", PASSPHRASE, staging=staging, scrypt_n=1024)
    assert created is False
    with pytest.raises(WrongPassphraseError):
        Repository.initialize(provider, "repo", "wrong passphrase!", staging=staging, scrypt_n=1024)
    with pytest.raises(WrongPassphraseError):
        Repository.open(provider, "repo", "wrong passphrase!", staging=staging)


def test_initialize_refuses_a_non_empty_foreign_directory(env: Env) -> None:
    write(env.backup_root / "photos" / "keep.jpg", "precious")
    with pytest.raises(ConflictError):
        Repository.initialize(
            LocalProvider(env.backup_root),
            "photos",
            PASSPHRASE,
            staging=env.settings.resolved_backup_staging_dir,
            scrypt_n=1024,
        )
    assert (env.backup_root / "photos" / "keep.jpg").read_text() == "precious"


def test_local_provider_is_jailed_and_needs_the_mount(env: Env, tmp_path: Path) -> None:
    provider = LocalProvider(env.backup_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (env.backup_root / "link").symlink_to(outside)
    with pytest.raises(RemoteError):
        provider.stat("link/x")
    with pytest.raises(RemoteError):
        provider.stat("../outside")
    with pytest.raises(RemoteError, match="mounted"):
        LocalProvider(tmp_path / "unmounted").connect()


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------


def test_backup_then_restore_into_a_new_folder(env: Env) -> None:
    write(env.source / "a.txt", "alpha", mtime_ns=1_700_000_000_000_000_000)
    write(env.source / "sub" / "b.bin", os.urandom(5000))
    os.chmod(env.source / "a.txt", 0o600)
    job_id = env.job()
    run = env.backup(job_id)
    assert run.status == RUN_SUCCEEDED, run.error
    assert (run.files, run.new_blobs, run.reused) == (2, 2, 0)
    [snapshot] = env.snapshots()
    assert snapshot.snapshot_id == run.snapshot_id
    assert snapshot.file_count == 2 and snapshot.total_bytes == 5005

    # Nothing on the target reveals names or content.
    for path in env.repo_dir.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert b"alpha" not in data and b"a.txt" not in data

    restored = env.run(
        RUN_RESTORE,
        params={
            "snapshot_id": snapshot.snapshot_id,
            "paths": [],
            "target": {"kind": "files", "root": str(env.root), "path": "restored"},
            "overwrite": False,
        },
    )
    assert restored.status == RUN_SUCCEEDED, restored.error
    assert tree(env.root / "restored") == tree(env.source)
    info = (env.root / "restored" / "a.txt").stat()
    assert info.st_mtime_ns == 1_700_000_000_000_000_000
    assert info.st_mode & 0o777 == 0o600


def test_second_backup_reuses_unchanged_files_and_dedups_content(env: Env) -> None:
    write(env.source / "a.txt", "same content")
    write(env.source / "b.txt", "same content")  # identical: one blob
    write(env.source / "c.txt", "other")
    job_id = env.job()
    first = env.backup(job_id)
    assert (first.files, first.new_blobs) == (3, 2)
    second = env.backup(job_id)
    assert (second.files, second.reused, second.new_blobs, second.bytes_uploaded) == (3, 3, 0, 0)
    write(env.source / "c.txt", "changed!")
    third = env.backup(job_id)
    assert (third.reused, third.new_blobs) == (2, 1)
    assert len(env.snapshots()) == 3
    assert len(env.blob_files()) == 3


def test_unreadable_file_makes_a_partial_snapshot(env: Env) -> None:
    write(env.source / "ok.txt", "fine")
    write(env.source / "locked.txt", "secret")
    job_id = env.job()
    os.chmod(env.source / "locked.txt", 0)
    try:
        if os.access(env.source / "locked.txt", os.R_OK):
            pytest.skip("running as root: permissions are not enforced")
        run = env.backup(job_id)
    finally:
        os.chmod(env.source / "locked.txt", 0o600)
    assert run.status == RUN_PARTIAL
    assert run.files == 1 and run.errors == 1
    assert run.summary["problems"][0]["path"] == "locked.txt"


def test_cancelled_backup_writes_no_snapshot(env: Env) -> None:
    write(env.source / "a.txt", "alpha")
    run = env.backup(env.job(), cancel=True)
    assert run.status == RUN_CANCELLED
    assert env.snapshots() == []
    assert list((env.repo_dir / "snapshots").iterdir()) == []


def test_missing_source_and_wrong_passphrase_fail_cleanly(env: Env) -> None:
    write(env.source / "a.txt", "alpha")
    job_id = env.job()
    (env.source / "a.txt").unlink()
    env.source.rmdir()
    run = env.backup(job_id)
    assert run.status == RUN_FAILED and "does not exist" in (run.error or "")

    env.source.mkdir()
    with env.db.session_factory() as session:
        repo = session.get(BackupRepository, env.repository_id)
        assert repo is not None
        repo.passphrase_encrypted = env.cipher.encrypt_json({"passphrase": "not the right one"})
        session.commit()
    run = env.backup(job_id)
    assert run.status == RUN_FAILED and "passphrase" in (run.error or "")
    assert env.events("backup_run.failed")


def test_unmounted_target_fails_instead_of_writing_elsewhere(env: Env) -> None:
    write(env.source / "a.txt", "alpha")
    job_id = env.job()
    os.rename(env.repo_dir, env.backup_root / "moved-away")
    run = env.backup(job_id)
    assert run.status == RUN_FAILED
    assert "No backup repository" in (run.error or "")
    assert not env.repo_dir.exists()


def test_pull_backup_from_a_remote(env: Env) -> None:
    write(env.remote / "server" / "etc.conf", "remote config")
    write(env.remote / "server" / "data" / "x.csv", "1,2,3")
    job_id = env.job(SOURCE_REMOTE, source_path="server")
    run = env.backup(job_id, other=LocalDirProvider(env.remote))
    assert run.status == RUN_SUCCEEDED, run.error
    assert run.files == 2
    with env.db.session_factory() as session:
        job = session.get(BackupJob, job_id)
        assert job is not None and job.direction == "pull"
    again = env.backup(job_id, other=LocalDirProvider(env.remote))
    assert again.reused == 2


def test_pull_backup_needs_a_local_repository(env: Env) -> None:
    with env.db.session_factory() as session:
        repo = BackupRepository(
            name="cloud",
            target_kind="remote",
            connection_id=env.connection_id,
            repo_path="r",
            passphrase_encrypted=env.cipher.encrypt_json({"passphrase": PASSPHRASE}),
        )
        session.add(repo)
        session.commit()
        from app.core.errors import BadRequestError

        with pytest.raises(BadRequestError, match="local repository"):
            jobs.create_job(
                session,
                env.settings,
                values={
                    "name": "pull-cloud",
                    "repository_id": repo.id,
                    "source_kind": SOURCE_REMOTE,
                    "source_connection_id": env.connection_id,
                },
                confirm=True,
                actor=env.admin(session),
            )


def test_system_state_backup_and_staged_restore(env: Env) -> None:
    run = env.backup(env.job(SOURCE_SYSTEM))
    assert run.status == RUN_SUCCEEDED, run.error
    [snapshot] = env.snapshots()
    staged = env.run(
        RUN_RESTORE,
        params={
            "snapshot_id": snapshot.snapshot_id,
            "paths": [],
            "target": {"kind": "staging"},
            "overwrite": False,
        },
    )
    assert staged.status == RUN_SUCCEEDED, staged.error
    base = engine.staging_restore_dir(env.settings, staged.id)
    marker = json.loads((base / engine.RESTORE_MARKER).read_text())
    assert set(marker["files"]) >= {"nas.db", "credentials.key"}
    assert marker["snapshot_id"] == snapshot.snapshot_id
    connection = sqlite3.connect(base / "nas.db")
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        users = connection.execute("SELECT username FROM users").fetchall()
    finally:
        connection.close()
    assert ("admin",) in users
    key = env.settings.resolved_credentials_key_file.read_bytes()
    assert (base / "credentials.key").read_bytes() == key


# ---------------------------------------------------------------------------
# Restore safety
# ---------------------------------------------------------------------------


def _restore(env: Env, snapshot_id: str, **params: Any) -> BackupRun:
    base: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "paths": [],
        "target": {"kind": "files", "root": str(env.root), "path": "docs"},
        "overwrite": False,
    }
    dry_run = bool(params.pop("dry_run", False))
    return env.run(RUN_RESTORE, params=base | params, dry_run=dry_run)


def test_restore_in_place_skips_existing_files_unless_overwrite(env: Env) -> None:
    write(env.source / "a.txt", "version 1")
    write(env.source / "b.txt", "bee")
    env.backup(env.job())
    [snapshot] = env.snapshots()
    write(env.source / "a.txt", "version 2")
    (env.source / "b.txt").unlink()

    skipped = _restore(env, snapshot.snapshot_id)
    assert (skipped.files, skipped.skipped) == (1, 1)
    assert (env.source / "a.txt").read_text() == "version 2"
    assert (env.source / "b.txt").read_text() == "bee"

    preview = _restore(env, snapshot.snapshot_id, overwrite=True, dry_run=True)
    assert preview.summary["replaced"] == ["a.txt", "b.txt"]
    assert (env.source / "a.txt").read_text() == "version 2"

    replaced = _restore(env, snapshot.snapshot_id, overwrite=True)
    assert replaced.status == RUN_SUCCEEDED
    assert (env.source / "a.txt").read_text() == "version 1"
    kept = env.source / replaced.summary["replaced_dir"] / "a.txt"
    assert kept.read_text() == "version 2"
    assert env.events("backup.restore_replaced")


def test_restore_selected_paths(env: Env) -> None:
    write(env.source / "keep" / "one.txt", "1")
    write(env.source / "keep" / "two.txt", "2")
    write(env.source / "other.txt", "3")
    env.backup(env.job())
    [snapshot] = env.snapshots()
    run = _restore(
        env,
        snapshot.snapshot_id,
        paths=["keep"],
        target={"kind": "files", "root": str(env.root), "path": "out"},
    )
    assert run.files == 2
    assert tree(env.root / "out") == {"keep/one.txt": b"1", "keep/two.txt": b"2"}


def test_restore_refuses_unsafe_manifest_paths(env: Env) -> None:
    repo = env.repo()
    snapshot_id = new_snapshot_id(T0)
    repo.write_snapshot(
        snapshot_id,
        {
            "entries": [
                {"path": "../escape.txt", "size": 1, "sha256": "0" * 64, "blob": "0" * 64},
                {"path": "/abs.txt", "size": 1, "sha256": "0" * 64, "blob": "0" * 64},
            ]
        },
    )
    run = _restore(env, snapshot_id, target={"kind": "files", "root": str(env.root), "path": "out"})
    assert run.status == RUN_PARTIAL and run.errors == 2
    assert not (env.root / "escape.txt").exists()
    assert not (env.root.parent / "escape.txt").exists()


def test_restore_detects_a_tampered_blob(env: Env) -> None:
    write(env.source / "a.txt", "alpha")
    env.backup(env.job())
    [snapshot] = env.snapshots()
    [blob] = env.blob_files()
    data = bytearray(blob.read_bytes())
    data[-5] ^= 0xFF
    blob.write_bytes(bytes(data))
    run = _restore(
        env, snapshot.snapshot_id, target={"kind": "files", "root": str(env.root), "path": "out"}
    )
    assert run.status == RUN_PARTIAL
    assert not (env.root / "out" / "a.txt").exists()


def test_restore_to_a_remote_target(env: Env) -> None:
    write(env.source / "a.txt", "alpha")
    env.backup(env.job())
    [snapshot] = env.snapshots()
    write(env.remote / "dest" / "a.txt", "remote version")
    remote = LocalDirProvider(env.remote)
    params = {
        "snapshot_id": snapshot.snapshot_id,
        "paths": [],
        "target": {"kind": "remote", "connection_id": env.connection_id, "path": "dest"},
    }
    run = env.run(RUN_RESTORE, params=params | {"overwrite": False}, other=remote)
    assert run.skipped == 1
    assert (env.remote / "dest" / "a.txt").read_text() == "remote version"
    run = env.run(RUN_RESTORE, params=params | {"overwrite": True}, other=remote)
    assert run.status == RUN_SUCCEEDED, run.error
    assert (env.remote / "dest" / "a.txt").read_text() == "alpha"
    archived = list((env.remote / "dest" / ".nas-backup-replaced").rglob("a.txt"))
    assert [p.read_text() for p in archived] == ["remote version"]


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def test_verify_detects_corruption_and_missing_blobs(env: Env) -> None:
    write(env.source / "a.txt", "alpha")
    write(env.source / "b.txt", "bravo")
    env.backup(env.job())
    ok = env.run(RUN_VERIFY, params={"full": True})
    assert ok.status == RUN_SUCCEEDED, ok.error
    assert [s.verify_status for s in env.snapshots()] == [VERIFY_OK]

    first, second = env.blob_files()
    data = bytearray(first.read_bytes())
    data[30] ^= 0x01
    first.write_bytes(bytes(data))
    quick = env.run(RUN_VERIFY, params={"full": False})
    assert quick.status == RUN_SUCCEEDED  # presence only
    full = env.run(RUN_VERIFY, params={"full": True})
    assert full.status == RUN_FAILED
    assert "Integrity problems" in (full.error or "")
    [snapshot] = env.snapshots()
    assert snapshot.verify_status == VERIFY_FAILED and "corrupted" in (snapshot.verify_error or "")
    assert env.events("backup.verify_failed")

    second.unlink()
    quick = env.run(RUN_VERIFY, params={"full": False})
    assert quick.status == RUN_FAILED
    with env.db.session_factory() as session:
        repo = session.get(BackupRepository, env.repository_id)
        assert repo is not None and repo.last_verify_status == VERIFY_FAILED
        blobs = session.execute(select(BackupBlob)).scalars().all()
        assert len(blobs) == 1  # the cache was rebuilt from the target


def test_verify_rebuilds_the_catalogue(env: Env) -> None:
    write(env.source / "a.txt", "alpha")
    env.backup(env.job())
    with env.db.session_factory() as session:
        for row in session.execute(select(BackupSnapshot)).scalars():
            session.delete(row)
        session.commit()
    run = env.run(RUN_VERIFY, params={"full": True})
    assert run.status == RUN_SUCCEEDED
    [snapshot] = env.snapshots()
    assert snapshot.job_name is not None and snapshot.file_count == 1


# ---------------------------------------------------------------------------
# Retention and garbage collection
# ---------------------------------------------------------------------------


def _ids(times: list[datetime]) -> list[tuple[str, datetime]]:
    return [(f"s{i}", t) for i, t in enumerate(times)]


def test_select_retained_buckets() -> None:
    daily = [T0 + timedelta(days=d, hours=h) for d in range(10) for h in (0, 6)]
    snapshots = _ids(daily)
    keep = engine.select_retained(
        snapshots, keep_last=2, keep_daily=3, keep_weekly=0, keep_monthly=0
    )
    newest = sorted(snapshots, key=lambda s: s[1], reverse=True)
    # last 2, plus the newest of each of the 3 most recent days (one overlaps)
    expected = {newest[0][0], newest[1][0], newest[2][0], newest[4][0]}
    assert keep == expected

    weekly = engine.select_retained(
        _ids([T0 + timedelta(days=7 * w) for w in range(6)]),
        keep_last=0,
        keep_daily=0,
        keep_weekly=2,
        keep_monthly=0,
    )
    assert weekly == {"s5", "s4"}

    monthly = engine.select_retained(
        _ids([T0 + timedelta(days=30 * m) for m in range(5)]),
        keep_last=1,
        keep_daily=0,
        keep_weekly=0,
        keep_monthly=3,
    )
    assert monthly == {"s4", "s3", "s2"}

    only_newest = engine.select_retained(
        _ids([T0, T0 + timedelta(hours=1)]),
        keep_last=0,
        keep_daily=0,
        keep_weekly=0,
        keep_monthly=0,
    )
    assert only_newest == {"s1"}


def test_backup_applies_retention_and_collects_garbage(env: Env) -> None:
    job_id = env.job(keep_last=2)
    for version in range(4):
        write(env.source / "file.txt", f"version {version}")
        run = env.backup(job_id)
        assert run.status == RUN_SUCCEEDED, run.error
    snapshots = env.snapshots()
    assert len(snapshots) == 2
    assert len(env.blob_files()) == 2  # blobs of versions 0 and 1 are gone
    assert len(list((env.repo_dir / "snapshots").iterdir())) == 2
    assert env.events("backup.snapshot_deleted")


def test_prune_dry_run_changes_nothing(env: Env) -> None:
    job_id = env.job()
    for version in range(3):
        write(env.source / "file.txt", f"version {version}")
        env.backup(job_id)
    with env.db.session_factory() as session:
        job = session.get(BackupJob, job_id)
        assert job is not None
        job.keep_last = 1
        session.commit()
    preview = env.run(RUN_PRUNE, dry_run=True)
    assert preview.status == RUN_SUCCEEDED
    assert len(preview.summary["forget"]) == 2
    assert preview.summary["blobs_unreferenced"] == 2
    assert len(env.snapshots()) == 3 and len(env.blob_files()) == 3

    real = env.run(RUN_PRUNE)
    assert real.status == RUN_SUCCEEDED
    assert len(env.snapshots()) == 1 and len(env.blob_files()) == 1


def test_garbage_collection_refuses_when_a_snapshot_is_unreadable(env: Env) -> None:
    write(env.source / "a.txt", "alpha")
    env.backup(env.job())
    write(env.source / "a.txt", "beta")
    env.backup(env.job(name="second"))
    orphan = env.blob_files()
    # A third, unreferenced blob and a corrupted snapshot file.
    repo = env.repo()
    with repo.temp_file() as handle:
        handle.write(b"junk")
        repo.put_blob("f" * 64, handle)
    snapshot_file = sorted((env.repo_dir / "snapshots").iterdir())[0]
    snapshot_file.write_bytes(b"garbage")
    run = env.run(RUN_PRUNE)
    assert run.status == RUN_FAILED
    assert "Garbage collection refused" in (run.error or "")
    assert set(orphan) <= set(env.blob_files())
    assert (env.repo_dir / "blobs" / "ff" / ("f" * 64)).exists()


def test_snapshots_of_jobs_without_retention_are_never_pruned(env: Env) -> None:
    job_id = env.job()
    for version in range(3):
        write(env.source / "file.txt", f"version {version}")
        env.backup(job_id)
    run = env.run(RUN_PRUNE)
    assert run.summary["forget"] == []
    assert len(env.snapshots()) == 3


# ---------------------------------------------------------------------------
# Queueing and claiming
# ---------------------------------------------------------------------------


def test_claim_runs_one_run_per_repository(env: Env) -> None:
    write(env.source / "a.txt", "alpha")
    job_id = env.job()
    with env.db.session_factory() as session:
        job = session.get(BackupJob, job_id)
        repo = session.get(BackupRepository, env.repository_id)
        assert job is not None and repo is not None
        first = jobs.queue_backup(session, job, actor=env.admin(session))
        with pytest.raises(ConflictError):
            jobs.queue_backup(session, job, actor=env.admin(session))
        verify = jobs.queue_verify(
            session, repo, full=True, snapshot=None, actor=env.admin(session)
        )
        claimed = jobs.claim_next(session, owner="w1", lease_seconds=60)
        assert claimed is not None and claimed.id == first.id
        assert jobs.claim_next(session, owner="w2", lease_seconds=60) is None
        jobs.process_run(session, env.settings, claimed, cipher=env.cipher)
        assert claimed.status == RUN_SUCCEEDED
        nxt = jobs.claim_next(session, owner="w2", lease_seconds=60)
        assert nxt is not None and nxt.id == verify.id


def test_queue_requires_an_initialized_repository(env: Env) -> None:
    with env.db.session_factory() as session:
        repo = session.get(BackupRepository, env.repository_id)
        assert repo is not None
        repo.initialized_at = None
        session.commit()
        job = session.get(BackupJob, env.job())
        assert job is not None
        with pytest.raises(ConflictError, match="Initialize"):
            jobs.queue_backup(session, job, actor=env.admin(session))


def test_scheduled_jobs_are_queued_and_expired_leases_reclaimed(env: Env) -> None:
    job_id = env.job(schedule_minutes=60, enabled=True)
    with env.db.session_factory() as session:
        now = datetime.now().replace(microsecond=0) + timedelta(hours=1)
        queued = jobs.enqueue_due_jobs(session, now)
        assert [r.job_id for r in queued] == [job_id]
        assert queued[0].trigger == "schedule"
        assert jobs.enqueue_due_jobs(session, now) == []  # not due again yet
        run = jobs.claim_next(session, owner="dead", lease_seconds=1)
        assert run is not None
        assert jobs.reclaim_expired(session, now + timedelta(hours=1)) == 1
        session.refresh(run)
        assert run.status == RUN_FAILED


def test_cancel_queued_run(env: Env) -> None:
    with env.db.session_factory() as session:
        job = session.get(BackupJob, env.job())
        assert job is not None
        run = jobs.queue_backup(session, job, actor=env.admin(session))
        assert run.status == RUN_QUEUED
        jobs.cancel_run(session, run, actor=env.admin(session))
        assert run.status == RUN_CANCELLED
        with pytest.raises(ConflictError):
            jobs.cancel_run(session, run, actor=env.admin(session))


# ---------------------------------------------------------------------------
# Recovery CLI
# ---------------------------------------------------------------------------


def _cli(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, argv: list[str], stdin: str = ""
) -> int:
    from app import cli

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr("sys.argv", ["python -m app.cli", *argv])
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return cli.main()


def test_cli_restore_system_installs_the_staged_database(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env.backup(env.job(SOURCE_SYSTEM))
    [snapshot] = env.snapshots()
    staged = env.run(
        RUN_RESTORE,
        params={
            "snapshot_id": snapshot.snapshot_id,
            "paths": [],
            "target": {"kind": "staging"},
            "overwrite": False,
        },
    )
    with env.db.session_factory() as session:
        jobs.create_repository(
            session,
            env.settings,
            env.cipher,
            values={
                "name": "created-after-the-backup",
                "target_kind": "local",
                "local_root": str(env.backup_root),
                "repo_path": "later",
                "passphrase": PASSPHRASE,
            },
            actor=env.admin(session),
        )
    env.db.dispose()

    assert _cli(monkeypatch, env.settings, ["restore-system", "--run", str(staged.id)]) == 2
    assert "--yes" in capsys.readouterr().err
    assert (
        _cli(monkeypatch, env.settings, ["restore-system", "--run", str(staged.id), "--yes"]) == 0
    )
    out = capsys.readouterr().out
    assert "Installed system snapshot" in out and "before-restore" in out

    database = Database(env.settings.resolved_database_url)
    try:
        with database.session_factory() as session:
            names = set(session.execute(select(BackupRepository.name)).scalars())
            assert names == {"usb"}  # the state at backup time
            restored = session.execute(
                select(AuditEvent).where(AuditEvent.event == "backup.system_restored")
            ).scalar_one()
            assert restored.actor_username == "cli"
    finally:
        database.dispose()
    db_file = env.settings.data_dir / "nas.db"
    assert list(db_file.parent.glob("nas.db.before-restore-*"))

    # A tampered staged file is refused.
    staged_db = engine.staging_restore_dir(env.settings, staged.id) / "nas.db"
    staged_db.write_bytes(b"tampered")
    assert (
        _cli(monkeypatch, env.settings, ["restore-system", "--run", str(staged.id), "--yes"]) == 2
    )


def test_cli_backup_recover_lists_and_restores(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    write(env.source / "a.txt", "alpha")
    write(env.source / "deep" / "b.txt", "bravo")
    env.backup(env.job())
    [snapshot] = env.snapshots()
    repo_dir = str(env.repo_dir)

    assert (
        _cli(
            monkeypatch,
            env.settings,
            ["backup-recover", "--repo-dir", repo_dir, "--passphrase-stdin"],
            "wrong passphrase\n",
        )
        == 2
    )
    assert "passphrase" in capsys.readouterr().err

    assert (
        _cli(
            monkeypatch,
            env.settings,
            ["backup-recover", "--repo-dir", repo_dir, "--passphrase-stdin"],
            PASSPHRASE + "\n",
        )
        == 0
    )
    assert snapshot.snapshot_id in capsys.readouterr().out

    target = tmp_path / "recovered"
    argv = [
        "backup-recover",
        "--repo-dir",
        repo_dir,
        "--snapshot",
        snapshot.snapshot_id,
        "--target",
        str(target),
        "--passphrase-stdin",
    ]
    assert _cli(monkeypatch, env.settings, argv, PASSPHRASE + "\n") == 0
    assert tree(target) == tree(env.source)
    # A non-empty target is refused.
    assert _cli(monkeypatch, env.settings, argv, PASSPHRASE + "\n") == 2
