"""The pure alert evaluation engine (Phase 10)."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.services.monitoring import rules as engine
from app.services.monitoring.rules import Observation, OpenAlert, RuleSpec

T0 = datetime(2026, 11, 3, 12, 0, 0)


def _rule(**overrides: object) -> RuleSpec:
    values: dict[str, object] = {
        "id": 1,
        "name": "Disk almost full",
        "metric": "disk_percent",
        "target": None,
        "comparison": "gt",
        "threshold": 90.0,
        "duration_seconds": 300,
        "severity": "critical",
    }
    values.update(overrides)
    return RuleSpec(**values)  # type: ignore[arg-type]


def _disk(path: str, percent: float) -> Observation:
    return Observation(metric="disk_percent", target=path, value=percent)


def _open(target: str, state: str, started: datetime, alert_id: int = 7) -> OpenAlert:
    return OpenAlert(
        id=alert_id, target=target, state=state, started_at=started, last_seen_at=started
    )


def _actions(transitions: list[engine.Transition]) -> list[tuple[str, str]]:
    return [(t.action, t.target) for t in transitions]


def test_breach_opens_a_pending_alert() -> None:
    out = engine.evaluate(_rule(), [_disk("/srv", 95.0)], [], T0)
    assert _actions(out) == [("open", "/srv")]
    assert out[0].value == 95.0
    assert "Disk usage on /srv is 95.0 %" in out[0].message
    assert "above the threshold of 90.0 %" in out[0].message


def test_no_breach_no_alert() -> None:
    assert engine.evaluate(_rule(), [_disk("/srv", 90.0)], [], T0) == []


def test_zero_duration_fires_immediately() -> None:
    out = engine.evaluate(_rule(duration_seconds=0), [_disk("/srv", 95.0)], [], T0)
    assert _actions(out) == [("open_firing", "/srv")]


def test_pending_fires_only_after_the_duration() -> None:
    pending = [_open("/srv", "pending", T0)]
    early = engine.evaluate(_rule(), [_disk("/srv", 95.0)], pending, T0 + timedelta(seconds=299))
    assert _actions(early) == [("touch", "/srv")]
    due = engine.evaluate(_rule(), [_disk("/srv", 95.0)], pending, T0 + timedelta(seconds=300))
    assert _actions(due) == [("fire", "/srv")]


def test_a_flapping_value_never_fires() -> None:
    pending = [_open("/srv", "pending", T0)]
    out = engine.evaluate(_rule(), [_disk("/srv", 85.0)], pending, T0 + timedelta(seconds=60))
    assert _actions(out) == [("drop", "/srv")]


def test_firing_resolves_only_past_the_hysteresis_margin() -> None:
    firing = [_open("/srv", "firing", T0)]
    # 90 % threshold, 5 % margin -> clears at or below 85.5 %.
    still = engine.evaluate(_rule(), [_disk("/srv", 89.0)], firing, T0 + timedelta(minutes=10))
    assert _actions(still) == [("touch", "/srv")]
    cleared = engine.evaluate(_rule(), [_disk("/srv", 85.5)], firing, T0 + timedelta(minutes=10))
    assert _actions(cleared) == [("resolve", "/srv")]


def test_less_than_comparison() -> None:
    rule = _rule(metric="cpu_percent", comparison="lt", threshold=10.0, duration_seconds=0)
    obs = Observation(metric="cpu_percent", target="", value=5.0)
    out = engine.evaluate(rule, [obs], [], T0)
    assert _actions(out) == [("open_firing", "")]
    assert "below the threshold" in out[0].message
    firing = [_open("", "firing", T0)]
    # Must rise to 10.5 (threshold + 5 %) before resolving.
    assert _actions(engine.evaluate(rule, [Observation("cpu_percent", "", 10.2)], firing, T0)) == [
        ("touch", "")
    ]
    assert _actions(engine.evaluate(rule, [Observation("cpu_percent", "", 10.5)], firing, T0)) == [
        ("resolve", "")
    ]


def test_each_target_is_tracked_separately() -> None:
    out = engine.evaluate(
        _rule(duration_seconds=0), [_disk("/a", 95.0), _disk("/b", 50.0), _disk("/c", 99.0)], [], T0
    )
    assert sorted(_actions(out)) == [("open_firing", "/a"), ("open_firing", "/c")]


def test_rule_target_limits_matching() -> None:
    rule = _rule(target="/b", duration_seconds=0)
    out = engine.evaluate(rule, [_disk("/a", 95.0), _disk("/b", 95.0)], [], T0)
    assert _actions(out) == [("open_firing", "/b")]


def test_alert_for_a_target_outside_the_rule_is_resolved() -> None:
    """After a rule is narrowed to one target, alerts for others are closed."""
    rule = _rule(target="/b")
    out = engine.evaluate(rule, [_disk("/a", 95.0)], [_open("/a", "firing", T0)], T0)
    assert _actions(out) == [("resolve", "/a")]


def test_unobserved_target_is_kept_until_stale() -> None:
    firing = [_open("/mnt/usb", "firing", T0)]
    soon = engine.evaluate(_rule(), [], firing, T0 + timedelta(minutes=30))
    assert soon == []
    stale = engine.evaluate(_rule(), [], firing, T0 + engine.STALE_AFTER)
    assert _actions(stale) == [("resolve", "/mnt/usb")]
    pending = [_open("/mnt/usb", "pending", T0)]
    assert _actions(engine.evaluate(_rule(), [], pending, T0 + engine.STALE_AFTER)) == [
        ("drop", "/mnt/usb")
    ]


def test_other_metrics_are_ignored() -> None:
    obs = Observation(metric="memory_percent", target="", value=99.0)
    assert engine.evaluate(_rule(), [obs], [], T0) == []


def test_load_message_has_no_percent_unit() -> None:
    rule = _rule(metric="load_per_core", threshold=2.0, duration_seconds=0)
    out = engine.evaluate(rule, [Observation("load_per_core", "", 3.5)], [], T0)
    assert out[0].message == "Load per core is 3.50, above the threshold of 2.00"


# ---------------------------------------------------------------------------
# Condition metrics from checks
# ---------------------------------------------------------------------------


def _condition_rule(metric: str, duration: int = 0) -> RuleSpec:
    return _rule(metric=metric, threshold=None, duration_seconds=duration, severity="critical")


def test_raid_checks_produce_degraded_and_failed_member_observations() -> None:
    found = engine.check_observations(
        [
            (
                "raid",
                "md0",
                "critical",
                {"degraded": True, "failed_members": ["sdc1"], "summary": "raid1"},
            ),
            ("raid", "md1", "ok", {"degraded": False, "failed_members": []}),
        ]
    )
    degraded = engine.evaluate(_condition_rule("raid_degraded"), found, [], T0)
    assert _actions(degraded) == [("open_firing", "md0")]
    assert degraded[0].message == "RAID array md0 is degraded (raid1)"
    failed = engine.evaluate(_condition_rule("raid_failed_member"), found, [], T0)
    assert _actions(failed) == [("open_firing", "md0")]
    assert "sdc1" in failed[0].message


def test_service_down_and_recovery() -> None:
    down = engine.check_observations(
        [("service", "smbd.service", "critical", {"summary": "failed (failed)"})]
    )
    rule = _condition_rule("service_down", duration=120)
    assert _actions(engine.evaluate(rule, down, [], T0)) == [("open", "smbd.service")]
    up = engine.check_observations(
        [("service", "smbd.service", "ok", {"summary": "active (running)"})]
    )
    firing = [_open("smbd.service", "firing", T0)]
    assert _actions(engine.evaluate(rule, up, firing, T0)) == [("resolve", "smbd.service")]


def test_unknown_checks_never_resolve_or_open() -> None:
    unknown = engine.check_observations(
        [
            ("service", "smbd.service", "unknown", {}),
            ("raid", "md0", "unknown", {}),
            ("smart", "sda", "unknown", {"health": None}),
            ("helper", "helper", "unknown", {}),
        ]
    )
    assert unknown == []
    firing = [_open("smbd.service", "firing", T0)]
    assert engine.evaluate(_condition_rule("service_down"), unknown, firing, T0) == []


def test_smart_and_helper_observations() -> None:
    found = engine.check_observations(
        [
            ("smart", "sda", "critical", {"health": "failed"}),
            ("smart", "sdb", "ok", {"health": "passed"}),
            ("helper", "helper", "critical", {"error": "connection refused"}),
        ]
    )
    smart = engine.evaluate(_condition_rule("smart_failed"), found, [], T0)
    assert _actions(smart) == [("open_firing", "sda")]
    assert smart[0].message == "SMART health check failed for sda"
    helper = engine.evaluate(_condition_rule("helper_unavailable"), found, [], T0)
    assert _actions(helper) == [("open_firing", "helper")]
    assert "connection refused" in helper[0].message


def test_host_observations_skip_missing_values() -> None:
    found = engine.host_observations(
        cpu_percent=10.0,
        memory_percent=20.0,
        swap_percent=0.0,
        load_per_core=None,
        disks=[{"path": "/", "percent": 50.0}, {"path": "", "percent": 1.0}, {"path": "/x"}],
    )
    assert {(o.metric, o.target) for o in found} == {
        ("cpu_percent", ""),
        ("memory_percent", ""),
        ("swap_percent", ""),
        ("disk_percent", "/"),
    }


def test_messages_are_only_rewritten_while_breaching() -> None:
    """A resolution or a hysteresis-held value never claims to be "above the threshold"."""
    firing = [_open("/srv", "firing", T0)]
    held = engine.evaluate(_rule(), [_disk("/srv", 89.0)], firing, T0)
    assert _actions(held) == [("touch", "/srv")]
    assert held[0].message == "" and held[0].value == 89.0
    resolved = engine.evaluate(_rule(), [_disk("/srv", 10.0)], firing, T0)
    assert _actions(resolved) == [("resolve", "/srv")]
    assert resolved[0].message == "" and resolved[0].value == 10.0
    still = engine.evaluate(_rule(), [_disk("/srv", 97.0)], firing, T0)
    assert still[0].message.startswith("Disk usage on /srv is 97.0 %")
