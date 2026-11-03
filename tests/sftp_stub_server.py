"""A real (in-process) SFTP server over loopback for provider tests.

Modelled on paramiko's own test stub: password auth for one user, the
``sftp`` subsystem served from a temporary directory.
"""

from __future__ import annotations

import errno
import io
import os
import socket
import threading
from pathlib import Path
from types import TracebackType
from typing import Any

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from paramiko.common import (
    AUTH_FAILED,
    AUTH_SUCCESSFUL,
    OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED,
    OPEN_SUCCEEDED,
)
from paramiko.sftp import SFTP_FAILURE, SFTP_OK


def generate_ed25519() -> tuple[paramiko.PKey, str]:
    """A fresh Ed25519 key as ``(paramiko key, OpenSSH private key text)``."""
    pem = (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
        .decode()
    )
    return paramiko.Ed25519Key.from_private_key(io.StringIO(pem)), pem


def _err(exc: OSError) -> int:
    code: int = paramiko.SFTPServer.convert_errno(exc.errno or errno.EIO)
    return code


class _Auth(paramiko.ServerInterface):
    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password

    def get_allowed_auths(self, username: str) -> str:
        return "password"

    def check_auth_password(self, username: str, password: str) -> int:
        if username == self.username and password == self.password:
            return AUTH_SUCCESSFUL
        return AUTH_FAILED

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session":
            return OPEN_SUCCEEDED
        return OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class _Handle(paramiko.SFTPHandle):
    filename: str
    readfile: Any
    writefile: Any

    def stat(self) -> paramiko.SFTPAttributes | int:
        try:
            return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as exc:
            return _err(exc)


class _Sftp(paramiko.SFTPServerInterface):
    ROOT = "/nonexistent"

    def _real(self, path: str) -> str:
        return self.ROOT + self.canonicalize(path)

    def list_folder(self, path: str) -> list[paramiko.SFTPAttributes] | int:
        real = self._real(path)
        try:
            out = []
            for name in os.listdir(real):
                attr = paramiko.SFTPAttributes.from_stat(os.lstat(os.path.join(real, name)))
                attr.filename = name
                out.append(attr)
            return out
        except OSError as exc:
            return _err(exc)

    def stat(self, path: str) -> paramiko.SFTPAttributes | int:
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as exc:
            return _err(exc)

    def lstat(self, path: str) -> paramiko.SFTPAttributes | int:
        try:
            return paramiko.SFTPAttributes.from_stat(os.lstat(self._real(path)))
        except OSError as exc:
            return _err(exc)

    def open(
        self, path: str, flags: int, attr: paramiko.SFTPAttributes
    ) -> paramiko.SFTPHandle | int:
        real = self._real(path)
        try:
            fd = os.open(real, flags | getattr(os, "O_BINARY", 0), 0o644)
        except OSError as exc:
            return _err(exc)
        if flags & os.O_WRONLY:
            mode = "ab" if flags & os.O_APPEND else "wb"
        elif flags & os.O_RDWR:
            mode = "a+b" if flags & os.O_APPEND else "r+b"
        else:
            mode = "rb"
        fobj = os.fdopen(fd, mode)
        handle = _Handle(flags)
        handle.filename = real
        handle.readfile = fobj
        handle.writefile = fobj
        return handle

    def remove(self, path: str) -> int:
        try:
            os.remove(self._real(path))
        except OSError as exc:
            return _err(exc)
        return SFTP_OK

    def rename(self, oldpath: str, newpath: str) -> int:
        if os.path.exists(self._real(newpath)):
            return SFTP_FAILURE
        return self.posix_rename(oldpath, newpath)

    def posix_rename(self, oldpath: str, newpath: str) -> int:
        try:
            os.rename(self._real(oldpath), self._real(newpath))
        except OSError as exc:
            return _err(exc)
        return SFTP_OK

    def mkdir(self, path: str, attr: paramiko.SFTPAttributes) -> int:
        try:
            os.mkdir(self._real(path))
        except OSError as exc:
            return _err(exc)
        return SFTP_OK


class SftpTestServer:
    """``with SftpTestServer(root) as server: server.port, server.host_key_line``."""

    def __init__(self, root: Path, *, username: str = "sync", password: str = "s3cret") -> None:
        self.root = root
        self.username = username
        self.password = password
        self.host_key, _ = generate_ed25519()
        self.host_key_line = f"{self.host_key.get_name()} {self.host_key.get_base64()}"
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        # accept() is not woken by close() from another thread; poll instead.
        self._sock.settimeout(0.1)
        self.port = int(self._sock.getsockname()[1])
        self._transports: list[paramiko.Transport] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        root = str(self.root)

        class Root(_Sftp):
            ROOT = root

        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn.settimeout(None)
            transport = paramiko.Transport(conn)
            transport.add_server_key(self.host_key)
            transport.set_subsystem_handler("sftp", paramiko.SFTPServer, Root)
            self._transports.append(transport)
            try:
                transport.start_server(server=_Auth(self.username, self.password))
            except (paramiko.SSHException, EOFError, OSError):
                continue

    def __enter__(self) -> SftpTestServer:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._sock.close()
        for transport in self._transports:
            transport.close()
        self._thread.join(timeout=5)

    def config(self, **overrides: Any) -> dict[str, Any]:
        return {
            "host": "127.0.0.1",
            "port": self.port,
            "username": self.username,
            "host_key": self.host_key_line,
            **overrides,
        }
