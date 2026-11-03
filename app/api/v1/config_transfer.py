"""API routes for configuration export, import and rollback (Phase 12).

Every route is session-only: an export can carry password hashes and
credentials (sealed with the caller's passphrase) and an import can change
any configuration, so neither may be reachable with an API token.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.api.v1.deps import CurrentUser, request_audit_context, require_session_permission
from app.api.v1.remotes import get_cipher
from app.core.secrets import CredentialCipher
from app.db.base import utcnow_naive
from app.db.dependencies import get_db
from app.schemas.config_transfer import (
    ConfigExportRequest,
    ConfigImportPlan,
    ConfigImportRequest,
    ConfigImportResult,
    ConfigImportRunOut,
    ConfigRollbackRequest,
)
from app.services import audit
from app.services.config_transfer import export, importer

router = APIRouter(prefix="/config", tags=["config"])


@router.post("/export")
def export_config(
    payload: ConfigExportRequest,
    request: Request,
    current: CurrentUser = Depends(require_session_permission("config:export")),
    db: Session = Depends(get_db),
    cipher: CredentialCipher = Depends(get_cipher),
) -> Response:
    """Download the configuration as JSON (a file attachment)."""
    document = export.build_document(
        db, request.app.state.settings, cipher, passphrase=payload.passphrase
    )
    audit.record_audit_event(
        db,
        event=audit.CONFIG_EXPORTED,
        category=audit.CATEGORY_CONFIG,
        actor=current.user,
        details={
            "secrets_included": payload.passphrase is not None,
            "counts": {
                name: len(value) if isinstance(value, list) else len(value or {})
                for name, value in document["sections"].items()
            },
        },
        **audit.request_context(request_audit_context(request)),
    )
    stamp = utcnow_naive().strftime("%Y%m%dT%H%M%SZ")
    return Response(
        content=json.dumps(document, indent=2, sort_keys=True),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="nas-manager-config-{stamp}.json"'},
    )


@router.post("/import/preview", response_model=ConfigImportPlan)
def preview_import(
    payload: ConfigImportRequest,
    request: Request,
    current: CurrentUser = Depends(require_session_permission("config:import")),
    db: Session = Depends(get_db),
    cipher: CredentialCipher = Depends(get_cipher),
) -> ConfigImportPlan:
    """Validate a document and report what an import would do. Changes nothing."""
    plan = importer.preview(
        db,
        request.app.state.settings,
        cipher,
        document=payload.document,
        passphrase=payload.passphrase,
        mode=payload.mode,
        actor=current.user,
    )
    audit.record_audit_event(
        db,
        event=audit.CONFIG_IMPORT_PREVIEWED,
        category=audit.CATEGORY_CONFIG,
        actor=current.user,
        details={
            "mode": payload.mode,
            "source": payload.source_name,
            "errors": len(plan["errors"]),
            "warnings": len(plan["warnings"]),
        },
        **audit.request_context(request_audit_context(request)),
    )
    return ConfigImportPlan.model_validate(plan)


@router.post("/import", response_model=ConfigImportResult)
def apply_import(
    payload: ConfigImportRequest,
    request: Request,
    current: CurrentUser = Depends(require_session_permission("config:import")),
    db: Session = Depends(get_db),
    cipher: CredentialCipher = Depends(get_cipher),
) -> ConfigImportResult:
    """Apply a document in one transaction. Refused (nothing changes) when
    anything is invalid; needs ``confirm`` when the plan has warnings."""
    try:
        run, plan = importer.apply(
            db,
            request.app.state.settings,
            cipher,
            document=payload.document,
            passphrase=payload.passphrase,
            mode=payload.mode,
            confirm=payload.confirm,
            source_name=payload.source_name,
            actor=current.user,
            context=request_audit_context(request),
        )
    except importer.ImportValidationError as exc:
        details: dict[str, Any] = exc.details or {}
        audit.record_audit_event(
            db,
            event=audit.CONFIG_IMPORT_REFUSED,
            category=audit.CATEGORY_CONFIG,
            outcome=audit.OUTCOME_FAILURE,
            actor=current.user,
            details={"mode": payload.mode, "errors": (details.get("errors") or [])[:20]},
            **audit.request_context(request_audit_context(request)),
        )
        raise
    return ConfigImportResult(
        run=ConfigImportRunOut.model_validate(run), plan=ConfigImportPlan.model_validate(plan)
    )


@router.get("/imports", response_model=list[ConfigImportRunOut])
def list_imports(
    limit: int = Query(default=50, ge=1, le=500),
    current: CurrentUser = Depends(require_session_permission("config:import")),
    db: Session = Depends(get_db),
) -> list[ConfigImportRunOut]:
    return [ConfigImportRunOut.model_validate(row) for row in importer.list_runs(db, limit=limit)]


@router.post("/imports/{run_id}/rollback", response_model=ConfigImportResult)
def rollback_import(
    run_id: int,
    payload: ConfigRollbackRequest,
    request: Request,
    current: CurrentUser = Depends(require_session_permission("config:import")),
    db: Session = Depends(get_db),
    cipher: CredentialCipher = Depends(get_cipher),
) -> ConfigImportResult:
    """Undo the most recent import (objects it created are deleted, the rest
    is restored from its pre-import snapshot)."""
    run, plan = importer.rollback(
        db,
        request.app.state.settings,
        cipher,
        importer.get_run_or_404(db, run_id),
        confirm=payload.confirm,
        actor=current.user,
        context=request_audit_context(request),
    )
    return ConfigImportResult(
        run=ConfigImportRunOut.model_validate(run),
        plan=ConfigImportPlan.model_validate(plan | {"secrets_included": True}),
    )
