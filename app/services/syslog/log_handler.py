"""Best-effort forwarding of application log records to remote syslog.

Every process (web and worker) installs one ``RemoteLogHandler`` on the root
logger (see ``app.core.logging``). ``emit`` only formats the record and puts
it on a bounded queue, so a slow or unreachable collector never blocks a
request; a full queue drops records and counts them. A daemon thread sends
the queue to the destinations that have ``forward_logs`` enabled and a
matching minimum level.

Audit events are *not* forwarded here (the worker forwards them reliably
from the table), and neither is this package's own logging, so a failing
collector cannot cause a feedback loop.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import socket
import threading
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import SyslogDestination
from app.services.syslog import format as syslog_format
from app.services.syslog.transport import SyslogSender, SyslogTransportError, frame

_EXCLUDED_PREFIXES = ("app.audit", "app.services.syslog")


@dataclass(frozen=True)
class LogTarget:
    id: int
    name: str
    host: str
    port: int
    protocol: str
    ca_pem: str | None
    format: str
    facility: str
    min_level: int


def targets_from_rows(rows: list[SyslogDestination]) -> list[LogTarget]:
    return [
        LogTarget(
            id=row.id,
            name=row.name,
            host=row.host,
            port=row.port,
            protocol=row.protocol,
            ca_pem=row.ca_pem,
            format=row.format,
            facility=row.facility,
            min_level=logging.getLevelName(row.log_min_level),
        )
        for row in rows
        if row.enabled and row.forward_logs
    ]


class RemoteLogHandler(logging.Handler):
    def __init__(
        self,
        *,
        queue_size: int,
        hostname: str,
        app_name: str,
        procid: str,
        timeout: float,
    ) -> None:
        super().__init__()
        self.queue: queue.Queue[tuple[int, syslog_format.SyslogMessage] | None] = queue.Queue(
            maxsize=queue_size
        )
        self.identity = {"hostname": hostname, "app_name": app_name, "procid": procid}
        self.timeout = timeout
        self.dropped = 0
        self.send_errors = 0
        self._targets: tuple[LogTarget, ...] = ()
        self._senders: dict[int, tuple[LogTarget, SyslogSender]] = {}
        self._min_level = logging.CRITICAL + 1
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def targets(self) -> tuple[LogTarget, ...]:
        return self._targets

    def configure(self, targets: list[LogTarget]) -> None:
        with self._lock:
            self._targets = tuple(targets)
            self._min_level = min((t.min_level for t in targets), default=logging.CRITICAL + 1)
            # Senders are owned by the sending thread; it drops stale ones.
            if targets and self._thread is None:
                self._thread = threading.Thread(target=self._run, name="syslog-logs", daemon=True)
                self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < self._min_level or record.name.startswith(_EXCLUDED_PREFIXES):
            return
        try:
            message = syslog_format.log_message(
                levelno=record.levelno,
                logger_name=record.name,
                created=record.created,
                text=self.format(record),
                request_id=getattr(record, "request_id", None),
            )
        except Exception:
            self.handleError(record)
            return
        try:
            self.queue.put_nowait((record.levelno, message))
        except queue.Full:
            self.dropped += 1

    def _sender(self, target: LogTarget) -> SyslogSender:
        cached = self._senders.get(target.id)
        if cached is not None and cached[0] == target:
            return cached[1]
        if cached is not None:
            cached[1].close()
        sender = SyslogSender(
            host=target.host,
            port=target.port,
            protocol=target.protocol,
            ca_pem=target.ca_pem,
            timeout=self.timeout,
        )
        self._senders[target.id] = (target, sender)
        return sender

    def _deliver(self, levelno: int, message: syslog_format.SyslogMessage) -> None:
        targets = self._targets
        for stale in set(self._senders) - {t.id for t in targets}:
            self._senders.pop(stale)[1].close()
        for target in targets:
            if levelno < target.min_level:
                continue
            payload = syslog_format.render(
                message, fmt=target.format, facility=target.facility, **self.identity
            )
            try:
                self._sender(target).send(
                    [frame(payload, protocol=target.protocol, fmt=target.format)]
                )
            except SyslogTransportError:
                self.send_errors += 1

    def _run(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                break
            try:
                self._deliver(*item)
            except Exception:
                self.send_errors += 1
            finally:
                self.queue.task_done()
        for _, sender in self._senders.values():
            sender.close()
        self._senders.clear()

    def flush(self, timeout: float = 5.0) -> None:
        """Wait (up to ``timeout``) until the queue has been sent; for tests
        and orderly shutdown."""
        if self._thread is None:
            return
        done = threading.Event()

        def _wait() -> None:
            self.queue.join()
            done.set()

        threading.Thread(target=_wait, daemon=True).start()
        done.wait(timeout)

    def close(self) -> None:
        if self._thread is not None:
            with contextlib.suppress(queue.Full):
                self.queue.put_nowait(None)
            self._thread.join(timeout=2)
            self._thread = None
        super().close()


def reload_log_forwarding(session: Session) -> int:
    """Point this process's handler at the current destinations. Returns the
    number of active log targets (0 when no handler is installed)."""
    from app.core.logging import remote_log_handler

    handler = remote_log_handler()
    if handler is None:
        return 0
    rows = list(session.execute(select(SyslogDestination)).scalars())
    targets = targets_from_rows(rows)
    handler.configure(targets)
    return len(targets)


def make_handler(settings: Settings) -> RemoteLogHandler:
    return RemoteLogHandler(
        queue_size=settings.syslog_queue_size,
        hostname=socket.gethostname(),
        app_name=settings.syslog_identifier,
        procid=str(os.getpid()),
        timeout=settings.syslog_timeout_seconds,
    )
