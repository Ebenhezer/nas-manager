"""Request/response framing and shared validation helpers for the privileged helper.

Wire protocol: one JSON document per request/response, prefixed with a
4-byte big-endian length so partial reads cannot be misparsed. Messages
larger than ``MAX_PAYLOAD_BYTES`` are refused outright.
"""

from __future__ import annotations

import hashlib
import json
import socket
import struct
from typing import Any

MAX_PAYLOAD_BYTES = 1 << 20  # 1 MiB

# A device name is a single path component that may never start with ``-``
# (so it cannot be parsed as an option by the underlying command) and may
# never contain path separators or whitespace.
DEVICE_NAME_RE_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}"
DEVICE_PATH_PATTERN = r"^/dev/([A-Za-z0-9][A-Za-z0-9._+-]{0,63})/?$"

# Filesystem types the helper is allowed to mount. Every value is compared
# against an allowlist before it reaches the command line.
ALLOWED_FSTYPES: frozenset[str] = frozenset(
    {
        "ext2",
        "ext3",
        "ext4",
        "xfs",
        "btrfs",
        "vfat",
        "exfat",
        "ntfs",
        "ntfs3",
        "iso9660",
        "udf",
        "f2fs",
    }
)

# Option tokens that are refused because they can change what the command
# actually does (attach arbitrary files, re-bind directories, remount) in
# ways recovery tooling would not expect. This is a safety list, not a
# complete policy; operators review the preview before confirming.
FORBIDDEN_OPTION_TOKENS: frozenset[str] = frozenset({"loop", "bind", "rbind", "remount"})

OPTIONS_RE = r"^[A-Za-z0-9._+/@-]+(,[A-Za-z0-9._+/@-]+)*$"


def validate_device_name(name: str) -> str:
    """Return the canonical ``/dev/<name>`` path or raise ValueError."""
    import re

    if not re.fullmatch(DEVICE_NAME_RE_PATTERN, name):
        raise ValueError(
            "device name must be 1-64 characters of letters, digits, '.', '_', '+' or '-' "
            "and must not start with '-'"
        )
    return f"/dev/{name}"


def validate_mountpoint(mountpoint: str) -> str:
    """Validate an absolute, normalised mount point path."""
    import re
    from pathlib import Path

    stripped = mountpoint.strip()
    if not stripped or "\x00" in stripped or not stripped.startswith("/"):
        raise ValueError("mount point must be an absolute path")
    if not re.fullmatch(r"/[A-Za-z0-9._+@/-]+", stripped) and stripped != "/":
        raise ValueError("mount point contains unsupported characters")
    if ".." in Path(stripped).parts:
        raise ValueError("mount point must not contain '..'")
    return str(Path(stripped).resolve())


def validate_fstype(fstype: str) -> str:
    value = fstype.strip().lower()
    if value not in ALLOWED_FSTYPES:
        raise ValueError(f"unsupported filesystem type: {fstype}")
    return value


def validate_mount_options(options: str) -> str:
    """Validate a comma-separated mount option string and drop dangerous tokens."""
    import re

    cleaned = options.strip()
    if not cleaned:
        raise ValueError("mount options must not be empty")
    if not re.fullmatch(OPTIONS_RE, cleaned):
        raise ValueError("mount options contain unsupported characters")
    tokens = [token.strip() for token in cleaned.split(",")]
    if not tokens or any(not token for token in tokens):
        raise ValueError("mount options must be a comma-separated list of tokens")
    rejected = sorted({token for token in tokens if token in FORBIDDEN_OPTION_TOKENS})
    if rejected:
        raise ValueError(f"refusing mount option(s): {', '.join(rejected)}")
    return ",".join(tokens)


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


def _encode_frame(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode()
    if len(body) > MAX_PAYLOAD_BYTES:
        raise ValueError("payload exceeds the maximum frame size")
    return struct.pack(">I", len(body)) + body


def _read_exact(conn: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = conn.recv(remaining)
        if not chunk:
            raise ConnectionError("peer closed the connection mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(conn: socket.socket) -> dict[str, Any]:
    prefix = _read_exact(conn, 4)
    (length,) = struct.unpack(">I", prefix)
    if length == 0 or length > MAX_PAYLOAD_BYTES:
        raise ValueError(f"invalid frame length: {length}")
    body = _read_exact(conn, length)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid JSON in frame") from exc
    if not isinstance(payload, dict):
        raise ValueError("frame payload must be a JSON object")
    return payload


def write_frame(conn: socket.socket, payload: dict[str, Any]) -> None:
    conn.sendall(_encode_frame(payload))


def encode_request(op: str, params: dict[str, Any]) -> bytes:
    return _encode_frame({"op": op, "params": params})


def encode_response(
    *,
    ok: bool,
    op: str,
    request_id: str | None = None,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> bytes:
    payload: dict[str, Any] = {"ok": ok, "op": op}
    if request_id is not None:
        payload["request_id"] = request_id
    if ok:
        payload["result"] = result or {}
    else:
        payload["error"] = error or {}
    return _encode_frame(payload)


def intent_digest(op: str, params: dict[str, Any]) -> str:
    """Deterministic binding of an operation to its exact validated parameters.

    The application computes this digest from the parameters it validated and
    sends it with the request; the helper recomputes it from the parameters it
    re-validated and refuses to run the operation on a mismatch. A confirmed
    action is therefore bound to exactly the device/mount point/options the
    operator saw in the preview, and a confused or tampered request cannot
    mount something else under the same confirmation.
    """
    canonical = "|".join(
        f"{key}={params[key]}" for key in sorted(params) if params[key] is not None
    )
    return hashlib.sha256(f"{op}|{canonical}".encode()).hexdigest()
