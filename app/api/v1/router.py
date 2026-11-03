from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    api_tokens,
    audit,
    auth,
    disks,
    files,
    groups,
    health,
    public_uploads,
    remote_sync,
    remotes,
    roles,
    samba,
    settings,
    sync,
    system,
    uploads,
    users,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(system.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(groups.router)
api_router.include_router(roles.router)
api_router.include_router(audit.router)
api_router.include_router(files.router)
api_router.include_router(samba.router)
api_router.include_router(settings.router)
api_router.include_router(disks.router)
api_router.include_router(disks.storage_router)
api_router.include_router(public_uploads.router_public)
api_router.include_router(public_uploads.router_admin)
api_router.include_router(api_tokens.router)
api_router.include_router(api_tokens.admin_router)
api_router.include_router(uploads.router)
api_router.include_router(sync.router)
api_router.include_router(sync.info_router)
api_router.include_router(remotes.router)
api_router.include_router(remote_sync.status_router)
api_router.include_router(remote_sync.jobs_router)
api_router.include_router(remote_sync.runs_router)
api_router.include_router(remote_sync.conflicts_router)
