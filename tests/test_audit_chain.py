"""The audit hash chain, retention pruning and after-commit log emission (Phase 13)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow_naive
from app.db.session import Database
from app.models import AuditEvent, SyslogDestination, User
from app.services import audit, audit_chain
from tests.conftest import ADMIN_PASSWORD


@pytest.fixture
def session(db: Database) -> Iterator[Session]:
    with db.session_factory() as s:
        yield s


def _record(session: Session, n: int = 1, **kwargs: object) -> list[AuditEvent]:
    return [
        audit.record_audit_event(
            session,
            event=audit.USER_UPDATED,
            category=audit.CATEGORY_USER,
            actor_username="admin",
            details={"n": i, "note": "café"},
            **kwargs,  # type: ignore[arg-type]
        )
        for i in range(n)
    ]


def test_seal_is_ordered_and_idempotent(session: Session) -> None:
    _record(session, 3)
    assert audit_chain.seal(session) == 3
    assert audit_chain.seal(session) == 0
    rows = list(session.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars())
    previous = audit_chain.GENESIS
    for row in rows:
        assert row.chain_hash == audit_chain.link(previous, row)
        previous = row.chain_hash
    _record(session, 2)
    assert audit_chain.seal(session, batch=1) == 2  # continues from the head
    result = audit_chain.verify(session)
    assert result.ok and result.checked == 5 and result.unsealed == 0


def test_unsealed_tail_is_fine(session: Session) -> None:
    _record(session, 2)
    audit_chain.seal(session)
    _record(session)
    result = audit_chain.verify(session)
    assert result.ok and result.unsealed == 1


@pytest.mark.parametrize(
    "tamper",
    [
        "UPDATE audit_events SET outcome = 'failure' WHERE id = :id",
        "UPDATE audit_events SET details = '{\"n\": 99}' WHERE id = :id",
        "UPDATE audit_events SET created_at = '2020-01-01 00:00:00.000000' WHERE id = :id",
        "DELETE FROM audit_events WHERE id = :id",
    ],
)
def test_tampering_is_detected(session: Session, tamper: str) -> None:
    rows = _record(session, 4)
    audit_chain.seal(session)
    target = rows[1].id
    session.execute(text(tamper), {"id": target})
    session.commit()
    result = audit_chain.verify(session)
    assert not result.ok
    # An edit breaks at the row itself, a deletion at its successor.
    assert result.first_break_id in (target, rows[2].id)
    assert result.reason and "hash mismatch" in result.reason


def test_truncating_the_tail_is_detected(session: Session) -> None:
    rows = _record(session, 3)
    audit_chain.seal(session)
    session.execute(text("DELETE FROM audit_events WHERE id = :id"), {"id": rows[-1].id})
    session.commit()
    result = audit_chain.verify(session)
    assert not result.ok and "missing" in (result.reason or "")


def test_unsealed_row_inside_the_sealed_range(session: Session) -> None:
    rows = _record(session, 3)
    audit_chain.seal(session)
    session.execute(update(AuditEvent).where(AuditEvent.id == rows[1].id).values(chain_hash=None))
    session.commit()
    result = audit_chain.verify(session)
    assert not result.ok and result.first_break_id == rows[1].id
    assert "unsealed" in (result.reason or "")


def test_deleting_a_user_keeps_the_chain_valid(
    session: Session, migrated_settings: Settings
) -> None:
    from app.services.bootstrap_service import bootstrap_admin

    admin = bootstrap_admin(
        session, username="admin", password=ADMIN_PASSWORD, settings=migrated_settings
    )
    bob = User(username="bob", password_hash="x", is_active=True)
    session.add(bob)
    session.commit()
    audit.record_audit_event(
        session, event=audit.USER_UPDATED, category=audit.CATEGORY_USER, actor=bob
    )
    audit_chain.seal(session)
    session.delete(bob)
    session.commit()
    assert (
        session.execute(
            select(AuditEvent.actor_user_id).where(AuditEvent.actor_username == "bob")
        ).scalar_one()
        is None
    )
    assert audit_chain.verify(session).ok
    assert admin.id


def _age(session: Session, rows: list[AuditEvent], days: int) -> None:
    session.execute(
        update(AuditEvent)
        .where(AuditEvent.id.in_([r.id for r in rows]))
        .values(created_at=utcnow_naive() - timedelta(days=days))
    )
    session.commit()


def test_prune_moves_the_anchor_and_stays_verifiable(
    session: Session, migrated_settings: Settings
) -> None:
    old = _record(session, 3)
    _age(session, old, 120)
    _record(session, 2)
    # Unsealed events are never pruned.
    assert audit_chain.prune(session, migrated_settings) == 0
    plan = audit_chain.prune_plan(session, migrated_settings)
    assert plan.held_back_by_sealing and plan.expired == 3
    audit_chain.seal(session)
    assert audit_chain.prune(session, migrated_settings) == 3
    assert audit_chain.anchor(session)["id"] == old[-1].id
    remaining = list(session.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars())
    assert [r.event for r in remaining][-1] == audit.AUDIT_PRUNED
    assert remaining[-1].details == {"count": 3, "up_to_id": old[-1].id, "retention_days": 90}
    audit_chain.seal(session)
    assert audit_chain.verify(session).ok


def test_prune_keeps_the_newest_event(session: Session, migrated_settings: Settings) -> None:
    """Even when everything has expired the newest event stays, so SQLite
    never reuses an id below the anchor or a syslog cursor."""
    rows = _record(session, 3)
    _age(session, rows, 365)  # before sealing: changing a sealed row is tampering
    audit_chain.seal(session)
    assert audit_chain.prune(session, migrated_settings) == 2
    pruned_event = session.execute(
        select(AuditEvent).where(AuditEvent.event == audit.AUDIT_PRUNED)
    ).scalar_one()
    assert pruned_event.id > rows[-1].id
    assert audit_chain.seal(session) == 1  # chained after the kept event
    assert audit_chain.verify(session).ok


def test_prune_waits_for_enabled_syslog_destinations(
    session: Session, migrated_settings: Settings
) -> None:
    rows = _record(session, 4)
    audit_chain.seal(session)
    _age(session, rows, 200)
    session.add(
        SyslogDestination(
            name="lagging", host="h", port=514, protocol="tcp", last_audit_id=rows[1].id
        )
    )
    session.add(
        SyslogDestination(
            name="off", host="h", port=514, protocol="tcp", enabled=False, last_audit_id=0
        )
    )
    session.commit()
    plan = audit_chain.prune_plan(session, migrated_settings)
    assert plan.up_to_id == rows[1].id and plan.held_back_by_forwarding
    assert audit_chain.prune(session, migrated_settings) == 2
    status = audit_chain.integrity_status(session, migrated_settings)
    assert status["prune_held_back_by_forwarding"] and status["expired"] == 2


def test_retention_zero_keeps_everything(session: Session, migrated_settings: Settings) -> None:
    rows = _record(session, 2)
    audit_chain.seal(session)
    _age(session, rows, 5000)
    keep = migrated_settings.model_copy(update={"audit_retention_days": 0})
    assert audit_chain.prune(session, keep) == 0


def test_scheduled_verify_reports_a_new_break_once(session: Session) -> None:
    rows = _record(session, 3)
    audit_chain.seal(session)
    assert audit_chain.scheduled_verify(session).ok
    session.execute(
        text("UPDATE audit_events SET actor_username = 'mallory' WHERE id = :id"),
        {"id": rows[0].id},
    )
    session.commit()
    assert not audit_chain.scheduled_verify(session).ok
    assert not audit_chain.scheduled_verify(session).ok
    broken = session.execute(
        select(AuditEvent).where(AuditEvent.event == audit.AUDIT_CHAIN_BROKEN)
    ).scalars()
    assert len(list(broken)) == 1
    assert audit_chain.last_verify_result(session)["first_break_id"] == rows[0].id  # type: ignore[index]


# ---------------------------------------------------------------------------
# After-commit emission
# ---------------------------------------------------------------------------


def test_committed_events_are_logged(session: Session, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.audit"):
        row = audit.record_audit_event(
            session,
            event=audit.AUTH_LOGIN_FAILURE,
            category=audit.CATEGORY_AUTH,
            outcome=audit.OUTCOME_FAILURE,
            actor_username="eve",
            ip_address="192.0.2.9",
        )
    (record,) = [r for r in caplog.records if r.name == "app.audit"]
    assert record.levelno == logging.WARNING
    assert record.audit_id == row.id  # type: ignore[attr-defined]
    assert record.audit_event == "auth.login.failure"  # type: ignore[attr-defined]
    assert record.audit_ip == "192.0.2.9"  # type: ignore[attr-defined]


def test_rolled_back_events_are_not_logged(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.audit"):
        audit.record_audit_event(
            session, event=audit.USER_CREATED, category=audit.CATEGORY_USER, commit=False
        )
        session.flush()
        session.rollback()
        audit.record_audit_event(
            session, event=audit.USER_DELETED, category=audit.CATEGORY_USER, commit=False
        )
        session.commit()
    events = [r.audit_event for r in caplog.records if r.name == "app.audit"]  # type: ignore[attr-defined]
    assert events == ["user.deleted"]
