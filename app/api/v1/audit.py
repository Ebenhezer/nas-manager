from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.v1.deps import CurrentUser, require_permission
from app.db.dependencies import get_db
from app.schemas.audit import AuditEventOut, AuditPage
from app.services.audit import query_audit_events

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/events", response_model=AuditPage)
def list_audit_events(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    category: str | None = Query(default=None),
    event: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    actor: str | None = Query(default=None, description="actor username"),
    current: CurrentUser = Depends(require_permission("audit:read")),
    db: Session = Depends(get_db),
) -> AuditPage:
    rows, total = query_audit_events(
        db,
        limit=limit,
        offset=offset,
        category=category,
        event=event,
        outcome=outcome,
        actor_username=actor,
    )
    return AuditPage(
        events=[AuditEventOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
