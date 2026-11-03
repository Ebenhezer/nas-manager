"""Alert persistence, the notification outbox and the email/webhook transports (Phase 10)."""

from __future__ import annotations

import hashlib
import hmac
import json
import smtplib
import ssl
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, ClassVar

import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError
from app.core.secrets import CredentialCipher
from app.db.base import utcnow_naive
from app.db.session import Database
from app.models import AuditEvent, User
from app.models.monitoring import (
    Alert,
    AlertChannel,
    AlertNotification,
    AlertRule,
    MetricSample,
    MonitorCheck,
)
from app.services.monitoring import alerts, channels, notify

PASSWORD = "correct horse battery staple"


@pytest.fixture
def session(db: Database) -> Iterator[Session]:
    with db.session_factory() as s:
        yield s


@pytest.fixture
def admin(session: Session, migrated_settings: Settings) -> User:
    from app.services.bootstrap_service import bootstrap_admin

    return bootstrap_admin(session, username="admin", password=PASSWORD, settings=migrated_settings)


@pytest.fixture
def cipher(migrated_settings: Settings) -> CredentialCipher:
    return CredentialCipher.from_settings(migrated_settings)


@pytest.fixture
def only_rule(session: Session) -> AlertRule:
    """Disable every built-in rule except "Disk almost full" (90 %, 5 min, critical)."""
    rule = None
    for row in session.execute(select(AlertRule)).scalars():
        row.enabled = row.name == "Disk almost full"
        if row.enabled:
            rule = row
    session.commit()
    assert rule is not None
    return rule


def _sample(session: Session, *, disk: float, at: Any = None) -> MetricSample:
    row = MetricSample(
        sampled_at=at or utcnow_naive(),
        cpu_percent=1.0,
        load1=0.1,
        load_per_core=0.05,
        memory_percent=10.0,
        swap_percent=0.0,
        net_bytes_sent=0,
        net_bytes_recv=0,
    )
    row.disks = [{"path": "/srv", "percent": disk, "used": 1, "total": 2}]
    session.add(row)
    session.commit()
    return row


def _webhook(
    session: Session, admin: User, cipher: CredentialCipher, **overrides: Any
) -> AlertChannel:
    values: dict[str, Any] = {
        "name": "hook",
        "kind": "webhook",
        "config": {"url": "https://hooks.example/nas"},
        "secrets": {"signing_secret": "s3cret-signing-key"},
    }
    values.update(overrides)
    return channels.create_channel(session, values=values, cipher=cipher, actor=admin)


def _email(
    session: Session, admin: User, cipher: CredentialCipher, **overrides: Any
) -> AlertChannel:
    values: dict[str, Any] = {
        "name": "mail",
        "kind": "email",
        "config": {
            "host": "smtp.example.org",
            "port": 587,
            "security": "starttls",
            "username": "nas",
            "from_address": "nas@example.org",
            "to": "ops@example.org, admin@example.org",
        },
        "secrets": {"password": "smtp-pass-123"},
    }
    values.update(overrides)
    return channels.create_channel(session, values=values, cipher=cipher, actor=admin)


def _outbox(session: Session) -> list[AlertNotification]:
    session.expire_all()
    return list(session.execute(select(AlertNotification).order_by(AlertNotification.id)).scalars())


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def test_create_rule_validation(session: Session, admin: User) -> None:
    base = {"name": "x", "metric": "cpu_percent", "threshold": 50.0}
    with pytest.raises(BadRequestError, match="Unknown metric"):
        alerts.create_rule(session, values={**base, "metric": "bogus"}, actor=admin)
    with pytest.raises(BadRequestError, match="needs a threshold"):
        alerts.create_rule(session, values={**base, "threshold": None}, actor=admin)
    with pytest.raises(BadRequestError, match="between 0 and 100"):
        alerts.create_rule(session, values={**base, "threshold": 150.0}, actor=admin)
    with pytest.raises(BadRequestError, match="severity"):
        alerts.create_rule(session, values={**base, "severity": "panic"}, actor=admin)
    with pytest.raises(BadRequestError, match="single line"):
        alerts.create_rule(session, values={**base, "target": "a\nb"}, actor=admin)
    with pytest.raises(ConflictError):
        alerts.create_rule(session, values={**base, "name": "disk ALMOST full"}, actor=admin)
    load = alerts.create_rule(
        session, values={"name": "load", "metric": "load_per_core", "threshold": 400.0}, actor=admin
    )
    assert load.threshold == 400.0
    # Condition metrics drop a threshold.
    raid = alerts.create_rule(
        session,
        values={"name": "raid", "metric": "raid_degraded", "threshold": 3.0, "comparison": "lt"},
        actor=admin,
    )
    assert raid.threshold is None and raid.comparison == "gt"


