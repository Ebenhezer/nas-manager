"""SFTP provider (paramiko) with a pinned host key.

The server's host key must be stored with the connection (``host_key``,
``"<type> <base64>"`` as in known_hosts). Connections use ``RejectPolicy``,
so an unknown or changed key aborts before any credential is sent. The key
is obtained with ``probe_host_key`` and confirmed by an administrator.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import errno
import hashlib
import io
import posixpath
import socket
import stat
import uuid
from datetime import UTC, datetime
from typing import Any, BinaryIO

import paramiko

from app.core.errors import BadRequestError, RemoteError
from app.services.remote.base import (
    REMOTE_TMP_PREFIX,
    RawEntry,
    RemoteEntry,
    RemoteProvider,
    join,
    parent_of,
)

_KEY_CLASSES: tuple[type[paramiko.PKey], ...] = (
    paramiko.Ed25519Key,
    paramiko.ECDSAKey,
    paramiko.RSAKey,
)


def fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def parse_host_key(value: str) -> paramiko.PKey:
    """Parse ``"<type> <base64>"`` (a known_hosts line without the host)."""
    parts = value.strip().split()
    if len(parts) < 2:
        raise BadRequestError("host_key must look like '<type> <base64>'")
    key_type, data = parts[0], parts[1]
    try:
        return paramiko.PKey.from_type_string(key_type, base64.b64decode(data, validate=True))
    except (binascii.Error, ValueError, paramiko.SSHException) as exc:
        raise BadRequestError("host_key is not a valid SSH public key") from exc


def load_private_key(pem: str, passphrase: str | None) -> paramiko.PKey:
    last: Exception | None = None
    for cls in _KEY_CLASSES:
        try:
            return cls.from_private_key(io.StringIO(pem), password=passphrase or None)
        except paramiko.PasswordRequiredException as exc:
            raise BadRequestError("The private key needs a passphrase") from exc
        except (paramiko.SSHException, ValueError) as exc:
            last = exc
    raise BadRequestError("private_key is not a supported OpenSSH/PEM private key") from last


def _known_hosts_name(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def probe_host_key(host: str, port: int, *, timeout: int) -> dict[str, str | int]:
    """Fetch the server's host key without authenticating (for admin review)."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise RemoteError(f"Cannot reach {host}:{port}") from exc
    transport = paramiko.Transport(sock)
    try:
        transport.banner_timeout = timeout
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
    except (OSError, paramiko.SSHException, EOFError) as exc:
        raise RemoteError(f"SSH handshake with {host}:{port} failed") from exc
    finally:
        transport.close()
        sock.close()
    return {
        "host": host,
        "port": port,
        "key_type": key.get_name(),
        "fingerprint": fingerprint(key),
        "host_key": f"{key.get_name()} {key.get_base64()}",
    }


