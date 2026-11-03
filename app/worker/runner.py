"""The sync worker loop.

One process, started as ``python -m app.worker`` by its own systemd unit,
under the same unprivileged account as the web application. Each tick it:

1. writes a heartbeat (shown in the UI),
2. renews the leases of the runs it is executing,
3. fails runs whose owning worker died (lease expired),
4. queues runs for enabled jobs whose schedule is due,
5. claims queued runs (atomically) up to ``NAS_WORKER_CONCURRENCY``, at most
   one per job, and runs them on a thread pool, and
6. occasionally prunes old run history.

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

from app.core.config import Settings
from app.core.secrets import CredentialCipher
from app.db.base import utcnow_naive
from app.db.session import Database
from app.models import SyncRun
from app.services import remote_sync

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
            if time.monotonic() - self._last_prune >= _PRUNE_INTERVAL_SECONDS:
                self._last_prune = time.monotonic()
                pruned = remote_sync.prune_runs(session, self.settings)
                if pruned:
                    logger.info("pruned old sync runs", extra={"count": pruned})

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
        logger.info("sync worker stopped")
