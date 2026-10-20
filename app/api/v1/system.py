from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.v1.deps import CurrentUser, require_permission
from app.schemas.system import SystemSummary
from app.services.monitoring import collect_system_summary

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/summary", response_model=SystemSummary)
def get_system_summary(
    request: Request,
    current: CurrentUser = Depends(require_permission("dashboard:read")),
) -> SystemSummary:
    return collect_system_summary([request.app.state.settings.data_dir])
