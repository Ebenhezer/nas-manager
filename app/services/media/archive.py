"""ZIP downloads of several gallery items at once (Phase 15).

Media is already compressed, so entries are stored, not deflated. The
archive is written to a temporary file in the media cache directory and
removed once the response has been sent.
"""

from __future__ import annotations

import os
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import BadRequestError, NotFoundError
from app.models import MediaItem, MediaLibrary
from app.services.media import libraries
from app.services.media.libraries import MediaActor

MAX_ITEMS = 1000


def _unique_name(name: str, used: set[str]) -> str:
    candidate = name
    dot = name.rfind(".")
    stem, suffix = (name[:dot], name[dot:]) if dot > 0 else (name, "")
    counter = 1
    while candidate.lower() in used:
        candidate = f"{stem} ({counter}){suffix}"
        counter += 1
    used.add(candidate.lower())
    return candidate


def build_zip(
    session: Session, settings: Settings, actor: MediaActor, item_ids: list[int]
) -> tuple[Path, list[MediaItem], int]:
    """Write the items to a temporary ZIP; returns its path, the items and their bytes."""
    ids = list(dict.fromkeys(item_ids))
    if not ids:
        raise BadRequestError("No items selected")
    if len(ids) > MAX_ITEMS:
        raise BadRequestError(f"At most {MAX_ITEMS} items per download")
    entries: list[tuple[MediaItem, Path]] = []
    total = 0
    for item_id in ids:
        item = libraries.get_browsable_item(session, actor, item_id)
        library = session.get(MediaLibrary, item.library_id)
        assert library is not None  # noqa: S101 - the join above guarantees it
        path = libraries.item_path(settings, library, item)
        if not path.is_file():
            raise NotFoundError("The file is no longer on disk", details={"item_id": item_id})
        total += path.stat().st_size
        entries.append((item, path))
    if total > settings.media_zip_max_bytes:
        raise BadRequestError(
            "The selection is too large to download as one ZIP",
            details={"bytes": total, "max_bytes": settings.media_zip_max_bytes},
        )
    directory = settings.resolved_media_cache_dir / "zip"
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    handle, name = tempfile.mkstemp(prefix="media-", suffix=".zip", dir=directory)
    os.close(handle)
    target = Path(name)
    used: set[str] = set()
    try:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
            for item, path in entries:
                zf.write(path, arcname=_unique_name(item.name, used))
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target, [item for item, _ in entries], total


def archive_name() -> str:
    return f"media-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
