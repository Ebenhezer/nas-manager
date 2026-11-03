"""Encryption at rest for remote credentials (Phase 7).

Remote connection secrets (passwords, private keys, S3 secret keys, OAuth
refresh tokens) are stored as one Fernet token per connection. Fernet is
AES-128-CBC + HMAC-SHA256 with a random IV, so a database leak alone reveals
nothing. The key lives in a separate file (``NAS_CREDENTIALS_KEY_FILE``)
that is created with mode 0600 on first use and refused when it is readable
by anyone but its owner. The web process and the sync worker share it.

Key rotation is not implemented yet; losing the key file means re-entering
every remote credential.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings
from app.core.errors import ConfigurationError


def load_or_create_key(path: Path) -> bytes:
    """Return the Fernet key at ``path``, creating it atomically if missing."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as handle:
            handle.write(Fernet.generate_key())
            handle.flush()
            os.fsync(handle.fileno())
    try:
        info = path.lstat()
    except OSError as exc:
        raise ConfigurationError("The credentials key file cannot be read") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ConfigurationError("The credentials key file is not a regular file")
    if info.st_mode & 0o077:
        raise ConfigurationError(
            "The credentials key file must not be accessible by group or others (chmod 600)",
            details={"path": str(path)},
        )
    key = path.read_bytes().strip()
    try:
        Fernet(key)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError("The credentials key file does not hold a valid key") from exc
    return key


class CredentialCipher:
    """Encrypts and decrypts JSON objects of secret values."""

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def from_settings(cls, settings: Settings) -> CredentialCipher:
        return cls(load_or_create_key(settings.resolved_credentials_key_file))

    def encrypt_json(self, value: dict[str, Any]) -> str:
        return self._fernet.encrypt(json.dumps(value, sort_keys=True).encode()).decode()

    def decrypt_json(self, token: str | None) -> dict[str, Any]:
        if not token:
            return {}
        try:
            data = json.loads(self._fernet.decrypt(token.encode()))
        except (InvalidToken, ValueError) as exc:
            # Never echo the ciphertext: the message must stay client-safe.
            raise ConfigurationError(
                "Stored credentials cannot be decrypted with the configured key"
            ) from exc
        if not isinstance(data, dict):
            raise ConfigurationError("Stored credentials are malformed")
        return data
