"""The sync worker loop (Phase 7): scheduling, claiming, leases, pruning."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.db.base import utcnow_naive
from app.db.migrations import PROJECT_ROOT
from app.db.session import Database
from app.models import AuditEvent, RemoteConnection, SyncJob, SyncRun
from app.models.remote import RUN_QUEUED, RUN_RUNNING, RUN_SUCCEEDED
from app.services import remote_sync, remotes, settings_service
from app.worker.runner import Worker
from tests.remote_fakes import LocalDirProvider


@pytest.fixture
def remote_dir(tmp_path: Path) -> Path:
    path = tmp_path / "remote"
    path.mkdir()
    return path


@pytest.fixture
def worker(
    migrated_settings: Settings, db: Database, remote_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Worker:
    monkeypatch.setattr(
        remotes, "build_provider", lambda row, cipher, settings: LocalDirProvider(remote_dir)
    )
    return Worker(migrated_settings, db, owner="test-worker")


def _job(
    db: Database, settings: Settings, *, schedule: int | None = None, enabled: bool = False
) -> int:
    root = settings.resolved_file_roots[0]
    (root / "job").mkdir(exist_ok=True)
    (root / "job" / "a.txt").write_text("a")
    with db.session_factory() as session:
        conn = RemoteConnection(name=f"c{utcnow_naive().timestamp()}", provider="sftp")
        session.add(conn)
        session.flush()
        job = SyncJob(
            name=f"job{conn.id}",
            connection_id=conn.id,
            local_root=str(root),
            local_path="job",
            direction="push",
            schedule_minutes=schedule,
            enabled=enabled,
            next_run_at=utcnow_naive() - timedelta(seconds=1) if enabled else None,
        )
        session.add(job)
        session.commit()
        return job.id


def _runs(db: Database) -> list[SyncRun]:
    with db.session_factory() as session:
        return list(session.execute(select(SyncRun).order_by(SyncRun.id)).scalars())


def test_tick_writes_heartbeat(worker: Worker, db: Database) -> None:
    worker.tick()
    with db.session_factory() as session:
        assert settings_service.get_setting(session, remote_sync.WORKER_HEARTBEAT_KEY)


def test_scheduled_job_is_queued_and_run(
    worker: Worker, db: Database, migrated_settings: Settings, remote_dir: Path
) -> None:
    job_id = _job(db, migrated_settings, schedule=30, enabled=True)
    worker.tick()
    worker.wait_idle()
    runs = _runs(db)
    assert [(r.status, r.trigger) for r in runs] == [(RUN_SUCCEEDED, "schedule")]
    assert (remote_dir / "a.txt").read_text() == "a"
    with db.session_factory() as session:
        job = session.get(SyncJob, job_id)
        assert job is not None
        assert job.next_run_at is not None and job.next_run_at > utcnow_naive() + timedelta(
            minutes=29
        )
        assert job.last_status == RUN_SUCCEEDED
    worker.tick()  # not due again yet
    assert len(_runs(db)) == 1


def test_disabled_or_manual_jobs_are_not_scheduled(
    worker: Worker, db: Database, migrated_settings: Settings
) -> None:
    _job(db, migrated_settings, schedule=None, enabled=False)
    worker.tick()
    assert _runs(db) == []


def test_one_run_per_job_and_concurrency(
    worker: Worker, db: Database, migrated_settings: Settings
) -> None:
    job_id = _job(db, migrated_settings)
    with db.session_factory() as session:
        for _ in range(2):
            session.add(SyncRun(job_id=job_id, status=RUN_QUEUED, trigger="manual"))
        session.commit()
        first = remote_sync.claim_next(session, owner="a", lease_seconds=60, busy_job_ids=set())
        assert first is not None
        # The worker skips jobs it is already running.
        assert (
            remote_sync.claim_next(session, owner="a", lease_seconds=60, busy_job_ids={job_id})
            is None
        )


def test_expired_lease_is_reclaimed(
    worker: Worker, db: Database, migrated_settings: Settings
) -> None:
    job_id = _job(db, migrated_settings)
    with db.session_factory() as session:
        session.add(
            SyncRun(
                job_id=job_id,
                status=RUN_RUNNING,
                trigger="manual",
                lease_owner="dead-worker",
                lease_expires_at=utcnow_naive() - timedelta(seconds=5),
            )
        )
        session.commit()
    worker.tick()
    worker.wait_idle()
    run = _runs(db)[0]
    assert run.status == "failed"
    assert "worker stopped" in (run.error or "")
    with db.session_factory() as session:
        assert session.execute(
            select(AuditEvent).where(AuditEvent.event == "sync_run.failed")
        ).first()


def test_own_leases_are_renewed(worker: Worker, db: Database, migrated_settings: Settings) -> None:
    job_id = _job(db, migrated_settings)
    soon = utcnow_naive() + timedelta(seconds=1)
    with db.session_factory() as session:
        session.add(
            SyncRun(
                job_id=job_id,
                status=RUN_RUNNING,
                trigger="manual",
                lease_owner="test-worker",
                lease_expires_at=soon,
            )
        )
        session.commit()
        remote_sync.renew_leases(session, owner="test-worker", lease_seconds=300)
    run = _runs(db)[0]
    assert run.lease_expires_at is not None and run.lease_expires_at > soon + timedelta(seconds=200)


def test_old_runs_are_pruned(worker: Worker, db: Database, migrated_settings: Settings) -> None:
    job_id = _job(db, migrated_settings)
    old = utcnow_naive() - timedelta(days=migrated_settings.remote_sync_run_retention_days + 1)
    with db.session_factory() as session:
        session.add(SyncRun(job_id=job_id, status=RUN_SUCCEEDED, trigger="manual", created_at=old))
        session.add(SyncRun(job_id=job_id, status=RUN_QUEUED, trigger="manual", created_at=old))
        session.commit()
        assert remote_sync.prune_runs(session, migrated_settings) == 1
    assert [r.status for r in _runs(db)] == [RUN_QUEUED]


def test_stop_event_cancels_a_running_job(
    db: Database, migrated_settings: Settings, remote_dir: Path
) -> None:
    from app.core.secrets import CredentialCipher

    job_id = _job(db, migrated_settings)
    stop = threading.Event()
    stop.set()
    with db.session_factory() as session:
        run = SyncRun(job_id=job_id, status=RUN_RUNNING, trigger="manual")
        session.add(run)
        session.commit()
        remote_sync.process_run(
            session,
            migrated_settings,
            run,
            cipher=CredentialCipher.from_settings(migrated_settings),
            stop=stop,
            provider=LocalDirProvider(remote_dir),
        )
        assert run.status == "cancelled"
    assert not (remote_dir / "a.txt").exists()


def test_run_forever_exits_on_stop(worker: Worker) -> None:
    worker.stop.set()
    worker.run_forever()  # returns immediately, pool shut down


def test_worker_refuses_outdated_schema(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("NAS_")} | {
        "NAS_DATA_DIR": str(tmp_path / "state"),
        "NAS_LOG_TO_FILE": "false",
    }
    result = subprocess.run(
        [sys.executable, "-m", "app.worker"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 1
    assert "alembic upgrade head" in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Monitoring and alerting (Phase 10)
# ---------------------------------------------------------------------------


@pytest.fixture
def quiet_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No systemctl, an empty mdstat: the monitor runs without touching the host."""
    from app.services.monitoring import checks

    monkeypatch.setattr(shutil, "which", lambda name, path=None: None)
    mdstat = tmp_path / "mdstat"
    mdstat.write_text("Personalities :\nunused devices: <none>\n")
    monkeypatch.setattr(checks, "MDSTAT_PATH", mdstat)


