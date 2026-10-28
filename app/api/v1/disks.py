"""API routes for disk discovery, storage and RAID management (Phase 5).

Discovery is read-only and requires ``disks:read``. Mounting, unmounting and
scrubbing require ``disks:write``, an explicit ``confirm: true`` and an
``intent`` digest obtained from the matching preview endpoint — a confirmed
operation is bound to exactly what was previewed.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.v1.deps import CurrentUser, request_audit_context, require_permission
from app.core.config import Settings
from app.db.dependencies import get_db
from app.schemas.disks import (
    HelperStatus,
    MountPreview,
    MountPreviewRequest,
    MountRequest,
    OperationResult,
    PhysicalDisk,
    RaidList,
    ScrubPreview,
    ScrubRequest,
    SmartData,
    UnmountPreview,
    UnmountRequest,
    Volume,
)
from app.services import disks as disks_service

router = APIRouter(prefix="/disks", tags=["disks"])
storage_router = APIRouter(prefix="/storage", tags=["storage"])

DISKS_READ = require_permission("disks:read")
DISKS_WRITE = require_permission("disks:write")


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


@router.get("/status", response_model=HelperStatus)
def helper_status(request: Request, current: CurrentUser = Depends(DISKS_READ)) -> HelperStatus:
    return disks_service.helper_status(_settings(request))


@router.get("", response_model=list[PhysicalDisk])
def list_disks(request: Request, current: CurrentUser = Depends(DISKS_READ)) -> list[PhysicalDisk]:
    return disks_service.list_disks(_settings(request))


@router.get("/{name}", response_model=PhysicalDisk)
def get_disk(
    name: str, request: Request, current: CurrentUser = Depends(DISKS_READ)
) -> PhysicalDisk:
    return disks_service.disk_detail(_settings(request), name)


@router.get("/{name}/smart", response_model=SmartData)
def get_smart_status(
    name: str, request: Request, current: CurrentUser = Depends(DISKS_READ)
) -> SmartData:
    return disks_service.smart_status(_settings(request), name)


@router.post("/{name}/mount/preview", response_model=MountPreview)
def preview_mount(
    name: str,
    payload: MountPreviewRequest,
    request: Request,
    current: CurrentUser = Depends(DISKS_WRITE),
) -> MountPreview:
    return disks_service.mount_preview(_settings(request), name=name, **payload.model_dump())


@router.post("/{name}/mount", response_model=OperationResult)
def mount(
    name: str,
    payload: MountRequest,
    request: Request,
    current: CurrentUser = Depends(DISKS_WRITE),
    db: Session = Depends(get_db),
) -> OperationResult:
    return disks_service.mount_device(
        db,
        _settings(request),
        name=name,
        mountpoint=payload.mountpoint,
        fstype=payload.fstype,
        options=payload.options,
        confirm=payload.confirm,
        intent=payload.intent,
        actor=current.user,
        context=request_audit_context(request),
    )


@router.post("/{name}/unmount/preview", response_model=UnmountPreview)
def preview_unmount(
    name: str,
    payload: UnmountRequest,
    request: Request,
    current: CurrentUser = Depends(DISKS_WRITE),
) -> UnmountPreview:
    return disks_service.unmount_preview(
        _settings(request), name=name, mountpoint=payload.mountpoint
    )


@router.post("/{name}/unmount", response_model=OperationResult)
def unmount(
    name: str,
    payload: UnmountRequest,
    request: Request,
    current: CurrentUser = Depends(DISKS_WRITE),
    db: Session = Depends(get_db),
) -> OperationResult:
    return disks_service.unmount_device(
        db,
        _settings(request),
        name=name,
        mountpoint=payload.mountpoint,
        confirm=payload.confirm,
        intent=payload.intent,
        actor=current.user,
        context=request_audit_context(request),
    )


@storage_router.get("/volumes", response_model=list[Volume])
def list_volumes(request: Request, current: CurrentUser = Depends(DISKS_READ)) -> list[Volume]:
    return disks_service.list_volumes(_settings(request))


@storage_router.get("/raid", response_model=RaidList)
def list_raid(request: Request, current: CurrentUser = Depends(DISKS_READ)) -> RaidList:
    return disks_service.list_raid(_settings(request))


@storage_router.post("/raid/{name}/scrub/preview", response_model=ScrubPreview)
def preview_scrub(
    name: str, request: Request, current: CurrentUser = Depends(DISKS_WRITE)
) -> ScrubPreview:
    return disks_service.scrub_preview(_settings(request), name=name)


@storage_router.post("/raid/{name}/scrub", response_model=OperationResult)
def scrub(
    name: str,
    payload: ScrubRequest,
    request: Request,
    current: CurrentUser = Depends(DISKS_WRITE),
    db: Session = Depends(get_db),
) -> OperationResult:
    return disks_service.scrub_array(
        db,
        _settings(request),
        name=name,
        confirm=payload.confirm,
        intent=payload.intent,
        actor=current.user,
        context=request_audit_context(request),
    )
