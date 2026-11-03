"""Alert rules, alert state and the notification outbox.

``evaluate`` (run by the worker after each sample/check pass) turns the
engine's transitions into rows: new pending/firing alerts, firing and
resolution timestamps, and one queued ``alert_notifications`` row per
eligible channel when an alert fires or resolves. Delivery happens later in
``notify.deliver_due`` so a slow mail server never holds up evaluation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.db.base import utcnow_naive
from app.models import User
from app.models.monitoring import (
    ALERT_FIRING,
    ALERT_OPEN_STATES,
    ALERT_PENDING,
    ALERT_RESOLVED,
    ALERT_STATES,
    COMPARISONS,
    EVENT_FIRING,
    EVENT_RESOLVED,
    METRIC_LOAD,
    METRICS,
    NOTIFY_QUEUED,
    SEVERITIES,
    SEVERITY_CRITICAL,
    THRESHOLD_METRICS,
    Alert,
    AlertChannel,
    AlertNotification,
    AlertRule,
    MetricSample,
)
from app.services import audit
from app.services.monitoring import checks as checks_service
from app.services.monitoring import rules as engine

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {"warning": 1, "critical": 2}


def severity_rank(value: str) -> int:
    return _SEVERITY_RANK.get(value, 0)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def list_rules(session: Session) -> list[AlertRule]:
    return list(
        session.execute(select(AlertRule).order_by(AlertRule.metric, AlertRule.name)).scalars()
    )


def get_rule_or_404(session: Session, rule_id: int) -> AlertRule:
    row = session.get(AlertRule, rule_id)
    if row is None:
        raise NotFoundError("Alert rule not found", details={"id": rule_id})
    return row


def open_alert_counts(session: Session) -> dict[int, int]:
    return {
        int(rule_id): int(count)
        for rule_id, count in session.execute(
            select(Alert.rule_id, func.count(Alert.id))
            .where(Alert.state.in_(ALERT_OPEN_STATES))
            .group_by(Alert.rule_id)
        ).all()
    }


def _clean_name(name: str) -> str:
    value = name.strip()
    if not value or len(value) > 64 or any(ord(c) < 32 for c in value):
        raise BadRequestError("Rule name must be 1-64 printable characters")
    return value


def _clean_target(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > 255 or any(ord(c) < 32 for c in value):
        raise BadRequestError("Rule target must be a single line of at most 255 characters")
    return value


def _validate_rule(row: AlertRule) -> None:
    if row.metric not in METRICS:
        raise BadRequestError(
            "Unknown metric", details={"metric": row.metric, "allowed": list(METRICS)}
        )
    if row.severity not in SEVERITIES:
        raise BadRequestError("Unknown severity", details={"allowed": list(SEVERITIES)})
    if row.comparison not in COMPARISONS:
        raise BadRequestError("Unknown comparison", details={"allowed": list(COMPARISONS)})
    if row.metric in THRESHOLD_METRICS:
        if row.threshold is None:
            raise BadRequestError("This metric needs a threshold")
        upper = 1000.0 if row.metric == METRIC_LOAD else 100.0
        if not 0 <= row.threshold <= upper:
            raise BadRequestError(f"Threshold must be between 0 and {upper:g}")
    else:
        # Condition metrics are true/false; a threshold would be meaningless.
        row.threshold = None
        row.comparison = "gt"


def _ensure_unique(session: Session, name: str, *, exclude_id: int | None = None) -> None:
    stmt = select(AlertRule.id).where(func.lower(AlertRule.name) == name.lower())
    if exclude_id is not None:
        stmt = stmt.where(AlertRule.id != exclude_id)
    if session.execute(stmt).first() is not None:
        raise ConflictError("An alert rule with that name already exists")


def _rule_details(row: AlertRule) -> dict[str, Any]:
    return {
        "name": row.name,
        "metric": row.metric,
        "target": row.target,
        "comparison": row.comparison,
        "threshold": row.threshold,
        "duration_seconds": row.duration_seconds,
        "severity": row.severity,
        "enabled": row.enabled,
        "notify": row.notify,
    }


def create_rule(
    session: Session,
    *,
    values: dict[str, Any],
    actor: User,
    context: dict[str, str | None] | None = None,
) -> AlertRule:
    name = _clean_name(values["name"])
    _ensure_unique(session, name)
    row = AlertRule(
        name=name,
        metric=values["metric"],
        target=_clean_target(values.get("target")),
        comparison=values.get("comparison") or "gt",
        threshold=values.get("threshold"),
        duration_seconds=int(values.get("duration_seconds") or 0),
        severity=values.get("severity") or "warning",
        enabled=bool(values.get("enabled", True)),
        notify=bool(values.get("notify", True)),
        builtin=False,
    )
    _validate_rule(row)
    session.add(row)
    session.flush()
    audit.record_audit_event(
        session,
        event=audit.ALERT_RULE_CREATED,
        category=audit.CATEGORY_MONITORING,
        actor=actor,
        target_type="alert_rule",
        target_id=row.id,
        details=_rule_details(row),
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return row


def update_rule(
    session: Session,
    row: AlertRule,
    *,
    changes: dict[str, Any],
    actor: User,
    context: dict[str, str | None] | None = None,
) -> AlertRule:
    """Apply the provided fields. The metric cannot change (create a new rule)."""
    before = _rule_details(row)
    if "name" in changes and changes["name"] is not None:
        name = _clean_name(changes["name"])
        if name != row.name:
            _ensure_unique(session, name, exclude_id=row.id)
            row.name = name
    if "target" in changes:
        row.target = _clean_target(changes["target"])
    for field in ("comparison", "threshold", "duration_seconds", "severity", "enabled", "notify"):
        if field in changes and (changes[field] is not None or field == "threshold"):
            setattr(row, field, changes[field])
    _validate_rule(row)
    after = _rule_details(row)
    changed = sorted(key for key in after if after[key] != before[key])
    if not row.enabled:
        # A disabled rule stops watching: close its alerts without notifying.
        # (A changed threshold/target needs nothing here: the next evaluation
        # resolves what no longer matches, and notifies.)
        _close_rule_alerts(session, row.id, utcnow_naive())
    audit.record_audit_event(
        session,
        event=audit.ALERT_RULE_UPDATED,
        category=audit.CATEGORY_MONITORING,
        actor=actor,
        target_type="alert_rule",
        target_id=row.id,
        details={"name": row.name, "changed": changed, **after},
        **audit.request_context(context),
        commit=False,
    )
    session.commit()
    return row


def delete_rule(
    session: Session,
    row: AlertRule,
    *,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> None:
    if row.builtin:
        raise ConflictError("Built-in rules cannot be deleted; disable the rule instead")
    audit.record_audit_event(
        session,
        event=audit.ALERT_RULE_DELETED,
        category=audit.CATEGORY_MONITORING,
        actor=actor,
        target_type="alert_rule",
        target_id=row.id,
        details=_rule_details(row),
        **audit.request_context(context),
        commit=False,
    )
    session.delete(row)
    session.commit()


def _close_rule_alerts(session: Session, rule_id: int, now: datetime) -> None:
    for alert in session.execute(
        select(Alert).where(Alert.rule_id == rule_id, Alert.state.in_(ALERT_OPEN_STATES))
    ).scalars():
        if alert.state == ALERT_PENDING:
            session.delete(alert)
        else:
            alert.state = ALERT_RESOLVED
            alert.resolved_at = now


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def get_alert_or_404(session: Session, alert_id: int) -> Alert:
    row = session.get(Alert, alert_id)
    if row is None:
        raise NotFoundError("Alert not found", details={"id": alert_id})
    return row


def query_alerts(
    session: Session,
    *,
    state: str | None = None,
    severity: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[tuple[Alert, AlertRule, str | None]], int]:
    """Alerts with their rule and acknowledging username, newest first.

    ``state`` may be one of the stored states or ``open`` (pending + firing).
    """
    conditions: list[Any] = []
    if state == "open":
        conditions.append(Alert.state.in_(ALERT_OPEN_STATES))
    elif state is not None:
        if state not in ALERT_STATES:
            raise BadRequestError(
                "Unknown alert state", details={"allowed": [*ALERT_STATES, "open"]}
            )
        conditions.append(Alert.state == state)
    if severity is not None:
        if severity not in SEVERITIES:
            raise BadRequestError("Unknown severity", details={"allowed": list(SEVERITIES)})
        conditions.append(Alert.severity == severity)
    total = int(session.execute(select(func.count(Alert.id)).where(*conditions)).scalar_one())
    rows = session.execute(
        select(Alert, AlertRule, User.username)
        .join(AlertRule, AlertRule.id == Alert.rule_id)
        .outerjoin(User, User.id == Alert.acknowledged_by)
        .where(*conditions)
        .order_by(Alert.last_seen_at.desc(), Alert.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [(alert, rule, username) for alert, rule, username in rows], total


def counts(session: Session) -> dict[str, int]:
    open_rows = session.execute(
        select(Alert.state, Alert.severity, Alert.acknowledged_at).where(
            Alert.state.in_(ALERT_OPEN_STATES)
        )
    ).all()
    firing = [row for row in open_rows if row.state == ALERT_FIRING]
    return {
        "firing": len(firing),
        "pending": len(open_rows) - len(firing),
        "critical": sum(1 for row in firing if row.severity == SEVERITY_CRITICAL),
        "warning": sum(1 for row in firing if row.severity != SEVERITY_CRITICAL),
        "unacknowledged": sum(1 for row in firing if row.acknowledged_at is None),
    }


def acknowledge(
    session: Session,
    alert: Alert,
    *,
    actor: User,
    context: dict[str, str | None] | None = None,
) -> Alert:
    if alert.state != ALERT_FIRING:
        raise ConflictError("Only firing alerts can be acknowledged")
    if alert.acknowledged_at is None:
        alert.acknowledged_at = utcnow_naive()
        alert.acknowledged_by = actor.id
        audit.record_audit_event(
            session,
            event=audit.ALERT_ACKNOWLEDGED,
            category=audit.CATEGORY_MONITORING,
            actor=actor,
            target_type="alert",
            target_id=alert.id,
            details={"message": alert.message, "severity": alert.severity},
            **audit.request_context(context),
            commit=False,
        )
        session.commit()
    return alert


# ---------------------------------------------------------------------------
# Evaluation (worker)
# ---------------------------------------------------------------------------


def _spec(row: AlertRule) -> engine.RuleSpec:
    return engine.RuleSpec(
        id=row.id,
        name=row.name,
        metric=row.metric,
        target=row.target,
        comparison=row.comparison,
        threshold=row.threshold,
        duration_seconds=row.duration_seconds,
        severity=row.severity,
    )


def observations(session: Session, sample: MetricSample | None) -> list[engine.Observation]:
    found: list[engine.Observation] = []
    if sample is not None:
        found += engine.host_observations(
            cpu_percent=sample.cpu_percent,
            memory_percent=sample.memory_percent,
            swap_percent=sample.swap_percent,
            load_per_core=sample.load_per_core,
            disks=sample.disks,
        )
    found += engine.check_observations(
        (row.kind, row.target, row.status, row.details)
        for row in checks_service.list_checks(session)
    )
    return found


def queue_notifications(session: Session, alert: Alert, event: str, now: datetime) -> int:
    queued = 0
    for channel in session.execute(
        select(AlertChannel).where(AlertChannel.enabled.is_(True))
    ).scalars():
        if severity_rank(alert.severity) < severity_rank(channel.min_severity):
            continue
        if event == EVENT_RESOLVED and not channel.send_resolved:
            continue
        session.add(
            AlertNotification(
                alert_id=alert.id,
                channel_id=channel.id,
                event=event,
                status=NOTIFY_QUEUED,
                attempts=0,
                next_attempt_at=now,
            )
        )
        queued += 1
    return queued


def _apply(session: Session, rule: AlertRule, transition: engine.Transition, now: datetime) -> None:
    if transition.action in (engine.ACTION_OPEN, engine.ACTION_OPEN_FIRING):
        firing = transition.action == engine.ACTION_OPEN_FIRING
        alert = Alert(
            rule_id=rule.id,
            target=transition.target,
            state=ALERT_FIRING if firing else ALERT_PENDING,
            severity=rule.severity,
            message=transition.message,
            value=transition.value,
            started_at=now,
            firing_at=now if firing else None,
            last_seen_at=now,
        )
        session.add(alert)
        session.flush()
        if firing:
            logger.warning("alert firing", extra={"alert_id": alert.id, "alert": alert.message})
            if rule.notify:
                queue_notifications(session, alert, EVENT_FIRING, now)
        return

    existing = session.get(Alert, transition.alert_id)
    if existing is None:  # pragma: no cover - deleted concurrently
        return
    alert = existing
    if transition.action == engine.ACTION_DROP:
        session.delete(alert)
        return
    if transition.message:
        alert.message = transition.message
    if transition.value is not None:
        alert.value = transition.value
    if transition.action == engine.ACTION_RESOLVE:
        alert.state = ALERT_RESOLVED
        alert.resolved_at = now
        logger.info("alert resolved", extra={"alert_id": alert.id, "alert": alert.message})
        if rule.notify:
            queue_notifications(session, alert, EVENT_RESOLVED, now)
        return
    alert.last_seen_at = now
    alert.severity = rule.severity
    if transition.action == engine.ACTION_FIRE:
        alert.state = ALERT_FIRING
        alert.firing_at = now
        logger.warning("alert firing", extra={"alert_id": alert.id, "alert": alert.message})
        if rule.notify:
            queue_notifications(session, alert, EVENT_FIRING, now)


def evaluate(
    session: Session, settings: Settings, now: datetime, sample: MetricSample | None
) -> list[engine.Transition]:
    """Evaluate every enabled rule against the latest sample and checks."""
    found = observations(session, sample)
    applied: list[engine.Transition] = []
    for rule in session.execute(select(AlertRule).where(AlertRule.enabled.is_(True))).scalars():
        open_rows = session.execute(
            select(Alert).where(Alert.rule_id == rule.id, Alert.state.in_(ALERT_OPEN_STATES))
        ).scalars()
        open_alerts = [
            engine.OpenAlert(
                id=row.id,
                target=row.target,
                state=row.state,
                started_at=row.started_at,
                last_seen_at=row.last_seen_at,
            )
            for row in open_rows
        ]
        for transition in engine.evaluate(_spec(rule), found, open_alerts, now):
            _apply(session, rule, transition, now)
            if transition.action != engine.ACTION_TOUCH:
                applied.append(transition)
    session.commit()
    return applied


def prune(session: Session, settings: Settings, now: datetime | None = None) -> int:
    """Delete resolved alerts (and their outbox rows) past the retention."""
    cutoff = (now or utcnow_naive()) - timedelta(days=settings.alert_retention_days)
    old = select(Alert.id).where(Alert.state == ALERT_RESOLVED, Alert.resolved_at < cutoff)
    session.execute(delete(AlertNotification).where(AlertNotification.alert_id.in_(old)))
    result = session.execute(
        delete(Alert).where(Alert.state == ALERT_RESOLVED, Alert.resolved_at < cutoff)
    )
    session.commit()
    return int(getattr(result, "rowcount", 0) or 0)
