from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    audit,
    auth,
    files,
    groups,
    health,
    public_uploads,
    roles,
    samba,
    settings,
    system,
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
api_router.include_router(public_uploads.router_public)
api_router.include_router(public_uploads.router_admin)
