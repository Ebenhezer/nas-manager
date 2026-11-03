"""Notification delivery: SMTP email and webhook POSTs, through an outbox.

Email: STARTTLS (default) or implicit TLS with certificate verification;
plain SMTP only on channels flagged ``allow_insecure``. Messages are built
with ``EmailMessage`` from validated single-line addresses, so header
injection is impossible.

Webhook: a JSON ``POST`` (HTTPS unless ``allow_insecure``), never following
redirects, with an optional ``X-NAS-Signature: sha256=<hex HMAC of the
body>`` and an optional ``Authorization`` header value from the secrets.

The worker calls ``deliver_due``: each queued row is attempted; failures
back off exponentially (1, 2, 4 ... minutes, at most an hour) until
``NAS_ALERT_NOTIFY_MAX_ATTEMPTS``, after which the row is marked failed and
audited.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import smtplib
import socket
import ssl
import threading
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

import httpx2
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__
from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.core.secrets import CredentialCipher
from app.db.base import utcnow_naive
from app.models import User
from app.models.monitoring import (
    CHANNEL_EMAIL,
    EVENT_FIRING,
    EVENT_RESOLVED,
    EVENT_TEST,
    NOTIFY_FAILED,
    NOTIFY_QUEUED,
    NOTIFY_SENT,
    Alert,
    AlertChannel,
    AlertNotification,
    AlertRule,
)
from app.services import audit
from app.services.monitoring import channels

logger = logging.getLogger(__name__)

# Tests replace this with an ``httpx2.MockTransport``.
WEBHOOK_TRANSPORT: httpx2.BaseTransport | None = None

_MAX_BACKOFF = timedelta(hours=1)
_BATCH = 25


class NotificationError(Exception):
    """A delivery attempt failed (message is safe to store and show)."""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value is not None else None


def render(
    alert: Alert | None, rule: AlertRule | None, event: str, *, hostname: str
) -> tuple[str, str, dict[str, Any]]:
    """``(subject, plain-text body, JSON payload)`` for one event."""
    if event == EVENT_TEST or alert is None or rule is None:
        subject = f"[NAS Manager] Test notification from {hostname}"
        body = (
            f"This is a test notification from NAS Manager on {hostname}.\n"
            "If you can read it, the channel works.\n"
        )
        payload: dict[str, Any] = {
            "event": EVENT_TEST,
            "host": hostname,
            "message": "Test notification",
            "source": "nas-manager",
            "version": __version__,
        }
        return subject, body, payload
    state = "RESOLVED" if event == EVENT_RESOLVED else alert.severity.upper()
    subject = f"[NAS Manager] {state}: {alert.message}"[:250]
    lines = [
        f"{'Resolved' if event == EVENT_RESOLVED else 'Firing'}: {alert.message}",
        "",
        f"Host:      {hostname}",
        f"Rule:      {rule.name}",
        f"Severity:  {alert.severity}",
        f"Started:   {_iso(alert.started_at)}",
    ]
    if alert.firing_at is not None:
        lines.append(f"Firing:    {_iso(alert.firing_at)}")
    if event == EVENT_RESOLVED and alert.resolved_at is not None:
        lines.append(f"Resolved:  {_iso(alert.resolved_at)}")
    payload = {
        "event": event,
        "host": hostname,
        "alert": {
            "id": alert.id,
            "rule": rule.name,
            "metric": rule.metric,
            "target": alert.target,
            "severity": alert.severity,
            "state": alert.state,
            "message": alert.message,
            "value": alert.value,
            "started_at": _iso(alert.started_at),
            "firing_at": _iso(alert.firing_at),
            "resolved_at": _iso(alert.resolved_at),
        },
        "source": "nas-manager",
        "version": __version__,
    }
    return subject, "\n".join(lines) + "\n", payload


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


def send_email(
    config: dict[str, Any],
    secrets: dict[str, Any],
    *,
    allow_insecure: bool,
    subject: str,
    body: str,
    timeout: int,
) -> None:
    security = config.get("security", "starttls")
    if security == "none" and not allow_insecure:
        raise NotificationError("plain SMTP refused (allow_insecure is off)")
    message = EmailMessage()
    message["Subject"] = " ".join(subject.split())
    message["From"] = config["from_address"]
    message["To"] = ", ".join(config["to"])
    message["Date"] = formatdate(localtime=False, usegmt=True)
    message["Message-ID"] = make_msgid(domain=config["from_address"].rsplit("@", 1)[-1])
    message.set_content(body)
    context = ssl.create_default_context()
    host, port = config["host"], int(config["port"])
    try:
        client: smtplib.SMTP
        if security == "tls":
            client = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
        with client:
            client.ehlo()
            if security == "starttls":
                client.starttls(context=context)
                client.ehlo()
            username, password = config.get("username"), secrets.get("password")
            if username:
                if not password:
                    raise NotificationError("the SMTP username is set but no password is stored")
                client.login(username, password)
            client.send_message(message)
    except NotificationError:
        raise
    except smtplib.SMTPAuthenticationError as exc:
        raise NotificationError(f"SMTP authentication failed ({exc.smtp_code})") from exc
    except smtplib.SMTPException as exc:
        raise NotificationError(f"SMTP error: {exc}"[:500]) from exc
    except (OSError, ssl.SSLError) as exc:
        raise NotificationError(f"cannot reach {host}:{port}: {exc}"[:500]) from exc


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def send_webhook(
    config: dict[str, Any],
    secrets: dict[str, Any],
    *,
    allow_insecure: bool,
    payload: dict[str, Any],
    timeout: int,
    transport: httpx2.BaseTransport | None = None,
) -> None:
    url = str(config["url"])
    if url.startswith("http://") and not allow_insecure:
        raise NotificationError("plain HTTP refused (allow_insecure is off)")
    body = json.dumps(payload, sort_keys=True).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"nas-manager/{__version__}",
    }
    if secrets.get("signing_secret"):
        headers["X-NAS-Signature"] = sign(str(secrets["signing_secret"]), body)
    if secrets.get("authorization"):
        headers["Authorization"] = str(secrets["authorization"])
    try:
        with httpx2.Client(
            timeout=timeout,
            follow_redirects=False,
            transport=transport or WEBHOOK_TRANSPORT,
        ) as client:
            response = client.post(url, content=body, headers=headers)
    except httpx2.HTTPError as exc:
        raise NotificationError(f"webhook request failed: {exc}"[:500]) from exc
    if not 200 <= response.status_code < 300:
        raise NotificationError(f"webhook answered HTTP {response.status_code}")


def deliver(
    channel: AlertChannel,
    cipher: CredentialCipher,
    settings: Settings,
    *,
    subject: str,
    body: str,
    payload: dict[str, Any],
) -> None:
    try:
        secrets = channels.stored_secrets(channel, cipher)
    except ConfigurationError as exc:
        raise NotificationError("the channel's secrets cannot be decrypted") from exc
    timeout = settings.alert_notify_timeout_seconds
    if channel.kind == CHANNEL_EMAIL:
        send_email(
            channel.config,
            secrets,
            allow_insecure=channel.allow_insecure,
            subject=subject,
            body=body,
            timeout=timeout,
        )
    else:
        send_webhook(
            channel.config,
            secrets,
            allow_insecure=channel.allow_insecure,
            payload=payload,
            timeout=timeout,
        )


# ---------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------


def backoff(attempts: int) -> timedelta:
    return min(timedelta(minutes=2 ** max(0, attempts - 1)), _MAX_BACKOFF)


def pending_count(session: Session, now: datetime) -> int:
    return len(
        session.execute(
            select(AlertNotification.id)
            .where(
                AlertNotification.status == NOTIFY_QUEUED,
                AlertNotification.next_attempt_at <= now,
            )
            .limit(_BATCH)
        ).all()
    )


def deliver_due(
    session: Session,
    settings: Settings,
    cipher: CredentialCipher,
    *,
    now: datetime | None = None,
    stop: threading.Event | None = None,
) -> dict[str, int]:
    """Attempt every queued notification whose time has come."""
    now = now or utcnow_naive()
    hostname = socket.gethostname()
    result = {"sent": 0, "retry": 0, "failed": 0}
    rows = list(
        session.execute(
            select(AlertNotification)
            .where(
                AlertNotification.status == NOTIFY_QUEUED,
                AlertNotification.next_attempt_at <= now,
            )
            .order_by(AlertNotification.id)
            .limit(_BATCH)
        ).scalars()
    )
    for row in rows:
        if stop is not None and stop.is_set():
            break
        channel = session.get(AlertChannel, row.channel_id)
        alert = session.get(Alert, row.alert_id)
        rule = session.get(AlertRule, alert.rule_id) if alert is not None else None
        row.attempts += 1
        try:
            if channel is None or alert is None or rule is None:  # pragma: no cover - cascades
                raise NotificationError("the channel or alert no longer exists")
            if not channel.enabled:
                raise NotificationError("the channel is disabled")
            subject, body, payload = render(alert, rule, row.event, hostname=hostname)
            deliver(channel, cipher, settings, subject=subject, body=body, payload=payload)
        except NotificationError as exc:
            row.last_error = str(exc)
            final = row.attempts >= settings.alert_notify_max_attempts or (
                channel is not None and not channel.enabled
            )
            if final:
                row.status = NOTIFY_FAILED
                result["failed"] += 1
                logger.error(
                    "alert notification failed",
                    extra={
                        "notification_id": row.id,
                        "channel_id": row.channel_id,
                        "error": str(exc),
                    },
                )
                audit.record_audit_event(
                    session,
                    event=audit.ALERT_NOTIFICATION_FAILED,
                    category=audit.CATEGORY_MONITORING,
                    outcome=audit.OUTCOME_FAILURE,
                    actor_username="system",
                    target_type="alert",
                    target_id=row.alert_id,
                    details={
                        "channel": channel.name if channel is not None else row.channel_id,
                        "event": row.event,
                        "attempts": row.attempts,
                        "error": str(exc),
                    },
                    commit=False,
                )
            else:
                row.next_attempt_at = now + backoff(row.attempts)
                result["retry"] += 1
                logger.warning(
                    "alert notification will be retried",
                    extra={"notification_id": row.id, "attempts": row.attempts, "error": str(exc)},
                )
        else:
            row.status = NOTIFY_SENT
            row.sent_at = utcnow_naive()
            row.last_error = None
            result["sent"] += 1
        session.commit()
    return result


def send_test(
    session: Session,
    channel: AlertChannel,
    *,
    cipher: CredentialCipher,
    settings: Settings,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> tuple[bool, str]:
    """Send a test message now (from the web process) and record the result."""
    subject, body, payload = render(None, None, EVENT_TEST, hostname=socket.gethostname())
    try:
        deliver(channel, cipher, settings, subject=subject, body=body, payload=payload)
    except NotificationError as exc:
        ok, message = False, str(exc)
    else:
        ok, message = True, "Test notification sent"
    channel.last_test_at = utcnow_naive()
    channel.last_test_ok = ok
    channel.last_test_message = message
    audit.record_audit_event(
        session,
        event=audit.ALERT_CHANNEL_TESTED,
        category=audit.CATEGORY_MONITORING,
        outcome=audit.OUTCOME_SUCCESS if ok else audit.OUTCOME_FAILURE,
        actor=actor,
        target_type="alert_channel",
        target_id=channel.id,
        details={"name": channel.name, "ok": ok, "message": message},
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return ok, message


__all__ = [
    "EVENT_FIRING",
    "EVENT_RESOLVED",
    "NotificationError",
    "deliver_due",
    "render",
    "send_email",
    "send_test",
    "send_webhook",
]
