"""Local log outputs (Phase 13): native journald, daily rotation, compression."""

from __future__ import annotations

import gzip
import logging
import re
import socket
import struct
import time
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path

import pytest

from app.core import logging as app_logging
from app.core.config import Settings
from app.core.journald import JournaldHandler, encode, field_name, journal_stream_connected


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[tuple[Path, socket.socket]]:
    path = tmp_path / "journal.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(path))
    sock.settimeout(2)
    yield path, sock
    sock.close()


@pytest.fixture(autouse=True)
def _restore_root_handlers() -> Iterator[None]:
    root = logging.getLogger()
    before = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        if handler not in before[0]:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(before[1])


def _decode(data: bytes) -> dict[str, str]:
    fields: dict[str, str] = {}
    while data:
        line, sep, rest = data.partition(b"\n")
        if b"=" in line:
            key, _, value = line.partition(b"=")
            fields[key.decode()] = value.decode()
            data = rest
        else:
            (size,) = struct.unpack("<Q", rest[:8])
            fields[line.decode()] = rest[8 : 8 + size].decode()
            data = rest[8 + size + 1 :]
        assert sep
    return fields


def test_encoding_is_binary_safe() -> None:
    data = encode({"MESSAGE": "two\nlines", "PRIORITY": 3, "EMPTY": None})
    assert data.startswith(b"MESSAGE\n" + struct.pack("<Q", 9) + b"two\nlines\n")
    assert data.endswith(b"PRIORITY=3\n")
    assert _decode(data) == {"MESSAGE": "two\nlines", "PRIORITY": "3"}


def test_field_names() -> None:
    assert field_name("audit_event") == "NAS_AUDIT_EVENT"
    assert field_name("nas_x") == "NAS_X"
    assert field_name("__private") == "NAS_PRIVATE"
    assert field_name("weird-key.1") == "NAS_WEIRD_KEY_1"
    assert field_name("___") is None


def test_handler_sends_structured_fields(journal: tuple[Path, socket.socket]) -> None:
    path, sock = journal
    handler = JournaldHandler(path, "nas-manager-test", reserved=app_logging._RESERVED)
    handler.addFilter(app_logging.ContextFilter())
    log = logging.getLogger("tests.journald")
    log.propagate = False
    log.addHandler(handler)
    try:
        log.warning(
            "login refused for %s",
            "eve",
            extra={"audit_event": "auth.login.failure", "api_key": "s3cret", "count": 2},
        )
        fields = _decode(sock.recv(65536))
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("failed")
        with_traceback = _decode(sock.recv(65536))
    finally:
        log.removeHandler(handler)
        handler.close()
    assert fields["MESSAGE"] == "login refused for eve"
    assert fields["PRIORITY"] == "4"
    assert fields["SYSLOG_IDENTIFIER"] == "nas-manager-test"
    assert fields["LOGGER"] == "tests.journald"
    assert fields["NAS_AUDIT_EVENT"] == "auth.login.failure"
    assert fields["NAS_API_KEY"] == "***"  # redacted like every other output
    assert fields["NAS_COUNT"] == "2"
    assert fields["CODE_FUNC"] == "test_handler_sends_structured_fields"
    assert "REQUEST_ID" not in fields
    assert with_traceback["PRIORITY"] == "3"
    assert "ValueError: boom" in with_traceback["MESSAGE"]


def test_missing_socket_does_not_raise(tmp_path: Path) -> None:
    handler = JournaldHandler(tmp_path / "absent.sock", "x", reserved=set())
    handler.handleError = lambda record: None  # type: ignore[method-assign]
    handler.emit(logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None))
    handler.close()


def test_auto_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sock_path = tmp_path / "journal.sock"
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    assert not journal_stream_connected(sock_path)
    monkeypatch.setenv("JOURNAL_STREAM", "8:12345")
    assert not journal_stream_connected(sock_path)  # no socket
    sock_path.touch()
    assert journal_stream_connected(sock_path)


def _settings(tmp_path: Path, **values: object) -> Settings:
    values = {"log_to_file": False, **values}
    return Settings(
        _env_file=None,
        environment="test",
        data_dir=tmp_path / "data",
        **values,  # type: ignore[arg-type]
    )


def test_journald_replaces_the_console(
    journal: tuple[Path, socket.socket], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, sock = journal
    monkeypatch.setenv("JOURNAL_STREAM", "8:1")
    settings = _settings(tmp_path, log_journald="auto", log_journald_socket=path)
    app_logging.configure_logging(settings)
    kinds = {type(h).__name__ for h in logging.getLogger().handlers}
    assert "JournaldHandler" in kinds and "StreamHandler" not in kinds
    logging.getLogger("tests.auto").warning("via journal")
    assert _decode(sock.recv(65536))["MESSAGE"] == "via journal"
    assert app_logging.logging_status(settings)["journald"] is True

    app_logging.configure_logging(_settings(tmp_path, log_journald="off"))
    kinds = {type(h).__name__ for h in logging.getLogger().handlers}
    assert "JournaldHandler" not in kinds and "StreamHandler" in kinds


def test_size_rotation_with_compression(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path, log_to_file=True, log_to_console=False, log_max_bytes=1024, log_compress=True
    )
    app_logging.configure_logging(settings)
    handlers = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]
    assert len(handlers) == 1
    log = logging.getLogger("tests.rotation")
    for i in range(60):
        log.warning("line %03d %s", i, "x" * 40)
    log_dir = settings.resolved_log_dir
    rotated = sorted(log_dir.glob("nas-manager.log.*.gz"))
    assert rotated, list(log_dir.iterdir())
    assert len(rotated) <= settings.log_backup_count
    with gzip.open(rotated[0], "rt") as fh:
        assert "line" in fh.read()
    assert not list(log_dir.glob("nas-manager.log.[0-9]"))  # no uncompressed copies left


def test_daily_rotation(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        log_to_file=True,
        log_to_console=False,
        log_rotation="daily",
        log_compress=True,
        log_backup_count=2,
    )
    app_logging.configure_logging(settings)
    (handler,) = [
        h for h in logging.getLogger().handlers if isinstance(h, TimedRotatingFileHandler)
    ]
    assert handler.when == "MIDNIGHT" and handler.utc
    log = logging.getLogger("tests.daily")
    log.warning("yesterday")
    handler.rolloverAt = int(time.time()) - 1  # the next record rolls the file over
    log.warning("today")
    (rotated,) = settings.resolved_log_dir.glob("nas-manager.log.*.gz")
    assert re.fullmatch(r"nas-manager\.log\.\d{4}-\d{2}-\d{2}\.gz", rotated.name)
    with gzip.open(rotated, "rt") as fh:
        assert "yesterday" in fh.read()
    assert "today" in (settings.resolved_log_dir / "nas-manager.log").read_text()


def test_status_reports_configuration(tmp_path: Path) -> None:
    settings = _settings(tmp_path, log_rotation="daily", log_journald="off")
    app_logging.configure_logging(settings)
    status = app_logging.logging_status(settings)
    assert status["rotation"] == "daily" and status["journald"] is False
    assert status["file"] is None and status["remote_targets"] == 0