def test_monitor_pass_samples_checks_and_waits_for_its_interval(
    worker: Worker, db: Database, quiet_host: None
) -> None:
    from app.models.monitoring import MetricSample, MonitorCheck

    worker.tick()
    worker.wait_idle()
    with db.session_factory() as session:
        assert len(session.execute(select(MetricSample)).all()) == 1
        services = session.execute(select(MonitorCheck)).scalars().all()
        assert {row.status for row in services} == {"unknown"}
        assert len(services) == 5  # the default NAS_MONITOR_SERVICES
    worker.tick()  # the next sample is not due yet
    worker.wait_idle()
    with db.session_factory() as session:
        assert len(session.execute(select(MetricSample)).all()) == 1


def test_monitor_disabled_does_nothing(
    migrated_settings: Settings, db: Database, quiet_host: None
) -> None:
    from app.models.monitoring import MetricSample

    worker = Worker(
        migrated_settings.model_copy(update={"monitor_enabled": False}), db, owner="off"
    )
    worker.tick()
    worker.wait_idle()
    with db.session_factory() as session:
        assert session.execute(select(MetricSample)).first() is None


def test_fired_alert_is_delivered_by_the_notify_pool(
    worker: Worker,
    db: Database,
    migrated_settings: Settings,
    quiet_host: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx2

    from app.core.secrets import CredentialCipher
    from app.models import User
    from app.models.monitoring import Alert, AlertNotification, AlertRule
    from app.services.monitoring import channels, notify

    received: list[httpx2.Request] = []

    def server(request: httpx2.Request) -> httpx2.Response:
        received.append(request)
        return httpx2.Response(200)

    monkeypatch.setattr(notify, "WEBHOOK_TRANSPORT", httpx2.MockTransport(server))
    with db.session_factory() as session:
        admin = User(username="root-admin", password_hash="x")
        session.add(admin)
        session.flush()
        channels.create_channel(
            session,
            values={"name": "hook", "kind": "webhook", "config": {"url": "https://h.example/x"}},
            cipher=CredentialCipher.from_settings(migrated_settings),
            actor=admin,
        )
        for rule in session.execute(select(AlertRule)).scalars():
            rule.enabled = rule.name == "Disk usage high"
            rule.threshold = 0.0
            rule.duration_seconds = 0
        session.commit()

    worker.tick()  # sample + evaluation -> firing alert, notification queued
    worker.wait_idle()
    worker.tick()  # the queued notification is handed to the notify pool
    worker.wait_idle()
    with db.session_factory() as session:
        assert session.execute(select(Alert.state)).scalars().all()
        assert {a.state for a in session.execute(select(Alert)).scalars()} == {"firing"}
        statuses = {n.status for n in session.execute(select(AlertNotification)).scalars()}
        assert statuses == {"sent"}
    assert received and received[0].url.host == "h.example"


def test_monitor_failures_are_isolated(
    worker: Worker,
    db: Database,
    quiet_host: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.models.monitoring import MonitorCheck
    from app.services.monitoring import samples

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("psutil exploded")

    monkeypatch.setattr(samples, "record_sample", broken)
    worker.tick()
    worker.wait_idle()
    assert "metric sample failed" in caplog.text
    with db.session_factory() as session:
        # The checks still ran after the failed sample.
        assert session.execute(select(MonitorCheck)).first() is not None


def test_prune_covers_samples(worker: Worker, db: Database, quiet_host: None) -> None:
    from app.models.monitoring import MetricSample

    with db.session_factory() as session:
        session.add(
            MetricSample(
                sampled_at=utcnow_naive() - timedelta(days=30),
                cpu_percent=1.0,
                memory_percent=1.0,
                swap_percent=0.0,
                net_bytes_sent=0,
                net_bytes_recv=0,
            )
        )
        session.commit()
    worker.tick()
    worker.wait_idle()
    with db.session_factory() as session:
        stamps = session.execute(select(MetricSample.sampled_at)).scalars().all()
        assert stamps and all(s > utcnow_naive() - timedelta(days=1) for s in stamps)