def test_builtin_rules_cannot_be_deleted(
    session: Session, admin: User, only_rule: AlertRule
) -> None:
    with pytest.raises(ConflictError, match="Built-in"):
        alerts.delete_rule(session, only_rule, actor=admin)


def test_rule_changes_are_audited(session: Session, admin: User, only_rule: AlertRule) -> None:
    alerts.update_rule(session, only_rule, changes={"threshold": 95.0}, actor=admin)
    event = session.execute(
        select(AuditEvent).where(AuditEvent.event == "alert_rule.updated")
    ).scalar_one()
    assert event.details is not None
    assert event.details["changed"] == ["threshold"]


# ---------------------------------------------------------------------------
# Evaluation and the outbox
# ---------------------------------------------------------------------------


def test_pending_then_firing_then_resolved_with_notifications(
    session: Session,
    migrated_settings: Settings,
    admin: User,
    cipher: CredentialCipher,
    only_rule: AlertRule,
) -> None:
    hook = _webhook(session, admin, cipher)
    now = utcnow_naive()
    sample = _sample(session, disk=95.0, at=now)
    changed = alerts.evaluate(session, migrated_settings, now, sample)
    assert [t.action for t in changed] == ["open"]
    alert = session.execute(select(Alert)).scalar_one()
    assert alert.state == "pending" and alert.target == "/srv"
    assert _outbox(session) == []

    later = now + timedelta(seconds=300)
    alerts.evaluate(session, migrated_settings, later, _sample(session, disk=96.0, at=later))
    session.refresh(alert)
    assert alert.state == "firing" and alert.firing_at == later and alert.value == 96.0
    [firing] = _outbox(session)
    assert (firing.channel_id, firing.event, firing.status) == (hook.id, "firing", "queued")

    end = later + timedelta(minutes=5)
    alerts.evaluate(session, migrated_settings, end, _sample(session, disk=50.0, at=end))
    session.refresh(alert)
    assert alert.state == "resolved" and alert.resolved_at == end
    assert [n.event for n in _outbox(session)] == ["firing", "resolved"]


def test_dropped_pending_alert_leaves_no_trace(
    session: Session, migrated_settings: Settings, only_rule: AlertRule
) -> None:
    now = utcnow_naive()
    alerts.evaluate(session, migrated_settings, now, _sample(session, disk=95.0, at=now))
    later = now + timedelta(seconds=60)
    alerts.evaluate(session, migrated_settings, later, _sample(session, disk=10.0, at=later))
    assert session.execute(select(Alert)).first() is None


def test_channel_filters(
    session: Session,
    migrated_settings: Settings,
    admin: User,
    cipher: CredentialCipher,
) -> None:
    """min_severity, send_resolved and disabled channels decide what is queued."""
    critical_only = _webhook(session, admin, cipher, name="critical", min_severity="critical")
    no_resolved = _webhook(session, admin, cipher, name="noresolve", send_resolved=False)
    _webhook(session, admin, cipher, name="off", enabled=False)
    for row in session.execute(select(AlertRule)).scalars():
        row.enabled = row.metric == "disk_percent"
        row.duration_seconds = 0
    session.commit()
    now = utcnow_naive()
    # 85 % trips only the 80 % warning rule.
    alerts.evaluate(session, migrated_settings, now, _sample(session, disk=85.0, at=now))
    assert [(n.channel_id, n.event) for n in _outbox(session)] == [(no_resolved.id, "firing")]
    # 95 % also trips the critical rule.
    alerts.evaluate(session, migrated_settings, now, _sample(session, disk=95.0, at=now))
    queued = {(n.channel_id, n.event) for n in _outbox(session)}
    assert (critical_only.id, "firing") in queued
    # Back to normal: the critical-only channel hears about its resolution,
    # the no-resolve channel does not.
    alerts.evaluate(session, migrated_settings, now, _sample(session, disk=10.0, at=now))
    resolved = [(n.channel_id, n.event) for n in _outbox(session) if n.event == "resolved"]
    assert resolved == [(critical_only.id, "resolved")]


