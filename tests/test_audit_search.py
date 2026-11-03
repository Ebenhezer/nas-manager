"""Audit search, export, facets, integrity endpoints and permission-denied
auditing (Phase 13)."""

from __future__ import annotations

import csv
import io
import json
from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.db.base import utcnow_naive
from app.models import AuditEvent, User
from app.services import audit, audit_chain
from app.services.audit import AuditFilters, query_audit_events
from tests.conftest import ADMIN_PASSWORD, login


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


def _seed(app: FastAPI) -> None:
    with app.state.db.session_factory() as session:
        for values in (
            {
                "event": audit.FILE_DELETED,
                "category": audit.CATEGORY_FILE,
                "actor_username": "alice",
                "target_type": "file",
                "target_id": "photos/100%_done.jpg",
                "ip_address": "198.51.100.7",
                "request_id": "req-a",
                "details": {"root": "media", "path": "photos/100%_done.jpg"},
            },
            {
                "event": audit.FILE_RENAMED,
                "category": audit.CATEGORY_FILE,
                "actor_username": "bob",
                "target_type": "file",
                "target_id": "docs/report_final.odt",
                "details": {"new_name": '=HYPERLINK("x")'},
                "request_id": "req-b",
            },
            {
                "event": audit.AUTH_LOGIN_FAILURE,
                "category": audit.CATEGORY_AUTH,
                "outcome": audit.OUTCOME_FAILURE,
                "actor_username": "mallory",
                "ip_address": "203.0.113.5",
            },
        ):
            audit.record_audit_event(session, **values)
        # Backdate the login failure for the time-range filters.
        session.execute(
            update(AuditEvent)
            .where(AuditEvent.actor_username == "mallory")
            .values(created_at=utcnow_naive() - timedelta(days=3))
        )
        session.commit()


def _search(client: TestClient, **params: str) -> list[str]:
    response = client.get("/api/v1/audit/events", params={"limit": "200", **params})
    assert response.status_code == 200, response.text
    return [e["event"] for e in response.json()["events"]]


def test_filters(app: FastAPI, authed_client: TestClient) -> None:
    _seed(app)
    assert _search(authed_client, q="HYPERLINK") == ["file.renamed"]  # inside details
    assert _search(authed_client, q="REPORT_final") == ["file.renamed"]  # case-insensitive
    # LIKE wildcards are literal: "%" and "_" only match themselves.
    assert _search(authed_client, q="100%_") == ["file.deleted"]
    assert _search(authed_client, q="0%d") == []
    assert _search(authed_client, q="t_final") == ["file.renamed"]
    assert _search(authed_client, q="tXfinal") == []
    assert _search(authed_client, ip="198.51.100.7") == ["file.deleted"]
    assert _search(authed_client, request_id="req-b") == ["file.renamed"]
    assert _search(authed_client, target_type="file", target_id="docs/report_final.odt") == [
        "file.renamed"
    ]
    assert _search(authed_client, actor="mallory", outcome="failure") == ["auth.login.failure"]
    since = (utcnow_naive() - timedelta(days=1)).isoformat() + "Z"
    recent = _search(authed_client, since=since, category="file")
    assert sorted(recent) == ["file.deleted", "file.renamed"]
    assert _search(authed_client, until=since, category="auth") == ["auth.login.failure"]
    page = authed_client.get("/api/v1/audit/events", params={"category": "file", "limit": 1})
    body = page.json()
    assert body["total"] == 2 and len(body["events"]) == 1
    assert body["events"][0]["sealed"] is False and body["events"][0]["chain_hash"] is None


def test_query_validation(authed_client: TestClient) -> None:
    assert authed_client.get("/api/v1/audit/events", params={"q": "x" * 201}).status_code == 422
    assert (
        authed_client.get("/api/v1/audit/events", params={"since": "yesterday"}).status_code == 422
    )


def test_legacy_keyword_arguments_still_work(app: FastAPI) -> None:
    _seed(app)
    with app.state.db.session_factory() as session:
        rows, total = query_audit_events(session, category="auth")
        assert total == 1 and rows[0].actor_username == "mallory"
        rows, total = query_audit_events(session, filters=AuditFilters(q="alice"))
        assert total == 1


