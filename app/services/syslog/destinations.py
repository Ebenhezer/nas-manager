"""Syslog destination management: validation, CRUD and the test message.

Plain UDP and TCP send audit events in clear text, so creating such a
destination (or switching one to a plaintext protocol or another host)
needs ``confirm: true``; TLS always verifies the collector's certificate.
Each change bumps a configuration version in ``app_settings`` so every
process reloads its application-log forwarding.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import ssl
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.db.base import utcnow_naive
from app.models import AuditEvent, SyslogDestination, User
from app.models.syslog_destination import (
    FACILITIES,
    FORMATS,
    LOG_LEVELS,
    PROTOCOL_TLS,
    PROTOCOLS,
)
from app.services import audit
from app.services.remote_sync import ConfirmationRequiredError
from app.services.settings_service import get_setting, set_setting
from app.services.syslog import format as syslog_format
from app.services.syslog.transport import SyslogSender, SyslogTransportError, frame, tls_context

CONFIG_VERSION_KEY = "syslog.config_version"
DEFAULT_PORTS = {"udp": 514, "tcp": 514, "tls": 6514}
MAX_DESTINATIONS = 10
MAX_CA_PEM_BYTES = 64 * 1024

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST_RE = re.compile(rf"(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})*\.?")

_CONFIG_FIELDS = (
    "name",
    "enabled",
    "host",
    "port",
    "protocol",
    "ca_pem",
    "format",
    "facility",
    "forward_audit",
    "audit_categories",
    "audit_failures_only",
    "forward_logs",
    "log_min_level",
)


def config_version(session: Session) -> str:
    return get_setting(session, CONFIG_VERSION_KEY) or "0"


def _bump_version(session: Session) -> None:
    # set_setting commits, so the version moves with the change it announces.
    set_setting(session, CONFIG_VERSION_KEY, str(int(config_version(session)) + 1))


def list_destinations(session: Session) -> list[SyslogDestination]:
    return list(
        session.execute(select(SyslogDestination).order_by(SyslogDestination.name)).scalars()
    )


def get_destination_or_404(session: Session, destination_id: int) -> SyslogDestination:
    row = session.get(SyslogDestination, destination_id)
    if row is None:
        raise NotFoundError("Syslog destination not found", details={"id": destination_id})
    return row


def _host(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BadRequestError("host is required")
    host = value.strip()
    try:
        return str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        pass
    if not _HOST_RE.fullmatch(host):
        raise BadRequestError("host must be a host name or IP address", details={"value": host})
    return host.rstrip(".").lower()


def _ca_pem(value: Any) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or len(value.encode()) > MAX_CA_PEM_BYTES:
        raise BadRequestError("ca_pem must be PEM text of at most 64 KiB")
    pem = value.strip() + "\n"
    if "-----BEGIN CERTIFICATE-----" not in pem:
        raise BadRequestError("ca_pem must contain a PEM certificate")
    try:
        tls_context(pem)
    except (ssl.SSLError, ValueError) as exc:
        raise BadRequestError("ca_pem is not a valid CA certificate") from exc
    return pem


def _validated(values: dict[str, Any]) -> dict[str, Any]:
    """Normalise a complete set of configuration values."""
    name = values.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name.strip()):
        raise BadRequestError(
            "name must be 1-64 characters: letters, digits, space, '.', '_' or '-'"
        )
    protocol = values.get("protocol") or PROTOCOL_TLS
    if protocol not in PROTOCOLS:
        raise BadRequestError("Unknown protocol", details={"allowed": list(PROTOCOLS)})
    port = values.get("port") or DEFAULT_PORTS[protocol]
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise BadRequestError("port must be between 1 and 65535")
    fmt = values.get("format") or "rfc5424"
    if fmt not in FORMATS:
        raise BadRequestError("Unknown format", details={"allowed": list(FORMATS)})
    facility = values.get("facility") or "local0"
    if facility not in FACILITIES:
        raise BadRequestError("Unknown facility", details={"allowed": list(FACILITIES)})
    level = str(values.get("log_min_level") or "WARNING").upper()
    if level not in LOG_LEVELS:
        raise BadRequestError("Unknown log level", details={"allowed": list(LOG_LEVELS)})
    categories = values.get("audit_categories") or []
    if not isinstance(categories, list) or not all(isinstance(c, str) for c in categories):
        raise BadRequestError("audit_categories must be a list of category names")
    unknown = sorted(set(categories) - set(audit.CATEGORIES))
    if unknown:
        raise BadRequestError(
            "Unknown audit categories",
            details={"unknown": unknown, "allowed": list(audit.CATEGORIES)},
        )
    ca_pem = _ca_pem(values.get("ca_pem"))
    if ca_pem is not None and protocol != PROTOCOL_TLS:
        raise BadRequestError("ca_pem is only used with the tls protocol")
    return {
        "name": name.strip(),
        "enabled": bool(values.get("enabled", True)),
        "host": _host(values.get("host")),
        "port": port,
        "protocol": protocol,
        "ca_pem": ca_pem,
        "format": fmt,
        "facility": facility,
        "forward_audit": bool(values.get("forward_audit", True)),
        "audit_categories": sorted(set(categories)),
        "audit_failures_only": bool(values.get("audit_failures_only", False)),
        "forward_logs": bool(values.get("forward_logs", False)),
        "log_min_level": level,
    }


def _require_confirmation(values: dict[str, Any], confirm: bool) -> None:
    if values["protocol"] == PROTOCOL_TLS or confirm:
        return
    raise ConfirmationRequiredError(
        "This change needs confirmation; repeat it with confirm=true",
        details={
            "warnings": [
                f"{values['protocol'].upper()} syslog is not encrypted: audit events "
                "(user names, IP addresses, file names) cross the network in clear text "
                "and can be read or forged on the way. Prefer TLS."
            ]
        },
    )


def _name_taken(session: Session, name: str, exclude_id: int | None = None) -> bool:
    stmt = select(SyslogDestination.id).where(func.lower(SyslogDestination.name) == name.lower())
    if exclude_id is not None:
        stmt = stmt.where(SyslogDestination.id != exclude_id)
    return session.execute(stmt).first() is not None


def _audit_details(row: SyslogDestination) -> dict[str, Any]:
    return {
        "name": row.name,
        "host": row.host,
        "port": row.port,
        "protocol": row.protocol,
        "enabled": row.enabled,
        "forward_audit": row.forward_audit,
        "forward_logs": row.forward_logs,
    }


def create_destination(
    session: Session,
    *,
    values: dict[str, Any],
    actor: User,
    context: dict[str, str | None] | None = None,
) -> SyslogDestination:
    if session.execute(select(func.count(SyslogDestination.id))).scalar_one() >= MAX_DESTINATIONS:
        raise ConflictError(f"At most {MAX_DESTINATIONS} syslog destinations can be configured")
    clean = _validated(values)
    _require_confirmation(clean, bool(values.get("confirm")))
    if _name_taken(session, clean["name"]):
        raise ConflictError("A syslog destination with this name already exists")
    categories = clean.pop("audit_categories")
    row = SyslogDestination(**clean)
    row.audit_categories = categories
    # A new destination starts at the current end of the log unless the
    # administrator asks for the history to be sent as well.
    if not values.get("backfill"):
        row.last_audit_id = int(session.execute(select(func.max(AuditEvent.id))).scalar_one() or 0)
    session.add(row)
    session.flush()
    audit.record_audit_event(
        session,
        event=audit.SYSLOG_DESTINATION_CREATED,
        category=audit.CATEGORY_SYSLOG,
        actor=actor,
        target_type="syslog_destination",
        target_id=row.id,
        details={**_audit_details(row), "backfill": bool(values.get("backfill"))},
        commit=False,
        **audit.request_context(context),
    )
    _bump_version(session)
    return row


def update_destination(
    session: Session,
    row: SyslogDestination,
    *,
    changes: dict[str, Any],
    actor: User,
    context: dict[str, str | None] | None = None,
) -> SyslogDestination:
    confirm = bool(changes.pop("confirm", False))
    current = {name: getattr(row, name) for name in _CONFIG_FIELDS}
    merged = {**current, **{k: v for k, v in changes.items() if k in _CONFIG_FIELDS}}
    if "protocol" in changes and "port" not in changes and changes["protocol"] != row.protocol:
        merged["port"] = None  # back to the new protocol's default
    if merged["protocol"] != PROTOCOL_TLS and "ca_pem" not in changes:
        merged["ca_pem"] = None
    clean = _validated(merged)
    if clean["protocol"] != PROTOCOL_TLS and (
        clean["protocol"] != row.protocol
        or clean["host"] != row.host
        or (clean["enabled"] and not row.enabled)
    ):
        _require_confirmation(clean, confirm)
    if _name_taken(session, clean["name"], exclude_id=row.id):
        raise ConflictError("A syslog destination with this name already exists")
    changed = sorted(name for name in _CONFIG_FIELDS if clean[name] != current[name])
    categories = clean.pop("audit_categories")
    for name, value in clean.items():
        setattr(row, name, value)
    row.audit_categories = categories
    if changed:
        row.updated_at = utcnow_naive()
        # A changed collector gets a fresh chance instead of the old backoff.
        row.next_attempt_at = None
    audit.record_audit_event(
        session,
        event=audit.SYSLOG_DESTINATION_UPDATED,
        category=audit.CATEGORY_SYSLOG,
        actor=actor,
        target_type="syslog_destination",
        target_id=row.id,
        details={**_audit_details(row), "changed": changed},
        commit=False,
        **audit.request_context(context),
    )
    _bump_version(session)
    return row


def delete_destination(
    session: Session,
    row: SyslogDestination,
    *,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> None:
    details = _audit_details(row)
    target_id = row.id
    session.delete(row)
    audit.record_audit_event(
        session,
        event=audit.SYSLOG_DESTINATION_DELETED,
        category=audit.CATEGORY_SYSLOG,
        actor=actor,
        target_type="syslog_destination",
        target_id=target_id,
        details=details,
        commit=False,
        **audit.request_context(context),
    )
    _bump_version(session)


def message_identity(settings: Settings) -> dict[str, str]:
    """HOSTNAME / APP-NAME / PROCID used in every message from this process."""
    return {
        "hostname": socket.gethostname(),
        "app_name": settings.syslog_identifier,
        "procid": str(os.getpid()),
    }


def send_test(
    session: Session,
    row: SyslogDestination,
    *,
    settings: Settings,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> tuple[bool, str]:
    """Send one test message synchronously and audit the outcome."""
    payload = syslog_format.render(
        syslog_format.probe_message(row.name),
        fmt=row.format,
        facility=row.facility,
        **message_identity(settings),
    )
    sender = SyslogSender(
        host=row.host,
        port=row.port,
        protocol=row.protocol,
        ca_pem=row.ca_pem,
        timeout=settings.syslog_timeout_seconds,
    )
    try:
        sender.send([frame(payload, protocol=row.protocol, fmt=row.format)])
        ok = True
        message = (
            "Sent. UDP gives no delivery confirmation; check the collector."
            if row.protocol == "udp"
            else "Delivered to the collector's connection."
        )
    except SyslogTransportError as exc:
        ok, message = False, str(exc)
    finally:
        sender.close()
    audit.record_audit_event(
        session,
        event=audit.SYSLOG_DESTINATION_TESTED,
        category=audit.CATEGORY_SYSLOG,
        outcome=audit.OUTCOME_SUCCESS if ok else audit.OUTCOME_FAILURE,
        actor=actor,
        target_type="syslog_destination",
        target_id=row.id,
        details={"name": row.name, "message": message},
        **audit.request_context(context),
    )
    return ok, message
