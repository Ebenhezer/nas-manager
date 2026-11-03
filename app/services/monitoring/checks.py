"""Non-metric checks (services, RAID, SMART, helper) and their stored snapshots.

The worker runs these and upserts one ``monitor_checks`` row per target;
pages and the alert engine read the rows. RAID state comes straight from
``/proc/mdstat`` (world-readable, no helper needed); SMART needs root, so it
goes through the privileged helper's existing ``disk.list``/``disk.smart``
operations and is skipped when the helper is disabled.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.monitoring import (
    CHECK_CRITICAL,
    CHECK_KIND_HELPER,
    CHECK_KIND_RAID,
    CHECK_KIND_SERVICE,
    CHECK_KIND_SMART,
    CHECK_OK,
    CHECK_UNKNOWN,
    CHECK_WARNING,
    MonitorCheck,
)
from app.schemas.disks import RaidArray
from app.schemas.monitoring import RaidStatus
from app.services import disks
from app.services.monitoring import services

logger = logging.getLogger(__name__)

MDSTAT_PATH = Path("/proc/mdstat")
HELPER_TARGET = "helper"


def check_key(kind: str, target: str) -> str:
    return f"{kind}:{target}"


def list_checks(session: Session, kind: str | None = None) -> list[MonitorCheck]:
    stmt = select(MonitorCheck).order_by(MonitorCheck.kind, MonitorCheck.target)
    if kind is not None:
        stmt = stmt.where(MonitorCheck.kind == kind)
    return list(session.execute(stmt).scalars())


def _upsert(
    session: Session,
    *,
    kind: str,
    target: str,
    status: str,
    summary: str,
    details: dict[str, Any],
    now: datetime,
) -> MonitorCheck:
    key = check_key(kind, target)
    row = session.get(MonitorCheck, key)
    if row is None:
        row = MonitorCheck(key=key, kind=kind, target=target)
        session.add(row)
    row.status = status
    row.summary = summary[:1000]
    row.details = details
    row.checked_at = now
    return row


def _forget_others(session: Session, kind: str, keep: set[str]) -> None:
    """Drop rows of ``kind`` whose target is no longer checked."""
    stmt = delete(MonitorCheck).where(MonitorCheck.kind == kind)
    if keep:
        stmt = stmt.where(MonitorCheck.key.not_in([check_key(kind, t) for t in keep]))
    session.execute(stmt)


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


def run_service_checks(session: Session, settings: Settings, now: datetime) -> None:
    states = services.unit_states(settings)
    for state in states:
        _upsert(
            session,
            kind=CHECK_KIND_SERVICE,
            target=state.unit,
            status=state.status,
            summary=state.summary,
            details={
                "summary": state.summary,
                "description": state.description,
                "load_state": state.load_state,
                "active_state": state.active_state,
                "sub_state": state.sub_state,
                "unit_file_state": state.unit_file_state,
                "active_since": state.active_since,
                "error": state.error,
            },
            now=now,
        )
    _forget_others(session, CHECK_KIND_SERVICE, {state.unit for state in states})
    session.commit()


# ---------------------------------------------------------------------------
# RAID
# ---------------------------------------------------------------------------


def read_raid(path: Path | None = None) -> RaidStatus:
    source = path or MDSTAT_PATH
    try:
        text = source.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        # No md driver loaded: no software RAID on this host.
        return RaidStatus(available=True, arrays=[])
    except OSError as exc:
        return RaidStatus(available=False, error=f"cannot read {source}: {exc.strerror or exc}")
    return RaidStatus(available=True, arrays=disks.parse_mdstat(text))


def _raid_check(array: RaidArray) -> tuple[str, str, dict[str, Any]]:
    failed = [m.device for m in array.members if m.state == "failed"]
    if array.degraded or failed:
        status = CHECK_CRITICAL
    elif array.state not in (None, "active") or array.sync_action in ("recovery", "resync"):
        status = CHECK_WARNING
    else:
        status = CHECK_OK
    parts = [array.level or "?", array.state or "?"]
    if array.active_disks is not None and array.total_disks is not None:
        parts.append(f"{array.active_disks}/{array.total_disks} devices")
    if array.sync_action:
        parts.append(array.sync_action)
    if array.degraded:
        parts.append("degraded")
    summary = ", ".join(parts)
    return (
        status,
        summary,
        {
            "summary": summary,
            "level": array.level,
            "state": array.state,
            "degraded": array.degraded,
            "failed_members": failed,
            "active_disks": array.active_disks,
            "total_disks": array.total_disks,
            "sync_action": array.sync_action,
        },
    )


def run_raid_checks(session: Session, now: datetime, *, path: Path | None = None) -> None:
    raid = read_raid(path)
    if not raid.available:
        # Keep the previous rows: an unreadable mdstat is not "no arrays".
        logger.warning("RAID status unavailable", extra={"error": raid.error})
        return
    for array in raid.arrays:
        status, summary, details = _raid_check(array)
        _upsert(
            session,
            kind=CHECK_KIND_RAID,
            target=array.name,
            status=status,
            summary=summary,
            details=details,
            now=now,
        )
    _forget_others(session, CHECK_KIND_RAID, {array.name for array in raid.arrays})
    session.commit()


# ---------------------------------------------------------------------------
# Helper and SMART
# ---------------------------------------------------------------------------


def run_helper_check(session: Session, settings: Settings, now: datetime) -> bool:
    """Record whether the helper answers; returns availability."""
    if not settings.helper_enabled:
        _forget_others(session, CHECK_KIND_HELPER, set())
        session.commit()
        return False
    status = disks.helper_status(settings)
    _upsert(
        session,
        kind=CHECK_KIND_HELPER,
        target=HELPER_TARGET,
        status=CHECK_OK if status.available else CHECK_CRITICAL,
        summary="available" if status.available else (status.error or "unavailable"),
        details={"socket": status.socket, "error": status.error},
        now=now,
    )
    session.commit()
    return status.available


def run_smart_checks(session: Session, settings: Settings, now: datetime) -> None:
    """Read SMART health for every physical disk through the helper."""
    try:
        devices = disks.list_disks(settings)
    except Exception as exc:  # the helper is down or refused: keep previous rows
        logger.warning("SMART check skipped", extra={"error": str(exc)})
        return
    seen: set[str] = set()
    for device in devices:
        seen.add(device.name)
        try:
            smart = disks.smart_status(settings, device.name)
        except Exception as exc:
            status, summary = CHECK_UNKNOWN, f"SMART unavailable: {exc}"
            details: dict[str, Any] = {"health": None, "error": str(exc)}
        else:
            if smart.health == "failed":
                status = CHECK_CRITICAL
            elif smart.health == "passed":
                status = CHECK_OK
            else:
                status = CHECK_UNKNOWN
            summary = smart.health or smart.error or "no SMART data"
            details = {
                "health": smart.health,
                "error": smart.error,
                "model": smart.model or device.model,
                "temperature": smart.temperature,
            }
        _upsert(
            session,
            kind=CHECK_KIND_SMART,
            target=device.name,
            status=status,
            summary=summary,
            details=details,
            now=now,
        )
    _forget_others(session, CHECK_KIND_SMART, seen)
    session.commit()