def test_export_csv_and_jsonl(app: FastAPI, authed_client: TestClient) -> None:
    _seed(app)
    response = authed_client.get(
        "/api/v1/audit/events/export", params={"format": "csv", "category": "file"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["x-audit-rows"] == "2"
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert [r["event"] for r in rows] == ["file.renamed", "file.deleted"]  # newest first
    # A cell that a spreadsheet would evaluate is neutralised.
    assert rows[0]["details"].startswith("{")
    hostile = authed_client.get(
        "/api/v1/audit/events/export", params={"format": "csv", "q": "HYPERLINK"}
    )
    assert "=HYPERLINK" in hostile.text  # inside the JSON cell, which starts with "{"

    jsonl = authed_client.get(
        "/api/v1/audit/events/export", params={"format": "jsonl", "actor": "mallory"}
    )
    assert jsonl.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in jsonl.text.splitlines()]
    assert [line["event"] for line in lines] == ["auth.login.failure"]
    assert lines[0]["actor"] == "mallory" and lines[0]["created_at"].endswith("Z")

    with app.state.db.session_factory() as session:
        exports = list(
            session.execute(
                select(AuditEvent).where(AuditEvent.event == audit.AUDIT_EXPORTED)
            ).scalars()
        )
    assert len(exports) == 3
    assert exports[0].details == {
        "format": "csv",
        "filters": {"category": "file"},
        "rows": 2,
        "truncated": False,
    }


def test_export_formula_guard_on_plain_cells(app: FastAPI, authed_client: TestClient) -> None:
    with app.state.db.session_factory() as session:
        audit.record_audit_event(
            session,
            event=audit.FILE_UPLOADED,
            category=audit.CATEGORY_FILE,
            actor_username="eve",
            target_type="file",
            target_id="=cmd|' /C calc'!A0",
        )
    text = authed_client.get(
        "/api/v1/audit/events/export", params={"format": "csv", "actor": "eve"}
    ).text
    (row,) = list(csv.DictReader(io.StringIO(text)))
    assert row["target_id"] == "'=cmd|' /C calc'!A0"


def test_export_is_capped(app: FastAPI, authed_client: TestClient) -> None:
    app.state.settings.audit_export_max_rows = 100
    with app.state.db.session_factory() as session:
        for i in range(130):
            audit.record_audit_event(
                session,
                event=audit.USER_UPDATED,
                category=audit.CATEGORY_USER,
                target_id=i,
                commit=False,
            )
        session.commit()
    response = authed_client.get(
        "/api/v1/audit/events/export", params={"format": "jsonl", "category": "user"}
    )
    assert len(response.text.splitlines()) == 100
    assert response.headers["x-audit-rows"] == "100"


def test_facets(app: FastAPI, authed_client: TestClient) -> None:
    _seed(app)
    facets = authed_client.get("/api/v1/audit/facets").json()
    assert {"auth", "file", "syslog", "audit", "backup", "config"} <= set(facets["categories"])
    assert facets["categories"] == sorted(facets["categories"])
    assert {"file.deleted", "file.renamed", "auth.login.failure"} <= set(facets["events"])


def test_integrity_and_verify(app: FastAPI, authed_client: TestClient) -> None:
    _seed(app)
    status = authed_client.get("/api/v1/audit/integrity").json()
    assert status["sealed_up_to"] is None and status["unsealed"] >= 3
    assert status["retention_days"] == 90 and status["last_verify"] is None
    with app.state.db.session_factory() as session:
        audit_chain.seal(session)
    result = authed_client.post("/api/v1/audit/verify", headers=_csrf(authed_client)).json()
    assert result["ok"] is True and result["checked"] >= 3
    status = authed_client.get("/api/v1/audit/integrity").json()
    assert status["last_verify"]["ok"] is True and status["sealed_up_to"]
    with app.state.db.session_factory() as session:
        session.execute(
            update(AuditEvent)
            .where(AuditEvent.actor_username == "alice")
            .values(actor_username="nobody")
        )
        session.commit()
    broken = authed_client.post("/api/v1/audit/verify", headers=_csrf(authed_client)).json()
    assert broken["ok"] is False and broken["first_break_id"]
    with app.state.db.session_factory() as session:
        outcomes = list(
            session.execute(
                select(AuditEvent.outcome).where(AuditEvent.event == audit.AUDIT_VERIFIED)
            ).scalars()
        )
    assert outcomes == ["success", "failure"]


def test_permissions(app: FastAPI, admin_user: User, client: TestClient) -> None:
    paths = (
        "/api/v1/audit/events",
        "/api/v1/audit/events/export",
        "/api/v1/audit/facets",
        "/api/v1/audit/integrity",
    )
    for path in paths:
        assert client.get(path).status_code == 401, path
    viewer = _user_client(app, admin_user, "vic", "viewer")
    for path in paths:
        assert viewer.get(path).status_code == 403, path
    assert viewer.post("/api/v1/audit/verify", headers=_csrf(viewer)).status_code == 403
    operator = _user_client(app, admin_user, "olga", "operator")
    for path in paths:
        assert operator.get(path).status_code == 200, path


def test_permission_denials_are_audited_and_throttled(app: FastAPI, admin_user: User) -> None:
    viewer = _user_client(app, admin_user, "vic", "viewer")
    for _ in range(3):
        assert viewer.get("/api/v1/users").status_code == 403
    assert viewer.get("/api/v1/users/1").status_code == 403
    assert viewer.get("/api/v1/audit/events").status_code == 403
    with app.state.db.session_factory() as session:
        rows = list(
            session.execute(
                select(AuditEvent)
                .where(AuditEvent.event == audit.AUTH_PERMISSION_DENIED)
                .order_by(AuditEvent.id)
            ).scalars()
        )
    assert [(r.target_id, r.details["path"]) for r in rows] == [
        ("users:read", "/api/v1/users"),
        ("users:read", "/api/v1/users/{user_id}"),
        ("audit:read", "/api/v1/audit/events"),
    ]
    assert all(r.actor_username == "vic" and r.outcome == "failure" for r in rows)
    assert rows[0].details["method"] == "GET"


def test_lifecycle_events(app: FastAPI) -> None:
    with TestClient(app):
        pass
    with app.state.db.session_factory() as session:
        events = list(
            session.execute(
                select(AuditEvent.event)
                .where(AuditEvent.category == audit.CATEGORY_SYSTEM)
                .order_by(AuditEvent.id)
            ).scalars()
        )
    assert events == [audit.SYSTEM_STARTED, audit.SYSTEM_STOPPED]
