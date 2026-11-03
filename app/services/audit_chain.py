"""Tamper evidence for the audit log: a SHA-256 hash chain (Phase 13).

The worker *seals* events in id order: each event's ``chain_hash`` is
``sha256(previous chain_hash + canonical(event))``. Editing, deleting or
re-ordering a sealed event changes every later hash, which ``verify``
detects. The chain starts at an *anchor*: 64 zeros for a fresh log, or the
hash of the newest event removed by retention pruning, so pruning the oldest
events never looks like tampering. The newest sealed event is recorded as
the *head*, so removing the tail of the log is detected too.

The canonical form leaves out ``actor_user_id`` on purpose: that column is
``ON DELETE SET NULL``, so deleting a user would otherwise break the chain.
The username snapshot is covered.

What this does **not** protect against: someone with write access to the
database who recomputes the whole chain and the anchor/head settings. The
off-host copy kept by a remote syslog destination is the answer to that.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow_naive
from app.models import AuditEvent, SyslogDestination
from app.services import audit
from app.services.settings_service import get_setting, set_setting

logger = logging.getLogger(__name__)

GENESIS = "0" * 64
ANCHOR_KEY = "audit_chain_anchor"
HEAD_KEY = "audit_chain_head"
LAST_VERIFY_KEY = "audit_chain_last_verify"
_SEAL_BATCH = 1000
_VERIFY_BATCH = 2000
_PRUNE_BATCH = 5000


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def canonical(row: AuditEvent) -> str:
    """The stable text an event's hash covers."""
    payload: dict[str, Any] = {
        "id": row.id,
        "created_at": _naive_utc(row.created_at).isoformat(timespec="microseconds"),
        "actor_username": row.actor_username or "",
        "event": row.event,
        "category": row.category,
        "outcome": row.outcome,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "ip_address": row.ip_address,
        "user_agent": row.user_agent,
        "request_id": row.request_id,
        "details": row.details,
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def link(previous: str, row: AuditEvent) -> str:
    return hashlib.sha256((previous + canonical(row)).encode("utf-8")).hexdigest()


def _load_point(session: Session, key: str) -> dict[str, Any] | None:
    raw = get_setting(session, key)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict) or "id" not in value or "hash" not in value:
        return None
    return {"id": int(value["id"]), "hash": str(value["hash"])}


def anchor(session: Session) -> dict[str, Any]:
    return _load_point(session, ANCHOR_KEY) or {"id": 0, "hash": GENESIS}


def _store_point(session: Session, key: str, row_id: int, value: str) -> None:
    # set_setting commits; callers rely on that to persist the seal batch too.
    set_setting(session, key, json.dumps({"id": row_id, "hash": value}))


