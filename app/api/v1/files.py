from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.v1.deps import CurrentUser, request_audit_context, require_permission
from app.core.config import Settings
from app.db.dependencies import get_db
from app.schemas.files import (
    DeleteRequest,
    FileEntry,
    FileListing,
    MkdirRequest,
    RenameRequest,
    SearchResult,
)
from app.services import files

router = APIRouter(prefix="/files", tags=["files"])


@router.get("/roots", response_model=list[str])
def list_roots(
    request: Request,
    current: CurrentUser = Depends(require_permission("files:read")),
) -> list[str]:
    settings: Settings = request.app.state.settings
    return [str(root) for root in settings.resolved_file_roots]


@router.get("/list", response_model=FileListing)
def list_files(
    request: Request,
    root: str = Query(...),
    path: str = Query(default=""),
    current: CurrentUser = Depends(require_permission("files:read")),
    db: Session = Depends(get_db),
) -> FileListing:
    settings: Settings = request.app.state.settings
    root_path, rel, entries = files.list_dir(db, settings, root_str=root, rel_path=path)
    return FileListing(root=root_path, path=rel, entries=entries)


@router.get("/content", response_class=FileResponse)
def get_file_content(
    request: Request,
    root: str = Query(...),
    path: str = Query(...),
    current: CurrentUser = Depends(require_permission("files:read")),
    db: Session = Depends(get_db),
) -> FileResponse:
    settings: Settings = request.app.state.settings
    target = files.download_path(
        db,
        settings,
        root_str=root,
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
    current: CurrentUser = Depends(require_permission("files:read")),
    db: Session = Depends(get_db),
) -> SearchResult:
    settings: Settings = request.app.state.settings
    root_path, query, matches = files.search(db, settings, root_str=root, query=q, limit=limit)
    return SearchResult(root=root_path, query=query, matches=matches)


@router.post("/upload", response_model=FileEntry)
def upload_file(
    request: Request,
    file: UploadFile = File(...),
    root: str = Form(...),
    path: str = Form(""),
    overwrite: bool = Form(False),
    current: CurrentUser = Depends(require_permission("files:write")),
    db: Session = Depends(get_db),
) -> FileEntry:
    settings: Settings = request.app.state.settings
    return files.upload(
        db,
        settings,
        root_str=root,
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
    current: CurrentUser = Depends(require_permission("files:write")),
    db: Session = Depends(get_db),
) -> FileEntry:
    settings: Settings = request.app.state.settings
    return files.mkdir(
        db,
        settings,
        root_str=payload.root,
        rel_path=payload.path,
        name=payload.name,
        actor=current.user,
        context=request_audit_context(request),
    )


@router.patch("/rename", response_model=FileEntry)
def rename_entry(
    payload: RenameRequest,
    request: Request,
    current: CurrentUser = Depends(require_permission("files:write")),
    db: Session = Depends(get_db),
) -> FileEntry:
    settings: Settings = request.app.state.settings
    return files.rename(
        db,
        settings,
        root_str=payload.root,
        rel_path=payload.path,
        new_name=payload.new_name,
        actor=current.user,
        context=request_audit_context(request),
    )


@router.delete("", status_code=204)
def delete_entry(
    payload: DeleteRequest,
    request: Request,
    current: CurrentUser = Depends(require_permission("files:write")),
    db: Session = Depends(get_db),
) -> None:
    settings: Settings = request.app.state.settings
    files.delete(
        db,
        settings,
        root_str=payload.root,
        rel_path=payload.path,
        recursive=payload.recursive,
        actor=current.user,
        context=request_audit_context(request),
    )
