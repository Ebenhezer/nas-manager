"""Forward audit events to the syslog destinations (worker only).

Each destination keeps a cursor (``last_audit_id``). The worker sends the
events above it in id order and moves the cursor after every batch the
transport accepted, so events written while a collector is down are sent
when it comes back (at-least-once: a batch interrupted by an error is sent
again). Events a destination filters out still move its cursor.

Failures back off exponentially (5 s, 10 s, ... capped at 5 minutes). Only
the transitions are audited (``syslog.forward_failed`` on the first failure,
``syslog.forward_recovered`` on the next success), so an unreachable
collector cannot flood the log.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow_naive
from app.models import AuditEvent, SyslogDestination
from app.services import audit
from app.services.syslog import format as syslog_format
from app.services.syslog.destinations import message_identity
from app.services.syslog.transport import SyslogSender, SyslogTransportError, frame

logger = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 300
_MAX_BATCHES_PER_PASS = 20


def backoff_seconds(failures: int) -> int:
    return int(min(MAX_BACKOFF_SECONDS, 5 * 2 ** max(0, failures - 1)))


def matches(destination: SyslogDestination, row: AuditEvent) -> bool:
    if destination.audit_failures_only and row.outcome == audit.OUTCOME_SUCCESS:
        return False
    categories = destination.audit_categories
    return not categories or row.category in categories


def forward_destination(
    session: Session,
    destination: SyslogDestination,
    settings: Settings,
    *,
    now: datetime | None = None,
    stop: threading.Event | None = None,
) -> int:
    """Send pending events to one destination; returns the number sent."""
    now = now or utcnow_naive()
    identity = message_identity(settings)
    sender = SyslogSender(
        host=destination.host,
        port=destination.port,
        protocol=destination.protocol,
        ca_pem=destination.ca_pem,
        timeout=settings.syslog_timeout_seconds,
    )
    sent = 0
    try:
        for _ in range(_MAX_BATCHES_PER_PASS):
            if stop is not None and stop.is_set():
                break
            rows = list(
                session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.id > destination.last_audit_id)
                    .order_by(AuditEvent.id)
                    .limit(settings.syslog_batch_size)
                ).scalars()
            )
            if not rows:
                break
            frames = [
                frame(
                    syslog_format.render(
                        syslog_format.audit_message(row),
                        fmt=destination.format,
                        facility=destination.facility,
                        **identity,
                    ),
                    protocol=destination.protocol,
                    fmt=destination.format,
                )
                for row in rows
                if matches(destination, row)
            ]
            if frames:
                sender.send(frames)
            destination.last_audit_id = rows[-1].id
            destination.last_success_at = now
            session.commit()
            sent += len(frames)
            if len(rows) < settings.syslog_batch_size:
                break
        if destination.consecutive_failures and not sent:
            # Nothing matched: still prove the collector is reachable again
            # before reporting a recovery.
            sender.send(())
    except SyslogTransportError as exc:
        _record_failure(session, destination, str(exc), now)
        return sent
    finally:
        sender.close()
    if destination.consecutive_failures:
        _record_recovery(session, destination, now)
    return sent


def _record_failure(
    session: Session, destination: SyslogDestination, message: str, now: datetime
) -> None:
    session.rollback()
    destination.consecutive_failures += 1
    destination.last_error = message[:1000]
    destination.last_error_at = now
    destination.next_attempt_at = now + timedelta(
        seconds=backoff_seconds(destination.consecutive_failures)
    )
    first = destination.consecutive_failures == 1
    if first:
        logger.warning(
            "syslog forwarding failed",
            extra={"destination": destination.name, "error": destination.last_error},
        )
        audit.record_audit_event(
            session,
            event=audit.SYSLOG_FORWARD_FAILED,
            category=audit.CATEGORY_SYSLOG,
            outcome=audit.OUTCOME_FAILURE,
            actor_username="system",
            target_type="syslog_destination",
            target_id=destination.id,
            details={"name": destination.name, "error": destination.last_error},
            commit=False,
        )
    session.commit()


def _record_recovery(session: Session, destination: SyslogDestination, now: datetime) -> None:
    failures = destination.consecutive_failures
    destination.consecutive_failures = 0
    destination.next_attempt_at = None
    logger.info(
        "syslog forwarding recovered",
        extra={"destination": destination.name, "failures": failures},
    )
    audit.record_audit_event(
        session,
        event=audit.SYSLOG_FORWARD_RECOVERED,
        category=audit.CATEGORY_SYSLOG,
        actor_username="system",
        target_type="syslog_destination",
        target_id=destination.id,
        details={"name": destination.name, "failed_attempts": failures},
        commit=False,
    )
    session.commit()


def forward_due(
    session: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
    stop: threading.Event | None = None,
) -> dict[str, int]:
    """One forwarding pass over every enabled destination that is due."""
    now = now or utcnow_naive()
    destinations = list(
        session.execute(
            select(SyslogDestination)
            .where(SyslogDestination.enabled.is_(True), SyslogDestination.forward_audit.is_(True))
            .order_by(SyslogDestination.id)
        ).scalars()
    )
    result = {"sent": 0, "failed": 0, "skipped": 0}
    for destination in destinations:
        if destination.next_attempt_at is not None and destination.next_attempt_at > now:
            result["skipped"] += 1
            continue
        result["sent"] += forward_destination(session, destination, settings, now=now, stop=stop)
        if destination.consecutive_failures:
            result["failed"] += 1
    return result


def destination_status(session: Session, destination: SyslogDestination) -> dict[str, Any]:
    head = session.execute(
        select(AuditEvent.id).order_by(AuditEvent.id.desc()).limit(1)
    ).scalar_one_or_none()
    lag = max(0, int(head or 0) - destination.last_audit_id) if destination.forward_audit else 0
    return {
        "lag": lag,
        "failing": destination.consecutive_failures > 0,
    }
