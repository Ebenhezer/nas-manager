"""The alert evaluation engine: pure functions, no I/O.

Inputs are plain values (rules, observations, the currently open alerts) and
the output is a list of transitions that ``alerts.py`` persists. Keeping this
free of the database makes every timing rule directly testable.

Semantics:

- A rule matches every target of its metric, or only ``rule.target``.
- An *active* observation opens a ``pending`` alert; it becomes ``firing``
  once it has stayed active for ``duration_seconds`` (immediately when 0).
- A pending alert whose condition clears is dropped (it never fired).
- A firing threshold alert resolves only once the value is back past the
  threshold by a hysteresis margin, so a value hovering at the threshold
  does not flap between firing and resolved.
- A target that is no longer observed (an unmounted disk, a unit removed
  from the watch list, SMART unreadable) keeps its alert unchanged, and is
  resolved only after ``STALE_AFTER`` without any observation.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.models.monitoring import (
    ALERT_FIRING,
    ALERT_PENDING,
    CHECK_CRITICAL,
    CHECK_KIND_HELPER,
    CHECK_KIND_RAID,
    CHECK_KIND_SERVICE,
    CHECK_KIND_SMART,
    CHECK_OK,
    CHECK_WARNING,
    COMPARISON_LT,
    METRIC_CPU,
    METRIC_DISK,
    METRIC_HELPER_UNAVAILABLE,
    METRIC_LOAD,
    METRIC_MEMORY,
    METRIC_RAID_DEGRADED,
    METRIC_RAID_FAILED_MEMBER,
    METRIC_SERVICE_DOWN,
    METRIC_SMART_FAILED,
    METRIC_SWAP,
    THRESHOLD_METRICS,
)

HYSTERESIS_FRACTION = 0.05
STALE_AFTER = timedelta(hours=1)

ACTION_OPEN = "open"  # new alert, pending
ACTION_OPEN_FIRING = "open_firing"  # new alert, firing straight away
ACTION_FIRE = "fire"  # pending -> firing
ACTION_TOUCH = "touch"  # still active: refresh value and last_seen
ACTION_DROP = "drop"  # pending alert cleared before it fired
ACTION_RESOLVE = "resolve"  # firing -> resolved

_LABELS = {
    METRIC_CPU: "CPU usage",
    METRIC_LOAD: "Load per core",
    METRIC_MEMORY: "Memory usage",
    METRIC_SWAP: "Swap usage",
    METRIC_DISK: "Disk usage",
}
_UNITS = {METRIC_LOAD: ""}


@dataclass(frozen=True)
class RuleSpec:
    id: int
    name: str
    metric: str
    target: str | None
    comparison: str
    threshold: float | None
    duration_seconds: int
    severity: str


@dataclass(frozen=True)
class Observation:
    metric: str
    target: str
    # Threshold metrics carry a value; condition metrics carry ``active``.
    value: float | None = None
    active: bool = False
    detail: str = ""


@dataclass(frozen=True)
class OpenAlert:
    id: int
    target: str
    state: str
    started_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True)
class Transition:
    action: str
    rule_id: int
    target: str
    alert_id: int | None
    value: float | None
    message: str


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


def host_observations(
    *,
    cpu_percent: float | None,
    memory_percent: float | None,
    swap_percent: float | None,
    load_per_core: float | None,
    disks: Iterable[dict[str, Any]],
) -> list[Observation]:
    """Threshold observations from one host sample (host metrics have target "")."""
    found: list[Observation] = []
    for metric, value in (
        (METRIC_CPU, cpu_percent),
        (METRIC_MEMORY, memory_percent),
        (METRIC_SWAP, swap_percent),
        (METRIC_LOAD, load_per_core),
    ):
        if value is not None:
            found.append(Observation(metric=metric, target="", value=float(value)))
    for disk in disks:
        path = str(disk.get("path") or "")
        percent = disk.get("percent")
        if path and percent is not None:
            found.append(Observation(metric=METRIC_DISK, target=path, value=float(percent)))
    return found


def check_observations(
    checks: Iterable[tuple[str, str, str, dict[str, Any]]],
) -> list[Observation]:
    """Condition observations from stored checks: ``(kind, target, status, details)``.

    ``unknown`` checks yield nothing, so an unreadable source never resolves
    (or raises) an alert by itself.
    """
    found: list[Observation] = []
    for kind, target, status, details in checks:
        if kind == CHECK_KIND_SERVICE:
            if status in (CHECK_OK, CHECK_WARNING, CHECK_CRITICAL):
                detail = str(details.get("summary") or "")
                found.append(
                    Observation(
                        metric=METRIC_SERVICE_DOWN,
                        target=target,
                        active=status == CHECK_CRITICAL,
                        detail=detail,
                    )
                )
        elif kind == CHECK_KIND_RAID:
            if status in (CHECK_OK, CHECK_WARNING, CHECK_CRITICAL):
                failed = [str(m) for m in details.get("failed_members") or []]
                found.append(
                    Observation(
                        metric=METRIC_RAID_DEGRADED,
                        target=target,
                        active=bool(details.get("degraded")),
                        detail=str(details.get("summary") or ""),
                    )
                )
                found.append(
                    Observation(
                        metric=METRIC_RAID_FAILED_MEMBER,
                        target=target,
                        active=bool(failed),
                        detail=", ".join(failed),
                    )
                )
        elif kind == CHECK_KIND_SMART:
            health = details.get("health")
            if health in ("passed", "failed"):
                found.append(
                    Observation(
                        metric=METRIC_SMART_FAILED, target=target, active=health == "failed"
                    )
                )
        elif kind == CHECK_KIND_HELPER and status in (CHECK_OK, CHECK_CRITICAL):
            found.append(
                Observation(
                    metric=METRIC_HELPER_UNAVAILABLE,
                    target=target,
                    active=status == CHECK_CRITICAL,
                    detail=str(details.get("error") or ""),
                )
            )
    return found


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _threshold(rule: RuleSpec) -> float:
    # Threshold metrics always carry one (validated when the rule is written).
    return rule.threshold if rule.threshold is not None else 0.0


def _breaches(rule: RuleSpec, value: float) -> bool:
    if rule.comparison == COMPARISON_LT:
        return value < _threshold(rule)
    return value > _threshold(rule)


def _cleared(rule: RuleSpec, value: float) -> bool:
    """Past the threshold by the hysteresis margin (for firing alerts)."""
    threshold = _threshold(rule)
    margin = abs(threshold) * HYSTERESIS_FRACTION
    if rule.comparison == COMPARISON_LT:
        return value >= threshold + margin
    return value <= threshold - margin


def _fmt(metric: str, value: float) -> str:
    unit = _UNITS.get(metric, " %")
    return f"{value:.2f}{unit}" if metric == METRIC_LOAD else f"{value:.1f}{unit}"


def describe(rule: RuleSpec, obs: Observation) -> str:
    """Human-readable alert message (also used in notifications)."""
    if rule.metric in THRESHOLD_METRICS and obs.value is not None:
        label = _LABELS[rule.metric]
        where = f" on {obs.target}" if obs.target else ""
        relation = "below" if rule.comparison == COMPARISON_LT else "above"
        return (
            f"{label}{where} is {_fmt(rule.metric, obs.value)}, "
            f"{relation} the threshold of {_fmt(rule.metric, _threshold(rule))}"
        )
    suffix = f" ({obs.detail})" if obs.detail else ""
    if rule.metric == METRIC_RAID_DEGRADED:
        return f"RAID array {obs.target} is degraded{suffix}"
    if rule.metric == METRIC_RAID_FAILED_MEMBER:
        return f"RAID array {obs.target} has failed members: {obs.detail or '?'}"
    if rule.metric == METRIC_SMART_FAILED:
        return f"SMART health check failed for {obs.target}"
    if rule.metric == METRIC_SERVICE_DOWN:
        return f"Service {obs.target} is not running{suffix}"
    if rule.metric == METRIC_HELPER_UNAVAILABLE:
        return f"The privileged helper is unavailable{suffix}"
    return f"{rule.name}: {obs.target}"  # pragma: no cover - every metric handled above


def _matches(rule: RuleSpec, obs: Observation) -> bool:
    return obs.metric == rule.metric and (not rule.target or obs.target == rule.target)


def evaluate(
    rule: RuleSpec,
    observations: Iterable[Observation],
    open_alerts: Iterable[OpenAlert],
    now: datetime,
) -> list[Transition]:
    """Transitions for one enabled rule."""
    threshold_metric = rule.metric in THRESHOLD_METRICS
    observed = {obs.target: obs for obs in observations if _matches(rule, obs)}
    alerts = {alert.target: alert for alert in open_alerts}
    duration = timedelta(seconds=rule.duration_seconds)
    out: list[Transition] = []

    def emit(action: str, target: str, alert_id: int | None, obs: Observation) -> None:
        # The message describes a breach, so it is only (re)written while the
        # value is past the threshold: a resolution, or a firing alert held by
        # hysteresis below the threshold, keeps the message it fired with.
        breaching = action not in (ACTION_DROP, ACTION_RESOLVE) and (
            not threshold_metric or (obs.value is not None and _breaches(rule, obs.value))
        )
        message = describe(rule, obs) if breaching else ""
        out.append(Transition(action, rule.id, target, alert_id, obs.value, message))

    for target, obs in observed.items():
        alert = alerts.get(target)
        if threshold_metric:
            if obs.value is None:
                continue
            if alert is not None and alert.state == ALERT_FIRING:
                active = not _cleared(rule, obs.value)
            else:
                active = _breaches(rule, obs.value)
        else:
            active = obs.active

        if alert is None:
            if active:
                emit(ACTION_OPEN_FIRING if not duration else ACTION_OPEN, target, None, obs)
            continue
        if not active:
            emit(
                ACTION_DROP if alert.state == ALERT_PENDING else ACTION_RESOLVE,
                target,
                alert.id,
                obs,
            )
        elif alert.state == ALERT_PENDING and now - alert.started_at >= duration:
            emit(ACTION_FIRE, target, alert.id, obs)
        else:
            emit(ACTION_TOUCH, target, alert.id, obs)

    for target, alert in alerts.items():
        if target in observed:
            continue
        out_of_scope = bool(rule.target) and target != rule.target
        if out_of_scope or now - alert.last_seen_at >= STALE_AFTER:
            action = ACTION_DROP if alert.state == ALERT_PENDING else ACTION_RESOLVE
            out.append(Transition(action, rule.id, target, alert.id, None, ""))
    return out
