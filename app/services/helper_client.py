"""Client for the privileged helper over its Unix socket.

The web process never gains privileges; every privileged action is requested
here as one allowlisted operation carried over ``AF_UNIX`` with a
length-prefixed JSON frame. Errors from the helper are translated into typed
``AppError`` subclasses so API handlers produce the right status codes.
"""

from __future__ import annotations

import socket
from typing import Any

from app.core.config import Settings
from app.core.errors import (
    BadRequestError,
    ConfigurationError,
    ServiceUnavailableError,
)
from app.core.logging import request_id_var
from app.helper.protocol import read_frame, write_frame


class HelperUnavailableError(ServiceUnavailableError):
    """The helper is disabled, unreachable or answered incorrectly."""


class HelperValidationError(BadRequestError):
    """The helper rejected the operation parameters."""


class HelperIntentMismatchError(BadRequestError):
    """The confirmation intent does not match the operation parameters."""


class HelperOperationFailedError(BadRequestError):
    """The privileged operation ran but failed (device busy, mount refused...)."""


class HelperProtocolError(ConfigurationError):
    """The helper spoke a different protocol version or behaved unexpectedly."""


def helper_ping(settings: Settings) -> dict[str, Any]:
    """Cheap reachability check; raises ``HelperUnavailableError`` when down."""
    return call_helper(settings, "ping", {})


def call_helper(settings: Settings, op: str, params: dict[str, Any]) -> dict[str, Any]:
    """Send one allowlisted operation to the helper and return its result."""
    if not settings.helper_enabled:
        raise HelperUnavailableError(
            "the privileged helper is not enabled (NAS_HELPER_ENABLED=false); "
            "see docs/INSTALLATION.md to deploy nas-manager-helper.service"
        )
    socket_path = settings.resolved_helper_socket
    request_id = request_id_var.get()
    request: dict[str, Any] = {"op": op, "params": params}
    if request_id:
        request["request_id"] = request_id

    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(settings.helper_timeout_seconds)
    try:
        conn.connect(str(socket_path))
    except OSError as exc:
        conn.close()
        raise HelperUnavailableError(
            "the privileged helper is not reachable",
            details={"socket": str(socket_path), "error": str(exc)},
        ) from exc

    try:
        write_frame(conn, request)
        response = read_frame(conn)
    except (OSError, ValueError) as exc:
        raise HelperUnavailableError(
            "the privileged helper did not answer correctly",
            details={"socket": str(socket_path), "error": str(exc)},
        ) from exc
    finally:
        conn.close()

    return _parse_response(response, op, request_id)


def _parse_response(response: dict[str, Any], op: str, request_id: str | None) -> dict[str, Any]:
    if not isinstance(response.get("ok"), bool):
        raise HelperProtocolError("the privileged helper sent an invalid response")
    received_op = response.get("op")
    if received_op != op:
        raise HelperProtocolError(
            "the privileged helper answered a different operation",
            details={"expected": op, "received": received_op},
        )
    if request_id is not None and response.get("request_id") != request_id:
        raise HelperProtocolError("the privileged helper lost the request id")
    if response["ok"]:
        result = response.get("result")
        if not isinstance(result, dict):
            raise HelperProtocolError("the privileged helper result is not an object")
        return result

    error = response.get("error") or {}
    code = error.get("code")
    message = error.get("message", "the privileged helper rejected the operation")
    details = error.get("details")
    if code == "validation_error":
        raise HelperValidationError(message, details=details)
    if code == "intent_mismatch":
        raise HelperIntentMismatchError(message, details=details)
    if code == "operation_failed":
        raise HelperOperationFailedError(message, details=details)
    if code == "unknown_operation":
        raise HelperProtocolError(
            message, details={"hint": "the app and the helper are out of sync"}
        )
    raise HelperProtocolError(message, details=details)