def test_silent_rule_queues_nothing(
    session: Session,
    migrated_settings: Settings,
    admin: User,
    cipher: CredentialCipher,
    only_rule: AlertRule,
) -> None:
    _webhook(session, admin, cipher)
    only_rule.notify = False
    only_rule.duration_seconds = 0
    session.commit()
    now = utcnow_naive()
    alerts.evaluate(session, migrated_settings, now, _sample(session, disk=99.0, at=now))
    assert session.execute(select(Alert)).scalar_one().state == "firing"
    assert _outbox(session) == []


def test_checks_drive_condition_alerts(
    session: Session, migrated_settings: Settings, admin: User, cipher: CredentialCipher
) -> None:
    _webhook(session, admin, cipher)
    row = MonitorCheck(key="raid:md0", kind="raid", target="md0", status="critical", summary="x")
    row.details = {"degraded": True, "failed_members": ["sdc1"], "summary": "raid1, degraded"}
    row.checked_at = utcnow_naive()
    session.add(row)
    session.commit()
    alerts.evaluate(session, migrated_settings, utcnow_naive(), None)
    fired = {a.message for a in session.execute(select(Alert)).scalars()}
    assert fired == {
        "RAID array md0 is degraded (raid1, degraded)",
        "RAID array md0 has failed members: sdc1",
    }
    assert len(_outbox(session)) == 2


def test_disabling_a_rule_closes_its_alerts_silently(
    session: Session,
    migrated_settings: Settings,
    admin: User,
    cipher: CredentialCipher,
    only_rule: AlertRule,
) -> None:
    _webhook(session, admin, cipher)
    only_rule.duration_seconds = 0
    session.commit()
    now = utcnow_naive()
    alerts.evaluate(session, migrated_settings, now, _sample(session, disk=99.0, at=now))
    alerts.update_rule(session, only_rule, changes={"enabled": False}, actor=admin)
    alert = session.execute(select(Alert)).scalar_one()
    assert alert.state == "resolved"
    assert [n.event for n in _outbox(session)] == ["firing"]


def test_acknowledge(
    session: Session, migrated_settings: Settings, admin: User, only_rule: AlertRule
) -> None:
    now = utcnow_naive()
    alerts.evaluate(session, migrated_settings, now, _sample(session, disk=99.0, at=now))
    pending = session.execute(select(Alert)).scalar_one()
    with pytest.raises(ConflictError):
        alerts.acknowledge(session, pending, actor=admin)
    later = now + timedelta(minutes=6)
    alerts.evaluate(session, migrated_settings, later, _sample(session, disk=99.0, at=later))
    alerts.acknowledge(session, pending, actor=admin)
    alerts.acknowledge(session, pending, actor=admin)  # idempotent, audited once
    assert pending.acknowledged_by == admin.id
    events = session.execute(
        select(AuditEvent).where(AuditEvent.event == "alert.acknowledged")
    ).all()
    assert len(events) == 1
    assert alerts.counts(session) == {
        "firing": 1,
        "pending": 0,
        "critical": 1,
        "warning": 0,
        "unacknowledged": 0,
    }


def test_prune_removes_old_resolved_alerts(
    session: Session, migrated_settings: Settings, only_rule: AlertRule
) -> None:
    old = utcnow_naive() - timedelta(days=migrated_settings.alert_retention_days + 1)
    session.add(
        Alert(
            rule_id=only_rule.id,
            target="/old",
            state="resolved",
            severity="critical",
            message="old",
            started_at=old,
            last_seen_at=old,
            resolved_at=old,
        )
    )
    session.add(
        Alert(
            rule_id=only_rule.id,
            target="/open",
            state="firing",
            severity="critical",
            message="open",
            started_at=old,
            last_seen_at=old,
        )
    )
    session.commit()
    assert alerts.prune(session, migrated_settings) == 1
    assert [a.target for a in session.execute(select(Alert)).scalars()] == ["/open"]


