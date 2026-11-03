"""Local directory "provider" for backup repositories on external storage (Phase 11).

Backups to a USB disk, a second internal disk or an NFS mount are written
to a directory under one of ``NAS_BACKUP_LOCAL_ROOTS``. This provider gives
that directory the same interface as the remote providers, so the backup
engine has one code path. Every path is resolved and re-checked against the
base, symlinks are never followed, and writes go through a temporary file,
``fsync`` and an atomic rename.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from app.core.errors import RemoteError
from app.services.remote.base import (
    REMOTE_TMP_PREFIX,
    RawEntry,
    RemoteEntry,
    RemoteProvider,
    join,
    parent_of,
)

_CHUNK = 1024 * 1024


class LocalProvider(RemoteProvider):
    def __init__(self, base: Path) -> None:
        self.base = base.resolve(strict=False)

    def connect(self) -> None:
        if not self.base.is_dir():
            raise RemoteError(
                "The backup directory does not exist (is the disk mounted?)",
                details={"path": str(self.base)},
            )

    def close(self) -> None:
        return None

    def _p(self, path: str) -> Path:
        target = self.base / path if path else self.base
        current = self.base
        for part in Path(path).parts if path else ():
            current = current / part
            if current.is_symlink():
                raise RemoteError("Refusing to follow a symlink", details={"path": path})
        if not target.resolve(strict=False).is_relative_to(self.base):
            raise RemoteError("Path escapes the backup directory", details={"path": path})
        return target

    @staticmethod
    def _raw(child: Path) -> RawEntry | None:
        info = child.lstat()
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            return None
        is_dir = stat.S_ISDIR(info.st_mode)
        return RawEntry(
            name=child.name,
            is_dir=is_dir,
            size=0 if is_dir else info.st_size,
            mtime=datetime.fromtimestamp(info.st_mtime, tz=UTC),
        )

    def _list(self, path: str) -> list[RawEntry]:
        directory = self._p(path)
        try:
            children = sorted(directory.iterdir())
        except OSError as exc:
            raise RemoteError(f"Cannot list directory: {exc}", details={"path": path}) from exc
        return [raw for child in children if (raw := self._raw(child)) is not None]

    def stat(self, path: str) -> RemoteEntry | None:
        target = self._p(path)
        try:
            raw = self._raw(target)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RemoteError(f"Cannot stat: {exc}", details={"path": path}) from exc
        if raw is None:
            return None
        return RemoteEntry(path=path, is_dir=raw.is_dir, size=raw.size, mtime=raw.mtime)

    def download(self, path: str, out: BinaryIO) -> None:
        try:
            fd = os.open(self._p(path), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as handle:
                shutil.copyfileobj(handle, out, _CHUNK)
        except OSError as exc:
            raise RemoteError(f"Read failed: {exc}", details={"path": path}) from exc

    def upload(self, source: BinaryIO, path: str) -> None:
        final = self._p(path)
        temporary = self._p(join(parent_of(path), f"{REMOTE_TMP_PREFIX}{uuid.uuid4().hex}.part"))
        try:
            with temporary.open("xb") as out:
                shutil.copyfileobj(source, out, _CHUNK)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, final)
            _fsync_dir(final.parent)
        except OSError as exc:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise RemoteError(f"Write failed: {exc}", details={"path": path}) from exc

    def makedirs(self, path: str) -> None:
        try:
            self._p(path).mkdir(parents=True, exist_ok=True, mode=0o750)
        except OSError as exc:
            raise RemoteError(f"mkdir failed: {exc}", details={"path": path}) from exc

    def move(self, src: str, dst: str) -> None:
        target = self._p(dst)
        if target.exists() or target.is_symlink():
            raise RemoteError("Move destination exists", details={"path": dst})
        try:
            os.rename(self._p(src), target)
        except OSError as exc:
            raise RemoteError(f"Move failed: {exc}", details={"path": src}) from exc

    def delete(self, path: str) -> None:
        target = self._p(path)
        if target.is_dir():
            raise RemoteError("Refusing to delete a directory", details={"path": path})
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            raise RemoteError(f"Delete failed: {exc}", details={"path": path}) from exc


def _fsync_dir(directory: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
