"""Unix socket server for the privileged helper (Phase 5).

Bound as root, the server authenticates every peer with ``SO_PEERCRED``
(uid must be root or in ``NAS_HELPER_ALLOWED_UIDS``), wraps exactly one
allowlisted operation per connection, and refuses oversized or malformed
frames. The socket is group-owned so the unprivileged service user can
connect through it.
"""

from __future__ import annotations

import grp
import logging
import os
import signal
import socket
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

from app.core.config import Settings
from app.helper.operations import HelperError, UnknownOperationError, dispatch
from app.helper.protocol import read_frame, write_frame

logger = logging.getLogger(__name__)

_MAX_WORKERS = 8
_SO_PEERCRED = getattr(socket, "SO_PEERCRED", None)


def _peer_uid(conn: socket.socket) -> int:
    """Numeric UID of the connected peer via ``SO_PEERCRED`` (-1 if unsupported)."""
    if _SO_PEERCRED is None:
        return -1
    data = conn.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", data)
    return int(uid)


def _set_socket_access(socket_path: Path, settings: Settings) -> None:
    """The freshly bound socket is root:root; hand the service group access."""
    try:
        group = grp.getgrnam(settings.helper_socket_group)
    except KeyError:
        logger.warning(
            "socket group %r not found; helper socket is root-only",
            settings.helper_socket_group,
        )
        os.chmod(socket_path, 0o600)
        return
    os.chown(socket_path, 0, group.gr_gid)
    os.chmod(socket_path, 0o660)


def _handle_connection(conn: socket.socket, settings: Settings) -> None:
    op = "?"
    request_id: str | None = None
    try:
        request = read_frame(conn)
        op = request.get("op", "?")
        request_id = request.get("request_id")
        if not isinstance(op, str) or not op:
            raise UnknownOperationError("op must be a non-empty string")
        result = dispatch(op, request.get("params", {}), settings)
        write_frame(
            conn,
            {"ok": True, "op": op, "request_id": request_id, "result": result},
        )
    except HelperError as exc:
        logger.warning("operation %s rejected: %s", op, exc.message)
        with suppress(OSError):
            write_frame(
                conn,
                {
                    "ok": False,
                    "op": op,
                    "request_id": request_id,
                    "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                },
            )
    except Exception:
        logger.exception("unhandled error while handling operation %s", op)
        with suppress(OSError):
            write_frame(
                conn,
                {
                    "ok": False,
                    "op": op,
                    "request_id": request_id,
                    "error": {"code": "internal_error", "message": "internal helper error"},
                },
            )
    finally:
        with suppress(OSError):
            conn.close()


def serve(settings: Settings) -> None:
    """Bind the socket and serve operations until SIGINT/SIGTERM."""
    stop_event = threading.Event()

    def _shutdown(signum: int, _frame: object) -> None:
        logger.info("helper received signal %s; shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    socket_path = settings.resolved_helper_socket
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    with suppress(OSError):
        # Group traversal: the unprivileged service user must reach the socket.
        os.chmod(socket_path.parent, 0o750)  # noqa: S103 - deliberate shared-directory access
    if socket_path.exists():
        socket_path.unlink()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
        _set_socket_access(socket_path, settings)
        server.listen(32)
        server.settimeout(1.0)
        logger.info(
            "helper listening on %s",
            socket_path,
            extra={
                "allowed_uids": sorted(settings.helper_allowed_uid_set),
                "socket_group": settings.helper_socket_group,
            },
        )
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="helper") as pool:
            while not stop_event.is_set():
                try:
                    conn, _addr = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if stop_event.is_set():
                        break
                    raise
                uid = _peer_uid(conn)
                if uid not in settings.helper_allowed_uid_set:
                    logger.warning("refusing connection from uid %s (not allowlisted)", uid)
                    conn.close()
                    continue
                pool.submit(_handle_connection, conn, settings)
    finally:
        server.close()
        with suppress(OSError):
            socket_path.unlink()
        logger.info("helper stopped")
