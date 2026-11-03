"""Provider-neutral remote store interface (Phase 7).

Every provider exposes the same small set of operations over POSIX-style
paths relative to its configured base (``""`` is the base itself). Names a
remote server reports are untrusted: anything that is not a plain single
path segment (``..``, separators, control characters, NUL) is rejected
before it can be turned into a local path, so a hostile server cannot steer
writes outside the local jail.
"""

from __future__ import annotations

import posixpath
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import BinaryIO, Self

from app.core.errors import BadRequestError, RemoteError

# Never synced; holds archived (instead of deleted) files on both sides.
ARCHIVE_DIR_NAME = ".nas-sync-archive"
# Partial uploads on the remote side, renamed into place when complete.
REMOTE_TMP_PREFIX = ".nas-remote-"
_MAX_NAME_BYTES = 255
_MAX_DEPTH = 64


@dataclass(frozen=True)
class RawEntry:
    """One directory entry as a provider reports it (name not yet trusted)."""

    name: str
    is_dir: bool
    size: int = 0
    mtime: datetime | None = None
    etag: str | None = None
    # Hex MD5 of the content when the provider reports one (S3, Drive).
    md5: str | None = None
    # Why the provider cannot sync this entry (e.g. a duplicate name in
    # Google Drive); such entries are reported as rejected, never synced.
    problem: str | None = None


@dataclass(frozen=True)
class RemoteEntry:
    """A validated entry; ``path`` is relative to the walked base."""

    path: str
    is_dir: bool
    size: int = 0
    mtime: datetime | None = None
    etag: str | None = None
    md5: str | None = None

    @property
    def name(self) -> str:
        return posixpath.basename(self.path)


@dataclass
class WalkResult:
    files: dict[str, RemoteEntry] = field(default_factory=dict)
    # Paths (relative, best effort) of entries that were refused and why.
    rejected: list[tuple[str, str]] = field(default_factory=list)


def check_name(name: str) -> str | None:
    """Return why ``name`` is not a safe single path segment, or None."""
    if not name or name in {".", ".."}:
        return "empty or dot name"
    if "/" in name or "\\" in name:
        return "contains a path separator"
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "contains control characters"
    if len(name.encode("utf-8", "surrogateescape")) > _MAX_NAME_BYTES:
        return "name too long"
    return None


def normalize_remote_path(path: str) -> str:
    """Normalize a user-supplied remote path (relative, POSIX, no traversal).

    Leading and trailing slashes are ignored so ``/backups/`` and ``backups``
    name the same directory relative to the connection's base.
    """
    if "\x00" in path:
        raise BadRequestError("Remote path contains a null byte")
    if "\\" in path:
        raise BadRequestError("Remote paths use forward slashes")
    parts = [part for part in path.strip().strip("/").split("/") if part != ""]
    for part in parts:
        reason = check_name(part)
        if reason is not None:
            raise BadRequestError(f"Invalid remote path segment: {reason}", details={"path": path})
    if len(parts) > _MAX_DEPTH:
        raise BadRequestError("Remote path is too deep")
    return "/".join(parts)


def join(*parts: str) -> str:
    return "/".join(part.strip("/") for part in parts if part.strip("/"))


def is_internal_name(name: str) -> bool:
    return name == ARCHIVE_DIR_NAME or name.startswith(REMOTE_TMP_PREFIX)


class RemoteProvider(ABC):
    """Base class. Subclasses implement the protocol-specific primitives."""

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def _list(self, path: str) -> list[RawEntry]:
        """Entries directly inside ``path`` (raises RemoteError if missing)."""

    @abstractmethod
    def stat(self, path: str) -> RemoteEntry | None:
        """The entry at ``path`` (with ``path`` as given), or None if absent."""

    @abstractmethod
    def download(self, path: str, out: BinaryIO) -> None: ...

    @abstractmethod
    def upload(self, source: BinaryIO, path: str) -> None:
        """Write ``source`` to ``path``, never exposing a partial file under
        the final name where the protocol allows it. Parents must exist."""

    @abstractmethod
    def makedirs(self, path: str) -> None: ...

    @abstractmethod
    def move(self, src: str, dst: str) -> None:
        """Rename ``src`` to ``dst`` (used to archive, never to delete)."""

    @abstractmethod
    def delete(self, path: str) -> None:
        """Remove the *file* at ``path`` (a missing file is not an error).

        Only backup retention and garbage collection call this (Phase 11),
        and only for files inside a backup repository it created. Remote
        sync never deletes; it archives with ``move``.
        """

    def dir_exists(self, path: str) -> bool:
        """Whether ``path`` is an existing directory (the base always is)."""
        if not path:
            return True
        entry = self.stat(path)
        return entry is not None and entry.is_dir

    def test(self) -> str:
        """Connectivity check: list the base directory."""
        entries = self._list("")
        return f"Connected; {len(entries)} entries at the base path"

    def list_dir(self, path: str, *, limit: int) -> tuple[list[RemoteEntry], int]:
        """Validated entries of ``path`` (for browsing) and the rejected count."""
        entries: list[RemoteEntry] = []
        rejected = 0
        for raw in self._list(path):
            if raw.problem is not None or check_name(raw.name) is not None:
                rejected += 1
                continue
            if len(entries) < limit:
                entries.append(_entry(join(path, raw.name), raw))
        entries.sort(key=lambda entry: (not entry.is_dir, entry.path.lower()))
        return entries, rejected

    def walk(self, base: str) -> WalkResult:
        """Every file under ``base`` (recursively), keyed by relative path.

        The archive directory and in-flight temp files are skipped.
        """
        result = WalkResult()
        for rel, raw in self._iter_tree(base, "", 0):
            reason = raw.problem or check_name(raw.name)
            if reason is not None:
                result.rejected.append((join(rel, repr(raw.name)), reason))
                continue
            if not raw.is_dir:
                path = join(rel, raw.name)
                result.files[path] = _entry(path, raw)
        return result

    def _iter_tree(self, base: str, rel: str, depth: int) -> Iterator[tuple[str, RawEntry]]:
        if depth > _MAX_DEPTH:
            raise RemoteError("Remote directory tree is too deep", details={"path": rel})
        for raw in self._list(join(base, rel)):
            if depth == 0 and raw.name == ARCHIVE_DIR_NAME:
                continue
            if raw.name.startswith(REMOTE_TMP_PREFIX):
                continue
            yield rel, raw
            if raw.is_dir and raw.problem is None and check_name(raw.name) is None:
                yield from self._iter_tree(base, join(rel, raw.name), depth + 1)


def _entry(path: str, raw: RawEntry) -> RemoteEntry:
    return RemoteEntry(
        path=path,
        is_dir=raw.is_dir,
        size=raw.size,
        mtime=raw.mtime,
        etag=raw.etag,
        md5=raw.md5,
    )


def parent_of(path: str) -> str:
    return posixpath.dirname(path)
