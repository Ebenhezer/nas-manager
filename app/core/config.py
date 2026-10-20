"""Application configuration loaded from NAS_* environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed settings. Nothing here is environment-specific by default."""

    model_config = SettingsConfigDict(
        env_prefix="NAS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "NAS Manager"
    environment: Literal["development", "production", "test"] = "production"
    debug: bool = False

    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)

    data_dir: Path = Path("data")
    log_dir: Path | None = None
    database_url: str | None = None
    auto_migrate: bool = False

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "text"] = "json"
    log_to_console: bool = True
    log_to_file: bool = True
    log_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    log_backup_count: int = Field(default=5, ge=0, le=100)

    # ------------------------------------------------------------------
    # Sessions and CSRF. Only NAS_SESSION_COOKIE_SECURE is a security
    # choice the operator must make explicitly: true when TLS terminates
    # in front of the application (recommended production value).
    # ------------------------------------------------------------------
    session_cookie_name: str = "nas_session"
    session_cookie_secure: bool = False
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    session_ttl_seconds: int = Field(default=12 * 3600, ge=60, le=30 * 86400)
    # Maximum age of an *idle* session: it replaces the TTL when longer.
    session_idle_max_seconds: int = Field(default=7 * 86400, ge=60, le=365 * 86400)
    csrf_cookie_name: str = "nas_csrf"
    csrf_max_age_seconds: int = Field(default=12 * 3600, ge=300, le=30 * 86400)

    # Login throttling: per-account lockout (persistent) and per-IP rate
    # limiting (in-process sliding window, see app/services/throttle.py).
    login_lockout_threshold: int = Field(default=5, ge=1, le=100)
    login_lockout_minutes: int = Field(default=15, ge=1, le=1440)
    login_ip_max_attempts: int = Field(default=20, ge=1, le=10000)
    login_ip_window_seconds: int = Field(default=300, ge=10, le=86400)

    # Password policy and Argon2id cost. Costs follow RFC 9106 low-memory
    # guidance; tests override them (see tests/conftest.py) so hashing stays
    # fast without changing production behaviour.
    password_min_length: int = Field(default=12, ge=8, le=128)
    password_max_length: int = Field(default=128, ge=16, le=512)
    argon2_time_cost: int = Field(default=3, ge=1, le=100)
    argon2_memory_cost: int = Field(default=65536, ge=64, le=1 << 30)
    argon2_parallelism: int = Field(default=4, ge=1, le=256)

    # Name used by `python -m app.cli bootstrap-admin` when --username is
    # not given. Not a secret.
    admin_username: str = "admin"

    @field_validator("data_dir", "log_dir")
    @classmethod
    def _absolute(cls, value: Path | None) -> Path | None:
        return None if value is None else value.expanduser().resolve()

    @field_validator("session_cookie_name", "csrf_cookie_name")
    @classmethod
    def _cookie_names(cls, value: str) -> str:
        value = value.strip()
        if not value or not all(c.isalnum() or c in "._- " for c in value):
            raise ValueError("cookie name must be a simple ASCII token")
        return value

    @model_validator(mode="after")
    def _check_cookies_and_argon2(self) -> Self:
        if self.session_cookie_samesite == "none" and not self.session_cookie_secure:
            raise ValueError("SameSite=None requires NAS_SESSION_COOKIE_SECURE=true")
        if self.session_idle_max_seconds < self.session_ttl_seconds:
            raise ValueError("NAS_SESSION_IDLE_MAX_SECONDS may not be shorter than the TTL")
        if self.argon2_memory_cost < 8 * self.argon2_parallelism:
            raise ValueError("NAS_ARGON2_MEMORY_COST must be at least 8 * NAS_ARGON2_PARALLELISM")
        return self

    @property
    def resolved_log_dir(self) -> Path:
        return self.log_dir if self.log_dir is not None else self.data_dir / "logs"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url is not None:
            return self.database_url
        return f"sqlite:///{self.data_dir / 'nas.db'}"

    def ensure_directories(self) -> None:
        """Create state directories (never removes or overwrites anything)."""
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.resolved_log_dir.mkdir(parents=True, exist_ok=True, mode=0o750)


@lru_cache
def get_settings() -> Settings:
    return Settings()
