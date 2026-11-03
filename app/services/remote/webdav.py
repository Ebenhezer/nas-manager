"""WebDAV provider (RFC 4918) over httpx2.

``url`` is the base collection, e.g. ``https://cloud.example.org/remote.php/dav/files/me/``.
HTTPS is required unless the connection sets ``allow_insecure``; TLS
certificates are always verified. Uploads ``PUT`` to a temp name and are
``MOVE``d into place so a partial upload never appears under the final name.
"""

from __future__ import annotations

import posixpath
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, BinaryIO
from urllib.parse import quote, unquote, urlsplit

import httpx2

from app.core.errors import RemoteError
from app.services.remote.base import (
    REMOTE_TMP_PREFIX,
    RawEntry,
    RemoteEntry,
    RemoteProvider,
    join,
    parent_of,
)

_DAV = "{DAV:}"
_PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop>'
    b"<d:resourcetype/><d:getcontentlength/><d:getlastmodified/><d:getetag/>"
    b"</d:prop></d:propfind>"
)
# Upper bound for one PROPFIND response (a very large directory).
_MAX_LISTING_BYTES = 64 * 1024 * 1024
_CHUNK = 1024 * 1024


class WebDavProvider(RemoteProvider):
    def __init__(
        self,
        config: dict[str, Any],
        secrets: dict[str, Any],
        *,
        timeout: int,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self.url = str(config["url"]).rstrip("/") + "/"
        self.username = str(config.get("username") or "")
        self.password = str(secrets.get("password") or "")
        self.timeout = timeout
        self.transport = transport
        self._client: httpx2.Client | None = None

    def connect(self) -> None:
        auth = (self.username, self.password) if self.username else None
        self._client = httpx2.Client(
            auth=auth,
            timeout=self.timeout,
            verify=True,
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def client(self) -> httpx2.Client:
        if self._client is None:
            raise RemoteError("WebDAV session is not connected")
        return self._client

    def _href(self, path: str, *, collection: bool = False) -> str:
        encoded = "/".join(quote(part, safe="") for part in path.split("/") if part)
        url = self.url + encoded
        if collection and encoded:
            url += "/"
        return url

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx2.Response:
        try:
            return self.client.request(method, url, **kwargs)
        except httpx2.HTTPError as exc:
            raise RemoteError(f"WebDAV {method} failed: {exc}") from exc

    def _propfind(
        self, path: str, depth: str, *, collection: bool = False
    ) -> list[tuple[str, RawEntry]]:
        url = self._href(path, collection=collection)
        response = self._request(
            "PROPFIND",
            url,
            content=_PROPFIND_BODY,
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
        )
        if response.status_code == 404:
            raise FileNotFoundError(path)
        if response.status_code == 401:
            raise RemoteError("WebDAV authentication failed")
        if response.status_code != 207:
            raise RemoteError(
                f"WebDAV PROPFIND returned HTTP {response.status_code}", details={"path": path}
            )
        if len(response.content) > _MAX_LISTING_BYTES:
            raise RemoteError("WebDAV listing is too large", details={"path": path})
        return _parse_multistatus(response.content)

    def _list(self, path: str) -> list[RawEntry]:
        try:
            items = self._propfind(path, "1", collection=True)
        except FileNotFoundError as exc:
            raise RemoteError("Remote directory not found", details={"path": path}) from exc
        base = unquote(urlsplit(self._href(path, collection=True)).path).rstrip("/")
        entries: list[RawEntry] = []
        for href_path, raw in items:
            if href_path.rstrip("/") == base:
                continue  # the collection itself
            entries.append(raw)
        return entries

    def dir_exists(self, path: str) -> bool:
        if not path:
            return True
        try:
            items = self._propfind(path, "0", collection=True)
        except FileNotFoundError:
            return False
        return bool(items) and items[0][1].is_dir

    def stat(self, path: str) -> RemoteEntry | None:
        try:
            items = self._propfind(path, "0")
        except FileNotFoundError:
            return None
        if not items:
            return None
        raw = items[0][1]
        return RemoteEntry(
            path=path, is_dir=raw.is_dir, size=raw.size, mtime=raw.mtime, etag=raw.etag
        )

    def download(self, path: str, out: BinaryIO) -> None:
        try:
            with self.client.stream("GET", self._href(path)) as response:
                if response.status_code != 200:
                    raise RemoteError(
                        f"WebDAV GET returned HTTP {response.status_code}", details={"path": path}
                    )
                for chunk in response.iter_bytes(_CHUNK):
                    out.write(chunk)
        except httpx2.HTTPError as exc:
            raise RemoteError(f"Download failed: {exc}", details={"path": path}) from exc

    def upload(self, source: BinaryIO, path: str) -> None:
        temporary = join(parent_of(path), f"{REMOTE_TMP_PREFIX}{uuid.uuid4().hex}.part")
        response = self._request("PUT", self._href(temporary), content=_iter_file(source))
        if response.status_code not in (200, 201, 204):
            raise RemoteError(
                f"WebDAV PUT returned HTTP {response.status_code}", details={"path": path}
            )
        moved = self._request(
            "MOVE",
            self._href(temporary),
            headers={"Destination": self._href(path), "Overwrite": "T"},
        )
        if moved.status_code not in (201, 204):
            self._request("DELETE", self._href(temporary))
            raise RemoteError(
                f"WebDAV MOVE returned HTTP {moved.status_code}", details={"path": path}
            )

    def makedirs(self, path: str) -> None:
        current = ""
        for part in [p for p in path.split("/") if p]:
            current = join(current, part)
            response = self._request("MKCOL", self._href(current, collection=True))
            if response.status_code in (201, 405):  # created, or already exists
                continue
            raise RemoteError(
                f"WebDAV MKCOL returned HTTP {response.status_code}", details={"path": current}
            )

    def move(self, src: str, dst: str) -> None:
        response = self._request(
            "MOVE", self._href(src), headers={"Destination": self._href(dst), "Overwrite": "F"}
        )
        if response.status_code not in (201, 204):
            raise RemoteError(
                f"WebDAV MOVE returned HTTP {response.status_code}", details={"path": src}
            )

    def delete(self, path: str) -> None:
        response = self._request("DELETE", self._href(path))
        if response.status_code not in (200, 204, 404):
            raise RemoteError(
                f"WebDAV DELETE returned HTTP {response.status_code}", details={"path": path}
            )


def _iter_file(source: BinaryIO) -> Any:
    while chunk := source.read(_CHUNK):
        yield chunk


def _parse_multistatus(body: bytes) -> list[tuple[str, RawEntry]]:
    """Parse a 207 Multi-Status body into ``(href path, entry)`` pairs.

    ElementTree never resolves external entities, and the bundled expat
    (>= 2.4) caps entity amplification, so untrusted server XML cannot read
    local files or explode in memory; the body size is also capped.
    """
    try:
        root = ET.fromstring(body)  # noqa: S314
    except ET.ParseError as exc:
        raise RemoteError("WebDAV server returned malformed XML") from exc
    results: list[tuple[str, RawEntry]] = []
    for response in root.findall(f"{_DAV}response"):
        href = response.findtext(f"{_DAV}href") or ""
        href_path = unquote(urlsplit(href).path)
        name = posixpath.basename(href_path.rstrip("/"))
        prop = _ok_prop(response)
        if prop is None:
            continue
        resourcetype = prop.find(f"{_DAV}resourcetype")
        is_dir = resourcetype is not None and resourcetype.find(f"{_DAV}collection") is not None
        results.append(
            (
                href_path,
                RawEntry(
                    name=name,
                    is_dir=is_dir,
                    size=_int(prop.findtext(f"{_DAV}getcontentlength")),
                    mtime=_http_date(prop.findtext(f"{_DAV}getlastmodified")),
                    etag=(prop.findtext(f"{_DAV}getetag") or "").strip() or None,
                ),
            )
        )
    return results


def _ok_prop(response: ET.Element) -> ET.Element | None:
    for propstat in response.findall(f"{_DAV}propstat"):
        status = propstat.findtext(f"{_DAV}status") or ""
        if " 200 " in f"{status} ":
            return propstat.find(f"{_DAV}prop")
    return None


def _int(value: str | None) -> int:
    try:
        return int((value or "0").strip())
    except ValueError:
        return 0


def _http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
