"""Remote syslog (Phase 13): formats, transports, forwarder, log handler, API."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow_naive
from app.db.session import Database
from app.models import AuditEvent, SyslogDestination, User
from app.services import audit
from app.services.remote_sync import ConfirmationRequiredError
from app.services.syslog import destinations
from app.services.syslog import format as syslog_format
from app.services.syslog.forwarder import backoff_seconds, forward_due
from app.services.syslog.log_handler import LogTarget, RemoteLogHandler
from app.services.syslog.transport import SyslogSender, SyslogTransportError, frame
from tests.conftest import ADMIN_PASSWORD, login
from tests.syslog_receivers import Receiver, make_tls_material

# ---------------------------------------------------------------------------
# Formats (pure)
# ---------------------------------------------------------------------------


def _event(**overrides: object) -> AuditEvent:
    values: dict[str, object] = {
        "id": 7,
        "created_at": datetime(2026, 11, 3, 8, 5, 9, 123456),
        "actor_username": "admin",
        "event": "user.created",
        "category": "user",
        "outcome": "success",
        "target_type": "user",
        "target_id": "3",
        "ip_address": "192.0.2.10",
        "user_agent": "pytest",
        "request_id": "req-1",
        "details": {"username": 'bob"]\\'},
    }
    values.update(overrides)
    return AuditEvent(**values)


IDENTITY = {"hostname": "nas box", "app_name": "nas-manager", "procid": "42"}


def test_rfc5424_audit_message() -> None:
    payload = syslog_format.render(
        syslog_format.audit_message(_event()), fmt="rfc5424", facility="local0", **IDENTITY
    ).decode()
    # local0 (16) * 8 + notice (5) = 133; spaces in the host name are replaced.
    assert payload.startswith("<133>1 2026-11-03T08:05:09.123456Z nas_box nas-manager 42 user [")
    sd = payload[payload.index("[") : payload.index("] ") + 1]
    assert sd.startswith("[nas@32473 ")
    assert 'event="user.created"' in sd and 'target="user:3"' in sd and 'id="7"' in sd
    body = payload[payload.index("] ") + 2 :]
    assert '"details": {"username": "bob\\"]\\\\"}' in body


def test_structured_data_escaping_and_failure_severity() -> None:
    row = _event(outcome="failure", actor_username='ev"il]', ip_address=None)
    payload = syslog_format.render(
        syslog_format.audit_message(row), fmt="rfc5424", facility="authpriv", **IDENTITY
    ).decode()
    assert payload.startswith("<84>1 ")  # authpriv (10) * 8 + warning (4)
    assert 'actor="ev\\"il\\]"' in payload
    assert "ip=" not in payload  # empty parameters are left out


def test_rfc3164_is_single_line() -> None:
    message = syslog_format.SyslogMessage(
        severity=syslog_format.SEVERITY_ERROR,
        timestamp=datetime(2026, 1, 5, 7, 8, 9, tzinfo=UTC),
        msgid="log",
        text="first\nsecond",
    )
    payload = syslog_format.render(message, fmt="rfc3164", facility="daemon", **IDENTITY).decode()
    assert payload == "<27>Jan  5 07:08:09 nas_box nas-manager[42]: first second"


def test_framing() -> None:
    assert frame(b"abc", protocol="tcp", fmt="rfc5424") == b"3 abc"
    assert frame("é".encode(), protocol="tls", fmt="rfc5424") == b"2 \xc3\xa9"
    assert frame(b"a\nb", protocol="tcp", fmt="rfc3164") == b"a b\n"
    long = ("x" * 2047 + "é").encode()
    truncated = frame(long, protocol="udp", fmt="rfc5424")
    assert len(truncated) == 2047  # never splits a UTF-8 sequence
    truncated.decode()


def test_backoff_is_capped() -> None:
    assert [backoff_seconds(n) for n in (1, 2, 3)] == [5, 10, 20]
    assert backoff_seconds(30) == 300


# ---------------------------------------------------------------------------
# Transports against loopback collectors
# ---------------------------------------------------------------------------


@pytest.fixture
def tls(tmp_path: Path) -> Iterator[tuple[str, Receiver]]:
    ca_pem, context = make_tls_material(tmp_path)
    receiver = Receiver("tls", context)
    yield ca_pem, receiver
    receiver.close()


@pytest.mark.parametrize("protocol", ["udp", "tcp"])
def test_plain_transports_deliver(protocol: str) -> None:
    receiver = Receiver(protocol)
    try:
        sender = SyslogSender(host="127.0.0.1", port=receiver.port, protocol=protocol)
        sender.send([frame(b"<13>1 - - - - - - one", protocol=protocol, fmt="rfc5424")])
        sender.send([frame(b"<13>1 - - - - - - two", protocol=protocol, fmt="rfc5424")])
        sender.close()
        messages = receiver.wait_for(2)
        assert [m.rsplit(b" ", 1)[1] for m in messages] == [b"one", b"two"]
    finally:
        receiver.close()


def test_tls_verifies_with_the_configured_ca(tls: tuple[str, Receiver]) -> None:
    ca_pem, receiver = tls
    sender = SyslogSender(host="127.0.0.1", port=receiver.port, protocol="tls", ca_pem=ca_pem)
    sender.send([frame(b"hello", protocol="tls", fmt="rfc5424")])
    sender.close()
    assert receiver.wait_for(1) == [b"hello"]


def test_tls_refuses_an_unknown_certificate(tls: tuple[str, Receiver]) -> None:
    _, receiver = tls
    sender = SyslogSender(host="127.0.0.1", port=receiver.port, protocol="tls")
    with pytest.raises(SyslogTransportError, match="certificate verification failed"):
        sender.send([b"x"])


def test_unreachable_collector() -> None:
    probe = Receiver("tcp")
    port = probe.port
    probe.close()
    sender = SyslogSender(host="127.0.0.1", port=port, protocol="tcp", timeout=1)
    with pytest.raises(SyslogTransportError, match="refused"):
        sender.send([b"x"])


# ---------------------------------------------------------------------------
# Destinations and the worker's forwarder
# ---------------------------------------------------------------------------


@pytest.fixture
def session(db: Database) -> Iterator[Session]:
    with db.session_factory() as s:
        yield s


@pytest.fixture
def admin(session: Session, migrated_settings: Settings) -> User:
    from app.services.bootstrap_service import bootstrap_admin

    return bootstrap_admin(
        session, username="admin", password=ADMIN_PASSWORD, settings=migrated_settings
    )


def _record(session: Session, event: str = audit.USER_CREATED, **kwargs: object) -> AuditEvent:
    return audit.record_audit_event(
        session,
        event=event,
        category=str(kwargs.pop("category", audit.CATEGORY_USER)),
        actor_username="admin",
        **kwargs,  # type: ignore[arg-type]
    )


def _destination(session: Session, admin: User, port: int, **values: object) -> SyslogDestination:
    return destinations.create_destination(
        session,
        values={
            "name": "collector",
            "host": "127.0.0.1",
            "port": port,
            "protocol": "tcp",
            "confirm": True,
            **values,
        },
        actor=admin,
    )


def _events(messages: list[bytes]) -> list[str]:
    return [re.search(rb'event="([^"]+)"', m).group(1).decode() for m in messages]  # type: ignore[union-attr]


def test_plaintext_destination_needs_confirmation(session: Session, admin: User) -> None:
    with pytest.raises(ConfirmationRequiredError) as info:
        destinations.create_destination(
            session,
            values={"name": "c", "host": "logs.example.org", "protocol": "udp"},
            actor=admin,
        )
    assert "clear text" in info.value.details["warnings"][0]
    row = destinations.create_destination(
        session,
        values={"name": "c", "host": "logs.example.org", "protocol": "udp", "confirm": True},
        actor=admin,
    )
    assert row.port == 514
    # Switching an existing TLS destination to TCP needs confirmation again.
    secure = destinations.create_destination(
        session, values={"name": "t", "host": "logs.example.org", "protocol": "tls"}, actor=admin
    )
    assert secure.port == 6514
    with pytest.raises(ConfirmationRequiredError):
        destinations.update_destination(session, secure, changes={"protocol": "tcp"}, actor=admin)
    session.rollback()
    updated = destinations.update_destination(
        session, secure, changes={"protocol": "tcp", "confirm": True}, actor=admin
    )
    assert updated.port == 514


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"host": "bad host"}, "host"),
        ({"host": "-x"}, "host"),
        ({"name": ""}, "name"),
        ({"protocol": "http"}, "protocol"),
        ({"facility": "kern"}, "facility"),
        ({"audit_categories": ["nope"]}, "categories"),
        ({"ca_pem": "not a pem", "protocol": "tls"}, "ca_pem"),
        ({"ca_pem": "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----"}, "ca_pem"),
        ({"log_min_level": "LOUD"}, "level"),
    ],
)
def test_destination_validation(
    session: Session, admin: User, values: dict[str, object], message: str
) -> None:
    from app.core.errors import BadRequestError

    base: dict[str, object] = {"name": "c", "host": "logs.example.org", "protocol": "tls"}
    with pytest.raises(BadRequestError, match=re.compile(message, re.I)):
        destinations.create_destination(session, values={**base, **values}, actor=admin)


def test_ca_pem_only_with_tls(session: Session, admin: User, tmp_path: Path) -> None:
    from app.core.errors import BadRequestError

    ca_pem, _ = make_tls_material(tmp_path)
    row = destinations.create_destination(
        session,
        values={"name": "c", "host": "127.0.0.1", "protocol": "tls", "ca_pem": ca_pem},
        actor=admin,
    )
    assert row.ca_pem is not None
    with pytest.raises(BadRequestError, match="tls"):
        destinations.create_destination(
            session,
            values={
                "name": "d",
                "host": "127.0.0.1",
                "protocol": "tcp",
                "ca_pem": ca_pem,
                "confirm": True,
            },
            actor=admin,
        )
    # Leaving TLS drops the stored CA.
    updated = destinations.update_destination(
        session, row, changes={"protocol": "tcp", "confirm": True}, actor=admin
    )
    assert updated.ca_pem is None


def test_forwarding_starts_at_the_end_or_backfills(
    session: Session, admin: User, migrated_settings: Settings
) -> None:
    _record(session, target_type="user", target_id=1)
    receiver = Receiver("tcp")
    try:
        _destination(session, admin, receiver.port)
        _destination(session, admin, receiver.port, name="history", backfill=True)
        _record(session, event=audit.USER_UPDATED)
        result = forward_due(session, migrated_settings)
        messages = receiver.wait_for(result["sent"])
    finally:
        receiver.close()
    events = _events(messages)
    # "collector" gets its own creation event onwards; "history" everything.
    assert events.count("user.updated") == 2
    assert events.count("user.created") == 1
    for row in session.execute(select(SyslogDestination)).scalars():
        assert (
            row.last_audit_id
            == session.execute(
                select(AuditEvent.id).order_by(AuditEvent.id.desc()).limit(1)
            ).scalar_one()
        )


def test_forwarding_filters_still_advance_the_cursor(
    session: Session, admin: User, migrated_settings: Settings
) -> None:
    receiver = Receiver("udp")
    try:
        dest = _destination(
            session,
            admin,
            receiver.port,
            protocol="udp",
            audit_categories=["auth"],
            audit_failures_only=True,
        )
        _record(session, event=audit.AUTH_LOGIN_SUCCESS, category=audit.CATEGORY_AUTH)
        _record(
            session,
            event=audit.AUTH_LOGIN_FAILURE,
            category=audit.CATEGORY_AUTH,
            outcome=audit.OUTCOME_FAILURE,
        )
        _record(session, event=audit.USER_DELETED, outcome=audit.OUTCOME_FAILURE)
        assert forward_due(session, migrated_settings)["sent"] == 1
        assert _events(receiver.wait_for(1)) == ["auth.login.failure"]
        session.refresh(dest)
        assert dest.last_audit_id == max(session.execute(select(AuditEvent.id)).scalars())
    finally:
        receiver.close()


def test_outage_keeps_the_cursor_backs_off_and_recovers(
    session: Session, admin: User, migrated_settings: Settings
) -> None:
    probe = Receiver("tcp")
    port = probe.port
    probe.close()
    dest = _destination(session, admin, port)
    start = dest.last_audit_id
    _record(session, event=audit.USER_UPDATED)
    now = utcnow_naive()
    assert forward_due(session, migrated_settings, now=now)["failed"] == 1
    session.refresh(dest)
    assert dest.last_audit_id == start
    assert dest.consecutive_failures == 1 and dest.last_error
    assert dest.next_attempt_at == now + timedelta(seconds=5)
    # Still backing off: skipped, and no second failure event.
    assert forward_due(session, migrated_settings, now=now)["skipped"] == 1
    assert forward_due(session, migrated_settings, now=now + timedelta(seconds=6))["failed"] == 1
    failures = session.execute(
        select(AuditEvent).where(AuditEvent.event == audit.SYSLOG_FORWARD_FAILED)
    ).scalars()
    assert len(list(failures)) == 1

    receiver = Receiver("tcp")
    try:
        dest.port = receiver.port
        session.commit()
        later = now + timedelta(minutes=10)
        result = forward_due(session, migrated_settings, now=later)
        events = _events(receiver.wait_for(result["sent"]))
    finally:
        receiver.close()
    # Everything written since the destination was created arrives, in order
    # (its own creation event included).
    assert events[:3] == [
        "syslog.destination_created",
        "user.updated",
        "syslog.forward_failed",
    ]
    session.refresh(dest)
    assert dest.consecutive_failures == 0 and dest.next_attempt_at is None
    recovered = session.execute(
        select(AuditEvent).where(AuditEvent.event == audit.SYSLOG_FORWARD_RECOVERED)
    ).scalar_one()
    assert recovered.details == {"name": "collector", "failed_attempts": 2}


def test_tls_forwarding_end_to_end(
    session: Session, admin: User, migrated_settings: Settings, tls: tuple[str, Receiver]
) -> None:
    ca_pem, receiver = tls
    destinations.create_destination(
        session,
        values={"name": "tls", "host": "127.0.0.1", "port": receiver.port, "ca_pem": ca_pem},
        actor=admin,
    )
    _record(session, event=audit.USER_DELETED)
    assert forward_due(session, migrated_settings)["sent"] == 2  # + its creation event
    messages = receiver.wait_for(2)
    assert _events(messages) == ["syslog.destination_created", "user.deleted"]
    assert all(m.startswith(b"<133>1 ") for m in messages)


def test_disabled_and_log_only_destinations_are_not_forwarded(
    session: Session, admin: User, migrated_settings: Settings
) -> None:
    _destination(session, admin, 9, enabled=False)
    _destination(session, admin, 9, name="logs", forward_audit=False, forward_logs=True)
    _record(session)
    assert forward_due(session, migrated_settings) == {"sent": 0, "failed": 0, "skipped": 0}


def test_config_version_moves_with_every_change(session: Session, admin: User) -> None:
    before = destinations.config_version(session)
    row = _destination(session, admin, 514)
    after_create = destinations.config_version(session)
    destinations.update_destination(session, row, changes={"forward_logs": True}, actor=admin)
    after_update = destinations.config_version(session)
    destinations.delete_destination(session, row, actor=admin)
    assert len({before, after_create, after_update, destinations.config_version(session)}) == 4


# ---------------------------------------------------------------------------
# Application log forwarding
# ---------------------------------------------------------------------------


def _target(port: int, *, level: int = logging.WARNING, protocol: str = "udp") -> LogTarget:
    return LogTarget(
        id=1,
        name="c",
        host="127.0.0.1",
        port=port,
        protocol=protocol,
        ca_pem=None,
        format="rfc5424",
        facility="local1",
        min_level=level,
    )


def _handler(queue_size: int = 100) -> RemoteLogHandler:
    from app.core.logging import ContextFilter, JsonFormatter

    handler = RemoteLogHandler(
        queue_size=queue_size, hostname="nas", app_name="nas-manager", procid="1", timeout=2
    )
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContextFilter())
    return handler


def test_log_handler_forwards_by_level_and_redacts() -> None:
    receiver = Receiver("udp")
    handler = _handler()
    log = logging.getLogger("tests.syslog.app")
    log.propagate = False
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    try:
        handler.configure([_target(receiver.port)])
        log.info("too quiet")
        log.warning("disk nearly full", extra={"password": "hunter2"})
        logging.getLogger("app.audit").addHandler(handler)
        logging.getLogger("app.audit").warning("audit line")  # forwarded by the worker instead
        handler.flush()
        messages = receiver.wait_for(1)
    finally:
        logging.getLogger("app.audit").removeHandler(handler)
        log.removeHandler(handler)
        handler.close()
        receiver.close()
    assert len(messages) == 1
    text = messages[0].decode()
    assert text.startswith("<140>1 ")  # local1 (17) * 8 + warning (4)
    assert "disk nearly full" in text and "hunter2" not in text and '"password": "***"' in text
    assert 'logger="tests.syslog.app"' in text


def test_log_handler_drops_when_the_queue_is_full() -> None:
    handler = _handler(queue_size=100)
    # Configure without starting the thread: emit only enqueues.
    handler._targets = (_target(9),)
    handler._min_level = logging.WARNING
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", None, None)
    for _ in range(105):
        handler.emit(record)
    assert handler.dropped == 5
    handler.close()


def test_log_handler_survives_an_unreachable_collector() -> None:
    probe = Receiver("tcp")
    port = probe.port
    probe.close()
    handler = _handler()
    try:
        handler.configure([_target(port, protocol="tcp")])
        handler.emit(logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", None, None))
        handler.flush()
        assert handler.send_errors == 1
    finally:
        handler.close()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def _user_client(app: FastAPI, admin_user: User, username: str, role: str) -> TestClient:
    from app.services.users_service import create_user

    with app.state.db.session_factory() as session:
        create_user(
            session,
            username=username,
            password=ADMIN_PASSWORD,
            display_name=None,
            email=None,
            role_names=[role],
            groups=[],
            settings=app.state.settings,
            actor=admin_user,
        )
    client = TestClient(app)
    login(client, username, ADMIN_PASSWORD)
    return client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("nas_csrf", "") or ""}


def test_api_flow_and_permissions(
    app: FastAPI, admin_user: User, authed_client: TestClient, client: TestClient
) -> None:
    receiver = Receiver("tcp")
    try:
        assert client.get("/api/v1/syslog/status").status_code == 401
        body = {"name": "collector", "host": "127.0.0.1", "port": receiver.port, "protocol": "tcp"}
        response = authed_client.post(
            "/api/v1/syslog/destinations", json=body, headers=_csrf(authed_client)
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "confirmation_required"
        response = authed_client.post(
            "/api/v1/syslog/destinations",
            json={**body, "confirm": True, "forward_logs": True},
            headers=_csrf(authed_client),
        )
        assert response.status_code == 201, response.text
        created = response.json()
        assert created["has_ca_pem"] is False and created["lag"] == 1  # its own event
        dest_id = created["id"]

        test = authed_client.post(
            f"/api/v1/syslog/destinations/{dest_id}/test", headers=_csrf(authed_client)
        )
        assert test.json()["ok"] is True
        assert b"syslog test message" in receiver.wait_for(1)[0]

        status = authed_client.get("/api/v1/syslog/status").json()
        assert status["logging"]["remote_targets"] == 1  # reloaded in this process
        assert status["destinations"][0]["lag"] >= 1  # the audit events written since

        operator = _user_client(app, admin_user, "olga", "operator")
        assert operator.get("/api/v1/syslog/destinations").status_code == 200
        assert (
            operator.patch(
                f"/api/v1/syslog/destinations/{dest_id}",
                json={"enabled": False},
                headers=_csrf(operator),
            ).status_code
            == 403
        )
        viewer = _user_client(app, admin_user, "vic", "viewer")
        assert viewer.get("/api/v1/syslog/status").status_code == 403

        patched = authed_client.patch(
            f"/api/v1/syslog/destinations/{dest_id}",
            json={"forward_logs": False},
            headers=_csrf(authed_client),
        )
        assert patched.status_code == 200 and patched.json()["forward_logs"] is False
        assert authed_client.get("/api/v1/syslog/status").json()["logging"]["remote_targets"] == 0
        assert (
            authed_client.delete(
                f"/api/v1/syslog/destinations/{dest_id}", headers=_csrf(authed_client)
            ).status_code
            == 204
        )
    finally:
        receiver.close()
    with app.state.db.session_factory() as session:
        events = set(
            session.execute(
                select(AuditEvent.event).where(AuditEvent.category == audit.CATEGORY_SYSLOG)
            ).scalars()
        )
    assert events == {
        audit.SYSLOG_DESTINATION_CREATED,
        audit.SYSLOG_DESTINATION_TESTED,
        audit.SYSLOG_DESTINATION_UPDATED,
        audit.SYSLOG_DESTINATION_DELETED,
    }


def test_api_test_reports_failure(authed_client: TestClient) -> None:
    probe = Receiver("tcp")
    port = probe.port
    probe.close()
    created = authed_client.post(
        "/api/v1/syslog/destinations",
        json={
            "name": "gone",
            "host": "127.0.0.1",
            "port": port,
            "protocol": "tcp",
            "confirm": True,
        },
        headers=_csrf(authed_client),
    ).json()
    result = authed_client.post(
        f"/api/v1/syslog/destinations/{created['id']}/test", headers=_csrf(authed_client)
    ).json()
    assert result == {"ok": False, "message": "connection refused"}


def test_info_advertises_syslog(client: TestClient) -> None:
    features = client.get("/api/v1/info").json()["features"]
    assert "syslog" in features and "audit_search" in features


def test_naive_and_aware_timestamps_render_the_same() -> None:
    naive = datetime(2026, 1, 1, 12, 0, 0)
    aware = naive.replace(tzinfo=UTC)
    m1 = syslog_format.SyslogMessage(severity=5, timestamp=naive, msgid="x", text="t")
    m2 = syslog_format.SyslogMessage(severity=5, timestamp=aware, msgid="x", text="t")
    assert syslog_format.render_rfc5424(m1, facility="user", **IDENTITY) == (
        syslog_format.render_rfc5424(m2, facility="user", **IDENTITY)
    )
