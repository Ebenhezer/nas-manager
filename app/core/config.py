"""Application configuration loaded from NAS_* environment variables."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_GIB = 1024**3

# systemd unit names the monitor may query: never an option (no leading
# "-"), no path separators, a known unit type suffix.
UNIT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._:-]{0,200}\.(service|socket|timer|mount)")


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

    # ------------------------------------------------------------------
    # Reverse proxy and transport (Phase 14). NAS_FORWARDED_ALLOW_IPS lists
    # the proxy addresses whose X-Forwarded-For/-Proto headers are trusted
    # (uvicorn's forwarded_allow_ips; "*" trusts everyone, never do that on
    # a reachable port). NAS_TRUSTED_HOSTS is a comma-separated list of
    # accepted Host header values ("*.example.org" allowed); empty disables
    # the check. NAS_HSTS_MAX_AGE_SECONDS > 0 sends Strict-Transport-Security
    # on HTTPS responses.
    # ------------------------------------------------------------------
    forwarded_allow_ips: str = "127.0.0.1"
    trusted_hosts: str = ""
    hsts_max_age_seconds: int = Field(default=0, ge=0, le=2 * 365 * 86400)

    data_dir: Path = Path("data")
    log_dir: Path | None = None
    database_url: str | None = None
    auto_migrate: bool = False

    # ------------------------------------------------------------------
    # Privileged helper (Phase 5). The web process never runs as root;
    # every privileged action (disk mount/umount, RAID scrub, SMART reads,
    # Samba config write + smbd reload) goes through this separate,
    # tightly restricted helper process over a Unix socket. These settings
    # only configure *access*: discovery/actions fail with a clear error
    # when NAS_HELPER_ENABLED is false or the socket is unreachable.
    # ------------------------------------------------------------------
    helper_enabled: bool = False
    helper_socket: Path | None = None
    helper_timeout_seconds: int = Field(default=10, ge=1, le=120)
    # Comma-separated numeric UIDs allowed to connect (root is always
    # allowed). The deploy template points this at the NAS manager service
    # account's UID; the helper additionally verifies identity via SO_PEERCRED.
    helper_allowed_uids: str = "0"
    # Socket group ownership (name). The helper chowns the socket to
    # root:<group> so the unprivileged service user can connect through it.
    helper_socket_group: str = "nas-manager"

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "text"] = "json"
    log_to_console: bool = True
    log_to_file: bool = True
    # Each process needs its own file: the sync worker (Phase 7) sets
    # NAS_LOG_FILE_NAME=worker.log so two processes never rotate one file.
    log_file_name: str = Field(
        default="nas-manager.log", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
    )
    log_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    log_backup_count: int = Field(default=5, ge=0, le=100)
    # Phase 13: "daily" rotates at midnight UTC and keeps log_backup_count
    # days; compression gzips rotated files.
    log_rotation: Literal["size", "daily"] = "size"
    log_compress: bool = False
    # Native journald output. "auto" switches it on when systemd connected
    # stdout to the journal (JOURNAL_STREAM) and then skips the console
    # handler, so lines are not written twice.
    log_journald: Literal["auto", "on", "off"] = "auto"
    log_journald_socket: Path = Path("/run/systemd/journal/socket")
    syslog_identifier: str = Field(
        default="nas-manager", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$"
    )

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

    # ------------------------------------------------------------------
    # File manager. NAS_FILE_ROOTS is the list of absolute directories the
    # file manager may access (the "jail": every requested path is resolved
    # against one of these and rejected if it escapes). When empty, the
    # default root <data_dir>/files is used.
    # ------------------------------------------------------------------
    file_roots: list[Path] = Field(default_factory=list)
    max_upload_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=1024 * 1024)
    file_search_limit: int = Field(default=200, ge=1, le=10000)

    # ------------------------------------------------------------------
    # Samba integration. NAS_SAMBA_CONFIG_PATH is where smb.conf is
    # rendered (default: the system path). The web process never runs as
    # root and must not gain privileges, so it validates with testparm and
    # backs up before writing; reloading smbd is a privileged operation
    # deferred to the Phase 5 helper (status reports reload_required).
    # ------------------------------------------------------------------
    samba_config_path: Path = Path("/etc/samba/smb.conf")
    samba_backup_dir: Path | None = None

    # ------------------------------------------------------------------
    # Public upload folder (Phase 4). A dedicated drop folder that clients
    # may write to without an account (upload-only: there is no public
    # listing or download) and with strict per-uploader quotas. Uploads are
    # keyed by the authenticated username when a session is present ("user:"),
    # otherwise by the client IP address ("ip:"). NAS_PUBLIC_UPLOAD_ENABLED
    # is the master switch; the Settings page can override it at runtime
    # (stored in app_settings) without editing configuration files.
    # ------------------------------------------------------------------
    public_upload_dir: Path | None = None
    public_upload_enabled: bool = True
    public_upload_max_file_bytes: int = Field(default=_GIB, ge=1024)
    public_upload_default_quota_bytes: int = Field(default=_GIB, ge=1)
    # Request rate limit per uploader on POST /public/uploads (Phase 14):
    # checked before the body is read, in-process sliding window. 0 disables.
    public_upload_rate_max_requests: int = Field(default=30, ge=0, le=100_000)
    public_upload_rate_window_seconds: int = Field(default=300, ge=10, le=86400)

    # ------------------------------------------------------------------
    # API tokens and mobile clients (Phase 6). Tokens are personal,
    # scoped bearer credentials (``Authorization: Bearer nas_...``).
    # NAS_API_TOKEN_MAX_LIFETIME_DAYS=0 allows tokens that never expire.
    # Failed bearer lookups are rate limited per client IP, like logins.
    # ------------------------------------------------------------------
    api_token_max_per_user: int = Field(default=20, ge=1, le=1000)
    api_token_default_lifetime_days: int = Field(default=90, ge=1, le=3650)
    api_token_max_lifetime_days: int = Field(default=365, ge=0, le=3650)
    api_token_upload_quota_bytes: int = Field(default=_GIB, ge=1)
    api_token_auth_max_failures: int = Field(default=20, ge=1, le=10000)
    api_token_auth_window_seconds: int = Field(default=300, ge=10, le=86400)

    # Resumable (chunked) uploads and the sync change journal.
    resumable_upload_ttl_hours: int = Field(default=24, ge=1, le=24 * 30)
    resumable_chunk_max_bytes: int = Field(default=64 * 1024 * 1024, ge=64 * 1024)
    resumable_max_open_per_user: int = Field(default=10, ge=1, le=1000)
    sync_change_retention_days: int = Field(default=30, ge=1, le=3650)

    # ------------------------------------------------------------------
    # Remote and cloud synchronization (Phase 7). Remote credentials are
    # encrypted at rest with a Fernet key kept in NAS_CREDENTIALS_KEY_FILE
    # (default <data_dir>/secrets/credentials.key, created 0600 on first
    # use). Back it up together with the database: without it the stored
    # credentials cannot be decrypted. Sync jobs run in the separate worker
    # process (python -m app.worker), never inside the web process.
    # NAS_PUBLIC_BASE_URL is the externally reachable URL of this
    # application (e.g. https://nas.example.org); it is only needed to
    # build the Google Drive OAuth redirect URI.
    # ------------------------------------------------------------------
    credentials_key_file: Path | None = None
    public_base_url: str | None = None
    remote_connect_timeout_seconds: int = Field(default=20, ge=1, le=300)
    remote_browse_limit: int = Field(default=500, ge=1, le=10000)
    remote_sync_poll_seconds: int = Field(default=15, ge=1, le=3600)
    worker_concurrency: int = Field(default=1, ge=1, le=8)
    remote_sync_lease_seconds: int = Field(default=300, ge=30, le=86400)
    # Deletion guard: a run that would archive more than this many files,
    # or more than this percentage of the files it last synced, fails
    # before touching anything.
    remote_sync_max_delete_count: int = Field(default=500, ge=0, le=1_000_000)
    remote_sync_max_delete_percent: int = Field(default=10, ge=0, le=100)
    remote_sync_run_retention_days: int = Field(default=90, ge=1, le=3650)
    # Maximum number of planned actions stored with a run (dry-run preview).
    remote_sync_preview_limit: int = Field(default=1000, ge=10, le=100_000)

    # ------------------------------------------------------------------
    # Photo gallery and personal media libraries (Phase 8). Every user's
    # personal library is <NAS_MEDIA_DIR>/<username>; administrators may
    # also register shared libraries over folders inside NAS_FILE_ROOTS.
    # NAS_MEDIA_CACHE_DIR holds generated thumbnails, posters and (Phase 9)
    # transcodes; it is disposable and rebuilt on demand. Images larger than
    # NAS_MEDIA_MAX_IMAGE_PIXELS are indexed but never decoded.
    # ------------------------------------------------------------------
    media_dir: Path | None = None
    media_cache_dir: Path | None = None
    media_max_image_pixels: int = Field(default=100_000_000, ge=1_000_000, le=2_000_000_000)
    media_scan_interval_minutes: int = Field(default=60, ge=0, le=7 * 24 * 60)
    media_thumbnail_batch: int = Field(default=50, ge=1, le=10_000)
    media_search_limit: int = Field(default=500, ge=10, le=10_000)
    # Largest ZIP the gallery builds for a multi-item download (Phase 15).
    media_zip_max_bytes: int = Field(default=4 * _GIB, ge=1024 * 1024)
    media_scan_lease_seconds: int = Field(default=600, ge=30, le=86400)

    # ------------------------------------------------------------------
    # Video playback and streaming (Phase 9). ffprobe extracts metadata and
    # ffmpeg renders poster frames and, optionally, browser-compatible MP4
    # transcodes (H.264/AAC) of videos a browser cannot play directly. When
    # the binaries are missing, videos are still indexed and downloadable;
    # only metadata, posters and transcoding are unavailable. Transcodes are
    # cached in NAS_MEDIA_CACHE_DIR/transcodes and evicted least recently
    # used above NAS_MEDIA_TRANSCODE_CACHE_MAX_BYTES.
    # ------------------------------------------------------------------
    ffmpeg_path: Path | None = None
    ffprobe_path: Path | None = None
    media_probe_timeout_seconds: int = Field(default=30, ge=1, le=600)
    media_transcode_enabled: bool = True
    media_transcode_max_height: int = Field(default=1080, ge=144, le=4320)
    media_transcode_crf: int = Field(default=23, ge=0, le=51)
    media_transcode_preset: Literal[
        "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"
    ] = "veryfast"
    media_transcode_threads: int = Field(default=2, ge=0, le=64)
    media_transcode_timeout_seconds: int = Field(default=6 * 3600, ge=60, le=7 * 86400)
    media_transcode_cache_max_bytes: int = Field(default=20 * _GIB, ge=0)
    media_transcode_lease_seconds: int = Field(default=600, ge=30, le=86400)

    # ------------------------------------------------------------------
    # System monitoring and alerting (Phase 10). The worker samples host
    # metrics every NAS_MONITOR_SAMPLE_SECONDS (kept NAS_MONITOR_HISTORY_DAYS),
    # checks the systemd units in NAS_MONITOR_SERVICES, RAID arrays and
    # (through the privileged helper) SMART health, evaluates the alert
    # rules and delivers notifications. The web process only reads.
    # ------------------------------------------------------------------
    monitor_enabled: bool = True
    monitor_sample_seconds: int = Field(default=60, ge=10, le=3600)
    monitor_check_seconds: int = Field(default=60, ge=10, le=3600)
    monitor_history_days: int = Field(default=7, ge=1, le=365)
    monitor_services: str = (
        "smbd.service,nmbd.service,nas-manager.service,"
        "nas-manager-worker.service,nas-manager-helper.service"
    )
    # 0 disables SMART polling (SMART reads need the helper).
    monitor_smart_interval_minutes: int = Field(default=30, ge=0, le=7 * 24 * 60)
    monitor_process_limit: int = Field(default=25, ge=1, le=500)
    systemctl_path: Path | None = None
    alert_retention_days: int = Field(default=90, ge=1, le=3650)
    alert_notify_max_attempts: int = Field(default=5, ge=1, le=50)
    alert_notify_timeout_seconds: int = Field(default=15, ge=1, le=300)

    # ------------------------------------------------------------------
    # Backup and restore (Phase 11). Backups are encrypted, deduplicated
    # snapshot repositories on a remote connection or in a directory under
    # NAS_BACKUP_LOCAL_ROOTS (external disks, second disks, NFS mounts; the
    # default is <data_dir>/backups). The worker runs every backup, verify,
    # prune and restore; NAS_BACKUP_STAGING_DIR holds temporary encrypted
    # blobs, database copies and staged system restores. NAS_BACKUP_SCRYPT_N
    # is the scrypt cost for *new* repositories and sealed config exports.
    # ------------------------------------------------------------------
    backup_local_roots: list[Path] = Field(default_factory=list)
    backup_staging_dir: Path | None = None
    backup_lease_seconds: int = Field(default=600, ge=30, le=86400)
    backup_run_retention_days: int = Field(default=180, ge=1, le=3650)
    backup_scrypt_n: int = Field(default=2**15, ge=2**10, le=2**20)
    backup_browse_limit: int = Field(default=2000, ge=10, le=100_000)

    # ------------------------------------------------------------------
    # Auditing and remote syslog (Phase 13). The worker seals audit events
    # into a SHA-256 hash chain, forwards them to the syslog destinations
    # configured on the Logging page and prunes events older than
    # NAS_AUDIT_RETENTION_DAYS (0 keeps them forever) once they are sealed
    # and forwarded. Application log records are forwarded best effort
    # through a bounded in-memory queue of NAS_SYSLOG_QUEUE_SIZE records.
    # ------------------------------------------------------------------
    audit_retention_days: int = Field(default=90, ge=0, le=36500)
    audit_export_max_rows: int = Field(default=100_000, ge=100, le=10_000_000)
    syslog_batch_size: int = Field(default=500, ge=1, le=10_000)
    syslog_timeout_seconds: int = Field(default=5, ge=1, le=120)
    syslog_queue_size: int = Field(default=10_000, ge=100, le=1_000_000)

    @field_validator(
        "data_dir",
        "log_dir",
        "samba_config_path",
        "samba_backup_dir",
        "public_upload_dir",
        "helper_socket",
        "credentials_key_file",
        "media_dir",
        "media_cache_dir",
        "ffmpeg_path",
        "ffprobe_path",
        "systemctl_path",
        "backup_staging_dir",
    )
    @classmethod
    def _absolute(cls, value: Path | None) -> Path | None:
        return None if value is None else value.expanduser().resolve()

    @field_validator("file_roots", "backup_local_roots")
    @classmethod
    def _file_roots_absolute(cls, value: list[Path]) -> list[Path]:
        resolved: list[Path] = []
        for item in value:
            absolute = item.expanduser().resolve()
            if not absolute.is_absolute():
                raise ValueError(f"file root must be an absolute path: {item}")
            resolved.append(absolute)
        return resolved

    @field_validator("backup_scrypt_n")
    @classmethod
    def _power_of_two(cls, value: int) -> int:
        if value & (value - 1):
            raise ValueError("NAS_BACKUP_SCRYPT_N must be a power of two")
        return value

    @field_validator("session_cookie_name", "csrf_cookie_name")
    @classmethod
    def _cookie_names(cls, value: str) -> str:
        value = value.strip()
        if not value or not all(c.isalnum() or c in "._- " for c in value):
            raise ValueError("cookie name must be a simple ASCII token")
        return value

    @field_validator("public_base_url")
    @classmethod
    def _base_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip().rstrip("/")
        if not value.startswith(("https://", "http://")) or any(c in value for c in "?#"):
            raise ValueError("NAS_PUBLIC_BASE_URL must be an http(s) URL without query/fragment")
        return value

    @field_validator("forwarded_allow_ips", "trusted_hosts")
    @classmethod
    def _host_lists(cls, value: str) -> str:
        items = [part.strip() for part in value.split(",") if part.strip()]
        for item in items:
            if not re.fullmatch(r"[A-Za-z0-9*.:/\[\]_-]{1,253}", item):
                raise ValueError(f"invalid host or address: {item!r}")
        return ",".join(dict.fromkeys(items))

    @field_validator("monitor_services")
    @classmethod
    def _monitor_services(cls, value: str) -> str:
        units = [part.strip() for part in value.split(",") if part.strip()]
        for unit in units:
            if not UNIT_NAME_RE.fullmatch(unit):
                raise ValueError(f"NAS_MONITOR_SERVICES: invalid systemd unit name: {unit!r}")
        if len(units) > 64:
            raise ValueError("NAS_MONITOR_SERVICES: at most 64 units")
        return ",".join(dict.fromkeys(units))

    @model_validator(mode="after")
    def _check_cookies_and_argon2(self) -> Self:
        if self.session_cookie_samesite == "none" and not self.session_cookie_secure:
            raise ValueError("SameSite=None requires NAS_SESSION_COOKIE_SECURE=true")
        if self.session_idle_max_seconds < self.session_ttl_seconds:
            raise ValueError("NAS_SESSION_IDLE_MAX_SECONDS may not be shorter than the TTL")
        if (
            self.api_token_max_lifetime_days
            and self.api_token_default_lifetime_days > self.api_token_max_lifetime_days
        ):
            raise ValueError(
                "NAS_API_TOKEN_DEFAULT_LIFETIME_DAYS may not exceed NAS_API_TOKEN_MAX_LIFETIME_DAYS"
            )
        if self.argon2_memory_cost < 8 * self.argon2_parallelism:
            raise ValueError("NAS_ARGON2_MEMORY_COST must be at least 8 * NAS_ARGON2_PARALLELISM")
        return self

    @property
    def resolved_log_dir(self) -> Path:
        return self.log_dir if self.log_dir is not None else self.data_dir / "logs"

    @property
    def resolved_file_roots(self) -> list[Path]:
        """The file manager's jail roots; the default root when none are set."""
        if self.file_roots:
            return list(self.file_roots)
        return [self.data_dir / "files"]

    @property
    def resolved_samba_backup_dir(self) -> Path:
        return (
            self.samba_backup_dir
            if self.samba_backup_dir is not None
            else self.data_dir / "samba-backups"
        )

    @property
    def resolved_public_upload_dir(self) -> Path:
        """Where the public drop folder lives on disk."""
        return (
            self.public_upload_dir
            if self.public_upload_dir is not None
            else self.data_dir / "uploads"
        )

    @property
    def resolved_credentials_key_file(self) -> Path:
        """Fernet key protecting remote credentials at rest."""
        if self.credentials_key_file is not None:
            return self.credentials_key_file
        return self.data_dir / "secrets" / "credentials.key"

    @property
    def resolved_media_dir(self) -> Path:
        """Parent directory of every user's personal media library."""
        return self.media_dir if self.media_dir is not None else self.data_dir / "media"

    @property
    def resolved_media_cache_dir(self) -> Path:
        """Generated thumbnails, posters and transcodes (disposable)."""
        if self.media_cache_dir is not None:
            return self.media_cache_dir
        return self.data_dir / "media-cache"

    @property
    def resolved_backup_local_roots(self) -> list[Path]:
        """Directories that may hold local (external storage) repositories."""
        if self.backup_local_roots:
            return list(self.backup_local_roots)
        return [self.data_dir / "backups"]

    @property
    def resolved_backup_staging_dir(self) -> Path:
        """Temporary encrypted blobs, database copies and staged restores."""
        if self.backup_staging_dir is not None:
            return self.backup_staging_dir
        return self.data_dir / "backup-staging"

    @property
    def resolved_helper_socket(self) -> Path:
        """Unix socket the privileged helper listens on."""
        if self.helper_socket is not None:
            return self.helper_socket
        return Path("/run/nas-manager/helper.sock")

    @property
    def helper_allowed_uid_set(self) -> frozenset[int]:
        """Numeric UIDs permitted to talk to the helper (always includes root)."""
        uids: set[int] = {0}
        for part in self.helper_allowed_uids.split(","):
            token = part.strip()
            if token and token.isdigit() and int(token) >= 0:
                uids.add(int(token))
        return frozenset(uids)

    @property
    def trusted_host_list(self) -> list[str]:
        return [host for host in self.trusted_hosts.split(",") if host]

    @property
    def monitor_service_list(self) -> list[str]:
        return [unit for unit in self.monitor_services.split(",") if unit]

    @property
    def resolved_database_url(self) -> str:
        if self.database_url is not None:
            return self.database_url
        return f"sqlite:///{self.data_dir / 'nas.db'}"

    def ensure_directories(self) -> None:
        """Create state directories (never removes or overwrites anything)."""
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.resolved_log_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        if not self.file_roots:
            # The default managed file root; explicitly configured roots are
            # the operator's responsibility and are never auto-created.
            (self.data_dir / "files").mkdir(parents=True, exist_ok=True, mode=0o750)
        self.resolved_public_upload_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.resolved_credentials_key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.resolved_media_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.resolved_media_cache_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        if not self.backup_local_roots:
            (self.data_dir / "backups").mkdir(parents=True, exist_ok=True, mode=0o750)
        self.resolved_backup_staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)


def tighten_umask() -> int:
    """Never create group-writable or world-accessible files (Phase 14).

    The systemd units already set ``UMask=0027``; this keeps the database,
    logs and caches private when a process is started some other way. A
    stricter umask is kept as it is. Returns the umask now in effect.
    """
    current = os.umask(0o077)
    os.umask(current | 0o027)
    return current | 0o027


@lru_cache
def get_settings() -> Settings:
    return Settings()
