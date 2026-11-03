"""Remote connections: CRUD, connectivity tests, browsing and Drive OAuth (Phase 7).

Secrets never leave this module in clear text except towards the provider
that needs them: they are encrypted before they reach the database, the API
only learns which secret fields are set, and audit details list field names
only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets as pysecrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import (
    AppError,
    BadRequestError,
    ConfigurationError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from app.core.secrets import CredentialCipher
from app.db.base import utcnow_naive
from app.models import RemoteConnection, SyncJob, User
from app.models.remote import PROVIDER_GDRIVE
from app.services import audit
from app.services.remote import registry
from app.services.remote.base import RemoteEntry, RemoteProvider, normalize_remote_path

OAUTH_CALLBACK_PATH = "/api/v1/remotes/oauth/callback"
OAUTH_STATE_TTL = timedelta(minutes=10)


def _clean_name(name: str) -> str:
    value = name.strip()
    if not value or len(value) > 64 or any(ord(c) < 32 for c in value):
        raise BadRequestError("Connection name must be 1-64 printable characters")
    return value


def list_connections(session: Session) -> list[RemoteConnection]:
    return list(session.execute(select(RemoteConnection).order_by(RemoteConnection.name)).scalars())


def get_connection_or_404(session: Session, connection_id: int) -> RemoteConnection:
    row = session.get(RemoteConnection, connection_id)
    if row is None:
        raise NotFoundError("Remote connection not found", details={"id": connection_id})
    return row


def job_count(session: Session, connection_id: int) -> int:
    return int(
        session.execute(
            select(func.count(SyncJob.id)).where(SyncJob.connection_id == connection_id)
        ).scalar_one()
    )


def stored_secrets(row: RemoteConnection, cipher: CredentialCipher) -> dict[str, Any]:
    return cipher.decrypt_json(row.secrets_encrypted)


def secret_status(row: RemoteConnection, cipher: CredentialCipher) -> tuple[list[str], bool]:
    """``(names of the secret fields that are set, decryptable)``."""
    try:
        values = stored_secrets(row, cipher)
    except ConfigurationError:
        return [], False
    return sorted(name for name, value in values.items() if value), True


def _ensure_unique(session: Session, name: str, *, exclude_id: int | None = None) -> None:
    stmt = select(RemoteConnection.id).where(func.lower(RemoteConnection.name) == name.lower())
    if exclude_id is not None:
        stmt = stmt.where(RemoteConnection.id != exclude_id)
    if session.execute(stmt).first() is not None:
        raise ConflictError("A remote connection with that name already exists")


def create_connection(
    session: Session,
    *,
    name: str,
    provider: str,
    config: dict[str, Any],
    secrets: dict[str, str | None],
    cipher: CredentialCipher,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> RemoteConnection:
    clean_name = _clean_name(name)
    _ensure_unique(session, clean_name)
    clean_config, clean_secrets = registry.validate_config(provider, config, secrets, {})
    row = RemoteConnection(
        name=clean_name,
        provider=provider,
        secrets_encrypted=cipher.encrypt_json(clean_secrets) if clean_secrets else None,
        created_by=actor.id,
    )
    row.config = clean_config
    session.add(row)
    session.flush()
    audit.record_audit_event(
        session,
        event=audit.REMOTE_CREATED,
        category=audit.CATEGORY_REMOTE_SYNC,
        actor=actor,
        target_type="remote_connection",
        target_id=row.id,
        details={
            "name": clean_name,
            "provider": provider,
            "config": clean_config,
            "secrets_set": sorted(clean_secrets),
        },
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return row


def update_connection(
    session: Session,
    row: RemoteConnection,
    *,
    name: str | None,
    config: dict[str, Any] | None,
    secrets: dict[str, str | None] | None,
    cipher: CredentialCipher,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> RemoteConnection:
    """Rename and/or change settings; ``config`` keys merge over the stored
    config, ``secrets`` keys follow PATCH semantics (missing = keep)."""
    changes: list[str] = []
    if name is not None:
        clean_name = _clean_name(name)
        if clean_name != row.name:
            _ensure_unique(session, clean_name, exclude_id=row.id)
            row.name = clean_name
            changes.append("name")
    if config is not None or secrets:
        existing = stored_secrets(row, cipher)
        merged = {**row.config, **(config or {})}
        clean_config, clean_secrets = registry.validate_config(
            row.provider, merged, dict(secrets or {}), existing
        )
        if row.provider == PROVIDER_GDRIVE and clean_config.get("client_id") != row.config.get(
            "client_id"
        ):
            # A refresh token is bound to the OAuth client that obtained it.
            clean_secrets.pop("refresh_token", None)
        changes += sorted(k for k in clean_config if clean_config.get(k) != row.config.get(k))
        changes += sorted(
            f"secret:{k}"
            for k in set(clean_secrets) | set(existing)
            if clean_secrets.get(k) != existing.get(k)
        )
        row.config = clean_config
        row.secrets_encrypted = cipher.encrypt_json(clean_secrets) if clean_secrets else None
    audit.record_audit_event(
        session,
        event=audit.REMOTE_UPDATED,
        category=audit.CATEGORY_REMOTE_SYNC,
        actor=actor,
        target_type="remote_connection",
        target_id=row.id,
        details={"name": row.name, "changed": changes},
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return row


def delete_connection(
    session: Session,
    row: RemoteConnection,
    *,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> None:
    jobs = job_count(session, row.id)
    if jobs:
        raise ConflictError(
            "The connection is used by sync jobs; delete them first", details={"jobs": jobs}
        )
    audit.record_audit_event(
        session,
        event=audit.REMOTE_DELETED,
        category=audit.CATEGORY_REMOTE_SYNC,
        actor=actor,
        target_type="remote_connection",
        target_id=row.id,
        details={"name": row.name, "provider": row.provider},
        **audit.request_context(context),
        commit=False,
    )
    session.delete(row)
    session.commit()


def build_provider(
    row: RemoteConnection, cipher: CredentialCipher, settings: Settings
) -> RemoteProvider:
    return registry.open_provider(row.provider, row.config, stored_secrets(row, cipher), settings)


def test_connection(
    session: Session,
    row: RemoteConnection,
    *,
    cipher: CredentialCipher,
    settings: Settings,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> tuple[bool, str]:
    """Connect and list the base path; the outcome is stored and audited."""
    try:
        with build_provider(row, cipher, settings) as provider:
            message = provider.test()
        ok = True
    except AppError as exc:
        ok, message = False, exc.message
    row.last_tested_at = utcnow_naive()
    row.last_test_ok = ok
    row.last_test_message = message[:500]
    audit.record_audit_event(
        session,
        event=audit.REMOTE_TESTED,
        category=audit.CATEGORY_REMOTE_SYNC,
        outcome=audit.OUTCOME_SUCCESS if ok else audit.OUTCOME_FAILURE,
        actor=actor,
        target_type="remote_connection",
        target_id=row.id,
        details={"name": row.name, "message": message[:500]},
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return ok, message


def browse(
    row: RemoteConnection, *, path: str, cipher: CredentialCipher, settings: Settings
) -> tuple[str, list[RemoteEntry], int]:
    clean = normalize_remote_path(path)
    with build_provider(row, cipher, settings) as provider:
        entries, rejected = provider.list_dir(clean, limit=settings.remote_browse_limit)
    return clean, entries, rejected


def probe_host_key(host: str, port: int, settings: Settings) -> dict[str, str | int]:
    from app.services.remote.sftp import probe_host_key as probe

    clean_host, clean_port = registry.validate_host_port(host, port)
    return probe(clean_host, clean_port, timeout=settings.remote_connect_timeout_seconds)


# ---------------------------------------------------------------------------
# Google Drive OAuth (authorization code + PKCE)
# ---------------------------------------------------------------------------


def redirect_uri(settings: Settings) -> str:
    if not settings.public_base_url:
        raise ConfigurationError(
            "Set NAS_PUBLIC_BASE_URL to this application's external URL to connect Google Drive"
        )
    return f"{settings.public_base_url}{OAUTH_CALLBACK_PATH}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def start_oauth(
    session: Session,
    row: RemoteConnection,
    *,
    cipher: CredentialCipher,
    settings: Settings,
    actor: User,
) -> str:
    """Return the Google consent URL; the state is bound to this user."""
    from app.services.remote.gdrive import authorization_url

    if row.provider != PROVIDER_GDRIVE:
        raise BadRequestError("Only Google Drive connections use OAuth")
    uri = redirect_uri(settings)
    if not stored_secrets(row, cipher).get("client_secret"):
        raise BadRequestError("Set the OAuth client secret first")
    state = f"{row.id}.{pysecrets.token_urlsafe(32)}"
    verifier = pysecrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    row.oauth_pending_encrypted = cipher.encrypt_json(
        {
            "state_digest": _digest(state),
            "verifier": verifier,
            "user_id": actor.id,
            "expires_at": (utcnow_naive() + OAUTH_STATE_TTL).isoformat(),
        }
    )
    session.commit()
    return authorization_url(
        client_id=str(row.config["client_id"]),
        redirect_uri=uri,
        state=state,
        challenge=challenge.decode().rstrip("="),
    )


def finish_oauth(
    session: Session,
    *,
    state: str,
    code: str,
    cipher: CredentialCipher,
    settings: Settings,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> RemoteConnection:
    """Validate the callback state and store the refresh token (encrypted)."""
    from app.services.remote.gdrive import exchange_code

    head, _, _ = state.partition(".")
    if not head.isdigit():
        raise BadRequestError("Invalid OAuth state")
    row = get_connection_or_404(session, int(head))
    pending = cipher.decrypt_json(row.oauth_pending_encrypted)
    expected = str(pending.get("state_digest", ""))
    if not expected or not hmac.compare_digest(expected, _digest(state)):
        raise BadRequestError("Invalid or already used OAuth state")
    # One use only, whatever happens next.
    row.oauth_pending_encrypted = None
    session.commit()
    if pending.get("user_id") != actor.id:
        raise ForbiddenError("This authorization was started by another user")
    if datetime.fromisoformat(str(pending["expires_at"])) < utcnow_naive():
        raise BadRequestError("The authorization expired; start it again")
    secrets = stored_secrets(row, cipher)
    try:
        refresh = exchange_code(
            client_id=str(row.config["client_id"]),
            client_secret=str(secrets.get("client_secret", "")),
            code=code,
            verifier=str(pending["verifier"]),
            redirect_uri=redirect_uri(settings),
            timeout=settings.remote_connect_timeout_seconds,
        )
    except AppError as exc:
        audit.record_audit_event(
            session,
            event=audit.REMOTE_OAUTH_FAILED,
            category=audit.CATEGORY_REMOTE_SYNC,
            outcome=audit.OUTCOME_FAILURE,
            actor=actor,
            target_type="remote_connection",
            target_id=row.id,
            details={"name": row.name, "reason": exc.message},
            **audit.request_context(context),
        )
        raise
    secrets["refresh_token"] = refresh
    row.secrets_encrypted = cipher.encrypt_json(secrets)
    audit.record_audit_event(
        session,
        event=audit.REMOTE_OAUTH_CONNECTED,
        category=audit.CATEGORY_REMOTE_SYNC,
        actor=actor,
        target_type="remote_connection",
        target_id=row.id,
        details={"name": row.name},
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return row
