"""The on-target backup repository format (Phase 11).

Layout under the repository path on any provider::

    repo.json                 plaintext: format, version, scrypt parameters,
                              a sealed key-check value, creation time
    snapshots/<id>.snap       sealed, zlib-compressed JSON manifest
    blobs/<aa>/<name>         sealed file content; ``name`` is the keyed
                              HMAC of the content's SHA-256 (``aa`` = first
                              two hex digits)

Blobs are content-addressed, so identical content is stored once across
every snapshot and job in the repository. A snapshot is written only after
all of its blobs exist, so an interrupted run never leaves a snapshot that
references missing content; blobs it already uploaded are reused by the
next run or removed by garbage collection.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import secrets
import tempfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from app.core.errors import BadRequestError, ConflictError, RemoteError
from app.services.backup import crypto
from app.services.remote.base import RemoteProvider, join

REPO_FORMAT = "nas-manager-backup"
REPO_VERSION = 1
REPO_FILE = "repo.json"
SNAPSHOT_DIR = "snapshots"
BLOB_DIR = "blobs"
_CHECK_VALUE = b"nas-manager backup repository v1"
_REPO_FILE_MAX = 64 * 1024
SNAPSHOT_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
BLOB_RE = re.compile(r"^[0-9a-f]{64}$")


class WrongPassphraseError(BadRequestError):
    code = "wrong_passphrase"


class RepositoryNotFoundError(RemoteError):
    code = "repository_not_found"


def new_snapshot_id(now: datetime) -> str:
    return f"{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"


def snapshot_time(snapshot_id: str) -> datetime:
    return datetime.strptime(snapshot_id[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


@dataclass
class Repository:
    provider: RemoteProvider
    prefix: str
    keys: crypto.Keys
    staging: Path

    # -- opening ----------------------------------------------------------------

    @classmethod
    def open(
        cls, provider: RemoteProvider, prefix: str, passphrase: str, *, staging: Path
    ) -> Repository:
        info = _read_repo_file(provider, prefix)
        if info is None:
            raise RepositoryNotFoundError(
                "No backup repository at this location (is the target mounted and the path right?)",
                details={"path": prefix},
            )
        return cls(provider, prefix, _unlock(info, passphrase), staging)

    @classmethod
    def initialize(
        cls,
        provider: RemoteProvider,
        prefix: str,
        passphrase: str,
        *,
        staging: Path,
        scrypt_n: int,
    ) -> tuple[Repository, bool]:
        """Create a repository, or adopt the one already there.

        Returns ``(repository, created)``. An existing repository is only
        adopted when the passphrase unlocks it; a non-empty directory that is
        not a repository is refused, so a backup never mixes with other data.
        """
        info = _read_repo_file(provider, prefix)
        if info is not None:
            return cls(provider, prefix, _unlock(info, passphrase), staging), False
        if prefix and provider.dir_exists(prefix):
            entries, _ = provider.list_dir(prefix, limit=1)
            if entries:
                raise ConflictError(
                    "The target directory is not empty and is not a backup repository; "
                    "choose an empty or new directory",
                    details={"path": prefix},
                )
        params = crypto.KdfParams.new(scrypt_n)
        keys = crypto.derive_keys(passphrase, params)
        document = {
            "format": REPO_FORMAT,
            "version": REPO_VERSION,
            "kdf": params.as_dict(),
            "check": crypto.seal(_CHECK_VALUE, keys).hex(),
            "created_at": datetime.now(UTC).isoformat(),
        }
        for directory in (prefix, join(prefix, SNAPSHOT_DIR), join(prefix, BLOB_DIR)):
            if directory:
                provider.makedirs(directory)
        provider.upload(
            io.BytesIO(json.dumps(document, indent=2).encode()), join(prefix, REPO_FILE)
        )
        return cls(provider, prefix, keys, staging), True

    # -- paths --------------------------------------------------------------------

    def _blob_path(self, name: str) -> str:
        if not BLOB_RE.match(name):
            raise RemoteError("Invalid blob name", details={"blob": name})
        return join(self.prefix, BLOB_DIR, name[:2], name)

    def _snapshot_path(self, snapshot_id: str) -> str:
        if not SNAPSHOT_ID_RE.match(snapshot_id):
            raise BadRequestError("Invalid snapshot id", details={"snapshot_id": snapshot_id})
        return join(self.prefix, SNAPSHOT_DIR, f"{snapshot_id}.snap")

    @contextlib.contextmanager
    def temp_file(self) -> Iterator[BinaryIO]:
        """A private scratch file in the staging directory (removed on exit)."""
        self.staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = tempfile.TemporaryFile(dir=self.staging)  # noqa: SIM115
        try:
            yield handle
        finally:
            handle.close()

    # -- blobs --------------------------------------------------------------------

    def has_blob(self, name: str) -> bool:
        return self.provider.stat(self._blob_path(name)) is not None

    def put_blob(self, name: str, encrypted: BinaryIO) -> None:
        path = self._blob_path(name)
        self.provider.makedirs(join(self.prefix, BLOB_DIR, name[:2]))
        encrypted.seek(0)
        self.provider.upload(encrypted, path)

    def get_blob(self, name: str, out: BinaryIO) -> tuple[str, int]:
        """Download and decrypt one blob into ``out``; return its SHA-256 and size."""
        with self.temp_file() as encrypted:
            self.provider.download(self._blob_path(name), encrypted)
            encrypted.seek(0)
            return crypto.decrypt_stream(encrypted, out, self.keys)

    def list_blobs(self) -> dict[str, int]:
        base = join(self.prefix, BLOB_DIR)
        if not self.provider.dir_exists(base):
            return {}
        found: dict[str, int] = {}
        for path, entry in self.provider.walk(base).files.items():
            name = path.rsplit("/", 1)[-1]
            if BLOB_RE.match(name) and path == f"{name[:2]}/{name}":
                found[name] = entry.size
        return found

    def delete_blob(self, name: str) -> None:
        self.provider.delete(self._blob_path(name))

    # -- snapshots ------------------------------------------------------------------

    def write_snapshot(self, snapshot_id: str, manifest: dict[str, Any]) -> int:
        payload = crypto.seal(zlib.compress(json.dumps(manifest).encode(), 6), self.keys)
        self.provider.makedirs(join(self.prefix, SNAPSHOT_DIR))
        self.provider.upload(io.BytesIO(payload), self._snapshot_path(snapshot_id))
        return len(payload)

    def read_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        with self.temp_file() as handle:
            self.provider.download(self._snapshot_path(snapshot_id), handle)
            handle.seek(0)
            sealed = handle.read()
        try:
            document = json.loads(zlib.decompress(crypto.unseal(sealed, self.keys)))
        except crypto.DecryptionError as exc:
            raise RemoteError(
                f"Snapshot {snapshot_id} cannot be decrypted: {exc}",
                details={"snapshot_id": snapshot_id},
            ) from exc
        except (zlib.error, ValueError) as exc:
            raise RemoteError(
                f"Snapshot {snapshot_id} is malformed", details={"snapshot_id": snapshot_id}
            ) from exc
        if not isinstance(document, dict) or not isinstance(document.get("entries"), list):
            raise RemoteError(
                f"Snapshot {snapshot_id} is malformed", details={"snapshot_id": snapshot_id}
            )
        return document

    def list_snapshot_ids(self) -> list[str]:
        base = join(self.prefix, SNAPSHOT_DIR)
        if not self.provider.dir_exists(base):
            return []
        entries, _ = self.provider.list_dir(base, limit=1_000_000)
        ids = [
            entry.name[: -len(".snap")]
            for entry in entries
            if not entry.is_dir and entry.name.endswith(".snap")
        ]
        return sorted(i for i in ids if SNAPSHOT_ID_RE.match(i))

    def delete_snapshot(self, snapshot_id: str) -> None:
        self.provider.delete(self._snapshot_path(snapshot_id))


def _read_repo_file(provider: RemoteProvider, prefix: str) -> dict[str, Any] | None:
    path = join(prefix, REPO_FILE)
    entry = provider.stat(path)
    if entry is None or entry.is_dir:
        return None
    if entry.size > _REPO_FILE_MAX:
        raise RemoteError("repo.json is unexpectedly large", details={"path": path})
    buffer = io.BytesIO()
    provider.download(path, buffer)
    try:
        info = json.loads(buffer.getvalue()[:_REPO_FILE_MAX])
    except ValueError as exc:
        raise RemoteError("repo.json is not valid JSON", details={"path": path}) from exc
    if not isinstance(info, dict) or info.get("format") != REPO_FORMAT:
        raise RemoteError("This is not a NAS Manager backup repository", details={"path": path})
    if info.get("version") != REPO_VERSION:
        raise RemoteError(
            "Unsupported backup repository version", details={"version": info.get("version")}
        )
    return info


def _unlock(info: dict[str, Any], passphrase: str) -> crypto.Keys:
    try:
        params = crypto.KdfParams.from_dict(info.get("kdf"))
        keys = crypto.derive_keys(passphrase, params)
        if crypto.unseal(bytes.fromhex(str(info.get("check", ""))), keys) != _CHECK_VALUE:
            raise crypto.DecryptionError("check value mismatch")
    except (crypto.DecryptionError, ValueError) as exc:
        raise WrongPassphraseError("The passphrase does not unlock this backup repository") from exc
    return keys


def fsync_file(handle: BinaryIO) -> None:
    handle.flush()
    os.fsync(handle.fileno())
