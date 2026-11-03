"""Monitoring, alerts, rules and channels over the API (Phase 10)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.base import utcnow_naive
from app.models import AuditEvent, User
from app.models.monitoring import Alert, AlertChannel, AlertRule, MetricSample, MonitorCheck
from app.services import remote_sync, settings_service
from app.services.monitoring import checks, notify
from tests.conftest import ADMIN_PASSWORD, login

HOOK = {
    "name": "ntfy",
    "kind": "webhook",
    "config": {"url": "https://ntfy.example/nas"},
    "secrets": {"authorization": "Bearer very-secret-token"},
}


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


def _firing_alert(app: FastAPI, *, target: str = "/srv") -> int:
    with app.state.db.session_factory() as session:
        rule = session.execute(
            select(AlertRule).where(AlertRule.name == "Disk almost full")
        ).scalar_one()
        now = utcnow_naive()
        alert = Alert(
            rule_id=rule.id,
            target=target,
            state="firing",
            severity="critical",
            message=f"Disk usage on {target} is 99.0 %",
            value=99.0,
            started_at=now,
            firing_at=now,
            last_seen_at=now,
        )
        session.add(alert)
        session.commit()
        return alert.id


def test_permissions(app: FastAPI, admin_user: User, client: TestClient) -> None:
    for path in ("/api/v1/monitoring/overview", "/api/v1/alerts", "/api/v1/alert-rules"):
        assert client.get(path).status_code == 401, path
    viewer = _user_client(app, admin_user, "vic", "viewer")
    for path in ("/api/v1/monitoring/overview", "/api/v1/alerts/summary", "/api/v1/alert-channels"):
        assert viewer.get(path).status_code == 403, path
    assert viewer.get("/monitoring").status_code in (401, 403)
    assert viewer.get("/alerts").status_code in (401, 403)

    operator = _user_client(app, admin_user, "olga", "operator")
    for path in (
        "/api/v1/monitoring/overview",
        "/api/v1/monitoring/history",
        "/api/v1/monitoring/processes",
        "/api/v1/monitoring/services",
        "/api/v1/monitoring/raid",
        "/api/v1/monitoring/checks",
        "/api/v1/alerts",
        "/api/v1/alerts/summary",
        "/api/v1/alert-rules",
        "/api/v1/alert-channels",
        "/monitoring",
        "/alerts",
    ):
        assert operator.get(path).status_code == 200, path
    headers = _csrf(operator)
    rule = {"name": "x", "metric": "cpu_percent", "threshold": 50}
    assert operator.post("/api/v1/alert-rules", json=rule, headers=headers).status_code == 403
    assert operator.patch("/api/v1/alert-rules/1", json={}, headers=headers).status_code == 403
    assert operator.post("/api/v1/alert-channels", json=HOOK, headers=headers).status_code == 403
    alert_id = _firing_alert(app)
    ack = operator.post(f"/api/v1/alerts/{alert_id}/acknowledge", headers=headers)
    assert ack.status_code == 403
    # The operator page hides write controls.
    assert 'data-can-write="false"' in operator.get("/alerts").text
    assert 'id="rule-form"' not in operator.get("/alerts").text


def test_overview_reports_worker_offline_and_checks(
    authed_client: TestClient, app: FastAPI
) -> None:
    with app.state.db.session_factory() as session:
        row = MonitorCheck(key="service:smbd.service", kind="service", target="smbd.service")
        row.status, row.summary, row.checked_at = "ok", "active (running)", utcnow_naive()
        row.details = {"description": "Samba", "active_state": "active", "sub_state": "running"}
        session.add(row)
        session.commit()
    body = authed_client.get("/api/v1/monitoring/overview").json()
    assert body["enabled"] is True
    assert body["summary"]["memory"]["total"] > 0
    assert [c["key"] for c in body["checks"]] == ["service:smbd.service"]
    assert body["alerts"]["worker_online"] is False
    assert [s["key"] for s in body["alerts"]["synthetic"]] == ["worker_offline"]
    services = authed_client.get("/api/v1/monitoring/services").json()
    assert services[0]["unit"] == "smbd.service"
    assert services[0]["description"] == "Samba"

    with app.state.db.session_factory() as session:
        remote_sync.write_heartbeat(session)
        assert settings_service.get_setting(session, remote_sync.WORKER_HEARTBEAT_KEY)
    summary = authed_client.get("/api/v1/alerts/summary").json()
    assert summary["worker_online"] is True and summary["synthetic"] == []


def test_history_processes_and_raid(
    authed_client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with app.state.db.session_factory() as session:
        sample = MetricSample(
            sampled_at=utcnow_naive(),
            cpu_percent=42.0,
            memory_percent=10.0,
            swap_percent=0.0,
            net_bytes_sent=0,
            net_bytes_recv=0,
        )
        sample.disks = [{"path": "/", "percent": 12.0}]
        session.add(sample)
        session.commit()
    history = authed_client.get("/api/v1/monitoring/history?range=6h").json()
    assert history["range"] == "6h" and history["points"][0]["cpu_percent"] == 42.0
    assert authed_client.get("/api/v1/monitoring/history?range=1y").status_code == 422

    procs = authed_client.get("/api/v1/monitoring/processes?sort=memory&limit=3").json()
    assert 0 < len(procs) <= 3
    assert all("cmdline" not in p for p in procs)
    assert authed_client.get("/api/v1/monitoring/processes?sort=name").status_code == 422

    mdstat = tmp_path / "mdstat"
    mdstat.write_text("md0 : active raid1 sdb1[1] sda1[0]\n      10 blocks [2/1] [U_]\n")
    monkeypatch.setattr(checks, "MDSTAT_PATH", mdstat)
    raid = authed_client.get("/api/v1/monitoring/raid").json()
    assert raid["available"] is True
    assert raid["arrays"][0]["name"] == "md0" and raid["arrays"][0]["degraded"] is True


def test_rule_crud(authed_client: TestClient, csrf_header: dict[str, str]) -> None:
    rules = authed_client.get("/api/v1/alert-rules").json()
    assert len(rules) == 10 and all(r["builtin"] for r in rules)

    payload = {
        "name": "Backup disk",
        "metric": "disk_percent",
        "target": "/mnt/backup",
        "threshold": 70,
        "duration_seconds": 60,
        "severity": "warning",
    }
    created = authed_client.post("/api/v1/alert-rules", json=payload, headers=csrf_header)
    assert created.status_code == 201, created.text
    rule = created.json()
    assert rule["builtin"] is False and rule["open_alerts"] == 0
    dup = authed_client.post("/api/v1/alert-rules", json=payload, headers=csrf_header)
    assert dup.status_code == 409
    bad = authed_client.post(
        "/api/v1/alert-rules", json={**payload, "name": "b", "metric": "nope"}, headers=csrf_header
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["details"]["metric"] == "nope"

    patched = authed_client.patch(
        f"/api/v1/alert-rules/{rule['id']}",
        json={"threshold": 75, "target": "", "severity": "critical"},
        headers=csrf_header,
    ).json()
    assert patched["threshold"] == 75 and patched["target"] is None
    assert patched["severity"] == "critical"
    cleared = authed_client.patch(
        f"/api/v1/alert-rules/{rule['id']}", json={"threshold": None}, headers=csrf_header
    )
    assert cleared.status_code == 400

    builtin = next(r for r in rules if r["name"] == "Service down")
    assert (
        authed_client.delete(
            f"/api/v1/alert-rules/{builtin['id']}", headers=csrf_header
        ).status_code
        == 409
    )
    disabled = authed_client.patch(
        f"/api/v1/alert-rules/{builtin['id']}", json={"enabled": False}, headers=csrf_header
    )
    assert disabled.json()["enabled"] is False
    assert (
        authed_client.delete(f"/api/v1/alert-rules/{rule['id']}", headers=csrf_header).status_code
        == 204
    )
    assert (
        authed_client.patch("/api/v1/alert-rules/9999", json={}, headers=csrf_header).status_code
        == 404
    )


def test_alerts_list_and_acknowledge(
    authed_client: TestClient, csrf_header: dict[str, str], app: FastAPI
) -> None:
    alert_id = _firing_alert(app)
    page = authed_client.get("/api/v1/alerts?state=open").json()
    assert page["total"] == 1
    item = page["items"][0]
    assert item["rule_name"] == "Disk almost full" and item["metric"] == "disk_percent"
    assert authed_client.get("/api/v1/alerts?state=resolved").json()["total"] == 0
    assert authed_client.get("/api/v1/alerts?state=bogus").status_code == 400
    assert authed_client.get("/api/v1/alerts?severity=warning").json()["total"] == 0

    summary = authed_client.get("/api/v1/alerts/summary").json()
    assert summary["counts"]["firing"] == 1 and summary["counts"]["unacknowledged"] == 1

    acked = authed_client.post(f"/api/v1/alerts/{alert_id}/acknowledge", headers=csrf_header)
    assert acked.status_code == 200
    assert acked.json()["acknowledged_by"] == "admin"
    summary = authed_client.get("/api/v1/alerts/summary").json()
    assert summary["counts"]["unacknowledged"] == 0
    assert (
        authed_client.post("/api/v1/alerts/999/acknowledge", headers=csrf_header).status_code == 404
    )


def test_channel_lifecycle_never_leaks_secrets(
    authed_client: TestClient,
    csrf_header: dict[str, str],
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = authed_client.post("/api/v1/alert-channels", json=HOOK, headers=csrf_header)
    assert created.status_code == 201, created.text
    channel = created.json()
    assert channel["secrets_set"] == ["authorization"]
    assert channel["credentials_readable"] is True
    assert "very-secret-token" not in created.text
    listed = authed_client.get("/api/v1/alert-channels")
    assert "very-secret-token" not in listed.text

    patched = authed_client.patch(
        f"/api/v1/alert-channels/{channel['id']}",
        json={"config": {"url": "https://ntfy.example/other"}, "min_severity": "critical"},
        headers=csrf_header,
    ).json()
    assert patched["secrets_set"] == ["authorization"]
    assert patched["config"]["url"] == "https://ntfy.example/other"
    assert patched["min_severity"] == "critical"

    received: list[httpx2.Request] = []

    def server(request: httpx2.Request) -> httpx2.Response:
        received.append(request)
        return httpx2.Response(200)

    monkeypatch.setattr(notify, "WEBHOOK_TRANSPORT", httpx2.MockTransport(server))
    tested = authed_client.post(
        f"/api/v1/alert-channels/{channel['id']}/test", headers=csrf_header
    ).json()
    assert tested == {"ok": True, "message": "Test notification sent"}
    assert received[0].headers["Authorization"] == "Bearer very-secret-token"
    assert json.loads(received[0].content)["event"] == "test"
    after = authed_client.get("/api/v1/alert-channels").json()[0]
    assert after["last_test_ok"] is True

    monkeypatch.setattr(
        notify, "WEBHOOK_TRANSPORT", httpx2.MockTransport(lambda request: httpx2.Response(401))
    )
    failed = authed_client.post(
        f"/api/v1/alert-channels/{channel['id']}/test", headers=csrf_header
    ).json()
    assert failed == {"ok": False, "message": "webhook answered HTTP 401"}

    with app.state.db.session_factory() as session:
        stored = session.execute(select(AlertChannel)).scalar_one()
        assert "very-secret-token" not in (stored.secrets_encrypted or "")
        assert "very-secret-token" not in stored.config_json
        audit_rows = session.execute(
            select(AuditEvent).where(AuditEvent.category == "monitoring")
        ).scalars()
        assert "very-secret-token" not in json.dumps([row.details for row in audit_rows])

    assert (
        authed_client.delete(
            f"/api/v1/alert-channels/{channel['id']}", headers=csrf_header
        ).status_code
        == 204
    )
    assert authed_client.get("/api/v1/alert-channels").json() == []


def test_channel_validation_errors(authed_client: TestClient, csrf_header: dict[str, str]) -> None:
    def post(**overrides: Any) -> int:
        payload = {**HOOK, **overrides}
        return authed_client.post(
            "/api/v1/alert-channels", json=payload, headers=csrf_header
        ).status_code

    assert post(config={"url": "http://ntfy.example/nas"}) == 400
    assert post(config={"url": "http://ntfy.example/nas"}, allow_insecure=True) == 201
    assert post(name="x2", kind="sms") == 400
    assert post(name="x3", min_severity="info") == 400
    assert post(name="x4", secrets={"password": "x"}) == 400
    assert post(name="ntfy", config={"url": "https://a.example/"}) == 409


def test_channel_endpoints_refuse_api_tokens(
    authed_client: TestClient, csrf_header: dict[str, str], app: FastAPI
) -> None:
    token = authed_client.post(
        "/api/v1/tokens",
        json={"name": "script", "scopes": ["system:read", "system:write"]},
        headers=csrf_header,
    ).json()["token"]
    channel = authed_client.post("/api/v1/alert-channels", json=HOOK, headers=csrf_header).json()
    with TestClient(app) as bearer:
        bearer.headers["Authorization"] = f"Bearer {token}"
        assert bearer.get("/api/v1/alert-channels").status_code == 200
        assert bearer.get("/api/v1/monitoring/overview").status_code == 200
        assert bearer.post("/api/v1/alert-channels", json=HOOK).status_code == 403
        assert bearer.patch(f"/api/v1/alert-channels/{channel['id']}", json={}).status_code == 403
        assert bearer.post(f"/api/v1/alert-channels/{channel['id']}/test").status_code == 403
        # Rules carry no credentials: a scoped token may manage them.
        rule = {"name": "api rule", "metric": "cpu_percent", "threshold": 90}
        assert bearer.post("/api/v1/alert-rules", json=rule).status_code == 201


def test_info_advertises_monitoring(client: TestClient) -> None:
    features = client.get("/api/v1/info").json()["features"]
    assert "monitoring" in features and "alerting" in features
