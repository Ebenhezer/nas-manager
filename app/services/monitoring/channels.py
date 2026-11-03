"""Notification channels: email (SMTP) and webhook (HTTP POST).

Non-secret settings live in ``config``; the SMTP password, the webhook
signing secret and an optional webhook ``Authorization`` header value are
one Fernet token in ``secrets_encrypted`` (same key file as remote
credentials). Responses and audit details only ever name the secret fields.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, ConfigurationError, ConflictError, NotFoundError
from app.core.secrets import CredentialCipher
from app.models import User
from app.models.monitoring import (
    CHANNEL_EMAIL,
    CHANNEL_KINDS,
    CHANNEL_WEBHOOK,
    SEVERITIES,
    AlertChannel,
)
from app.services import audit

EMAIL_SECURITY = ("starttls", "tls", "none")
SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    CHANNEL_EMAIL: ("password",),
    CHANNEL_WEBHOOK: ("signing_secret", "authorization"),
}
CONFIG_FIELDS: dict[str, tuple[str, ...]] = {
    CHANNEL_EMAIL: ("host", "port", "security", "username", "from_address", "to"),
    CHANNEL_WEBHOOK: ("url",),
}
MAX_RECIPIENTS = 20

_ADDRESS_RE = re.compile(r"[^@\s<>()\[\],;:\"\\]+@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\]")


def _single_line(value: Any, field: str, *, max_length: int = 255) -> str:
    if not isinstance(value, str):
        raise BadRequestError(f"{field} must be a string")
    text = value.strip()
    if len(text) > max_length or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise BadRequestError(f"{field} must be a single line of at most {max_length} characters")
    return text


def _address(value: Any, field: str) -> str:
    address = _single_line(value, field, max_length=254)
    if not _ADDRESS_RE.fullmatch(address):
        raise BadRequestError(f"{field} is not a valid email address", details={"value": address})
    return address


def _validate_email(config: dict[str, Any], allow_insecure: bool) -> dict[str, Any]:
    host = _single_line(config.get("host", ""), "host")
    if not host or not _HOST_RE.fullmatch(host):
        raise BadRequestError("host must be a host name or IP address")
    try:
        port = int(config.get("port") or 587)
    except (TypeError, ValueError) as exc:
        raise BadRequestError("port must be a number") from exc
    if not 1 <= port <= 65535:
        raise BadRequestError("port must be between 1 and 65535")
    security = config.get("security") or "starttls"
    if security not in EMAIL_SECURITY:
        raise BadRequestError("Unknown security mode", details={"allowed": list(EMAIL_SECURITY)})
    if security == "none" and not allow_insecure:
        raise BadRequestError(
            "Unencrypted SMTP sends the password and alerts in clear text; "
            "set allow_insecure to use it"
        )
    username = _single_line(config.get("username") or "", "username")
    recipients = config.get("to")
    if isinstance(recipients, str):
        recipients = [part for part in re.split(r"[,\s]+", recipients) if part]
    if not isinstance(recipients, list) or not recipients:
        raise BadRequestError("to must list at least one recipient")
    if len(recipients) > MAX_RECIPIENTS:
        raise BadRequestError(f"at most {MAX_RECIPIENTS} recipients")
    return {
        "host": host,
        "port": port,
        "security": security,
        "username": username or None,
        "from_address": _address(config.get("from_address", ""), "from_address"),
        "to": [_address(item, "to") for item in recipients],
    }


def _validate_webhook(config: dict[str, Any], allow_insecure: bool) -> dict[str, Any]:
    url = _single_line(config.get("url", ""), "url", max_length=2048)
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise BadRequestError("url must be an absolute http(s) URL")
    if parts.scheme == "http" and not allow_insecure:
        raise BadRequestError("Plain HTTP webhooks need allow_insecure")
    if parts.username or parts.password:
        raise BadRequestError("Put credentials in the authorization secret, not in the URL")
    if parts.fragment:
        raise BadRequestError("url must not contain a fragment")
    return {"url": url}


def _validate_secrets(
    kind: str, provided: dict[str, str | None], existing: dict[str, Any]
) -> dict[str, str]:
    allowed = SECRET_FIELDS[kind]
    unknown = sorted(set(provided) - set(allowed))
    if unknown:
        raise BadRequestError("Unknown secret fields", details={"unknown": unknown})
    merged: dict[str, str] = {k: str(v) for k, v in existing.items() if k in allowed and v}
    for key, value in provided.items():
        if value is None or value == "":
            merged.pop(key, None)
        else:
            if len(value) > 4096 or any(c in value for c in "\r\n\0"):
                raise BadRequestError(f"{key} must be a single line")
            merged[key] = value
    return merged


def validate(
    kind: str,
    config: dict[str, Any],
    secrets: dict[str, str | None],
    existing_secrets: dict[str, Any],
    *,
    allow_insecure: bool,
) -> tuple[dict[str, Any], dict[str, str]]:
    if kind not in CHANNEL_KINDS:
        raise BadRequestError("Unknown channel kind", details={"allowed": list(CHANNEL_KINDS)})
    unknown = sorted(set(config) - set(CONFIG_FIELDS[kind]))
    if unknown:
        # Catches secrets put into config by mistake (they would be stored in clear).
        raise BadRequestError("Unknown config fields", details={"unknown": unknown})
    clean = (
        _validate_email(config, allow_insecure)
        if kind == CHANNEL_EMAIL
        else _validate_webhook(config, allow_insecure)
    )
    return clean, _validate_secrets(kind, secrets, existing_secrets)


def _clean_name(name: str) -> str:
    value = name.strip()
    if not value or len(value) > 64 or any(ord(c) < 32 for c in value):
        raise BadRequestError("Channel name must be 1-64 printable characters")
    return value


def _check_severity(value: str) -> str:
    if value not in SEVERITIES:
        raise BadRequestError("Unknown severity", details={"allowed": list(SEVERITIES)})
    return value


def _ensure_unique(session: Session, name: str, *, exclude_id: int | None = None) -> None:
    stmt = select(AlertChannel.id).where(func.lower(AlertChannel.name) == name.lower())
    if exclude_id is not None:
        stmt = stmt.where(AlertChannel.id != exclude_id)
    if session.execute(stmt).first() is not None:
        raise ConflictError("A channel with that name already exists")


def list_channels(session: Session) -> list[AlertChannel]:
    return list(session.execute(select(AlertChannel).order_by(AlertChannel.name)).scalars())


def get_channel_or_404(session: Session, channel_id: int) -> AlertChannel:
    row = session.get(AlertChannel, channel_id)
    if row is None:
        raise NotFoundError("Notification channel not found", details={"id": channel_id})
    return row


def stored_secrets(row: AlertChannel, cipher: CredentialCipher) -> dict[str, Any]:
    return cipher.decrypt_json(row.secrets_encrypted)


def secret_status(row: AlertChannel, cipher: CredentialCipher) -> tuple[list[str], bool]:
    """``(names of the secret fields that are set, decryptable)``."""
    try:
        values = stored_secrets(row, cipher)
    except ConfigurationError:
        return [], False
    return sorted(name for name, value in values.items() if value), True


def create_channel(
    session: Session,
    *,
    values: dict[str, Any],
    cipher: CredentialCipher,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> AlertChannel:
    name = _clean_name(values["name"])
    _ensure_unique(session, name)
    kind = values["kind"]
    allow_insecure = bool(values.get("allow_insecure", False))
    config, secrets = validate(
        kind,
        values.get("config") or {},
        values.get("secrets") or {},
        {},
        allow_insecure=allow_insecure,
    )
    row = AlertChannel(
        name=name,
        kind=kind,
        secrets_encrypted=cipher.encrypt_json(secrets) if secrets else None,
        min_severity=_check_severity(values.get("min_severity") or "warning"),
        allow_insecure=allow_insecure,
        send_resolved=bool(values.get("send_resolved", True)),
        enabled=bool(values.get("enabled", True)),
        created_by=actor.id,
    )
    row.config = config
    session.add(row)
    session.flush()
    audit.record_audit_event(
        session,
        event=audit.ALERT_CHANNEL_CREATED,
        category=audit.CATEGORY_MONITORING,
        actor=actor,
        target_type="alert_channel",
        target_id=row.id,
        details={
            "name": name,
            "kind": kind,
            "config": config,
            "secrets_set": sorted(secrets),
            "min_severity": row.min_severity,
            "allow_insecure": allow_insecure,
        },
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return row


def update_channel(
    session: Session,
    row: AlertChannel,
    *,
    changes: dict[str, Any],
    cipher: CredentialCipher,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> AlertChannel:
    """PATCH: ``config`` keys merge over the stored config; ``secrets`` keys
    replace (string), clear (null/"") or are kept (missing)."""
    changed: list[str] = []
    if changes.get("name") is not None:
        name = _clean_name(changes["name"])
        if name != row.name:
            _ensure_unique(session, name, exclude_id=row.id)
            row.name = name
            changed.append("name")
    for field in ("send_resolved", "enabled"):
        if changes.get(field) is not None and changes[field] != getattr(row, field):
            setattr(row, field, bool(changes[field]))
            changed.append(field)
    if changes.get("min_severity") is not None:
        severity = _check_severity(changes["min_severity"])
        if severity != row.min_severity:
            row.min_severity = severity
            changed.append("min_severity")
    allow_insecure = (
        bool(changes["allow_insecure"])
        if changes.get("allow_insecure") is not None
        else row.allow_insecure
    )
    existing = stored_secrets(row, cipher)
    config, secrets = validate(
        row.kind,
        {**row.config, **(changes.get("config") or {})},
        dict(changes.get("secrets") or {}),
        existing,
        allow_insecure=allow_insecure,
    )
    if allow_insecure != row.allow_insecure:
        row.allow_insecure = allow_insecure
        changed.append("allow_insecure")
    changed += sorted(f"config:{k}" for k in config if config.get(k) != row.config.get(k))
    changed += sorted(
        f"secret:{k}" for k in set(secrets) | set(existing) if secrets.get(k) != existing.get(k)
    )
    row.config = config
    row.secrets_encrypted = cipher.encrypt_json(secrets) if secrets else None
    audit.record_audit_event(
        session,
        event=audit.ALERT_CHANNEL_UPDATED,
        category=audit.CATEGORY_MONITORING,
        actor=actor,
        target_type="alert_channel",
        target_id=row.id,
        details={"name": row.name, "changed": changed},
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return row


def delete_channel(
    session: Session,
    row: AlertChannel,
    *,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> None:
    audit.record_audit_event(
        session,
        event=audit.ALERT_CHANNEL_DELETED,
        category=audit.CATEGORY_MONITORING,
        actor=actor,
        target_type="alert_channel",
        target_id=row.id,
        details={"name": row.name, "kind": row.kind},
        **audit.request_context(context),
        commit=False,
    )
    session.delete(row)
    session.commit()
