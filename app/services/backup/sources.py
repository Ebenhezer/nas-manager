"""What a backup job reads (Phase 11).

Every source yields ``SourceFile`` records: a POSIX relative path, size,
modification time, permission bits and an ``open()`` that returns a
readable binary stream. The engine never sees where the data came from.

* ``files``: a folder inside a managed file root (``NAS_FILE_ROOTS``).
* ``media``: the personal media libraries (``NAS_MEDIA_DIR``), never the
  disposable thumbnail and transcode cache.
* ``system``: a consistent online copy of the SQLite database (taken with
  SQLite's backup API and checked with ``PRAGMA integrity_check``), the
  credentials key and the Samba configuration.
* ``remote`` (*pull*): a folder on a remote connection, downloaded file by
  file into the staging directory.

Local walks reuse the sync scanner, so symlinks are never followed and
temporary files are skipped.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.core.errors import BadRequestError, ConfigurationError
from app.services.remote.base import RemoteProvider, join
from app.services.sync_engine import is_excluded, scan_local

SYSTEM_DATABASE = "nas.db"
SYSTEM_KEY = "credentials.key"
SYSTEM_SAMBA = "smb.conf"


@dataclass(frozen=True)
class SourceFile:
    path: str
    size: int
    mtime_ns: int
    mode: int
    open: Callable[[], AbstractContextManager[BinaryIO]]


@dataclass
class SourceScan:
    files: list[SourceFile] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    label: str = ""


def _open_local(path: Path) -> Callable[[], AbstractContextManager[BinaryIO]]:
    @contextlib.contextmanager
    def opener() -> Iterator[BinaryIO]:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            yield handle

    return opener


def scan_directory(base: Path, excludes: list[str], *, label: str) -> SourceScan:
    if not base.is_dir():
        raise BadRequestError(
            "The backup source directory does not exist", details={"path": str(base)}
        )
    found, rejected = scan_local(base, excludes)
    scan = SourceScan(rejected=list(rejected), label=label)
    for rel in sorted(found):
        local = found[rel]
        path = base / rel
        try:
            mode = stat.S_IMODE(os.lstat(path).st_mode)
        except OSError:
            continue
        scan.files.append(
            SourceFile(
                path=rel,
                size=local.size,
                mtime_ns=local.mtime_ns,
                mode=mode,
                open=_open_local(path),
            )
        )
    return scan


def sqlite_path(settings: Settings) -> Path:
    url = make_url(settings.resolved_database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ConfigurationError("System-state backups support only a file-based SQLite database")
    return Path(url.database)


def copy_database(source: Path, destination: Path) -> None:
    """Consistent online copy with SQLite's backup API, then an integrity check."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchone()
        finally:
            dst.close()
    finally:
        src.close()
    if not result or result[0] != "ok":
        raise ConfigurationError(
            "The database copy failed its integrity check", details={"result": str(result)}
        )


def scan_system(settings: Settings, workdir: Path) -> SourceScan:
    """Stage the system state in ``workdir`` (a private directory) and list it."""
    workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    scan = SourceScan(label="system state")
    database = workdir / SYSTEM_DATABASE
    copy_database(sqlite_path(settings), database)
    candidates: list[tuple[str, Path]] = [(SYSTEM_DATABASE, database)]
    key = settings.resolved_credentials_key_file
    if key.is_file():
        candidates.append((SYSTEM_KEY, key))
    samba = settings.samba_config_path
    if samba.is_file():
        if os.access(samba, os.R_OK):
            candidates.append((SYSTEM_SAMBA, samba))
        else:
            scan.rejected.append((SYSTEM_SAMBA, "not readable by the service account"))
    for name, path in candidates:
        info = path.stat()
        scan.files.append(
            SourceFile(
                path=name,
                size=info.st_size,
                mtime_ns=info.st_mtime_ns,
                mode=stat.S_IMODE(info.st_mode),
                open=_open_local(path),
            )
        )
    return scan


def scan_remote(
    provider: RemoteProvider, base: str, excludes: list[str], staging: Path
) -> SourceScan:
    """Walk a remote folder for a pull backup; files download on ``open()``."""
    walked = provider.walk(base)
    scan = SourceScan(rejected=list(walked.rejected), label=f"remote:{base or '/'}")
    for rel in sorted(walked.files):
        entry = walked.files[rel]
        if is_excluded(rel, excludes):
            continue
        mtime_ns = int(entry.mtime.timestamp() * 1_000_000_000) if entry.mtime else 0
        scan.files.append(
            SourceFile(
                path=rel,
                size=entry.size,
                mtime_ns=mtime_ns,
                mode=0o640,
                open=_open_remote(provider, join(base, rel), staging),
            )
        )
    return scan


def _open_remote(
    provider: RemoteProvider, path: str, staging: Path
) -> Callable[[], AbstractContextManager[BinaryIO]]:
    @contextlib.contextmanager
    def opener() -> Iterator[BinaryIO]:
        staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryFile(dir=staging) as handle:
            provider.download(path, handle)
            handle.seek(0)
            yield handle

    return opener
