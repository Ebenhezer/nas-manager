"""Credential encryption at rest (Phase 7)."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.core.secrets import CredentialCipher, load_or_create_key


def test_key_is_created_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "credentials.key"
    key = load_or_create_key(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert load_or_create_key(path) == key  # reused, never regenerated


def test_default_key_location(settings: Settings) -> None:
    settings.ensure_directories()
    CredentialCipher.from_settings(settings)
    assert (
        settings.resolved_credentials_key_file == settings.data_dir / "secrets" / "credentials.key"
    )
    assert settings.resolved_credentials_key_file.is_file()


def test_loose_permissions_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "credentials.key"
    path.write_bytes(Fernet.generate_key())
    path.chmod(0o644)
    with pytest.raises(ConfigurationError, match="chmod 600"):
        load_or_create_key(path)


def test_invalid_key_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "credentials.key"
    path.write_bytes(b"not a fernet key")
    path.chmod(0o600)
    with pytest.raises(ConfigurationError, match="valid key"):
        load_or_create_key(path)


def test_round_trip_and_wrong_key() -> None:
    cipher = CredentialCipher(Fernet.generate_key())
    token = cipher.encrypt_json({"password": "hunter2hunter2"})
    assert "hunter2" not in token
    assert cipher.decrypt_json(token) == {"password": "hunter2hunter2"}
    assert cipher.decrypt_json(None) == {}
    other = CredentialCipher(Fernet.generate_key())
    with pytest.raises(ConfigurationError) as excinfo:
        other.decrypt_json(token)
    assert token not in excinfo.value.message
