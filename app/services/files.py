"""File manager service.

Every operation resolves the requested relative path against one of the
configured managed roots (the "jail") and refuses anything that would escape
it — absolute paths, ``..`` traversal, null bytes and symlinks that resolve
outside the root. All mutations (and downloads) are written to the audit log.

The service never follows symlinks while listing or searching (``lstat`` /
``os.walk`` without ``followlinks``), and uploads stream to a temporary file
in the destination directory before an atomic ``os.replace`` so a partial
upload never appears under the final name.
"""

from __future__ import annotations

import os
import shutil
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import BinaryIO, TypedDict

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import User
from app.schemas.files import FileEntry
from app.services import audit

_CHUNK_SIZE = 1024 * 1024
_UPLOAD_TMP_PREFIX = ".nas-upload-"


class _RequestContext(TypedDict, total=False):
    ip_address: str | None
    user_agent: str | None
    request_id: str | None


def _audit_ctx(context: dict[str, str | None] | None) -> _RequestContext:
    source = context or {}
    return {
        "ip_address": source.get("ip_address"),
        "user_agent": source.get("user_agent"),
        "request_id": source.get("request_id"),
    }


def resolve_root(settings: Settings, root_str: str) -> Path:
    """Return the configured root matching ``root_str`` (exact match only)."""
    candidate = Path(root_str or "").expanduser().resolve(strict=False)
    for root in settings.resolved_file_roots:
        resolved = root.resolve(strict=False)
        if candidate == resolved:
            return resolved
    raise NotFoundError("Unknown file root", details={"root": root_str})