def seal(session: Session, *, batch: int = _SEAL_BATCH) -> int:
    """Seal unsealed events above the current head, oldest first. Returns
    the number of events sealed. Unsealed rows *below* the head (a
    transaction that committed late) are left for ``verify`` to report."""
    head_row = session.execute(
        select(AuditEvent)
        .where(AuditEvent.chain_hash.is_not(None))
        .order_by(AuditEvent.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if head_row is not None:
        previous, head_id = head_row.chain_hash or GENESIS, head_row.id
    else:
        start = anchor(session)
        previous, head_id = start["hash"], start["id"]
    total = 0
    while True:
        rows = list(
            session.execute(
                select(AuditEvent)
                .where(AuditEvent.id > head_id, AuditEvent.chain_hash.is_(None))
                .order_by(AuditEvent.id)
                .limit(batch)
            ).scalars()
        )
        if not rows:
            break
        for row in rows:
            previous = link(previous, row)
            row.chain_hash = previous
        head_id = rows[-1].id
        total += len(rows)
        _store_point(session, HEAD_KEY, head_id, previous)
        if len(rows) < batch:
            break
    return total


@dataclass
class VerifyResult:
    ok: bool
    checked: int
    sealed_up_to: int | None
    unsealed: int
    first_break_id: int | None = None
    reason: str | None = None
    verified_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify(session: Session) -> VerifyResult:
    """Recompute the chain from the anchor and compare every sealed event."""
    start = anchor(session)
    previous: str = start["hash"]
    after: int = start["id"]
    checked = 0
    sealed_up_to: int | None = None
    first_unsealed: int | None = None
    unsealed = 0

    def broken(row_id: int, reason: str) -> VerifyResult:
        return VerifyResult(
            ok=False,
            checked=checked,
            sealed_up_to=sealed_up_to,
            unsealed=unsealed,
            first_break_id=row_id,
            reason=reason,
        )

    while True:
        # populate_existing: compare what is in the database, never objects
        # this session happens to hold in its identity map.
        rows = list(
            session.execute(
                select(AuditEvent)
                .where(AuditEvent.id > after)
                .order_by(AuditEvent.id)
                .limit(_VERIFY_BATCH)
                .execution_options(populate_existing=True)
            ).scalars()
        )
        if not rows:
            break
        for row in rows:
            if row.chain_hash is None:
                unsealed += 1
                if first_unsealed is None:
                    first_unsealed = row.id
                continue
            if first_unsealed is not None:
                return broken(first_unsealed, "unsealed event inside the sealed range")
            if link(previous, row) != row.chain_hash:
                return broken(row.id, "hash mismatch: this or an earlier event was changed")
            previous = row.chain_hash
            sealed_up_to = row.id
            checked += 1
        after = rows[-1].id
        for row in rows:
            session.expunge(row)

    head = _load_point(session, HEAD_KEY)
    if head is not None and head["id"] > start["id"]:
        if sealed_up_to is None or sealed_up_to < head["id"]:
            return broken(head["id"], "sealed events at the end of the log are missing")
        head_row = session.get(AuditEvent, head["id"], populate_existing=True)
        if head_row is None or head_row.chain_hash != head["hash"]:
            return broken(head["id"], "the recorded chain head does not match")
    return VerifyResult(ok=True, checked=checked, sealed_up_to=sealed_up_to, unsealed=unsealed)


def record_verify_result(session: Session, result: VerifyResult) -> None:
    result.verified_at = utcnow_naive().isoformat(timespec="seconds") + "Z"
    set_setting(session, LAST_VERIFY_KEY, json.dumps(result.as_dict()))


def last_verify_result(session: Session) -> dict[str, Any] | None:
    raw = get_setting(session, LAST_VERIFY_KEY)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def scheduled_verify(session: Session) -> VerifyResult:
    """The worker's daily verification. A new break (or a different one) is
    recorded as a failure event and logged at ERROR; a known break is not
    re-reported every day."""
    previous = last_verify_result(session) or {}
    result = verify(session)
    record_verify_result(session, result)
    if not result.ok and (
        previous.get("ok", True) or previous.get("first_break_id") != result.first_break_id
    ):
        logger.error(
            "audit hash chain is broken",
            extra={"first_break_id": result.first_break_id, "reason": result.reason},
        )
        audit.record_audit_event(
            session,
            event=audit.AUDIT_CHAIN_BROKEN,
            category=audit.CATEGORY_AUDIT,
            outcome=audit.OUTCOME_FAILURE,
            actor_username="system",
            target_type="audit_event",
            target_id=result.first_break_id,
            details={"reason": result.reason, "checked": result.checked},
        )
    return result


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def _forwarding_floor(session: Session) -> int | None:
    """The lowest cursor of the enabled destinations forwarding audit events,
    or None when no destination needs to be waited for."""
    value = session.execute(
        select(func.min(SyslogDestination.last_audit_id)).where(
            SyslogDestination.enabled.is_(True), SyslogDestination.forward_audit.is_(True)
        )
    ).scalar_one_or_none()
    return None if value is None else int(value)


@dataclass
class PrunePlan:
    up_to_id: int  # 0 = nothing to prune
    expired: int  # events older than the retention period
    held_back_by_forwarding: bool
    held_back_by_sealing: bool


def prune_plan(session: Session, settings: Settings, now: datetime | None = None) -> PrunePlan:
    """Which prefix of the log may be removed: only events that are older
    than the retention period, sealed *and* forwarded to every enabled
    destination. Pruning removes a contiguous prefix of ids, so the chain
    stays verifiable from the new anchor."""
    if settings.audit_retention_days <= 0:
        return PrunePlan(0, 0, False, False)
    cutoff = (now or utcnow_naive()) - timedelta(days=settings.audit_retention_days)
    expired = int(
        session.execute(
            select(func.count(AuditEvent.id)).where(AuditEvent.created_at < cutoff)
        ).scalar_one()
    )
    if not expired:
        return PrunePlan(0, 0, False, False)
    newer = session.execute(
        select(func.min(AuditEvent.id)).where(AuditEvent.created_at >= cutoff)
    ).scalar_one_or_none()
    newest = int(session.execute(select(func.max(AuditEvent.id))).scalar_one() or 0)
    # The newest event is always kept: SQLite reuses the ids of deleted rows
    # at the end of a table, which would put new events below the anchor and
    # behind every syslog cursor.
    age_limit = min(int(newer) - 1, newest - 1) if newer is not None else newest - 1
    unsealed = session.execute(
        select(func.min(AuditEvent.id)).where(AuditEvent.chain_hash.is_(None))
    ).scalar_one_or_none()
    sealing_limit = None if unsealed is None else int(unsealed) - 1
    forwarding_limit = _forwarding_floor(session)
    bound = min(b for b in (age_limit, sealing_limit, forwarding_limit) if b is not None)
    candidate = session.execute(
        select(func.max(AuditEvent.id)).where(AuditEvent.id <= bound)
    ).scalar_one_or_none()
    return PrunePlan(
        up_to_id=int(candidate or 0),
        expired=expired,
        held_back_by_forwarding=forwarding_limit is not None and forwarding_limit < age_limit,
        held_back_by_sealing=sealing_limit is not None and sealing_limit < age_limit,
    )


def prune(session: Session, settings: Settings, now: datetime | None = None) -> int:
    """Delete the prunable prefix, move the anchor and record ``audit.pruned``."""
    plan = prune_plan(session, settings, now)
    if plan.up_to_id <= 0:
        return 0
    last = session.get(AuditEvent, plan.up_to_id)
    if last is None or last.chain_hash is None:
        return 0
    anchor_id, anchor_hash = last.id, last.chain_hash
    removed = 0
    while True:
        ids = list(
            session.execute(
                select(AuditEvent.id)
                .where(AuditEvent.id <= plan.up_to_id)
                .order_by(AuditEvent.id)
                .limit(_PRUNE_BATCH)
            ).scalars()
        )
        if not ids:
            break
        session.execute(delete(AuditEvent).where(AuditEvent.id.in_(ids)))
        removed += len(ids)
    # The anchor moves in the same transaction as the deletions.
    _store_point(session, ANCHOR_KEY, anchor_id, anchor_hash)
    audit.record_audit_event(
        session,
        event=audit.AUDIT_PRUNED,
        category=audit.CATEGORY_AUDIT,
        actor_username="system",
        details={
            "count": removed,
            "up_to_id": plan.up_to_id,
            "retention_days": settings.audit_retention_days,
        },
    )
    return removed


def integrity_status(session: Session, settings: Settings) -> dict[str, Any]:
    head = _load_point(session, HEAD_KEY)
    unsealed = int(
        session.execute(
            select(func.count(AuditEvent.id)).where(AuditEvent.chain_hash.is_(None))
        ).scalar_one()
    )
    plan = prune_plan(session, settings)
    return {
        "anchor_id": anchor(session)["id"],
        "sealed_up_to": head["id"] if head else None,
        "unsealed": unsealed,
        "retention_days": settings.audit_retention_days,
        "expired": plan.expired,
        "prunable_up_to": plan.up_to_id or None,
        "prune_held_back_by_forwarding": plan.held_back_by_forwarding,
        "prune_held_back_by_sealing": plan.held_back_by_sealing,
        "last_verify": last_verify_result(session),
    }
