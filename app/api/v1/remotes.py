"""API routes for remote connections (Phase 7).

Reading connection metadata needs ``remotes:read``. Anything that sets or
uses stored credentials needs ``remotes:write``, and creating/updating a
connection, probing an SFTP host key and the Google Drive OAuth flow also
need an interactive session (API tokens are refused), so a leaked token
cannot plant or redirect credentials.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.v1.deps import (
    CurrentUser,
    request_audit_context,
    require_permission,
    require_session_permission,
)
from app.core.config import Settings
from app.core.errors import AppError
from app.core.secrets import CredentialCipher
from app.db.dependencies import get_db
from app.models import RemoteConnection
from app.models.remote import PROVIDER_GDRIVE
from app.schemas.remotes import (
    HostKeyOut,
    HostKeyProbe,
    OAuthStart,
    RemoteBrowse,
    RemoteBrowseEntry,
    RemoteCreate,
    RemoteOut,
    RemoteTestResult,
    RemoteUpdate,
)
from app.services import remotes
from app.services.remote import registry

router = APIRouter(prefix="/remotes", tags=["remotes"])


def get_cipher(request: Request) -> CredentialCipher:
    """The credentials cipher, created on first use (the key file may be
    generated at that point)."""
    cipher: CredentialCipher | None = getattr(request.app.state, "cipher", None)
    if cipher is None:
        cipher = CredentialCipher.from_settings(request.app.state.settings)
        request.app.state.cipher = cipher
    return cipher


def _out(db: Session, row: RemoteConnection, cipher: CredentialCipher) -> RemoteOut:
    secrets_set, readable = remotes.secret_status(row, cipher)
    return RemoteOut(
        id=row.id,
        name=row.name,
        provider=row.provider,
        config=row.config,
        secrets_set=secrets_set,
        credentials_readable=readable,
        oauth_connected=("refresh_token" in secrets_set)
        if row.provider == PROVIDER_GDRIVE
        else None,
        last_tested_at=row.last_tested_at,
        last_test_ok=row.last_test_ok,
        last_test_message=row.last_test_message,
        job_count=remotes.job_count(db, row.id),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/providers")
def list_providers(
    current: CurrentUser = Depends(require_permission("remotes:read")),
) -> list[dict[str, object]]:
    return registry.specs_payload()


@router.post("/sftp/host-key", response_model=HostKeyOut)
def probe_host_key(
    payload: HostKeyProbe,
    request: Request,
    current: CurrentUser = Depends(require_session_permission("remotes:write")),
) -> HostKeyOut:
    """Fetch an SSH server's host key so an administrator can verify and pin it."""
    settings: Settings = request.app.state.settings
    return HostKeyOut.model_validate(remotes.probe_host_key(payload.host, payload.port, settings))


@router.get("/oauth/callback", include_in_schema=False)
def oauth_callback(
    request: Request,
    state: str = Query(default="", max_length=512),
    code: str | None = Query(default=None, max_length=2048),
    error: str | None = Query(default=None, max_length=256),
    current: CurrentUser = Depends(require_session_permission("remotes:write")),
    db: Session = Depends(get_db),
    cipher: CredentialCipher = Depends(get_cipher),
) -> RedirectResponse:
    """Google redirects the browser here after consent."""
    settings: Settings = request.app.state.settings
    outcome: dict[str, str]
    if error or not code:
        outcome = {"oauth": "error", "message": error or "No authorization code returned"}
    else:
        try:
            row = remotes.finish_oauth(
                db,
                state=state,
                code=code,
                cipher=cipher,
                settings=settings,
                actor=current.user,
                context=request_audit_context(request),
            )
            outcome = {"oauth": "connected", "connection": str(row.id)}
        except AppError as exc:
            outcome = {"oauth": "error", "message": exc.message}
    return RedirectResponse(f"/remote-sync?{urlencode(outcome)}", status_code=303)


@router.get("", response_model=list[RemoteOut])
def list_connections(
    current: CurrentUser = Depends(require_permission("remotes:read")),
    db: Session = Depends(get_db),
    cipher: CredentialCipher = Depends(get_cipher),
) -> list[RemoteOut]:
    return [_out(db, row, cipher) for row in remotes.list_connections(db)]


