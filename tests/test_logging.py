from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.logging import (
    LOG_FILE_NAME,
    REDACTED,
    ContextFilter,
    JsonFormatter,
    configure_logging,
    request_id_var,
)


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)


def _record(msg: str = "hello", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("test.logger", logging.INFO, __file__, 1, msg, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    ContextFilter().filter(record)
    return record


def test_json_formatter_core_fields() -> None:
    payload = json.loads(JsonFormatter().format(_record("hello", path="/x")))
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["path"] == "/x"
    assert "timestamp" in payload
    assert "request_id" not in payload


def test_request_id_included_when_set() -> None:
    token = request_id_var.set("abc-123")
    try:
        payload = json.loads(JsonFormatter().format(_record()))
    finally:
        request_id_var.reset(token)
    assert payload["request_id"] == "abc-123"


@pytest.mark.parametrize(
    "key", ["password", "api_key", "access_token", "client_secret", "Authorization", "credentials"]
)
def test_sensitive_extras_are_redacted(key: str) -> None:
    payload = json.loads(JsonFormatter().format(_record(**{key: "hunter2"})))
    assert payload[key] == REDACTED
    assert "hunter2" not in json.dumps(payload)


def test_exception_is_serialised() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exception"]


def test_file_handler_writes_json_lines(settings: Settings, tmp_path: Path) -> None:
    cfg = settings.model_copy(update={"log_to_file": True, "log_dir": tmp_path / "logs"})
    configure_logging(cfg)
    logging.getLogger("t").info("written", extra={"token": "s3cr3t"})
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = (tmp_path / "logs" / LOG_FILE_NAME).read_text()
    line = json.loads(content.strip().splitlines()[-1])
    assert line["message"] == "written"
    assert line["token"] == REDACTED
    assert "s3cr3t" not in content


def test_configure_logging_is_idempotent(settings: Settings, tmp_path: Path) -> None:
    cfg = settings.model_copy(update={"log_to_file": True, "log_dir": tmp_path / "logs"})
    configure_logging(cfg)
    first = len(logging.getLogger().handlers)
    configure_logging(cfg)
    assert len(logging.getLogger().handlers) == first


def test_rotation_settings_applied(settings: Settings, tmp_path: Path) -> None:
    from logging.handlers import RotatingFileHandler

    cfg = settings.model_copy(
        update={
            "log_to_file": True,
            "log_dir": tmp_path / "logs",
            "log_max_bytes": 2048,
            "log_backup_count": 3,
        }
    )
    configure_logging(cfg)
    rotating = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]
    assert len(rotating) == 1
    assert rotating[0].maxBytes == 2048
    assert rotating[0].backupCount == 3


def test_explicit_request_id_extra_wins_over_context() -> None:
    token = request_id_var.set("ambient")
    try:
        payload = json.loads(JsonFormatter().format(_record(request_id="explicit")))
    finally:
        request_id_var.reset(token)
    assert payload["request_id"] == "explicit"


def test_uvicorn_color_message_is_not_logged() -> None:
    payload = json.loads(JsonFormatter().format(_record(color_message="\x1b[36mhi\x1b[0m")))
    assert "color_message" not in payload
