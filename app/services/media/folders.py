"""Folders of a media library, and moving/renaming what is in them (Phase 15).

A library's folders are its real directories: listings merge the folders the
index knows about (with item counts and a cover) with the subdirectories on
disk, so a freshly created, still empty folder shows up too. Changes are
limited to the caller's own personal library, like uploads and deletions.

Moves and renames update ``MediaItem.rel_path`` in place, so albums,
favorites and thumbnails (keyed by item id and a size/mtime fingerprint)
follow the file. The change journal gets a ``deleted`` entry for the old
path and a ``created`` entry for the new one, which is what a mirroring
client needs. A library scan running at the same moment can at worst index
a moved file once more under its new name; the next scan settles it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import MediaItem, MediaLibrary
from app.models.media import CHANGE_CREATED, CHANGE_DELETED, KIND_IMAGE
from app.services import audit, files
from app.services.media import changes, libraries, uploads
from app.services.media.libraries import MediaActor
from app.services.media.search import clean_folder, folder_clause


@dataclass
class FolderInfo:
    name: str
    path: str
    item_count: int = 0
    cover_item_id: int | None = None
    latest_taken_at: datetime | None = None
    # (is image, taken_at) of the cover: the newest image, else the newest video.
    cover_key: tuple[bool, datetime] | None = None


def _audit(
    session: Session,
    event: str,
    actor: MediaActor,
    library: MediaLibrary,
    context: dict[str, str | None] | None,
    **details: object,
) -> None:
    audit.record_audit_event(
        session,
        event=event,
        category=audit.CATEGORY_MEDIA,
        actor=actor.user,
        target_type="media_library",
        target_id=library.id,
        details={"library_id": library.id, **details},
        **audit.request_context(context),
        commit=False,
    )


def _folder_dir(settings: Settings, library: MediaLibrary, folder: str) -> Path:
    base = libraries.library_path(settings, library)
    return files.resolve_in_root(base, folder) if folder else base


def _clean_new_name(name: str) -> str:
    value = files.sanitize_entry_name(name)
    if value.startswith("."):
        raise BadRequestError("Hidden names are not accepted")
    return value


def _join(folder: str, name: str) -> str:
    return f"{folder}/{name}" if folder else name


def _is_listed_dir(entry: os.DirEntry[str]) -> bool:
    return not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False)


def list_folders(
    session: Session, settings: Settings, library: MediaLibrary, folder: str
) -> list[FolderInfo]:
    """The immediate subfolders of ``folder``, sorted by name."""
    folder = clean_folder(folder)
    prefix = f"{folder}/" if folder else ""
    found: dict[str, FolderInfo] = {}
    rows = session.execute(
        select(MediaItem.rel_path, MediaItem.id, MediaItem.taken_at, MediaItem.kind).where(
            MediaItem.library_id == library.id,
            folder_clause(folder, recursive=True),
        )
    ).all()
    for rel_path, item_id, taken_at, kind in rows:
        rest = rel_path[len(prefix) :]
        if "/" not in rest:
            continue  # directly in this folder, not in a subfolder
        name = rest.split("/", 1)[0]
        info = found.setdefault(name, FolderInfo(name=name, path=_join(folder, name)))
        info.item_count += 1
        if info.latest_taken_at is None or taken_at > info.latest_taken_at:
            info.latest_taken_at = taken_at
        key = (kind == KIND_IMAGE, taken_at)
        if info.cover_key is None or key > info.cover_key:
            info.cover_item_id = item_id
            info.cover_key = key
    directory = _folder_dir(settings, library, folder)
    if directory.is_dir():
        with os.scandir(directory) as entries:
            for entry in entries:
                if _is_listed_dir(entry) and entry.name not in found:
                    found[entry.name] = FolderInfo(name=entry.name, path=_join(folder, entry.name))
    return sorted(found.values(), key=lambda info: info.name.lower())


def create_folder(
    session: Session,
    settings: Settings,
    actor: MediaActor,
    library: MediaLibrary,
    path: str,
    *,
    context: dict[str, str | None] | None = None,
) -> str:
    libraries.require_writable(library, actor)
    parts = uploads.subfolder_parts(path, None)
    rel = "/".join(parts)
    directory = _folder_dir(settings, library, rel)
    if directory.exists():
        raise ConflictError("A folder or file with that name already exists", details={"path": rel})
    directory.mkdir(parents=True, mode=0o750)
    _audit(session, audit.MEDIA_FOLDER_CREATED, actor, library, context, path=rel)
    session.commit()
    return rel


def _existing_folder(settings: Settings, library: MediaLibrary, path: str) -> tuple[str, Path]:
    folder = clean_folder(path)
    if not folder:
        raise BadRequestError("The library's top folder cannot be changed")
    directory = _folder_dir(settings, library, folder)
    if directory.is_symlink() or not directory.is_dir():
        raise NotFoundError("Folder not found", details={"path": folder})
    return folder, directory


def _repath(session: Session, item: MediaItem, rel_path: str) -> None:
    """Point ``item`` at ``rel_path`` and journal the move; the caller commits."""
    changes.record(session, item, CHANGE_DELETED)
    item.rel_path = rel_path
    item.name = rel_path.rsplit("/", 1)[-1]
    session.flush()
    changes.record(session, item, CHANGE_CREATED)


def rename_folder(
    session: Session,
    settings: Settings,
    actor: MediaActor,
    library: MediaLibrary,
    path: str,
    new_name: str,
    *,
    context: dict[str, str | None] | None = None,
) -> str:
    libraries.require_writable(library, actor)
    folder, directory = _existing_folder(settings, library, path)
    name = _clean_new_name(new_name)
    parent = folder.rsplit("/", 1)[0] if "/" in folder else ""
    new_folder = _join(parent, name)
    if new_folder == folder:
        return folder
    target = directory.parent / name
    if target.exists() or target.is_symlink():
        raise ConflictError(
            "A folder or file with that name already exists", details={"path": new_folder}
        )
    os.rename(directory, target)
    old_prefix, new_prefix = f"{folder}/", f"{new_folder}/"
    items = session.execute(
        select(MediaItem).where(
            MediaItem.library_id == library.id, folder_clause(folder, recursive=True)
        )
    ).scalars()
    for item in list(items):
        _repath(session, item, new_prefix + item.rel_path[len(old_prefix) :])
    _audit(
        session,
        audit.MEDIA_FOLDER_RENAMED,
        actor,
        library,
        context,
        path=folder,
        new_path=new_folder,
    )
    session.commit()
    return new_folder


def delete_folder(
    session: Session,
    settings: Settings,
    actor: MediaActor,
    library: MediaLibrary,
    path: str,
    *,
    context: dict[str, str | None] | None = None,
) -> None:
    """Remove an empty folder (nothing indexed below it and nothing on disk)."""
    libraries.require_writable(library, actor)
    folder, directory = _existing_folder(settings, library, path)
    indexed = session.execute(
        select(MediaItem.id).where(
            and_(MediaItem.library_id == library.id, folder_clause(folder, recursive=True))
        )
    ).first()
    if indexed is not None or any(directory.iterdir()):
        raise ConflictError("Only empty folders can be deleted", details={"path": folder})
    directory.rmdir()
    _audit(session, audit.MEDIA_FOLDER_DELETED, actor, library, context, path=folder)
    session.commit()


def move_items(
    session: Session,
    settings: Settings,
    actor: MediaActor,
    library: MediaLibrary,
    item_ids: list[int],
    folder: str,
    *,
    context: dict[str, str | None] | None = None,
) -> int:
    """Move items of ``library`` into ``folder`` (created if missing).

    Colliding names become ``name (1).ext``; items already there are skipped.
    Returns how many files moved.
    """
    libraries.require_writable(library, actor)
    parts = uploads.subfolder_parts(folder, None) if clean_folder(folder) else []
    target = uploads.target_directory(settings, library, parts)
    base = libraries.library_path(settings, library)
    moved: list[dict[str, object]] = []
    for item_id in dict.fromkeys(item_ids):
        item = libraries.get_browsable_item(session, actor, item_id)
        if item.library_id != library.id:
            raise BadRequestError(
                "Items can only be moved within their own library", details={"item_id": item_id}
            )
        source = libraries.item_path(settings, library, item)
        if not source.is_file():
            raise NotFoundError("The file is no longer on disk", details={"item_id": item_id})
        if source.parent == target:
            continue
        final = uploads.place_file(source, target, item.name)
        old_path = item.rel_path
        _repath(session, item, final.relative_to(base).as_posix())
        moved.append({"item_id": item.id, "from": old_path, "to": item.rel_path})
    if moved:
        _audit(
            session,
            audit.MEDIA_ITEMS_MOVED,
            actor,
            library,
            context,
            folder="/".join(parts),
            count=len(moved),
            items=moved[:100],
        )
    session.commit()
    return len(moved)


def rename_item(
    session: Session,
    settings: Settings,
    actor: MediaActor,
    item: MediaItem,
    new_name: str,
    *,
    context: dict[str, str | None] | None = None,
) -> MediaItem:
    """Rename a file in place; the extension (and so the type) must stay."""
    library = session.get(MediaLibrary, item.library_id)
    if library is None:  # pragma: no cover - cascades make this unreachable
        raise NotFoundError("Media item not found")
    libraries.require_writable(library, actor)
    name = uploads.clean_filename(new_name)
    if name == item.name:
        return item
    if Path(name).suffix.lower() != Path(item.name).suffix.lower():
        raise BadRequestError(
            "Renaming cannot change the file extension",
            details={"extension": Path(item.name).suffix},
        )
    source = libraries.item_path(settings, library, item)
    if not source.is_file():
        raise NotFoundError("The file is no longer on disk", details={"item_id": item.id})
    target = source.parent / name
    try:
        os.link(source, target)
    except FileExistsError as error:
        raise ConflictError(
            "A file with that name already exists", details={"name": name}
        ) from error
    except OSError:
        # No hard links here; a case-only rename also lands here on some filesystems.
        if target.exists() and not target.samefile(source):
            raise ConflictError(
                "A file with that name already exists", details={"name": name}
            ) from None
        os.rename(source, target)
    else:
        source.unlink()
    old_path = item.rel_path
    parent = old_path.rsplit("/", 1)[0] if "/" in old_path else ""
    _repath(session, item, _join(parent, name))
    audit.record_audit_event(
        session,
        event=audit.MEDIA_ITEM_RENAMED,
        category=audit.CATEGORY_MEDIA,
        actor=actor.user,
        target_type="media_item",
        target_id=item.id,
        details={"library_id": library.id, "from": old_path, "to": item.rel_path},
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return item
