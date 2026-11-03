"""Google Drive provider (Drive API v3 over httpx2) and its OAuth helpers.

Authorization uses the OAuth 2.0 authorization-code flow with PKCE. The
administrator registers a "Web application" OAuth client in Google Cloud
(redirect URI ``<NAS_PUBLIC_BASE_URL>/api/v1/remotes/oauth/callback``) and
stores its client id/secret on the connection; ``Connect`` then sends them
to Google's consent page, and the callback stores the resulting refresh
token (encrypted). Access tokens live in memory only.

Drive is not a path-addressed store: every item has an id and may share its
name with siblings. Paths are resolved segment by segment from the
connection's ``folder_id`` (default: My Drive root). Duplicate names and
Google-native documents (Docs/Sheets/Slides, which have no binary content)
are reported as rejected and never synced.
"""

from __future__ import annotations

import posixpath
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any, BinaryIO
from urllib.parse import urlencode

import httpx2

from app.core.errors import RemoteError
from app.services.remote.base import RawEntry, RemoteEntry, RemoteProvider, join, parent_of

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint, not a secret
API_URL = "https://www.googleapis.com/drive/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3"
SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER_MIME = "application/vnd.google-apps.folder"
_NATIVE_PREFIX = "application/vnd.google-apps."
_FIELDS = "id,name,mimeType,size,modifiedTime,md5Checksum"
_CHUNK = 1024 * 1024


def authorization_url(*, client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{AUTH_URL}?{urlencode(query)}"


def exchange_code(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    verifier: str,
    redirect_uri: str,
    timeout: int,
    transport: httpx2.BaseTransport | None = None,
) -> str:
    """Trade an authorization code for a refresh token."""
    with httpx2.Client(timeout=timeout, trust_env=False, transport=transport) as client:
        try:
            response = client.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": verifier,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                },
            )
        except httpx2.HTTPError as exc:
            raise RemoteError(f"Google token exchange failed: {exc}") from exc
    if response.status_code != 200:
        raise RemoteError(
            "Google refused the authorization code",
            details={"status": response.status_code, "error": _oauth_error(response)},
        )
    refresh_token = response.json().get("refresh_token")
    if not refresh_token:
        raise RemoteError("Google did not return a refresh token; retry the authorization")
    return str(refresh_token)


