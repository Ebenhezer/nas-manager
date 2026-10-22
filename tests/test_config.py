from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def make(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


def test_defaults_are_safe(tmp_path: Path) -> None:
    s = make(data_dir=tmp_path)
    assert s.host == "127.0.0.1"
    assert s.environment == "production"
    assert s.debug is False
    assert s.auto_migrate is False
    assert s.resolved_database_url == f"sqlite:///{tmp_path / 'nas.db'}"
    assert s.resolved_log_dir == tmp_path / "logs"


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NAS_PORT", "9090")
    monkeypatch.setenv("NAS_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("NAS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NAS_DATABASE_URL", "sqlite:////tmp/other.db")
    s = make()
    assert s.port == 9090
    assert s.log_level == "DEBUG"
    assert s.data_dir == tmp_path.resolve()
    assert s.resolved_database_url == "sqlite:////tmp/other.db"


def test_relative_data_dir_is_resolved_to_absolute() -> None:
    assert make(data_dir=Path("relative-data")).data_dir.is_absolute()


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_invalid_port_rejected(port: int) -> None:
    with pytest.raises(ValidationError):
        make(port=port)


def test_invalid_log_level_rejected() -> None:
    with pytest.raises(ValidationError):
        make(log_level="LOUD")


def test_non_loopback_bind_allowed_after_auth_exists() -> None:
    # Phase 1 refused non-loopback binds until authentication existed. Phase 2
    # ships authentication, so the guard is gone and explicit 0.0.0.0 is legal.
    s = make(host="0.0.0.0")  # noqa: S104
    assert s.host == "0.0.0.0"  # noqa: S104


def test_samesite_none_requires_secure_cookie() -> None:
    with pytest.raises(ValidationError, match="SameSite=None"):
        make(session_cookie_samesite="none")


def test_samesite_none_accepted_when_secure() -> None:
    s = make(session_cookie_samesite="none", session_cookie_secure=True)
    assert s.session_cookie_samesite == "none"


def test_idle_ceiling_may_not_be_shorter_than_ttl() -> None:
    with pytest.raises(ValidationError, match="IDLE"):
        make(session_idle_max_seconds=3600, session_ttl_seconds=7200)


def test_argon2_defaults_are_costed_for_production() -> None:
    s = make()
    assert s.argon2_time_cost == 3
    assert s.argon2_memory_cost == 65536
    assert s.argon2_parallelism == 4


def test_argon2_memory_below_floor_rejected() -> None:
    # Must satisfy the field floor (>=64) while still violating 8 * parallelism.
    with pytest.raises(ValidationError, match="parallelism"):
        make(argon2_memory_cost=64, argon2_parallelism=16)


@pytest.mark.parametrize("bad_name", ["", "has space%", "a,b", "x;y"])
def test_invalid_cookie_names_rejected(bad_name: str) -> None:
    with pytest.raises(ValidationError):
        make(session_cookie_name=bad_name)
    with pytest.raises(ValidationError):
        make(csrf_cookie_name=bad_name)


def test_ensure_directories_is_idempotent_and_non_destructive(tmp_path: Path) -> None:
    s = make(data_dir=tmp_path / "state")
    s.ensure_directories()
    marker = s.data_dir / "keep.txt"
    marker.write_text("user file")
    s.ensure_directories()
    assert marker.read_text() == "user file"
    assert s.resolved_log_dir.is_dir()


@pytest.mark.parametrize("template", [".env.example", "deploy/nas-manager.env.example"])
def test_env_templates_are_valid_and_use_known_settings(template: str, tmp_path: Path) -> None:
    """Shipped env templates must parse, validate and only name real settings."""
    from app.db.migrations import PROJECT_ROOT

    lines = (PROJECT_ROOT / template).read_text().splitlines()
    pairs = [line.split("=", 1) for line in lines if line and not line.startswith("#")]
    assert pairs
    for key, _ in pairs:
        assert key.startswith("NAS_")
        assert key.removeprefix("NAS_").lower() in Settings.model_fields, key
    # Loads and validates (data dir redirected so nothing is created in the repository).
    Settings(_env_file=PROJECT_ROOT / template, data_dir=tmp_path)


def test_production_env_template_does_not_override_unit_paths() -> None:
    from app.db.migrations import PROJECT_ROOT

    active = [
        line
        for line in (PROJECT_ROOT / "deploy/nas-manager.env.example").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    assert not any(line.startswith(("NAS_DATA_DIR", "NAS_LOG_DIR")) for line in active)
