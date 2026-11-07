"""File manager endpoints.

NAS locations (the configured file roots) need ``files:read`` /
``files:write``; plugged-in USB drives (Phase 16) need ``removable:read`` /
``removable:write``. Which one applies depends on the ``root`` a request
names, so the check happens per request (``_access``) rather than in a
route dependency. Copy/move jobs (``/files/transfers``) need read access to
every source and write access to the destination (and to the sources for a
move).
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.deps import (
    CurrentUser,
    check_permission,
    request_audit_context,
    require_any_permission,
    require_permission,
)
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.db.dependencies import get_db
from app.models.file_transfer import FileTransfer
from app.schemas.files import (
    DeleteRequest,
    FileEntry,
    FileListing,
    FileLocation,
    MkdirRequest,
    RenameRequest,
    SearchResult,
    TransferCreate,
    TransferItem,
    TransferOut,
    TransferProblem,
)
from app.services import file_transfers, files, removable
from app.services.file_transfers import Location, TransferManager

router = APIRouter(prefix="/files", tags=["files"])

ANY_READ = require_any_permission("files:read", "removable:read")
ANY_WRITE = require_any_permission("files:write", "removable:write")


def _access(request: Request, db: Session, current: CurrentUser, root: str, *, write: bool) -> bool:
    """Check access to the location ``root`` names; True when it is a USB drive."""
    settings: Settings = request.app.state.settings
    access = "write" if write else "read"
    try:
        files.resolve_root(settings, root)
    except NotFoundError:
        if removable.find_drive(settings, root) is None:
            if Path(root).parent == settings.removable_mount_dir:
                raise NotFoundError(
                    "That USB drive is no longer connected", details={"root": root}
                ) from None
            # Without NAS access, unknown roots stay 403 (no probing of paths).
            check_permission(request, db, current, f"files:{access}")
            raise
        check_permission(request, db, current, f"removable:{access}")
        return True
    check_permission(request, db, current, f"files:{access}")
    return False


@router.get("/roots", response_model=list[str])
def list_roots(
    request: Request,
    current: CurrentUser = Depends(require_permission("files:read")),
) -> list[str]:
    settings: Settings = request.app.state.settings
    return [str(root) for root in settings.resolved_file_roots]


@router.get("/locations", response_model=list[FileLocation])
def list_locations(
    request: Request,
    current: CurrentUser = Depends(ANY_READ),
) -> list[FileLocation]:
    """NAS roots (``files:read``) and plugged-in USB drives (``removable:read``)."""
    settings: Settings = request.app.state.settings
    result: list[FileLocation] = []
    if "files:read" in current.permissions:
        writable = "files:write" in current.permissions
        for root in settings.resolved_file_roots:
            result.append(
                FileLocation(
                    root=str(root), label=root.name or str(root), kind="nas", writable=writable
                )
            )
    if "removable:read" in current.permissions:
        can_write = "removable:write" in current.permissions
        for drive in removable.list_drives(settings):
            result.append(
                FileLocation(
                    root=drive.root,
                    label=drive.label,
                    kind="removable",
                    writable=can_write and drive.writable,
                    fstype=drive.fstype,
                    device=drive.device,
                    total_bytes=drive.total_bytes,
                    free_bytes=drive.free_bytes,
                )
            )
    return result


@router.get("/list", response_model=FileListing)
def list_files(
    request: Request,
    root: str = Query(...),
    path: str = Query(default=""),
    current: CurrentUser = Depends(ANY_READ),
    db: Session = Depends(get_db),
) -> FileListing:
    settings: Settings = request.app.state.settings
    usb = _access(request, db, current, root, write=False)
    root_path, rel, entries = files.list_dir(
        db, settings, root_str=root, allow_removable=usb, rel_path=path
    )
    return FileListing(root=root_path, path=rel, entries=entries)


@router.get("/content", response_class=FileResponse)
def get_file_content(
    request: Request,
    root: str = Query(...),
    path: str = Query(...),
    current: CurrentUser = Depends(ANY_READ),
    db: Session = Depends(get_db),
) -> FileResponse:
    settings: Settings = request.app.state.settings
    usb = _access(request, db, current, root, write=False)
    target = files.download_path(
        db,
        settings,
        root_str=root,
        allow_removable=usb,
        rel_path=path,
        actor=current.user,
        context=request_audit_context(request),
    )
    return FileResponse(target, filename=target.name, media_type="application/octet-stream")


@router.get("/search", response_model=SearchResult)
def search_files(
    request: Request,
    root: str = Query(...),
    q: str = Query(min_length=1, max_length=255),
    limit: int | None = Query(default=None, ge=1, le=1000),
    current: CurrentUser = Depends(ANY_READ),
    db: Session = Depends(get_db),
) -> SearchResult:
    settings: Settings = request.app.state.settings
    usb = _access(request, db, current, root, write=False)
    root_path, query, matches = files.search(
        db, settings, root_str=root, allow_removable=usb, query=q, limit=limit
    )
    return SearchResult(root=root_path, query=query, matches=matches)


@router.post("/upload", response_model=FileEntry)
def upload_file(
    request: Request,
    file: UploadFile = File(...),
    root: str = Form(...),
    path: str = Form(""),
    overwrite: bool = Form(False),
    current: CurrentUser = Depends(ANY_WRITE),
    db: Session = Depends(get_db),
) -> FileEntry:
    settings: Settings = request.app.state.settings
    usb = _access(request, db, current, root, write=True)
    return files.upload(
        db,
        settings,
        root_str=root,
        allow_removable=usb,
        rel_dir=path,
        filename=file.filename or "",
        source=file.file,
        overwrite=overwrite,
        actor=current.user,
        context=request_audit_context(request),
    )


@router.post("/mkdir", response_model=FileEntry)
def create_directory(
    payload: MkdirRequest,
    request: Request,
    current: CurrentUser = Depends(ANY_WRITE),
    db: Session = Depends(get_db),
) -> FileEntry:
    settings: Settings = request.app.state.settings
    usb = _access(request, db, current, payload.root, write=True)
    return files.mkdir(
        db,
        settings,
        root_str=payload.root,
        allow_removable=usb,
        rel_path=payload.path,
        name=payload.name,
        actor=current.user,
        context=request_audit_context(request),
    )


@router.patch("/rename", response_model=FileEntry)
def rename_entry(
    payload: RenameRequest,
    request: Request,
    current: CurrentUser = Depends(ANY_WRITE),
    db: Session = Depends(get_db),
) -> FileEntry:
    settings: Settings = request.app.state.settings
    usb = _access(request, db, current, payload.root, write=True)
    return files.rename(
        db,
        settings,
        root_str=payload.root,
        allow_removable=usb,
        rel_path=payload.path,
        new_name=payload.new_name,
        actor=current.user,
        context=request_audit_context(request),
    )


@router.delete("", status_code=204)
def delete_entry(
    payload: DeleteRequest,
    request: Request,
    current: CurrentUser = Depends(ANY_WRITE),
    db: Session = Depends(get_db),
) -> None:
    settings: Settings = request.app.state.settings
    usb = _access(request, db, current, payload.root, write=True)
    files.delete(
        db,
        settings,
        root_str=payload.root,
        allow_removable=usb,
        rel_path=payload.path,
        recursive=payload.recursive,
        actor=current.user,
        context=request_audit_context(request),
    )


# ---------------------------------------------------------------------------
# Copy / move jobs (Phase 16)
# ---------------------------------------------------------------------------


def _transfer_out(row: FileTransfer) -> TransferOut:
    return TransferOut(
        id=row.id,
        user_id=row.user_id,
        mode=row.mode,
        sources=[TransferItem(**item) for item in json.loads(row.sources)],
        dest_root=row.dest_root,
        dest_path=row.dest_path,
        conflict=row.conflict,
        state=row.state,
        total_bytes=row.total_bytes,
        done_bytes=row.done_bytes,
        total_files=row.total_files,
        done_files=row.done_files,
        skipped_files=row.skipped_files,
        current_name=row.current_name,
        message=row.message,
        errors=[TransferProblem(**item) for item in file_transfers.errors_of(row)],
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _location(
    request: Request, db: Session, current: CurrentUser, root: str, path: str, *, write: bool
) -> Location:
    settings: Settings = request.app.state.settings
    usb = _access(request, db, current, root, write=write)
    resolved = files.resolve_location(settings, root, allow_removable=usb).resolve()
    files.resolve_in_root(resolved, path)  # refuse escapes before queueing
    return Location(root=resolved, rel=path.strip("/"))


def _own_transfer(db: Session, current: CurrentUser, transfer_id: int) -> FileTransfer:
    """Transfers are private to the user who started them (others get 404)."""
    row = db.get(FileTransfer, transfer_id)
    if row is None or row.user_id != current.user.id:
        raise NotFoundError("Transfer not found", details={"transfer_id": transfer_id})
    return row


@router.post("/transfers", response_model=TransferOut, status_code=202)
def create_transfer(
    payload: TransferCreate,
    request: Request,
    current: CurrentUser = Depends(ANY_WRITE),
    db: Session = Depends(get_db),
) -> TransferOut:
    """Copy or move files and folders between NAS folders and USB drives."""
    settings: Settings = request.app.state.settings
    move = payload.mode == "move"
    sources = [
        _location(request, db, current, item.root, item.path, write=move)
        for item in payload.sources
    ]
    destination = _location(request, db, current, payload.dest_root, payload.dest_path, write=True)
    row = file_transfers.create(
        db,
        settings,
        actor=current.user,
        sources=sources,
        destination=destination,
        mode=payload.mode,
        conflict=payload.conflict,
        context=request_audit_context(request),
    )
    manager: TransferManager = request.app.state.transfers
    manager.submit(row.id)
    return _transfer_out(row)


@router.get("/transfers", response_model=list[TransferOut])
def list_transfers(
    limit: int = Query(default=20, ge=1, le=200),
    current: CurrentUser = Depends(ANY_READ),
    db: Session = Depends(get_db),
) -> list[TransferOut]:
    """The caller's most recent transfers, newest first."""
    rows = (
        db.execute(
            select(FileTransfer)
            .where(FileTransfer.user_id == current.user.id)
            .order_by(FileTransfer.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [_transfer_out(row) for row in rows]


@router.get("/transfers/{transfer_id}", response_model=TransferOut)
def get_transfer(
    transfer_id: int,
    current: CurrentUser = Depends(ANY_READ),
    db: Session = Depends(get_db),
) -> TransferOut:
    return _transfer_out(_own_transfer(db, current, transfer_id))


@router.post("/transfers/{transfer_id}/cancel", response_model=TransferOut)
def cancel_transfer(
    transfer_id: int,
    request: Request,
    current: CurrentUser = Depends(ANY_READ),
    db: Session = Depends(get_db),
) -> TransferOut:
    row = _own_transfer(db, current, transfer_id)
    file_transfers.request_cancel(db, row, current.user, request_audit_context(request))
    return _transfer_out(row)