def resolve_in_root(root: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` against ``root`` and refuse anything that escapes."""
    if "\x00" in rel_path:
        raise BadRequestError("Path contains a null byte")
    candidate = Path(rel_path)
    if candidate.is_absolute():
        raise BadRequestError("Absolute paths are not allowed")
    root_resolved = root.resolve(strict=False)
    target = (root_resolved / candidate).resolve(strict=False)
    if not target.is_relative_to(root_resolved):
        raise BadRequestError("Path escapes the managed root")
    return target


def sanitize_entry_name(name: str) -> str:
    """Validate a bare file/directory name (no separators, no traversal)."""
    if "\x00" in name:
        raise BadRequestError("Name contains a null byte")
    value = name.strip()
    if not value or value in {".", ".."}:
        raise BadRequestError("Invalid name")
    pure = PurePath(value)
    if pure.name != value or "/" in value or "\\" in value or value.startswith("~"):
        raise BadRequestError("Name must not contain path separators")
    if any(ord(c) < 32 for c in value):
        raise BadRequestError("Name contains control characters")
    return value


def _entry_for(path: Path, root: Path) -> FileEntry:
    st = path.lstat()  # never follow symlinks while listing
    return FileEntry(
        name=path.name,
        path=str(path.relative_to(root)),
        is_dir=stat.S_ISDIR(st.st_mode),
        size=st.st_size,
        modified=datetime.fromtimestamp(st.st_mtime, tz=UTC),
        mode=stat.filemode(st.st_mode),
    )


def list_dir(
    session: Session,
    settings: Settings,
    *,
    root_str: str,
    rel_path: str,
) -> tuple[str, str, list[FileEntry]]:
    root = resolve_root(settings, root_str)
    target = resolve_in_root(root, rel_path)
    if not target.is_dir():
        raise BadRequestError("Not a directory", details={"path": rel_path})
    entries = [_entry_for(child, root) for child in target.iterdir()]
    entries.sort(key=lambda entry: (not entry.is_dir, entry.name.lower()))
    return str(root), rel_path, entries


def download_path(
    session: Session,
    settings: Settings,
    *,
    root_str: str,
    rel_path: str,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> Path:
    root = resolve_root(settings, root_str)
    target = resolve_in_root(root, rel_path)
    if not target.is_file():
        raise NotFoundError("File not found", details={"path": rel_path})
    audit.record_audit_event(
        session,
        event=audit.FILE_DOWNLOADED,
        category=audit.CATEGORY_FILE,
        actor=actor,
        target_type="file",
        target_id=rel_path,
        details={"root": str(root), "path": rel_path, "size": target.stat().st_size},
        **_audit_ctx(context),
        commit=False,
    )
    session.commit()
    return target


def upload(
    session: Session,
    settings: Settings,
    *,
    root_str: str,
    rel_dir: str,
    filename: str,
    source: BinaryIO,
    overwrite: bool,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> FileEntry:
    root = resolve_root(settings, root_str)
    directory = resolve_in_root(root, rel_dir)
    if not directory.is_dir():
        raise BadRequestError("Upload target is not a directory")
    name = sanitize_entry_name(filename)
    target = directory / name
    if target.exists() and not overwrite:
        raise ConflictError(
            "A file or directory with that name already exists", details={"name": name}
        )
    if target.exists() and target.is_dir():
        raise BadRequestError("Cannot overwrite a directory with a file")

    temporary = directory / f"{_UPLOAD_TMP_PREFIX}{uuid.uuid4().hex}.part"
    total = 0
    try:
        with temporary.open("wb") as out:
            while True:
                chunk = source.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > settings.max_upload_bytes:
                    raise BadRequestError(
                        f"Upload exceeds the limit of {settings.max_upload_bytes} bytes",
                        details={"max_bytes": settings.max_upload_bytes},
                    )
                out.write(chunk)
        # Defence in depth: re-check the final target stays inside the root.
        if not target.resolve(strict=False).is_relative_to(root.resolve(strict=False)):
            raise BadRequestError("Path escapes the managed root")
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    rel_path = str(target.relative_to(root))
    audit.record_audit_event(
        session,
        event=audit.FILE_UPLOADED,
        category=audit.CATEGORY_FILE,
        actor=actor,
        target_type="file",
        target_id=rel_path,
        details={"root": str(root), "path": rel_path, "size": total, "overwrite": overwrite},
        **_audit_ctx(context),
        commit=False,
    )
    session.commit()
    return _entry_for(target, root)


def mkdir(
    session: Session,
    settings: Settings,
    *,
    root_str: str,
    rel_path: str,
    name: str,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> FileEntry:
    root = resolve_root(settings, root_str)
    parent = resolve_in_root(root, rel_path)
    if not parent.is_dir():
        raise BadRequestError("Parent is not a directory")
    safe = sanitize_entry_name(name)
    target = parent / safe
    if target.exists():
        raise ConflictError(
            "A file or directory with that name already exists", details={"name": safe}
        )
    target.mkdir()
    rel_path_new = str(target.relative_to(root))
    audit.record_audit_event(
        session,
        event=audit.FILE_CREATED_DIR,
        category=audit.CATEGORY_FILE,
        actor=actor,
        target_type="directory",
        target_id=rel_path_new,
        details={"root": str(root), "path": rel_path_new},
        **_audit_ctx(context),
        commit=False,
    )
    session.commit()
    return _entry_for(target, root)


def rename(
    session: Session,
    settings: Settings,
    *,
    root_str: str,
    rel_path: str,
    new_name: str,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> FileEntry:
    root = resolve_root(settings, root_str)
    source = resolve_in_root(root, rel_path)
    if source == root.resolve(strict=False):
        raise BadRequestError("Cannot rename the root directory")
    if not source.exists():
        raise NotFoundError("Not found", details={"path": rel_path})
    safe = sanitize_entry_name(new_name)
    target = source.parent / safe
    if target.exists():
        raise ConflictError(
            "A file or directory with that name already exists", details={"name": safe}
        )
    source.rename(target)
    rel_path_new = str(target.relative_to(root))
    audit.record_audit_event(
        session,
        event=audit.FILE_RENAMED,
        category=audit.CATEGORY_FILE,
        actor=actor,
        target_type="file",
        target_id=rel_path_new,
        details={"root": str(root), "from": rel_path, "to": rel_path_new},
        **_audit_ctx(context),
        commit=False,
    )
    session.commit()
    return _entry_for(target, root)


def delete(
    session: Session,
    settings: Settings,
    *,
    root_str: str,
    rel_path: str,
    recursive: bool,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> None:
    root = resolve_root(settings, root_str)
    target = resolve_in_root(root, rel_path)
    if target == root.resolve(strict=False):
        raise BadRequestError("Cannot delete the root directory")
    if not target.exists():
        raise NotFoundError("Not found", details={"path": rel_path})
    is_dir = target.is_dir() and not target.is_symlink()
    if is_dir:
        if recursive:
            shutil.rmtree(target)
        else:
            try:
                target.rmdir()  # succeeds only when the directory is empty
            except OSError as exc:
                raise BadRequestError(
                    "Directory is not empty; pass recursive=true to delete it and everything inside"
                ) from exc
    else:
        target.unlink()
    audit.record_audit_event(
        session,
        event=audit.FILE_DELETED,
        category=audit.CATEGORY_FILE,
        actor=actor,
        target_type="directory" if is_dir else "file",
        target_id=rel_path,
        details={"root": str(root), "path": rel_path, "recursive": recursive},
        **_audit_ctx(context),
        commit=False,
    )
    session.commit()


def search(
    session: Session,
    settings: Settings,
    *,
    root_str: str,
    query: str,
    limit: int | None = None,
) -> tuple[str, str, list[FileEntry]]:
    root = resolve_root(settings, root_str)
    needle = query.strip().lower()
    if not needle:
        raise BadRequestError("Search query is required")
    cap = limit or settings.file_search_limit
    matches: list[FileEntry] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            if name.startswith(_UPLOAD_TMP_PREFIX):
                continue
            if needle in name.lower():
                matches.append(_entry_for(Path(dirpath) / name, root))
                if len(matches) >= cap:
                    return str(root), query, matches
    return str(root), query, matches
