"""Alerts, alert rules and notification channels (Phase 10).

Reading needs ``system:read``; acknowledging alerts and changing rules or
channels needs ``system:write``. Creating, updating and testing channels
also needs an interactive session (API tokens are refused), because those
calls set or use stored credentials, like the Phase 7 remote connections.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.v1.deps import (
    CurrentUser,
    request_audit_context,
    require_permission,
    require_session_permission,
)
from app.api.v1.monitoring import alert_summary
from app.api.v1.remotes import get_cipher
from app.core.secrets import CredentialCipher
from app.db.dependencies import get_db
from app.models.monitoring import Alert, AlertChannel, AlertRule
from app.schemas.alerts import (
    AlertChannelCreate,
    AlertChannelOut,
    AlertChannelUpdate,
    AlertOut,
    AlertPage,
    AlertRuleCreate,
    AlertRuleOut,
    AlertRuleUpdate,
    ChannelTestResult,
)
from app.schemas.monitoring import AlertSummary
from app.services.monitoring import alerts, channels, notify

router = APIRouter(prefix="/alerts", tags=["alerts"])
rules_router = APIRouter(prefix="/alert-rules", tags=["alerts"])
channels_router = APIRouter(prefix="/alert-channels", tags=["alerts"])


def _alert_out(alert: Alert, rule: AlertRule, acknowledged_by: str | None) -> AlertOut:
    return AlertOut(
        id=alert.id,
        rule_id=rule.id,
        rule_name=rule.name,
        metric=rule.metric,
        target=alert.target,
        state=alert.state,
        severity=alert.severity,
        message=alert.message,
        value=alert.value,
        started_at=alert.started_at,
        firing_at=alert.firing_at,
        last_seen_at=alert.last_seen_at,
        resolved_at=alert.resolved_at,
        acknowledged_at=alert.acknowledged_at,
        acknowledged_by=acknowledged_by,
    )


def _rule_out(row: AlertRule, open_counts: dict[int, int]) -> AlertRuleOut:
    return AlertRuleOut(
        id=row.id,
        name=row.name,
        metric=row.metric,
        target=row.target,
        comparison=row.comparison,
        threshold=row.threshold,
        duration_seconds=row.duration_seconds,
        severity=row.severity,
        enabled=row.enabled,
        notify=row.notify,
        builtin=row.builtin,
        open_alerts=open_counts.get(row.id, 0),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _channel_out(row: AlertChannel, cipher: CredentialCipher) -> AlertChannelOut:
    secrets_set, readable = channels.secret_status(row, cipher)
    return AlertChannelOut(
        id=row.id,
        name=row.name,
        kind=row.kind,
        config=row.config,
        secrets_set=secrets_set,
        credentials_readable=readable,
        min_severity=row.min_severity,
        allow_insecure=row.allow_insecure,
        send_resolved=row.send_resolved,
        enabled=row.enabled,
        last_test_at=row.last_test_at,
        last_test_ok=row.last_test_ok,
        last_test_message=row.last_test_message,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


@router.get("", response_model=AlertPage)
def list_alerts(
    state: str | None = Query(default=None, max_length=16),
    severity: str | None = Query(default=None, max_length=16),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current: CurrentUser = Depends(require_permission("system:read")),
    db: Session = Depends(get_db),
) -> AlertPage:
    rows, total = alerts.query_alerts(
        db, state=state, severity=severity, limit=limit, offset=offset
    )
    return AlertPage(
        items=[_alert_out(alert, rule, username) for alert, rule, username in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/summary", response_model=AlertSummary)
def summary(
    request: Request,
    current: CurrentUser = Depends(require_permission("system:read")),
    db: Session = Depends(get_db),
) -> AlertSummary:
    return alert_summary(db, request.app.state.settings)


@router.post("/{alert_id}/acknowledge", response_model=AlertOut)
def acknowledge(
    alert_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("system:write")),
    db: Session = Depends(get_db),
) -> AlertOut:
    alert = alerts.acknowledge(
        db,
        alerts.get_alert_or_404(db, alert_id),
        actor=current.user,
        context=request_audit_context(request),
    )
    rule = alerts.get_rule_or_404(db, alert.rule_id)
    return _alert_out(alert, rule, current.user.username)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@rules_router.get("", response_model=list[AlertRuleOut])
def list_rules(
    current: CurrentUser = Depends(require_permission("system:read")),
    db: Session = Depends(get_db),
) -> list[AlertRuleOut]:
    open_counts = alerts.open_alert_counts(db)
    return [_rule_out(row, open_counts) for row in alerts.list_rules(db)]


@rules_router.post("", response_model=AlertRuleOut, status_code=201)
def create_rule(
    payload: AlertRuleCreate,
    request: Request,
    current: CurrentUser = Depends(require_permission("system:write")),
    db: Session = Depends(get_db),
) -> AlertRuleOut:
    row = alerts.create_rule(
        db,
        values=payload.model_dump(),
        actor=current.user,
        context=request_audit_context(request),
    )
    return _rule_out(row, {})


@rules_router.patch("/{rule_id}", response_model=AlertRuleOut)
def update_rule(
    rule_id: int,
    payload: AlertRuleUpdate,
    request: Request,
    current: CurrentUser = Depends(require_permission("system:write")),
    db: Session = Depends(get_db),
) -> AlertRuleOut:
    row = alerts.update_rule(
        db,
        alerts.get_rule_or_404(db, rule_id),
        changes=payload.model_dump(exclude_unset=True),
        actor=current.user,
        context=request_audit_context(request),
    )
    return _rule_out(row, alerts.open_alert_counts(db))


@rules_router.delete("/{rule_id}", status_code=204)
def delete_rule(
    rule_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("system:write")),
    db: Session = Depends(get_db),
) -> None:
    alerts.delete_rule(
        db,
        alerts.get_rule_or_404(db, rule_id),
        actor=current.user,
        context=request_audit_context(request),
    )


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


@channels_router.get("", response_model=list[AlertChannelOut])
def list_channels(
    current: CurrentUser = Depends(require_permission("system:read")),
    db: Session = Depends(get_db),
    cipher: CredentialCipher = Depends(get_cipher),
) -> list[AlertChannelOut]:
    return [_channel_out(row, cipher) for row in channels.list_channels(db)]


@channels_router.post("", response_model=AlertChannelOut, status_code=201)
def create_channel(
    payload: AlertChannelCreate,
    request: Request,
    current: CurrentUser = Depends(require_session_permission("system:write")),
    db: Session = Depends(get_db),
    cipher: CredentialCipher = Depends(get_cipher),
) -> AlertChannelOut:
    row = channels.create_channel(
        db,
        values=payload.model_dump(),
        cipher=cipher,
        actor=current.user,
        context=request_audit_context(request),
    )
    return _channel_out(row, cipher)


@channels_router.patch("/{channel_id}", response_model=AlertChannelOut)
def update_channel(
    channel_id: int,
    payload: AlertChannelUpdate,
    request: Request,
    current: CurrentUser = Depends(require_session_permission("system:write")),
    db: Session = Depends(get_db),
    cipher: CredentialCipher = Depends(get_cipher),
) -> AlertChannelOut:
    row = channels.update_channel(
        db,
        channels.get_channel_or_404(db, channel_id),
        changes=payload.model_dump(exclude_unset=True),
        cipher=cipher,
        actor=current.user,
        context=request_audit_context(request),
    )
    return _channel_out(row, cipher)


@channels_router.delete("/{channel_id}", status_code=204)
def delete_channel(
    channel_id: int,
    request: Request,
    current: CurrentUser = Depends(require_permission("system:write")),
    db: Session = Depends(get_db),
) -> None:
    channels.delete_channel(
        db,
        channels.get_channel_or_404(db, channel_id),
        actor=current.user,
        context=request_audit_context(request),
    )


@channels_router.post("/{channel_id}/test", response_model=ChannelTestResult)
def test_channel(
    channel_id: int,
    request: Request,
    current: CurrentUser = Depends(require_session_permission("system:write")),
    db: Session = Depends(get_db),
    cipher: CredentialCipher = Depends(get_cipher),
) -> ChannelTestResult:
    ok, message = notify.send_test(
        db,
        channels.get_channel_or_404(db, channel_id),
        cipher=cipher,
        settings=request.app.state.settings,
        actor=current.user,
        context=request_audit_context(request),
    )
    return ChannelTestResult(ok=ok, message=message)
