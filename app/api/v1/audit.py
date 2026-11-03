"""Audit log search, export and integrity (``audit:read``).

Phase 13 adds free-text and time-range search, CSV/JSON Lines export (itself
audited), the facets the UI offers as filters, and the hash-chain status and
on-demand verification.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.v1.deps import CurrentUser, request_audit_context, require_permission
from app.db.base import utcnow_naive
from app.db.dependencies import get_db
from app.schemas.audit import (
    AuditEventOut,
    AuditFacets,
    AuditIntegrity,
    AuditPage,
    AuditVerifyResult,
)
from app.services import audit, audit_chain
from app.services.audit import AuditFilters, iter_audit_events, query_audit_events
from app.services.syslog.format import audit_event_dict

router = APIRouter(prefix="/audit", tags=["audit"])

_CSV_COLUMNS = (
    "id",
    "created_at",
    "category",
    "event",
    "outcome",
    "actor",
    "target_type",
    "target_id",
    "ip_address",
    "user_agent",
    "request_id",
    "details",
    "chain_hash",
)
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def audit_filters(
    category: str | None = Query(default=None, max_length=32),
    event: str | None = Query(default=None, max_length=64),
    outcome: str | None = Query(default=None, max_length=16),
    actor: str | None = Query(default=None, max_length=64, description="actor username"),
    target_type: str | None = Query(default=None, max_length=32),
    target_id: str | None = Query(default=None, max_length=64),
    request_id: str | None = Query(default=None, max_length=64),
    ip: str | None = Query(default=None, max_length=64),
    since: datetime | None = Query(default=None, description="inclusive, ISO 8601"),
    until: datetime | None = Query(default=None, description="exclusive, ISO 8601"),
    q: str | None = Query(default=None, max_length=200, description="free-text search"),
) -> AuditFilters:
    return AuditFilters(
        category=category,
        event=event,
        outcome=outcome,
        actor_username=actor,
        target_type=target_type,
        target_id=target_id,
        request_id=request_id,
        ip_address=ip,
        since=since,
        until=until,
        q=q,
    )


@router.get("/events", response_model=AuditPage)
def list_audit_events(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    filters: AuditFilters = Depends(audit_filters),
    current: CurrentUser = Depends(require_permission("audit:read")),
    db: Session = Depends(get_db),
) -> AuditPage:
    rows, total = query_audit_events(db, limit=limit, offset=offset, filters=filters)
    return AuditPage(
        events=[AuditEventOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    # Spreadsheets evaluate cells starting with these characters as formulas.
    return f"'{text}" if text.startswith(_FORMULA_PREFIXES) else text


def _csv_line(values: list[str]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\r\n").writerow(values)
    return buffer.getvalue()


@router.get("/events/export")
def export_audit_events(
    request: Request,
    format: Literal["csv", "jsonl"] = Query(default="csv"),
    filters: AuditFilters = Depends(audit_filters),
    current: CurrentUser = Depends(require_permission("audit:read")),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    settings = request.app.state.settings
    max_rows: int = settings.audit_export_max_rows
    _, total = query_audit_events(db, limit=1, filters=filters)
    rows = min(total, max_rows)
    audit.record_audit_event(
        db,
        event=audit.AUDIT_EXPORTED,
        category=audit.CATEGORY_AUDIT,
        actor=current.user,
        details={
            "format": format,
            "filters": filters.as_details(),
            "rows": rows,
            "truncated": total > max_rows,
        },
        **audit.request_context(request_audit_context(request)),
    )
    session_factory = request.app.state.db.session_factory

    def lines() -> Iterator[str]:
        # The stream outlives the request's session, so it uses its own.
        with session_factory() as session:
            if format == "csv":
                yield _csv_line(list(_CSV_COLUMNS))
            for row in iter_audit_events(session, filters, max_rows=max_rows):
                item = audit_event_dict(row)
                if format == "jsonl":
                    yield json.dumps(item, sort_keys=True, ensure_ascii=False, default=str) + "\n"
                else:
                    yield _csv_line([_csv_cell(item.get(column)) for column in _CSV_COLUMNS])

    stamp = utcnow_naive().strftime("%Y%m%dT%H%M%SZ")
    media_type = "text/csv; charset=utf-8" if format == "csv" else "application/x-ndjson"
    return StreamingResponse(
        lines(),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="nas-manager-audit-{stamp}.{format}"',
            "X-Audit-Rows": str(rows),
            "Cache-Control": "no-store",
        },
    )


@router.get("/facets", response_model=AuditFacets)
def facets(
    current: CurrentUser = Depends(require_permission("audit:read")),
    db: Session = Depends(get_db),
) -> AuditFacets:
    return AuditFacets(**audit.audit_facets(db))


@router.get("/integrity", response_model=AuditIntegrity)
def integrity(
    request: Request,
    current: CurrentUser = Depends(require_permission("audit:read")),
    db: Session = Depends(get_db),
) -> AuditIntegrity:
    return AuditIntegrity.model_validate(
        audit_chain.integrity_status(db, request.app.state.settings)
    )


@router.post("/verify", response_model=AuditVerifyResult)
def verify(
    request: Request,
    current: CurrentUser = Depends(require_permission("audit:read")),
    db: Session = Depends(get_db),
) -> AuditVerifyResult:
    result = audit_chain.verify(db)
    audit_chain.record_verify_result(db, result)
    audit.record_audit_event(
        db,
        event=audit.AUDIT_VERIFIED,
        category=audit.CATEGORY_AUDIT,
        outcome=audit.OUTCOME_SUCCESS if result.ok else audit.OUTCOME_FAILURE,
        actor=current.user,
        details={
            "checked": result.checked,
            "sealed_up_to": result.sealed_up_to,
            "first_break_id": result.first_break_id,
            "reason": result.reason,
        },
        **audit.request_context(request_audit_context(request)),
    )
    return AuditVerifyResult.model_validate(result.as_dict())
