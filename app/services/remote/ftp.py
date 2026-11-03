"""FTP and explicit FTPS providers (stdlib ``ftplib``).

FTPS uses ``AUTH TLS`` with certificate verification against the system
trust store and encrypts the data channel (``PROT P``). Plain FTP sends the
password in clear text and is only allowed when the connection sets
``allow_insecure``. Listings require ``MLSD`` (RFC 3659): the legacy
``LIST`` format is not machine-parseable reliably enough to sync from.
"""

from __future__ import annotations

import contextlib
import ftplib
import posixpath
import ssl
import uuid
from datetime import UTC, datetime
from typing import Any, BinaryIO

from app.core.errors import RemoteError
from app.services.remote.base import (
    REMOTE_TMP_PREFIX,
    RawEntry,
    RemoteEntry,
    RemoteProvider,
    join,
    parent_of,
)

_ERRORS = (ftplib.Error, OSError, EOFError)


class FtpProvider(RemoteProvider):
    def __init__(
        self, config: dict[str, Any], secrets: dict[str, Any], *, timeout: int, tls: bool
    ) -> None:
        self.host = str(config["host"])
        self.port = int(config.get("port") or 21)
        self.username = str(config.get("username") or "anonymous")
        self.base_path = str(config.get("base_path") or "")
        self.passive = bool(config.get("passive", True))
        self.password = str(secrets.get("password") or "")
        self.tls = tls
        self.timeout = timeout
        self._ftp: ftplib.FTP | None = None

    def connect(self) -> None:
        ftp: ftplib.FTP
        if self.tls:
            ftp = ftplib.FTP_TLS(context=ssl.create_default_context(), timeout=self.timeout)  # noqa: S321
        else:
            # Plain FTP is only reachable with allow_insecure (see registry).
            ftp = ftplib.FTP(timeout=self.timeout)  # noqa: S321
        try:
            ftp.connect(self.host, self.port, timeout=self.timeout)
            ftp.login(self.username, self.password)
            if isinstance(ftp, ftplib.FTP_TLS):
                ftp.prot_p()
            ftp.set_pasv(self.passive)
        except ftplib.error_perm as exc:
            _quiet_close(ftp)
            raise RemoteError("FTP login refused by the server") from exc
        except ssl.SSLError as exc:
            _quiet_close(ftp)
            raise RemoteError(f"TLS negotiation with {self.host} failed: {exc}") from exc
        except _ERRORS as exc:
            _quiet_close(ftp)
            raise RemoteError(f"Cannot connect to {self.host}:{self.port}: {exc}") from exc
        self._ftp = ftp

    def close(self) -> None:
        if self._ftp is not None:
            _quiet_close(self._ftp)
            self._ftp = None

    @property
    def ftp(self) -> ftplib.FTP:
        if self._ftp is None:
            raise RemoteError("FTP session is not connected")
        return self._ftp

    def _abs(self, path: str) -> str:
        base = self.base_path or ""
        joined = posixpath.join(base, path) if path else base
        return joined or "."

    def _list(self, path: str) -> list[RawEntry]:
        try:
            listing = list(self.ftp.mlsd(self._abs(path), facts=["type", "size", "modify"]))
        except ftplib.error_perm as exc:
            if str(exc)[:3] in {"500", "501", "502"}:
                raise RemoteError("The FTP server does not support MLSD listings") from exc
            raise RemoteError(
                f"Cannot list remote directory: {exc}", details={"path": path}
            ) from exc
        except _ERRORS as exc:
            raise RemoteError(
                f"Cannot list remote directory: {exc}", details={"path": path}
            ) from exc
        entries: list[RawEntry] = []
        for name, facts in listing:
            kind = facts.get("type", "").lower()
            if kind not in {"file", "dir"}:
                continue  # cdir, pdir, OS-specific links
            entries.append(
                RawEntry(
                    name=name,
                    is_dir=kind == "dir",
                    size=_int(facts.get("size")),
                    mtime=parse_modify(facts.get("modify")),
                )
            )
        return entries

    def stat(self, path: str) -> RemoteEntry | None:
        if not path:
            return RemoteEntry(path="", is_dir=True)
        name = posixpath.basename(path)
        try:
            entries = self._list(parent_of(path))
        except RemoteError:
            return None
        for raw in entries:
            if raw.name == name:
                return RemoteEntry(path=path, is_dir=raw.is_dir, size=raw.size, mtime=raw.mtime)
        return None

    def download(self, path: str, out: BinaryIO) -> None:
        try:
            self.ftp.retrbinary(f"RETR {self._abs(path)}", out.write)
        except _ERRORS as exc:
            raise RemoteError(f"Download failed: {exc}", details={"path": path}) from exc

    def upload(self, source: BinaryIO, path: str) -> None:
        temporary = join(parent_of(path), f"{REMOTE_TMP_PREFIX}{uuid.uuid4().hex}.part")
        try:
            self.ftp.storbinary(f"STOR {self._abs(temporary)}", source)
            try:
                self.ftp.rename(self._abs(temporary), self._abs(path))
            except ftplib.error_perm:
                # Some servers refuse to rename over an existing file. The
                # new content is complete in the temp file at this point.
                self.ftp.delete(self._abs(path))
                self.ftp.rename(self._abs(temporary), self._abs(path))
        except _ERRORS as exc:
            with contextlib.suppress(*_ERRORS):
                self.ftp.delete(self._abs(temporary))
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
                self.ftp.mkd(self._abs(current))
            except _ERRORS as exc:
                raise RemoteError(f"mkdir failed: {exc}", details={"path": current}) from exc

    def move(self, src: str, dst: str) -> None:
        if self.stat(dst) is not None:
            raise RemoteError("Move destination exists", details={"path": dst})
        try:
            self.ftp.rename(self._abs(src), self._abs(dst))
        except _ERRORS as exc:
            raise RemoteError(f"Move failed: {exc}", details={"path": src}) from exc


def parse_modify(value: str | None) -> datetime | None:
    """MLSD ``modify`` fact: ``YYYYMMDDHHMMSS[.sss]`` in UTC."""
    if not value:
        return None
    try:
        return datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _int(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _quiet_close(ftp: ftplib.FTP) -> None:
    try:
        ftp.quit()
    except _ERRORS:
        ftp.close()
