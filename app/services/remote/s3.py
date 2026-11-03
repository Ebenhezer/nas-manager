"""S3-compatible object storage provider (boto3).

Works with AWS S3 and compatible services (MinIO, Ceph RGW, Backblaze B2,
Wasabi, ...) through ``endpoint_url``. Object storage has no directories:
"folders" are key prefixes, ``makedirs`` is a no-op, and ``move`` is a
server-side copy followed by deleting the source key (only ever used to
archive into ``.nas-sync-archive/``). A single ``PUT`` is atomic, so uploads
need no temp key.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any, BinaryIO, Literal

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.errors import RemoteError
from app.services.remote.base import (
    ARCHIVE_DIR_NAME,
    REMOTE_TMP_PREFIX,
    RawEntry,
    RemoteEntry,
    RemoteProvider,
    WalkResult,
    check_name,
    join,
)

_ERRORS = (BotoCoreError, ClientError)


class S3Provider(RemoteProvider):
    def __init__(
        self,
        config: dict[str, Any],
        secrets: dict[str, Any],
        *,
        timeout: int,
        client: BaseClient | None = None,
    ) -> None:
        self.bucket = str(config["bucket"])
        self.prefix = str(config.get("prefix") or "").strip("/")
        self.endpoint_url = config.get("endpoint_url") or None
        self.region = config.get("region") or None
        self.access_key_id = config.get("access_key_id") or None
        self.addressing_style = _addressing_style(config.get("addressing_style"))
        self.secret_access_key = secrets.get("secret_access_key") or None
        self.timeout = timeout
        self._client = client

    def connect(self) -> None:
        if self._client is not None:
            return
        self._client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            region_name=self.region,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            config=Config(
                connect_timeout=self.timeout,
                read_timeout=self.timeout,
                retries={"max_attempts": 3, "mode": "standard"},
                s3={"addressing_style": self.addressing_style},
            ),
        )

    def close(self) -> None:
        self._client = None

    @property
    def client(self) -> Any:
        if self._client is None:
            raise RemoteError("S3 client is not connected")
        return self._client

    def _key(self, path: str) -> str:
        return join(self.prefix, path)

    def _dir_prefix(self, path: str) -> str:
        key = self._key(path)
        return f"{key}/" if key else ""

    def _pages(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            yield from paginator.paginate(Bucket=self.bucket, **kwargs)
        except _ERRORS as exc:
            raise RemoteError(f"S3 listing failed: {_describe(exc)}") from exc

    def test(self) -> str:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except _ERRORS as exc:
            raise RemoteError(f"S3 bucket check failed: {_describe(exc)}") from exc
        return f"Connected to bucket {self.bucket}"

    def _list(self, path: str) -> list[RawEntry]:
        prefix = self._dir_prefix(path)
        entries: list[RawEntry] = []
        for page in self._pages(Prefix=prefix, Delimiter="/"):
            for common in page.get("CommonPrefixes", []):
                name = str(common["Prefix"])[len(prefix) :].rstrip("/")
                entries.append(RawEntry(name=name, is_dir=True))
            for obj in page.get("Contents", []):
                name = str(obj["Key"])[len(prefix) :]
                if not name or name.endswith("/"):
                    continue  # folder marker objects
                entries.append(_raw(name, obj))
        return entries

    def walk(self, base: str) -> WalkResult:
        """One flat listing of every key under ``base`` (no per-folder calls)."""
        prefix = self._dir_prefix(base)
        result = WalkResult()
        for page in self._pages(Prefix=prefix):
            for obj in page.get("Contents", []):
                rel = str(obj["Key"])[len(prefix) :]
                if not rel or rel.endswith("/"):
                    continue
                parts = rel.split("/")
                if parts[0] == ARCHIVE_DIR_NAME or parts[-1].startswith(REMOTE_TMP_PREFIX):
                    continue
                reasons = [check_name(part) for part in parts]
                reason = next((r for r in reasons if r is not None), None)
                if reason is not None:
                    result.rejected.append((repr(rel), reason))
                    continue
                raw = _raw(parts[-1], obj)
                result.files[rel] = RemoteEntry(
                    path=rel,
                    is_dir=False,
                    size=raw.size,
                    mtime=raw.mtime,
                    etag=raw.etag,
                    md5=raw.md5,
                )
        return result

    def dir_exists(self, path: str) -> bool:
        """Prefixes are implicit: a "folder" exists when any key is under it."""
        if not path:
            return True
        for page in self._pages(Prefix=self._dir_prefix(path), MaxKeys=1):
            return bool(page.get("Contents") or page.get("CommonPrefixes"))
        return False

    def stat(self, path: str) -> RemoteEntry | None:
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=self._key(path))
        except ClientError as exc:
            if _status(exc) == 404:
                return None
            raise RemoteError(f"S3 HEAD failed: {_describe(exc)}", details={"path": path}) from exc
        except BotoCoreError as exc:
            raise RemoteError(f"S3 HEAD failed: {_describe(exc)}", details={"path": path}) from exc
        etag = _etag(head.get("ETag"))
        return RemoteEntry(
            path=path,
            is_dir=False,
            size=int(head.get("ContentLength", 0)),
            mtime=head.get("LastModified"),
            etag=etag,
            md5=_md5_from_etag(etag),
        )

    def download(self, path: str, out: BinaryIO) -> None:
        try:
            self.client.download_fileobj(self.bucket, self._key(path), out)
        except _ERRORS as exc:
            raise RemoteError(f"Download failed: {_describe(exc)}", details={"path": path}) from exc

    def upload(self, source: BinaryIO, path: str) -> None:
        try:
            self.client.upload_fileobj(source, self.bucket, self._key(path))
        except _ERRORS as exc:
            raise RemoteError(f"Upload failed: {_describe(exc)}", details={"path": path}) from exc

    def makedirs(self, path: str) -> None:
        return None

    def move(self, src: str, dst: str) -> None:
        if self.stat(dst) is not None:
            raise RemoteError("Move destination exists", details={"path": dst})
        try:
            self.client.copy(
                {"Bucket": self.bucket, "Key": self._key(src)}, self.bucket, self._key(dst)
            )
            self.client.delete_object(Bucket=self.bucket, Key=self._key(src))
        except _ERRORS as exc:
            raise RemoteError(f"Move failed: {_describe(exc)}", details={"path": src}) from exc


def _addressing_style(value: Any) -> Literal["auto", "path", "virtual"]:
    if value == "path":
        return "path"
    if value == "virtual":
        return "virtual"
    return "auto"


def _raw(name: str, obj: dict[str, Any]) -> RawEntry:
    etag = _etag(obj.get("ETag"))
    modified = obj.get("LastModified")
    return RawEntry(
        name=name,
        is_dir=False,
        size=int(obj.get("Size", 0)),
        mtime=modified if isinstance(modified, datetime) else None,
        etag=etag,
        md5=_md5_from_etag(etag),
    )


def _etag(value: Any) -> str | None:
    return str(value).strip('"') if value else None


def _md5_from_etag(etag: str | None) -> str | None:
    """Single-part uploads have the content MD5 as ETag; multipart ones do not."""
    if etag and len(etag) == 32 and "-" not in etag:
        return etag.lower()
    return None


def _status(exc: ClientError) -> int:
    return int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _describe(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return f"{_code(exc) or 'error'} (HTTP {_status(exc)})"
    return type(exc).__name__
