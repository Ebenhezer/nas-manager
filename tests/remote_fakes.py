"""Test doubles for remote providers (Phase 7)."""

from __future__ import annotations

import hashlib
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


class LocalDirProvider(RemoteProvider):
    """A "remote" backed by a local directory, with call recording.

    ``with_md5`` makes it report content MD5s like S3/Drive do;
    ``fail_uploads`` names paths whose upload raises RemoteError.
    """

    def __init__(
        self,
        base: Path,
        *,
        with_md5: bool = False,
        fail_uploads: set[str] | None = None,
        extra_names: list[str] | None = None,
    ) -> None:
        self.base = base
        self.with_md5 = with_md5
        self.fail_uploads = fail_uploads or set()
        self.extra_names = extra_names or []
        self.calls: list[tuple[str, str]] = []
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def _p(self, path: str) -> Path:
        return self.base / path if path else self.base

    def _raw(self, child: Path) -> RawEntry:
        info = child.lstat()
        is_dir = stat.S_ISDIR(info.st_mode)
        md5 = None
        if self.with_md5 and not is_dir:
            md5 = hashlib.md5(child.read_bytes(), usedforsecurity=False).hexdigest()
        return RawEntry(
            name=child.name,
            is_dir=is_dir,
            size=0 if is_dir else info.st_size,
            mtime=datetime.fromtimestamp(info.st_mtime_ns / 1e9, tz=UTC),
            md5=md5,
        )

    def _list(self, path: str) -> list[RawEntry]:
        directory = self._p(path)
        if not directory.is_dir():
            raise RemoteError("Remote directory not found", details={"path": path})
        entries = [self._raw(child) for child in sorted(directory.iterdir())]
        if not path:
            entries += [RawEntry(name=name, is_dir=False, size=1) for name in self.extra_names]
        return entries

    def stat(self, path: str) -> RemoteEntry | None:
        target = self._p(path)
        if not target.exists():
            return None
        raw = self._raw(target)
        return RemoteEntry(
            path=path, is_dir=raw.is_dir, size=raw.size, mtime=raw.mtime, md5=raw.md5
        )

    def download(self, path: str, out: BinaryIO) -> None:
        self.calls.append(("download", path))
        with self._p(path).open("rb") as handle:
            shutil.copyfileobj(handle, out)

    def upload(self, source: BinaryIO, path: str) -> None:
        self.calls.append(("upload", path))
        if path in self.fail_uploads:
            raise RemoteError("simulated upload failure", details={"path": path})
        temporary = self._p(join(parent_of(path), f"{REMOTE_TMP_PREFIX}{uuid.uuid4().hex}"))
        with temporary.open("wb") as out:
            shutil.copyfileobj(source, out)
        os.replace(temporary, self._p(path))

    def makedirs(self, path: str) -> None:
        self._p(path).mkdir(parents=True, exist_ok=True)

    def move(self, src: str, dst: str) -> None:
        self.calls.append(("move", f"{src} -> {dst}"))
        if self._p(dst).exists():
            raise RemoteError("Move destination exists")
        os.rename(self._p(src), self._p(dst))


class BrokenProvider(LocalDirProvider):
    """Every upload fails (simulates a dead connection mid-run)."""

    def upload(self, source: BinaryIO, path: str) -> None:
        raise RemoteError("connection reset")
