"""The sync worker loop.

One process, started as ``python -m app.worker`` by its own systemd unit,
under the same unprivileged account as the web application. Each tick it:

1. writes a heartbeat (shown in the UI),
2. renews the leases of the runs it is executing,
3. fails runs whose owning worker died (lease expired),
4. queues runs for enabled jobs whose schedule is due,
5. claims queued runs (atomically) up to ``NAS_WORKER_CONCURRENCY``, at most
   one per job, and runs them on a thread pool,
6. runs media work (Phase 8: library scans and thumbnail batches) on its own
   single-thread pool, so photos never take a sync slot, and video
   transcodes (Phase 9) on a third single-thread pool, so a long transcode
   never delays scans or thumbnails, and
7. runs the monitor pass (Phase 10: host sample, service/RAID/helper/SMART
   checks, alert rule evaluation) on its own single-thread pool when due,
   and delivers queued alert notifications on another, so a slow SMTP
   server or ``smartctl`` never delays a tick, and
8. occasionally prunes old run history, the media journal, orphaned
   thumbnail cache files, old metric samples and resolved alerts.

On SIGTERM/SIGINT it stops claiming, asks running jobs to stop after their
current file and waits for them.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.secrets import CredentialCipher
from app.db.base import utcnow_naive
from app.db.session import Database
from app.models import MediaLibrary, MediaTranscode, SyncRun
from app.services import remote_sync
from app.services.media import jobs as media_jobs
from app.services.media import transcode
from app.services.monitoring import alerts as monitor_alerts
from app.services.monitoring import checks as monitor_checks
from app.services.monitoring import notify as monitor_notify
from app.services.monitoring import samples as monitor_samples

logger = logging.getLogger(__name__)

_PRUNE_INTERVAL_SECONDS = 3600


class Worker:
    def __init__(self, settings: Settings, db: Database, *, owner: str | None = None) -> None:
        self.settings = settings
        self.db = db
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}"
        self.stop = threading.Event()
        self.cipher = CredentialCipher.from_settings(settings)
        self.pool = ThreadPoolExecutor(
            max_workers=settings.worker_concurrency, thread_name_prefix="sync"
        )
        self.running: dict[int, Future[None]] = {}
        self.media_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media")
        self.media_future: Future[None] | None = None
        self.transcode_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transcode")
        self.transcode_future: Future[None] | None = None
        self.monitor_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="monitor")
        self.monitor_future: Future[None] | None = None
        self.notify_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notify")
        self.notify_future: Future[None] | None = None
        # Monotonic deadlines; 0 means "due on the first tick".
        self._next_sample = 0.0
        self._next_check = 0.0
        self._next_smart = 0.0
        self._last_prune = 0.0

    def tick(self) -> None:
        """One scheduling pass (also called directly by tests)."""
        self.running = {job: fut for job, fut in self.running.items() if not fut.done()}
        lease = self.settings.remote_sync_lease_seconds
        with self.db.session_factory() as session:
            remote_sync.write_heartbeat(session)
            remote_sync.renew_leases(session, owner=self.owner, lease_seconds=lease)
            now = utcnow_naive()
            reclaimed = remote_sync.reclaim_expired(session, now)
            if reclaimed:
                logger.warning(
                    "reclaimed sync runs with expired leases", extra={"count": reclaimed}
                )
            remote_sync.enqueue_due_jobs(session, now)
            while not self.stop.is_set() and len(self.running) < self.settings.worker_concurrency:
                run = remote_sync.claim_next(
                    session, owner=self.owner, lease_seconds=lease, busy_job_ids=set(self.running)
                )
                if run is None:
                    break
                logger.info("sync run started", extra={"run_id": run.id, "job_id": run.job_id})
                self.running[run.job_id] = self.pool.submit(self._execute, run.id)
            self._media_tick(session, now)
            self._transcode_tick(session, now)
            self._monitor_tick(session, now)
            if time.monotonic() - self._last_prune >= _PRUNE_INTERVAL_SECONDS:
                self._last_prune = time.monotonic()
                pruned = remote_sync.prune_runs(session, self.settings)
                if pruned:
                    logger.info("pruned old sync runs", extra={"count": pruned})
                media_pruned = media_jobs.prune(session, self.settings)
                if any(media_pruned.values()):
                    logger.info("pruned media journal and cache", extra=media_pruned)
                samples_pruned = monitor_samples.prune(session, self.settings)
                alerts_pruned = monitor_alerts.prune(session, self.settings)
                if samples_pruned or alerts_pruned:
                    logger.info(
                        "pruned metric samples and resolved alerts",
                        extra={"samples": samples_pruned, "alerts": alerts_pruned},
                    )

    def _media_tick(self, session: Session, now: datetime) -> None:
        lease = self.settings.media_scan_lease_seconds
        media_jobs.renew_scan_leases(session, owner=self.owner, lease_seconds=lease)
        reclaimed = media_jobs.reclaim_scans(session, now)
        if reclaimed:
            logger.warning("re-queued media scans with expired leases", extra={"count": reclaimed})
        media_jobs.enqueue_due_scans(session, self.settings, now)
        if self.stop.is_set() or (self.media_future is not None and not self.media_future.done()):
            return
        library = media_jobs.claim_scan(session, owner=self.owner, lease_seconds=lease)
        if library is not None:
            logger.info("media scan started", extra={"library_id": library.id})
            self.media_future = self.media_pool.submit(self._scan, library.id)
        elif media_jobs.status(session)["pending_thumbnails"]:
            self.media_future = self.media_pool.submit(self._thumbnails)

    def _transcode_tick(self, session: Session, now: datetime) -> None:
        lease = self.settings.media_transcode_lease_seconds
        transcode.renew_leases(session, owner=self.owner, lease_seconds=lease)
        reclaimed = transcode.reclaim(session, now)
        if reclaimed:
            logger.warning("re-queued transcodes with expired leases", extra={"count": reclaimed})
        busy = self.transcode_future is not None and not self.transcode_future.done()
        if self.stop.is_set() or busy or not transcode.is_available(self.settings):
            return
        row = transcode.claim(session, owner=self.owner, lease_seconds=lease)
        if row is not None:
            logger.info("transcode started", extra={"item_id": row.item_id})
            self.transcode_future = self.transcode_pool.submit(self._transcode, row.id)

    def _monitor_tick(self, session: Session, now: datetime) -> None:
        if not self.settings.monitor_enabled or self.stop.is_set():
            return
        clock = time.monotonic()
        monitor_busy = self.monitor_future is not None and not self.monitor_future.done()
        if not monitor_busy:
            sample = clock >= self._next_sample
            check = clock >= self._next_check
            smart = (
                check
                and self.settings.monitor_smart_interval_minutes > 0
                and clock >= self._next_smart
            )
            if sample:
                self._next_sample = clock + self.settings.monitor_sample_seconds
            if check:
                self._next_check = clock + self.settings.monitor_check_seconds
            if smart:
                self._next_smart = clock + 60 * self.settings.monitor_smart_interval_minutes
            if sample or check:
                self.monitor_future = self.monitor_pool.submit(
                    self._monitor, sample=sample, check=check, smart=smart
                )
        notify_busy = self.notify_future is not None and not self.notify_future.done()
        if not notify_busy and monitor_notify.pending_count(session, now):
            self.notify_future = self.notify_pool.submit(self._notify)

    def _monitor(self, *, sample: bool, check: bool, smart: bool) -> None:
        """One monitor pass; each step is isolated so one failure never hides the rest."""
        with self.db.session_factory() as session:
            now = utcnow_naive()
            row = None
            if sample:
                try:
                    row, _ = monitor_samples.record_sample(session, self.settings, now)
                except Exception:
                    session.rollback()
                    logger.exception("metric sample failed")
            if check:
                try:
                    monitor_checks.run_service_checks(session, self.settings, now)
                except Exception:
                    session.rollback()
                    logger.exception("monitor check failed", extra={"check": "services"})
                try:
                    monitor_checks.run_raid_checks(session, now)
                except Exception:
                    session.rollback()
                    logger.exception("monitor check failed", extra={"check": "raid"})
                try:
                    helper_up = monitor_checks.run_helper_check(session, self.settings, now)
                    if smart and helper_up:
                        monitor_checks.run_smart_checks(session, self.settings, now)
                except Exception:
                    session.rollback()
                    logger.exception("monitor check failed", extra={"check": "helper/smart"})
            try:
                changed = monitor_alerts.evaluate(session, self.settings, now, row)
            except Exception:
                session.rollback()
                logger.exception("alert evaluation failed")
            else:
                if changed:
                    logger.info(
                        "alerts changed",
                        extra={
                            "transitions": [f"{t.action}:{t.rule_id}:{t.target}" for t in changed]
                        },
                    )

    def _notify(self) -> None:
        with self.db.session_factory() as session:
            result = monitor_notify.deliver_due(session, self.settings, self.cipher, stop=self.stop)
            if any(result.values()):
                logger.info("alert notifications processed", extra=result)

    def _transcode(self, row_id: int) -> None:
        with self.db.session_factory() as session:
            row = session.get(MediaTranscode, row_id)
            if row is None:  # pragma: no cover - deleted between claim and start
                return
            state = transcode.run(session, self.settings, row, stop=self.stop)
            logger.info("transcode finished", extra={"item_id": row.item_id, "status": state})

    def _scan(self, library_id: int) -> None:
        with self.db.session_factory() as session:
            library = session.get(MediaLibrary, library_id)
            if library is None:  # pragma: no cover - deleted between claim and start
                return
            result = media_jobs.run_scan(session, self.settings, library, stop=self.stop)
            logger.info(
                "media scan finished",
                extra={
                    "library_id": library_id,
                    "status": library.last_scan_status,
                    **(result.as_dict() if result is not None else {}),
                },
            )

    def _thumbnails(self) -> None:
        with self.db.session_factory() as session:
            media_jobs.process_thumbnails(session, self.settings, stop=self.stop)

    def _execute(self, run_id: int) -> None:
        with self.db.session_factory() as session:
            run = session.get(SyncRun, run_id)
            if run is None:  # pragma: no cover - deleted between claim and start
                return
            remote_sync.process_run(session, self.settings, run, cipher=self.cipher, stop=self.stop)
            logger.info(
                "sync run finished",
                extra={"run_id": run.id, "job_id": run.job_id, "status": run.status},
            )

    def wait_idle(self) -> None:
        for future in list(self.running.values()):
            future.result()
        if self.media_future is not None:
            self.media_future.result()
        if self.transcode_future is not None:
            self.transcode_future.result()
        if self.monitor_future is not None:
            self.monitor_future.result()
        if self.notify_future is not None:
            self.notify_future.result()

    def run_forever(self) -> None:
        logger.info("sync worker started", extra={"owner": self.owner})
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("sync worker tick failed")
            self.stop.wait(self.settings.remote_sync_poll_seconds)
        logger.info("sync worker stopping; waiting for running jobs")
        self.pool.shutdown(wait=True)
        self.media_pool.shutdown(wait=True)
        self.transcode_pool.shutdown(wait=True)
        self.monitor_pool.shutdown(wait=True)
        self.notify_pool.shutdown(wait=True)
        logger.info("sync worker stopped")