class SftpProvider(RemoteProvider):
    def __init__(self, config: dict[str, Any], secrets: dict[str, Any], *, timeout: int) -> None:
        self.host = str(config["host"])
        self.port = int(config.get("port") or 22)
        self.username = str(config["username"])
        self.base_path = str(config.get("base_path") or "")
        self.host_key = parse_host_key(str(config["host_key"]))
        self.password = secrets.get("password") or None
        private_key = secrets.get("private_key")
        self.pkey = (
            load_private_key(str(private_key), secrets.get("passphrase")) if private_key else None
        )
        self.timeout = timeout
        self._client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None

    # -- connection -----------------------------------------------------------

    def connect(self) -> None:
        client = paramiko.SSHClient()
        client.get_host_keys().add(
            _known_hosts_name(self.host, self.port), self.host_key.get_name(), self.host_key
        )
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(
                self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                pkey=self.pkey,
                timeout=self.timeout,
                banner_timeout=self.timeout,
                auth_timeout=self.timeout,
                allow_agent=False,
                look_for_keys=False,
            )
            self._sftp = client.open_sftp()
            self._sftp.get_channel().settimeout(self.timeout)  # type: ignore[union-attr]
        except paramiko.BadHostKeyException as exc:
            client.close()
            raise RemoteError(
                "The SSH host key changed; refusing to connect",
                details={
                    "presented": fingerprint(exc.key),
                    "expected": fingerprint(exc.expected_key),
                },
            ) from exc
        except paramiko.AuthenticationException as exc:
            client.close()
            raise RemoteError("SFTP authentication failed") from exc
        except (OSError, paramiko.SSHException, EOFError) as exc:
            client.close()
            raise RemoteError(f"Cannot connect to {self.host}:{self.port}: {exc}") from exc
        self._client = client

    def close(self) -> None:
        if self._sftp is not None:
            self._sftp.close()
            self._sftp = None
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def sftp(self) -> paramiko.SFTPClient:
        if self._sftp is None:
            raise RemoteError("SFTP session is not connected")
        return self._sftp

    def _abs(self, path: str) -> str:
        base = self.base_path or "."
        return posixpath.join(base, path) if path else base

    # -- operations -----------------------------------------------------------

    def _list(self, path: str) -> list[RawEntry]:
        try:
            attrs = self.sftp.listdir_attr(self._abs(path))
        except OSError as exc:
            raise RemoteError(
                f"Cannot list remote directory: {exc}", details={"path": path}
            ) from exc
        entries: list[RawEntry] = []
        for attr in attrs:
            mode = attr.st_mode or 0
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                continue
            entries.append(
                RawEntry(
                    name=attr.filename,
                    is_dir=stat.S_ISDIR(mode),
                    size=int(attr.st_size or 0),
                    mtime=_mtime(attr.st_mtime),
                )
            )
        return entries

    def stat(self, path: str) -> RemoteEntry | None:
        try:
            attr = self.sftp.lstat(self._abs(path))
        except FileNotFoundError:
            return None
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.ENOENT:
                return None
            raise RemoteError(f"Cannot stat remote path: {exc}", details={"path": path}) from exc
        mode = attr.st_mode or 0
        return RemoteEntry(
            path=path,
            is_dir=stat.S_ISDIR(mode),
            size=int(attr.st_size or 0),
            mtime=_mtime(attr.st_mtime),
        )

    def download(self, path: str, out: BinaryIO) -> None:
        try:
            self.sftp.getfo(self._abs(path), out)
        except OSError as exc:
            raise RemoteError(f"Download failed: {exc}", details={"path": path}) from exc

    def upload(self, source: BinaryIO, path: str) -> None:
        temporary = join(parent_of(path), f"{REMOTE_TMP_PREFIX}{uuid.uuid4().hex}.part")
        try:
            self.sftp.putfo(source, self._abs(temporary), confirm=True)
            self.sftp.posix_rename(self._abs(temporary), self._abs(path))
        except (OSError, paramiko.SSHException) as exc:
            with contextlib.suppress(OSError):
                self.sftp.remove(self._abs(temporary))
            raise RemoteError(f"Upload failed: {exc}", details={"path": path}) from exc

    def makedirs(self, path: str) -> None:
        current = ""
        for part in [p for p in path.split("/") if p]:
            current = join(current, part)
            existing = self.stat(current)
            if existing is not None:
                if not existing.is_dir:
                    raise RemoteError("A remote file is in the way", details={"path": current})
                continue
            try:
                self.sftp.mkdir(self._abs(current))
            except OSError as exc:
                raise RemoteError(f"mkdir failed: {exc}", details={"path": current}) from exc

    def move(self, src: str, dst: str) -> None:
        if self.stat(dst) is not None:
            raise RemoteError("Move destination exists", details={"path": dst})
        try:
            self.sftp.posix_rename(self._abs(src), self._abs(dst))
        except OSError as exc:
            raise RemoteError(f"Move failed: {exc}", details={"path": src}) from exc


def _mtime(value: int | float | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(int(value), tz=UTC)