# ---------------------------------------------------------------------------
# Channel validation and secrets
# ---------------------------------------------------------------------------


def test_channel_secrets_are_encrypted(
    session: Session, admin: User, cipher: CredentialCipher
) -> None:
    row = _email(session, admin, cipher)
    assert row.secrets_encrypted and "smtp-pass-123" not in row.secrets_encrypted
    assert row.config["to"] == ["ops@example.org", "admin@example.org"]
    assert channels.secret_status(row, cipher) == (["password"], True)
    created = session.execute(
        select(AuditEvent).where(AuditEvent.event == "alert_channel.created")
    ).scalar_one()
    assert "smtp-pass-123" not in json.dumps(created.details)
    # PATCH keeps a secret that is not mentioned, and clears one set to null.
    channels.update_channel(
        session, row, changes={"config": {"port": 2525}}, cipher=cipher, actor=admin
    )
    assert channels.stored_secrets(row, cipher) == {"password": "smtp-pass-123"}
    channels.update_channel(
        session, row, changes={"secrets": {"password": None}}, cipher=cipher, actor=admin
    )
    assert row.secrets_encrypted is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"kind": "pager"}, "Unknown channel kind"),
        ({"config": {"url": "http://hooks.example/x"}}, "Plain HTTP"),
        ({"config": {"url": "https://user:pw@hooks.example/x"}}, "credentials"),
        ({"config": {"url": "ftp://hooks.example/x"}}, "http"),
        ({"config": {"url": "https://h.example/x", "password": "oops"}}, "Unknown config"),
        ({"secrets": {"password": "x"}}, "Unknown secret"),
        ({"secrets": {"authorization": "Bearer a\r\nX-Evil: 1"}}, "single line"),
    ],
)
def test_webhook_validation(
    session: Session, admin: User, cipher: CredentialCipher, overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(BadRequestError, match=message):
        _webhook(session, admin, cipher, **overrides)


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"to": "ops@example.org\r\nBcc: victim@example.org"}, "valid email"),
        ({"to": []}, "at least one"),
        ({"from_address": "Evil <x@example.org>"}, "valid email"),
        ({"security": "none"}, "allow_insecure"),
        ({"security": "ssl3"}, "Unknown security"),
        ({"host": "smtp example"}, "host"),
        ({"port": 70000}, "port"),
    ],
)
def test_email_validation(
    session: Session, admin: User, cipher: CredentialCipher, config: dict[str, Any], message: str
) -> None:
    base = {
        "host": "smtp.example.org",
        "port": 587,
        "security": "starttls",
        "from_address": "nas@example.org",
        "to": ["ops@example.org"],
    }
    with pytest.raises(BadRequestError, match=message):
        _email(session, admin, cipher, config={**base, **config})


def test_plain_transports_need_allow_insecure(
    session: Session, admin: User, cipher: CredentialCipher
) -> None:
    hook = _webhook(
        session, admin, cipher, config={"url": "http://10.0.0.5/hook"}, allow_insecure=True
    )
    assert hook.allow_insecure
    with pytest.raises(BadRequestError, match="Plain HTTP"):
        channels.update_channel(
            session, hook, changes={"allow_insecure": False}, cipher=cipher, actor=admin
        )


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


class FakeSMTP:
    instances: ClassVar[list[FakeSMTP]] = []
    fail_login = False

    def __init__(self, host: str, port: int, timeout: int = 0, context: Any = None) -> None:
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls: list[str] = []
        self.sent: list[Any] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("quit")

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def starttls(self, context: ssl.SSLContext | None = None) -> None:
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode == ssl.CERT_REQUIRED
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        self.calls.append(f"login:{user}:{password}")

    def send_message(self, message: Any) -> None:
        self.calls.append("send")
        self.sent.append(message)


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    FakeSMTP.instances = []
    FakeSMTP.fail_login = False
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_email_starttls_login_and_message(
    session: Session,
    migrated_settings: Settings,
    admin: User,
    cipher: CredentialCipher,
    fake_smtp: type[FakeSMTP],
) -> None:
    row = _email(session, admin, cipher)
    ok, message = notify.send_test(
        session, row, cipher=cipher, settings=migrated_settings, actor=admin
    )
    assert (ok, message) == (True, "Test notification sent")
    [client] = fake_smtp.instances
    assert (client.host, client.port) == ("smtp.example.org", 587)
    assert client.calls == ["ehlo", "starttls", "ehlo", "login:nas:smtp-pass-123", "send", "quit"]
    sent = client.sent[0]
    assert sent["To"] == "ops@example.org, admin@example.org"
    assert sent["From"] == "nas@example.org"
    assert "Test notification" in sent["Subject"]
    assert row.last_test_ok is True


