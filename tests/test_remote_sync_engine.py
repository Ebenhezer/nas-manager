"""Sync engine: the pure planner and full runs against a local-dir "remote"."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.secrets import CredentialCipher
from app.db.session import Database
from app.models import AuditEvent, FileChange, RemoteConnection, SyncConflict, SyncJob, SyncRun
from app.models.remote import (
    CONFLICT_KEEP_BOTH,
    CONFLICT_PREFER_LOCAL,
    CONFLICT_PREFER_REMOTE,
    DELETE_ARCHIVE,
    DELETE_IGNORE,
    DIRECTION_BIDIRECTIONAL,
    DIRECTION_PULL,
    DIRECTION_PUSH,
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_PARTIAL,
    RUN_RUNNING,
    RUN_SUCCEEDED,
)
from app.services import remote_sync
from app.services.remote.base import ARCHIVE_DIR_NAME, RemoteEntry
from app.services.sync_engine import (
    OP_ARCHIVE_LOCAL,
    OP_ARCHIVE_REMOTE,
    OP_DOWNLOAD,
    OP_FORGET,
    OP_RESOLVE,
    OP_SKIP,
    OP_UPLOAD,
    Baseline,
    DeletionGuardError,
    LocalFile,
    check_deletion_guard,
    conflict_name,
    is_excluded,
    plan,
    scan_local,
)
from tests.remote_fakes import BrokenProvider, LocalDirProvider

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 1, 2, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Planner (pure)
# ---------------------------------------------------------------------------


def _lf(path: str, size: int = 1, mtime: int = 1) -> LocalFile:
    return LocalFile(path=path, size=size, mtime_ns=mtime)


def _rf(path: str, size: int = 1, mtime: datetime = T0, etag: str | None = None) -> RemoteEntry:
    return RemoteEntry(path=path, is_dir=False, size=size, mtime=mtime, etag=etag)


def _base(
    size: int = 1, mtime: int = 1, rmtime: datetime = T0, etag: str | None = None
) -> Baseline:
    return Baseline(
        local_size=size,
        local_mtime_ns=mtime,
        remote_size=size,
        remote_mtime=rmtime.replace(tzinfo=None),
        remote_etag=etag,
    )


def _ops(
    direction: str,
    local: list[LocalFile],
    remote: list[RemoteEntry],
    baseline: dict[str, Baseline],
    delete_policy: str = DELETE_IGNORE,
) -> list[tuple[str, str]]:
    result = plan(
        {f.path: f for f in local},
        {r.path: r for r in remote},
        baseline,
        direction=direction,
        delete_policy=delete_policy,
    )
    return [(a.op, a.path) for a in result.actions]


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        (DIRECTION_PUSH, [(OP_UPLOAD, "a")]),
        (DIRECTION_PULL, [(OP_DOWNLOAD, "b")]),
        (DIRECTION_BIDIRECTIONAL, [(OP_UPLOAD, "a"), (OP_DOWNLOAD, "b")]),
    ],
)
def test_plan_new_files_follow_direction(direction: str, expected: list[tuple[str, str]]) -> None:
    assert _ops(direction, [_lf("a")], [_rf("b")], {}) == expected


def test_plan_unchanged_is_noop() -> None:
    assert _ops(DIRECTION_BIDIRECTIONAL, [_lf("a")], [_rf("a")], {"a": _base()}) == []


def test_plan_one_way_never_overwrites_changed_destination() -> None:
    # push: the remote changed, the local did not -> skip, never upload
    assert _ops(DIRECTION_PUSH, [_lf("a")], [_rf("a", mtime=T1)], {"a": _base()}) == [
        (OP_SKIP, "a")
    ]
    # pull: the local changed, the remote did not -> skip, never download
    assert _ops(DIRECTION_PULL, [_lf("a", mtime=9)], [_rf("a")], {"a": _base()}) == [(OP_SKIP, "a")]


def test_plan_changes_copy_in_two_way() -> None:
    assert _ops(DIRECTION_BIDIRECTIONAL, [_lf("a", mtime=9)], [_rf("a")], {"a": _base()}) == [
        (OP_UPLOAD, "a")
    ]
    assert _ops(DIRECTION_BIDIRECTIONAL, [_lf("a")], [_rf("a", size=5)], {"a": _base()}) == [
        (OP_DOWNLOAD, "a")
    ]


def test_plan_etag_wins_over_mtime() -> None:
    # Same etag, different mtime: unchanged. Different etag: changed.
    base = {"a": _base(etag="x")}
    assert _ops(DIRECTION_PULL, [_lf("a")], [_rf("a", mtime=T1, etag="x")], base) == []
    assert _ops(DIRECTION_PULL, [_lf("a")], [_rf("a", etag="y")], base) == [(OP_DOWNLOAD, "a")]


def test_plan_conflicts() -> None:
    both = _ops(DIRECTION_BIDIRECTIONAL, [_lf("a", mtime=9)], [_rf("a", mtime=T1)], {"a": _base()})
    assert both == [(OP_RESOLVE, "a")]
    first = plan(
        {"a": _lf("a")}, {"a": _rf("a")}, {}, direction=DIRECTION_PUSH, delete_policy="ignore"
    )
    assert first.actions[0].op == OP_RESOLVE
    assert first.actions[0].conflict == "first_sync_differs"


def test_plan_deletions_never_delete() -> None:
    base = {"a": _base()}
    # ignore: reported only
    assert _ops(DIRECTION_BIDIRECTIONAL, [], [_rf("a")], base) == [(OP_SKIP, "a")]
    assert _ops(DIRECTION_BIDIRECTIONAL, [_lf("a")], [], base) == [(OP_SKIP, "a")]
    # archive: moved on the other side, and only in the allowed direction
    assert _ops(DIRECTION_PUSH, [], [_rf("a")], base, DELETE_ARCHIVE) == [(OP_ARCHIVE_REMOTE, "a")]
    assert _ops(DIRECTION_PULL, [_lf("a")], [], base, DELETE_ARCHIVE) == [(OP_ARCHIVE_LOCAL, "a")]
    assert _ops(DIRECTION_PULL, [], [_rf("a")], base, DELETE_ARCHIVE) == [(OP_SKIP, "a")]
    # gone on both sides: forget the baseline
    assert _ops(DIRECTION_BIDIRECTIONAL, [], [], base) == [(OP_FORGET, "a")]


def test_plan_modified_beats_deleted() -> None:
    base = {"a": _base()}
    result = plan(
        {},
        {"a": _rf("a", size=7)},
        base,
        direction=DIRECTION_BIDIRECTIONAL,
        delete_policy=DELETE_ARCHIVE,
    )
    assert [(a.op, a.conflict) for a in result.actions] == [(OP_DOWNLOAD, "modified_vs_deleted")]
    result = plan(
        {"a": _lf("a", size=7)},
        {},
        base,
        direction=DIRECTION_PUSH,
        delete_policy=DELETE_ARCHIVE,
    )
    assert [(a.op, a.conflict) for a in result.actions] == [(OP_UPLOAD, "modified_vs_deleted")]


def test_deletion_guard() -> None:
    base = {f"f{i}": _base() for i in range(100)}
    small = plan(
        {},
        {"f0": _rf("f0")},
        {"f0": _base()},
        direction=DIRECTION_PUSH,
        delete_policy=DELETE_ARCHIVE,
    )
    check_deletion_guard(small, max_count=500, max_percent=10)  # 1 of 1 is fine (below minimum)
    many = plan(
        {}, {p: _rf(p) for p in base}, base, direction=DIRECTION_PUSH, delete_policy=DELETE_ARCHIVE
    )
    with pytest.raises(DeletionGuardError):
        check_deletion_guard(many, max_count=500, max_percent=10)
    with pytest.raises(DeletionGuardError):
        check_deletion_guard(many, max_count=50, max_percent=100)
    check_deletion_guard(many, max_count=500, max_percent=100)


def test_conflict_name_is_unique() -> None:
    taken = {"d/report (conflict remote 20260101-000000).txt"}
    assert conflict_name("d/report.txt", "remote", "20260101-000000", taken.__contains__) == (
        "d/report (conflict remote 20260101-000000 1).txt"
    )
    assert (
        conflict_name("Makefile", "local", "s", lambda _p: False) == "Makefile (conflict local s)"
    )


def test_is_excluded() -> None:
    assert is_excluded("a/node_modules/x.js", ["node_modules"])
    assert is_excluded("photo.tmp", ["*.tmp"])
    assert not is_excluded("a/b.txt", ["*.tmp"])


def test_scan_local_skips_internal_and_symlinks(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("a")
    (tmp_path / ".nas-upload-1.part").write_text("x")
    (tmp_path / ARCHIVE_DIR_NAME).mkdir()
    (tmp_path / ARCHIVE_DIR_NAME / "old.txt").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "sub" / "a.txt")
    (tmp_path / "linkdir").symlink_to(tmp_path / "sub")
    (tmp_path / "skip.tmp").write_text("x")
    (tmp_path / "bad\\name").write_text("x")
    found, rejected = scan_local(tmp_path, ["*.tmp"])
    assert set(found) == {"sub/a.txt"}
    assert rejected == [("bad\\name", "contains a path separator")]


# ---------------------------------------------------------------------------
# Full runs
# ---------------------------------------------------------------------------


class Env:
    def __init__(self, settings: Settings, db: Database, tmp_path: Path) -> None:
        self.settings = settings
        self.db = db
        self.root = settings.resolved_file_roots[0]
        self.local = self.root / "job"
        self.local.mkdir(parents=True)
        self.remote = tmp_path / "remote"
        self.remote.mkdir()
        self.cipher = CredentialCipher.from_settings(settings)
        with db.session_factory() as session:
            conn = RemoteConnection(name="fake", provider="sftp")
            session.add(conn)
            session.commit()
            self.connection_id = conn.id

    def job(
        self,
        direction: str,
        conflict_policy: str = CONFLICT_KEEP_BOTH,
        delete_policy: str = DELETE_IGNORE,
        excludes: list[str] | None = None,
    ) -> int:
        with self.db.session_factory() as session:
            job = SyncJob(
                name=f"job-{direction}-{conflict_policy}-{delete_policy}",
                connection_id=self.connection_id,
                local_root=str(self.root),
                local_path="job",
                remote_path="",
                direction=direction,
                conflict_policy=conflict_policy,
                delete_policy=delete_policy,
            )
            job.exclude_patterns = excludes or []
            session.add(job)
            session.commit()
            return job.id

    def run(
        self,
        job_id: int,
        *,
        provider: LocalDirProvider | None = None,
        dry_run: bool = False,
        override: bool = False,
        cancel: bool = False,
    ) -> SyncRun:
        with self.db.session_factory() as session:
            run = SyncRun(
                job_id=job_id,
                status=RUN_RUNNING,
                trigger="manual",
                dry_run=dry_run,
                cancel_requested=cancel,
            )
            run.summary = {"override_deletion_guard": override}
            session.add(run)
            session.commit()
            remote_sync.process_run(
                session,
                self.settings,
                run,
                cipher=self.cipher,
                provider=provider or LocalDirProvider(self.remote),
            )
            session.expunge(run)
            return run

    def conflicts(self) -> list[SyncConflict]:
        with self.db.session_factory() as session:
            return list(session.execute(select(SyncConflict)).scalars())

    def events(self, event: str) -> list[AuditEvent]:
        with self.db.session_factory() as session:
            return list(
                session.execute(select(AuditEvent).where(AuditEvent.event == event)).scalars()
            )


@pytest.fixture
def env(migrated_settings: Settings, db: Database, tmp_path: Path) -> Iterator[Env]:
    yield Env(migrated_settings, db, tmp_path)


def write(path: Path, content: str, mtime: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if mtime is not None:
        os.utime(path, ns=(mtime, mtime))


def test_push_uploads_then_is_idempotent(env: Env) -> None:
    write(env.local / "a.txt", "alpha")
    write(env.local / "deep" / "b.txt", "bravo")
    job = env.job(DIRECTION_PUSH)
    run = env.run(job)
    assert run.status == RUN_SUCCEEDED
    assert (run.uploaded, run.errors) == (2, 0)
    assert (env.remote / "deep" / "b.txt").read_text() == "bravo"
    assert not list(env.remote.rglob(".nas-remote-*"))
    again = env.run(job)
    assert (again.status, again.uploaded, again.downloaded) == (RUN_SUCCEEDED, 0, 0)


def test_pull_downloads_atomically_and_journals(env: Env) -> None:
    write(env.remote / "x" / "photo.jpg", "jpeg-bytes")
    job = env.job(DIRECTION_PULL)
    run = env.run(job)
    assert (run.status, run.downloaded) == (RUN_SUCCEEDED, 1)
    assert (env.local / "x" / "photo.jpg").read_text() == "jpeg-bytes"
    assert not list(env.local.rglob(".nas-remote-*"))
    with env.db.session_factory() as session:
        changes = list(session.execute(select(FileChange)).scalars())
    assert [(c.path, c.kind) for c in changes] == [("job/x/photo.jpg", "created")]


def test_two_way_copies_each_side_change(env: Env) -> None:
    write(env.local / "a.txt", "one", mtime=1_000_000_000)
    write(env.remote / "b.txt", "two")
    job = env.job(DIRECTION_BIDIRECTIONAL)
    assert env.run(job).status == RUN_SUCCEEDED
    write(env.local / "a.txt", "one-changed", mtime=2_000_000_000)
    write(env.remote / "b.txt", "two-changed")
    run = env.run(job)
    assert (run.uploaded, run.downloaded, run.conflicts) == (1, 1, 0)
    assert (env.remote / "a.txt").read_text() == "one-changed"
    assert (env.local / "b.txt").read_text() == "two-changed"


def test_push_does_not_overwrite_changed_remote(env: Env) -> None:
    write(env.local / "a.txt", "local")
    job = env.job(DIRECTION_PUSH)
    env.run(job)
    write(env.remote / "a.txt", "edited on the remote")
    run = env.run(job)
    assert (run.status, run.skipped, run.uploaded) == (RUN_SUCCEEDED, 1, 0)
    assert (env.remote / "a.txt").read_text() == "edited on the remote"


def _diverge(env: Env, job: int) -> None:
    write(env.local / "doc.txt", "base", mtime=1_000_000_000)
    assert env.run(job).status == RUN_SUCCEEDED
    write(env.local / "doc.txt", "local edit", mtime=2_000_000_000)
    write(env.remote / "doc.txt", "remote edit!")


def test_keep_both_two_way_keeps_every_version(env: Env) -> None:
    job = env.job(DIRECTION_BIDIRECTIONAL)
    _diverge(env, job)
    run = env.run(job)
    assert (run.status, run.conflicts) == (RUN_SUCCEEDED, 1)
    copies = list(env.local.glob("doc (conflict remote *).txt"))
    assert len(copies) == 1 and copies[0].read_text() == "remote edit!"
    assert (env.local / "doc.txt").read_text() == "local edit"
    assert (env.remote / "doc.txt").read_text() == "local edit"
    conflict = env.conflicts()[0]
    assert (conflict.kind, conflict.resolution) == ("both_modified", "kept_both")
    assert env.events("sync.conflict")
    # The conflict copy reaches the remote on the next run.
    env.run(job)
    assert (env.remote / copies[0].name).read_text() == "remote edit!"


def test_keep_both_push_moves_remote_version_aside(env: Env) -> None:
    job = env.job(DIRECTION_PUSH)
    _diverge(env, job)
    env.run(job)
    copies = list(env.remote.glob("doc (conflict remote *).txt"))
    assert len(copies) == 1 and copies[0].read_text() == "remote edit!"
    assert (env.remote / "doc.txt").read_text() == "local edit"


def test_keep_both_pull_moves_local_version_aside(env: Env) -> None:
    job = env.job(DIRECTION_PULL)
    write(env.remote / "doc.txt", "base")
    env.run(job)
    write(env.local / "doc.txt", "local edit", mtime=2_000_000_000)
    write(env.remote / "doc.txt", "remote edit!")
    env.run(job)
    copies = list(env.local.glob("doc (conflict local *).txt"))
    assert len(copies) == 1 and copies[0].read_text() == "local edit"
    assert (env.local / "doc.txt").read_text() == "remote edit!"


def test_prefer_local_archives_remote_version(env: Env) -> None:
    job = env.job(DIRECTION_BIDIRECTIONAL, conflict_policy=CONFLICT_PREFER_LOCAL)
    _diverge(env, job)
    env.run(job)
    assert (env.remote / "doc.txt").read_text() == "local edit"
    archived = list((env.remote / ARCHIVE_DIR_NAME).rglob("doc.txt"))
    assert len(archived) == 1 and archived[0].read_text() == "remote edit!"


def test_prefer_remote_archives_local_version(env: Env) -> None:
    job = env.job(DIRECTION_BIDIRECTIONAL, conflict_policy=CONFLICT_PREFER_REMOTE)
    _diverge(env, job)
    env.run(job)
    assert (env.local / "doc.txt").read_text() == "remote edit!"
    archived = list((env.local / ARCHIVE_DIR_NAME).rglob("doc.txt"))
    assert len(archived) == 1 and archived[0].read_text() == "local edit"
    assert env.conflicts()[0].resolution == "remote_won_local_archived"


@pytest.mark.parametrize("with_md5", [False, True])
def test_first_sync_identical_files_are_adopted(env: Env, with_md5: bool) -> None:
    write(env.local / "same.txt", "identical")
    write(env.remote / "same.txt", "identical")
    provider = LocalDirProvider(env.remote, with_md5=with_md5)
    run = env.run(env.job(DIRECTION_BIDIRECTIONAL), provider=provider)
    assert (run.status, run.conflicts, run.uploaded, run.downloaded) == (RUN_SUCCEEDED, 0, 0, 0)
    assert ("download", "same.txt") in provider.calls if not with_md5 else provider.calls == []
    assert not list(env.local.rglob(".nas-remote-*"))


def test_first_sync_different_files_conflict(env: Env) -> None:
    write(env.local / "same.txt", "version A")
    write(env.remote / "same.txt", "version B")
    run = env.run(env.job(DIRECTION_BIDIRECTIONAL))
    assert run.conflicts == 1
    assert env.conflicts()[0].kind == "first_sync_differs"
    assert len(list(env.local.glob("same (conflict remote *).txt"))) == 1


def test_deletions_ignored_by_default(env: Env) -> None:
    write(env.local / "a.txt", "a")
    job = env.job(DIRECTION_BIDIRECTIONAL)
    env.run(job)
    (env.local / "a.txt").unlink()
    for _ in range(2):  # reported on every run, never propagated or restored
        run = env.run(job)
        assert (run.status, run.skipped, run.archived) == (RUN_SUCCEEDED, 1, 0)
        assert (env.remote / "a.txt").exists()
        assert not (env.local / "a.txt").exists()


def test_archive_policy_push_moves_remote_copy(env: Env) -> None:
    write(env.local / "a.txt", "a")
    job = env.job(DIRECTION_PUSH, delete_policy=DELETE_ARCHIVE)
    env.run(job)
    (env.local / "a.txt").unlink()
    run = env.run(job)
    assert run.archived == 1
    assert not (env.remote / "a.txt").exists()
    assert [p.read_text() for p in (env.remote / ARCHIVE_DIR_NAME).rglob("a.txt")] == ["a"]
    assert env.events("sync.archived")
    # The baseline is gone, so nothing is planned for it any more.
    assert env.run(job).archived == 0


def test_archive_policy_pull_moves_local_copy_and_journals(env: Env) -> None:
    write(env.remote / "a.txt", "a")
    job = env.job(DIRECTION_PULL, delete_policy=DELETE_ARCHIVE)
    env.run(job)
    (env.remote / "a.txt").unlink()
    env.run(job)
    assert not (env.local / "a.txt").exists()
    assert list((env.local / ARCHIVE_DIR_NAME).rglob("a.txt"))
    with env.db.session_factory() as session:
        kinds = [c.kind for c in session.execute(select(FileChange)).scalars()]
    assert kinds == ["created", "renamed"]


def test_modified_vs_deleted_restores(env: Env) -> None:
    write(env.local / "a.txt", "a")
    job = env.job(DIRECTION_BIDIRECTIONAL, delete_policy=DELETE_ARCHIVE)
    env.run(job)
    (env.local / "a.txt").unlink()
    write(env.remote / "a.txt", "remote edited it")
    run = env.run(job)
    assert (run.downloaded, run.archived, run.conflicts) == (1, 0, 1)
    assert (env.local / "a.txt").read_text() == "remote edited it"


def test_deletion_guard_blocks_mass_archive(env: Env) -> None:
    for i in range(20):
        write(env.local / f"f{i}.txt", str(i))
    job = env.job(DIRECTION_PUSH, delete_policy=DELETE_ARCHIVE)
    env.run(job)
    for i in range(10):
        (env.local / f"f{i}.txt").unlink()
    run = env.run(job)
    assert run.status == RUN_FAILED
    assert "archive" in (run.error or "")
    assert len(list(env.remote.glob("f*.txt"))) == 20  # nothing touched
    assert env.events("sync.deletion_guard_tripped")
    overridden = env.run(job, override=True)
    assert (overridden.status, overridden.archived) == (RUN_SUCCEEDED, 10)


def test_dry_run_changes_nothing(env: Env) -> None:
    write(env.local / "a.txt", "a")
    write(env.remote / "b.txt", "b")
    run = env.run(env.job(DIRECTION_BIDIRECTIONAL), dry_run=True)
    assert (run.status, run.uploaded, run.downloaded) == (RUN_SUCCEEDED, 1, 1)
    assert {a["op"] for a in run.summary["actions"]} == {"upload", "download"}
    assert not (env.remote / "a.txt").exists()
    assert not (env.local / "b.txt").exists()


def test_excludes_apply_to_both_sides(env: Env) -> None:
    write(env.local / "keep.txt", "k")
    write(env.local / "cache" / "x.bin", "x")
    write(env.remote / "junk.tmp", "j")
    run = env.run(env.job(DIRECTION_BIDIRECTIONAL, excludes=["cache", "*.tmp"]))
    assert (run.uploaded, run.downloaded) == (1, 0)
    assert not (env.remote / "cache").exists()


def test_hostile_remote_names_are_rejected(env: Env) -> None:
    provider = LocalDirProvider(env.remote, extra_names=["..", "evil\\name", "nul\x00"])
    run = env.run(env.job(DIRECTION_PULL), provider=provider)
    assert run.status == RUN_SUCCEEDED
    assert run.skipped == 3
    assert len(run.summary["rejected"]) == 3
    assert list(env.local.iterdir()) == []


def test_cancel_stops_before_the_next_file(env: Env) -> None:
    write(env.local / "a.txt", "a")
    run = env.run(env.job(DIRECTION_PUSH), cancel=True)
    assert (run.status, run.uploaded) == (RUN_CANCELLED, 0)


def test_failed_file_is_retried_next_run(env: Env) -> None:
    write(env.local / "a.txt", "a")
    write(env.local / "b.txt", "b")
    job = env.job(DIRECTION_PUSH)
    run = env.run(job, provider=LocalDirProvider(env.remote, fail_uploads={"b.txt"}))
    assert (run.status, run.uploaded, run.errors) == (RUN_PARTIAL, 1, 1)
    assert run.summary["problems"][0]["path"] == "b.txt"
    again = env.run(job)
    assert (again.status, again.uploaded) == (RUN_SUCCEEDED, 1)


def test_dead_connection_fails_the_run(env: Env) -> None:
    for i in range(15):
        write(env.local / f"f{i}.txt", str(i))
    run = env.run(env.job(DIRECTION_PUSH), provider=BrokenProvider(env.remote))
    assert run.status == RUN_FAILED
    assert run.errors == 10
    assert "consecutive" in (run.error or "")


def test_new_remote_folder_is_created_on_first_push(env: Env) -> None:
    write(env.local / "a.txt", "a")
    with env.db.session_factory() as session:
        job = SyncJob(
            name="nested",
            connection_id=env.connection_id,
            local_root=str(env.root),
            local_path="job",
            remote_path="not/there/yet",
            direction=DIRECTION_PUSH,
        )
        session.add(job)
        session.commit()
        job_id = job.id
    run = env.run(job_id)
    assert (run.status, run.uploaded) == (RUN_SUCCEEDED, 1)
    assert (env.remote / "not" / "there" / "yet" / "a.txt").read_text() == "a"
    # Once synced, a vanished remote folder fails instead of looking like a mass deletion.
    (env.remote / "not" / "there" / "yet" / "a.txt").unlink()
    (env.remote / "not" / "there" / "yet").rmdir()
    gone = env.run(job_id)
    assert gone.status == RUN_FAILED
    assert "no longer exists" in (gone.error or "")


def test_missing_local_directory_fails(env: Env) -> None:
    job = env.job(DIRECTION_PUSH)
    env.local.rmdir()
    run = env.run(job)
    assert run.status == RUN_FAILED
    assert env.events("sync_run.failed")