@router.post("", response_model=RemoteOut, status_code=201)
def create_connection(
    payload: RemoteCreate,
    request: Request,
    current: CurrentUser = Depends(require_session_permission("remotes:write")),
    db: Session = Depends(get_db),
    cipher: CredentialCipher = Depends(get_cipher),
) -> RemoteOut:
    row = remotes.create_connection(
        db,
        name=payload.name,
        provider=payload.provider,
        config=payload.config,
        secrets=payload.secrets,
        cipher=cipher,
        actor=current.user,
        context=request_audit_context(request),
    )
    return _out(db, row, cipher)


@router.get("/{connection_id}", response_model=RemoteOut)
def get_connection(
    connection_id: int,
    current: CurrentUser = Depends(require_permission("remotes:read")),
    db: Session = Depends(get_db),
    cipher: CredentialCipher = Depends(get_cipher),
) -> RemoteOut:
    return _out(db, remotes.get_connection_or_404(db, connection_id), cipher)


@router.patch("/{connection_id}", response_model=RemoteOut)
def update_connection(
    connection_id: int,
    payload: RemoteUpdate,
    request: Request,
    current: CurrentUser = Depends(require_session_permission("remotes:write")),
    db: Session = Depends(get_db),
    cipher: CredentialCipher = Depends(get_cipher),
) -> RemoteOut:
    row = remotes.update_connection(
        db,
        remotes.get_connection_or_404(db, connection_id),
        name=payload.name,
        config=payload.config,
        secrets=payload.secrets,
        cipher=cipher,
        actor=current.user,
        context=request_audit_context(request),
    )
    return _out(db, row, cipher)


@router.delete("/{connection_id}", status_code=204)
def delete_connection(
    connection_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("remotes:write")),
    db: Session = Depends(get_db),
) -> None:
    remotes.delete_connection(
        db,
        remotes.get_connection_or_404(db, connection_id),
        actor=current.user,
        context=request_audit_context(request),
    )


@router.post("/{connection_id}/test", response_model=RemoteTestResult)
def test_connection(
    connection_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("remotes:write")),
    db: Session = Depends(get_db),
    cipher: CredentialCipher = Depends(get_cipher),
) -> RemoteTestResult:
    ok, message = remotes.test_connection(
        db,
        remotes.get_connection_or_404(db, connection_id),
        cipher=cipher,
        settings=request.app.state.settings,
        actor=current.user,
        context=request_audit_context(request),
    )
    return RemoteTestResult(ok=ok, message=message)


@router.get("/{connection_id}/browse", response_model=RemoteBrowse)
def browse(
    connection_id: int,
    request: Request,
    path: str = Query(default="", max_length=4096),
    current: CurrentUser = Depends(require_permission("remotes:write")),
    db: Session = Depends(get_db),
    cipher: CredentialCipher = Depends(get_cipher),
) -> RemoteBrowse:
    """List one remote directory (to pick a job's remote path)."""
    clean, entries, rejected = remotes.browse(
        remotes.get_connection_or_404(db, connection_id),
        path=path,
        cipher=cipher,
        settings=request.app.state.settings,
    )
    return RemoteBrowse(
        path=clean,
        rejected=rejected,
        entries=[
            RemoteBrowseEntry(
                name=entry.name,
                path=entry.path,
                is_dir=entry.is_dir,
                size=entry.size,
                modified=entry.mtime,
            )
            for entry in entries
        ],
    )


@router.post("/{connection_id}/oauth/start", response_model=OAuthStart)
def oauth_start(
    connection_id: int,
    request: Request,
    current: CurrentUser = Depends(require_session_permission("remotes:write")),
    db: Session = Depends(get_db),
    cipher: CredentialCipher = Depends(get_cipher),
) -> OAuthStart:
    settings: Settings = request.app.state.settings
    url = remotes.start_oauth(
        db,
        remotes.get_connection_or_404(db, connection_id),
        cipher=cipher,
        settings=settings,
        actor=current.user,
    )
    return OAuthStart(authorization_url=url, redirect_uri=remotes.redirect_uri(settings))