def test_email_failure_is_reported_not_raised(
    session: Session,
    migrated_settings: Settings,
    admin: User,
    cipher: CredentialCipher,
    fake_smtp: type[FakeSMTP],
) -> None:
    fake_smtp.fail_login = True
    row = _email(session, admin, cipher)
    ok, message = notify.send_test(
        session, row, cipher=cipher, settings=migrated_settings, actor=admin
    )
    assert not ok and message == "SMTP authentication failed (535)"
    event = session.execute(
        select(AuditEvent).where(AuditEvent.event == "alert_channel.tested")
    ).scalar_one()
    assert event.outcome == "failure"


def test_email_unreachable_server(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    config = {
        "host": "127.0.0.1",
        "port": 1,
        "security": "starttls",
        "from_address": "a@example.org",
        "to": ["b@example.org"],
    }
    with pytest.raises(notify.NotificationError, match=r"cannot reach 127\.0\.0\.1:1"):
        notify.send_email(config, {}, allow_insecure=False, subject="s", body="b", timeout=1)


def test_plain_email_refused_at_send_time() -> None:
    config = {"host": "h", "port": 25, "security": "none", "from_address": "a@b.c", "to": ["d@e.f"]}
    with pytest.raises(notify.NotificationError, match="plain SMTP"):
        notify.send_email(config, {}, allow_insecure=False, subject="s", body="b", timeout=1)


def test_implicit_tls_uses_smtp_ssl(fake_smtp: type[FakeSMTP]) -> None:
    config = {
        "host": "h.example",
        "port": 465,
        "security": "tls",
        "from_address": "a@b.c",
        "to": ["d@e.f"],
    }
    notify.send_email(config, {}, allow_insecure=False, subject="s\r\nBcc: x", body="b", timeout=1)
    [client] = fake_smtp.instances
    assert isinstance(client.context, ssl.SSLContext)
    assert "starttls" not in client.calls
    # Folding whitespace is collapsed: no header injection through the subject.
    assert client.sent[0]["Subject"] == "s Bcc: x"


class Hook:
    def __init__(self, status: int = 204) -> None:
        self.status = status
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(self.status)


@pytest.fixture
def hook(monkeypatch: pytest.MonkeyPatch) -> Hook:
    server = Hook()
    monkeypatch.setattr(notify, "WEBHOOK_TRANSPORT", httpx2.MockTransport(server))
    return server


def test_deliver_due_posts_signed_webhook(
    session: Session,
    migrated_settings: Settings,
    admin: User,
    cipher: CredentialCipher,
    only_rule: AlertRule,
    hook: Hook,
) -> None:
    _webhook(session, admin, cipher, secrets={"signing_secret": "k", "authorization": "Bearer t0k"})
    only_rule.duration_seconds = 0
    session.commit()
    now = utcnow_naive()
    alerts.evaluate(session, migrated_settings, now, _sample(session, disk=97.0, at=now))
    assert notify.pending_count(session, now) == 1
    result = notify.deliver_due(session, migrated_settings, cipher, now=now)
    assert result == {"sent": 1, "retry": 0, "failed": 0}
    [request] = hook.requests
    assert str(request.url) == "https://hooks.example/nas"
    body = request.content
    expected = "sha256=" + hmac.new(b"k", body, hashlib.sha256).hexdigest()
    assert request.headers["X-NAS-Signature"] == expected
    assert request.headers["Authorization"] == "Bearer t0k"
    payload = json.loads(body)
    assert payload["event"] == "firing"
    assert payload["alert"]["severity"] == "critical"
    assert payload["alert"]["target"] == "/srv"
    assert "k" not in json.dumps(payload["alert"]).split('"')  # no secrets in the body
    [row] = _outbox(session)
    assert row.status == "sent" and row.sent_at is not None and row.attempts == 1


def test_deliver_due_retries_then_fails(
    session: Session,
    migrated_settings: Settings,
    admin: User,
    cipher: CredentialCipher,
    only_rule: AlertRule,
    hook: Hook,
) -> None:
    hook.status = 500
    _webhook(session, admin, cipher)
    only_rule.duration_seconds = 0
    session.commit()
    now = utcnow_naive()
    alerts.evaluate(session, migrated_settings, now, _sample(session, disk=97.0, at=now))
    assert notify.deliver_due(session, migrated_settings, cipher, now=now)["retry"] == 1
    [row] = _outbox(session)
    assert row.status == "queued" and row.attempts == 1
    assert row.next_attempt_at == now + timedelta(minutes=1)
    assert row.last_error == "webhook answered HTTP 500"
    # Not due yet: nothing happens.
    assert notify.deliver_due(session, migrated_settings, cipher, now=now) == {
        "sent": 0,
        "retry": 0,
        "failed": 0,
    }
    for _ in range(migrated_settings.alert_notify_max_attempts - 1):
        now += timedelta(hours=2)
        notify.deliver_due(session, migrated_settings, cipher, now=now)
    [row] = _outbox(session)
    assert row.status == "failed"
    assert row.attempts == migrated_settings.alert_notify_max_attempts
    failure = session.execute(
        select(AuditEvent).where(AuditEvent.event == "alert.notification_failed")
    ).scalar_one()
    assert failure.outcome == "failure"
    assert failure.details is not None and failure.details["channel"] == "hook"


def test_backoff_is_capped() -> None:
    assert notify.backoff(1) == timedelta(minutes=1)
    assert notify.backoff(3) == timedelta(minutes=4)
    assert notify.backoff(20) == timedelta(hours=1)


def test_webhook_redirect_is_not_followed() -> None:
    seen: list[str] = []

    def redirect(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(302, headers={"Location": "http://169.254.169.254/"})

    with pytest.raises(notify.NotificationError, match="HTTP 302"):
        notify.send_webhook(
            {"url": "https://hooks.example/x"},
            {},
            allow_insecure=False,
            payload={},
            timeout=1,
            transport=httpx2.MockTransport(redirect),
        )
    assert seen == ["https://hooks.example/x"]


def test_webhook_network_error() -> None:
    def broken(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    with pytest.raises(notify.NotificationError, match="webhook request failed"):
        notify.send_webhook(
            {"url": "https://hooks.example/x"},
            {},
            allow_insecure=False,
            payload={},
            timeout=1,
            transport=httpx2.MockTransport(broken),
        )


def test_plain_webhook_refused_at_send_time() -> None:
    with pytest.raises(notify.NotificationError, match="plain HTTP"):
        notify.send_webhook(
            {"url": "http://hooks.example/x"}, {}, allow_insecure=False, payload={}, timeout=1
        )


def test_raising_the_threshold_resolves_on_the_next_pass_and_notifies(
    session: Session,
    migrated_settings: Settings,
    admin: User,
    cipher: CredentialCipher,
    only_rule: AlertRule,
) -> None:
    _webhook(session, admin, cipher)
    only_rule.duration_seconds = 0
    session.commit()
    now = utcnow_naive()
    alerts.evaluate(session, migrated_settings, now, _sample(session, disk=95.0, at=now))
    # 90 % keeps it firing under the old threshold (clears at 85.5 %)...
    alerts.evaluate(session, migrated_settings, now, _sample(session, disk=90.0, at=now))
    assert session.execute(select(Alert)).scalar_one().state == "firing"
    alerts.update_rule(session, only_rule, changes={"threshold": 99.0}, actor=admin)
    assert session.execute(select(Alert)).scalar_one().state == "firing"
    # ...but not under the raised one (clears at 94.05 %).
    alerts.evaluate(session, migrated_settings, now, _sample(session, disk=90.0, at=now))
    assert session.execute(select(Alert)).scalar_one().state == "resolved"
    assert [n.event for n in _outbox(session)] == ["firing", "resolved"]
