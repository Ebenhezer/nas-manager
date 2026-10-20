from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.schemas.health import HealthResponse
from app.services.health import build_health_report

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "Database unreachable"}},
)
def get_health(request: Request, response: Response) -> HealthResponse:
    report = build_health_report(request.app.state.settings, request.app.state.db)
    if report.status == "unhealthy":
        response.status_code = 503
    return report