def _oauth_error(response: httpx2.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    value = body.get("error") if isinstance(body, dict) else None
    return str(value) if value else None


def _quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveProvider(RemoteProvider):
    def __init__(
        self,
        config: dict[str, Any],
        secrets: dict[str, Any],
        *,
        timeout: int,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self.client_id = str(config["client_id"])
        self.folder_id = str(config.get("folder_id") or "root")
        self.client_secret = str(secrets.get("client_secret") or "")
        self.refresh_token = str(secrets.get("refresh_token") or "")
        self.timeout = timeout
        self.transport = transport
        self._client: httpx2.Client | None = None
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._ids: dict[str, tuple[str, str]] = {}

    # -- connection and auth ----------------------------------------------------

    def connect(self) -> None:
        if not self.refresh_token:
            raise RemoteError("Google Drive is not authorized yet; use Connect first")
        self._client = httpx2.Client(
            timeout=self.timeout, trust_env=False, transport=self.transport
        )
        self._ids = {"": (self.folder_id, FOLDER_MIME)}

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def client(self) -> httpx2.Client:
        if self._client is None:
            raise RemoteError("Google Drive session is not connected")
        return self._client

    def _token(self, *, force: bool = False) -> str:
        if not force and self._access_token and time.monotonic() < self._expires_at:
            return self._access_token
        try:
            response = self.client.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
        except httpx2.HTTPError as exc:
            raise RemoteError(f"Google token refresh failed: {exc}") from exc
        if response.status_code != 200:
            raise RemoteError(
                "Google authorization was revoked or expired; reconnect the account",
                details={"error": _oauth_error(response)},
            )
        body = response.json()
        self._access_token = str(body["access_token"])
        self._expires_at = time.monotonic() + max(60, int(body.get("expires_in", 3600)) - 60)
        return self._access_token

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx2.Response:
        for attempt in range(2):
            headers = dict(kwargs.pop("headers", None) or {})
            headers["Authorization"] = f"Bearer {self._token(force=attempt > 0)}"
            try:
                response = self.client.request(method, url, headers=headers, **kwargs)
            except httpx2.HTTPError as exc:
                raise RemoteError(f"Google Drive request failed: {exc}") from exc
            if response.status_code != 401:
                return response
            kwargs["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
        raise RemoteError("Google Drive rejected the access token")

    def _check(self, response: httpx2.Response, what: str, path: str) -> dict[str, Any]:
        if response.status_code not in (200, 201):
            raise RemoteError(
                f"Google Drive {what} returned HTTP {response.status_code}",
                details={"path": path},
            )
        body = response.json()
        return body if isinstance(body, dict) else {}

    # -- id resolution ----------------------------------------------------------

    def _children(self, folder_id: str, *, name: str | None = None) -> Iterator[dict[str, Any]]:
        query = f"'{_quote(folder_id)}' in parents and trashed = false"
        if name is not None:
            query += f" and name = '{_quote(name)}'"
        token: str | None = None
        while True:
            params: dict[str, Any] = {
                "q": query,
                "fields": f"nextPageToken,files({_FIELDS})",
                "pageSize": 1000,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if token:
                params["pageToken"] = token
            body = self._check(
                self._request("GET", f"{API_URL}/files", params=params), "listing", folder_id
            )
            yield from body.get("files", [])
            token = body.get("nextPageToken")
            if not token:
                return

    def _resolve(self, path: str) -> tuple[str, str] | None:
        """``(id, mimeType)`` of the item at ``path``, or None."""
        if path in self._ids:
            return self._ids[path]
        parent = self._resolve(parent_of(path))
        if parent is None or parent[1] != FOLDER_MIME:
            return None
        matches = list(self._children(parent[0], name=posixpath.basename(path)))
        if not matches:
            return None
        if len(matches) > 1:
            raise RemoteError("Several Drive items share this name", details={"path": path})
        found = (str(matches[0]["id"]), str(matches[0]["mimeType"]))
        self._ids[path] = found
        return found

    def _folder_id(self, path: str) -> str:
        found = self._resolve(path)
        if found is None or found[1] != FOLDER_MIME:
            raise RemoteError("Remote folder not found", details={"path": path})
        return found[0]

    # -- operations -------------------------------------------------------------

    def _list(self, path: str) -> list[RawEntry]:
        items = list(self._children(self._folder_id(path)))
        counts: dict[str, int] = {}
        for item in items:
            counts[str(item["name"])] = counts.get(str(item["name"]), 0) + 1
        entries: list[RawEntry] = []
        for item in items:
            name = str(item["name"])
            mime = str(item.get("mimeType", ""))
            problem: str | None = None
            if counts[name] > 1:
                problem = "several Drive items share this name"
            elif mime.startswith(_NATIVE_PREFIX) and mime != FOLDER_MIME:
                problem = "Google-native document (no binary content to sync)"
            else:
                self._ids[join(path, name)] = (str(item["id"]), mime)
            entries.append(_raw(name, item, problem))
        return entries

    def stat(self, path: str) -> RemoteEntry | None:
        found = self._resolve(path)
        if found is None:
            return None
        if found[1] == FOLDER_MIME:
            return RemoteEntry(path=path, is_dir=True)
        body = self._check(
            self._request(
                "GET",
                f"{API_URL}/files/{found[0]}",
                params={"fields": _FIELDS, "supportsAllDrives": "true"},
            ),
            "metadata",
            path,
        )
        raw = _raw(str(body.get("name", "")), body, None)
        return RemoteEntry(
            path=path, is_dir=False, size=raw.size, mtime=raw.mtime, etag=raw.etag, md5=raw.md5
        )

    def download(self, path: str, out: BinaryIO) -> None:
        found = self._resolve(path)
        if found is None or found[1] == FOLDER_MIME:
            raise RemoteError("Remote file not found", details={"path": path})
        headers = {"Authorization": f"Bearer {self._token()}"}
        try:
            with self.client.stream(
                "GET",
                f"{API_URL}/files/{found[0]}",
                params={"alt": "media", "supportsAllDrives": "true"},
                headers=headers,
            ) as response:
                if response.status_code != 200:
                    raise RemoteError(
                        f"Google Drive download returned HTTP {response.status_code}",
                        details={"path": path},
                    )
                for chunk in response.iter_bytes(_CHUNK):
                    out.write(chunk)
        except httpx2.HTTPError as exc:
            raise RemoteError(f"Download failed: {exc}", details={"path": path}) from exc

    def upload(self, source: BinaryIO, path: str) -> None:
        """Resumable upload session; content replaces atomically on success."""
        existing = self._resolve(path)
        if existing is not None and existing[1] == FOLDER_MIME:
            raise RemoteError("A remote folder is in the way", details={"path": path})
        params = {"uploadType": "resumable", "supportsAllDrives": "true", "fields": "id"}
        if existing is None:
            session = self._request(
                "POST",
                f"{UPLOAD_URL}/files",
                params=params,
                json={
                    "name": posixpath.basename(path),
                    "parents": [self._folder_id(parent_of(path))],
                },
            )
        else:
            session = self._request(
                "PATCH", f"{UPLOAD_URL}/files/{existing[0]}", params=params, json={}
            )
        location = session.headers.get("location")
        if session.status_code != 200 or not location:
            raise RemoteError(
                f"Google Drive upload session returned HTTP {session.status_code}",
                details={"path": path},
            )
        try:
            response = self.client.put(location, content=_iter_file(source))
        except httpx2.HTTPError as exc:
            raise RemoteError(f"Upload failed: {exc}", details={"path": path}) from exc
        body = self._check(response, "upload", path)
        self._ids[path] = (str(body["id"]), "")

    def makedirs(self, path: str) -> None:
        current = ""
        for part in [p for p in path.split("/") if p]:
            parent_id = self._folder_id(current)
            current = join(current, part)
            found = self._resolve(current)
            if found is not None:
                if found[1] != FOLDER_MIME:
                    raise RemoteError("A remote file is in the way", details={"path": current})
                continue
            body = self._check(
                self._request(
                    "POST",
                    f"{API_URL}/files",
                    params={"fields": "id", "supportsAllDrives": "true"},
                    json={"name": part, "mimeType": FOLDER_MIME, "parents": [parent_id]},
                ),
                "create folder",
                current,
            )
            self._ids[current] = (str(body["id"]), FOLDER_MIME)

    def move(self, src: str, dst: str) -> None:
        found = self._resolve(src)
        if found is None:
            raise RemoteError("Remote file not found", details={"path": src})
        if self._resolve(dst) is not None:
            raise RemoteError("Move destination exists", details={"path": dst})
        old_parent = self._folder_id(parent_of(src))
        new_parent = self._folder_id(parent_of(dst))
        params: dict[str, str] = {"supportsAllDrives": "true", "fields": "id"}
        if old_parent != new_parent:
            params |= {"addParents": new_parent, "removeParents": old_parent}
        self._check(
            self._request(
                "PATCH",
                f"{API_URL}/files/{found[0]}",
                params=params,
                json={"name": posixpath.basename(dst)},
            ),
            "move",
            src,
        )
        self._ids.pop(src, None)
        self._ids[dst] = found


def _raw(name: str, item: dict[str, Any], problem: str | None) -> RawEntry:
    md5 = item.get("md5Checksum")
    return RawEntry(
        name=name,
        is_dir=item.get("mimeType") == FOLDER_MIME,
        size=int(item.get("size", 0) or 0),
        mtime=_rfc3339(item.get("modifiedTime")),
        etag=str(md5) if md5 else None,
        md5=str(md5).lower() if md5 else None,
        problem=problem,
    )


def _rfc3339(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _iter_file(source: BinaryIO) -> Iterator[bytes]:
    while chunk := source.read(_CHUNK):
        yield chunk
