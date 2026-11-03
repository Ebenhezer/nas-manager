"""Provider catalogue: field specs, input validation and the factory.

The specs are served to the UI (``GET /remotes/providers``) so the
connection form is generated from the same definitions the API validates
against. Fields marked ``secret`` are never stored in ``config`` and never
returned by the API; they go into the encrypted credentials blob.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit

from app.core.config import Settings
from app.core.errors import BadRequestError
from app.models.remote import (
    PROVIDER_FTP,
    PROVIDER_FTPS,
    PROVIDER_GDRIVE,
    PROVIDER_S3,
    PROVIDER_SFTP,
    PROVIDER_WEBDAV,
)
from app.services.remote.base import RemoteProvider, normalize_remote_path

_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION_RE = re.compile(r"^[a-z0-9-]{1,32}$")
_SIMPLE_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    kind: str = "text"  # text | int | bool | secret | textarea | choice
    required: bool = False
    default: Any = None
    help: str = ""
    choices: list[str] = field(default_factory=list)
    # Secret fields only the server sets (e.g. the OAuth refresh token).
    managed: bool = False

    @property
    def secret(self) -> bool:
        return self.kind == "secret"


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    label: str
    fields: list[FieldSpec]
    oauth: bool = False


_INSECURE = FieldSpec(
    "allow_insecure",
    "Allow unencrypted connection",
    "bool",
    default=False,
    help="Send credentials and data without TLS. Only for trusted local networks.",
)

PROVIDER_SPECS: dict[str, ProviderSpec] = {
    PROVIDER_SFTP: ProviderSpec(
        PROVIDER_SFTP,
        "SFTP (SSH)",
        [
            FieldSpec("host", "Host", required=True),
            FieldSpec("port", "Port", "int", default=22),
            FieldSpec("username", "Username", required=True),
            FieldSpec("base_path", "Base path", help="Remote directory all paths are relative to."),
            FieldSpec(
                "host_key",
                "Pinned host key",
                required=True,
                help="'<type> <base64>'; use 'Fetch host key' and verify the fingerprint.",
            ),
            FieldSpec("password", "Password", "secret"),
            FieldSpec("private_key", "Private key (OpenSSH/PEM)", "secret"),
            FieldSpec("passphrase", "Key passphrase", "secret"),
        ],
    ),
    PROVIDER_FTPS: ProviderSpec(
        PROVIDER_FTPS,
        "FTPS (explicit TLS)",
        [
            FieldSpec("host", "Host", required=True),
            FieldSpec("port", "Port", "int", default=21),
            FieldSpec("username", "Username", required=True),
            FieldSpec("base_path", "Base path"),
            FieldSpec("passive", "Passive mode", "bool", default=True),
            FieldSpec("password", "Password", "secret"),
        ],
    ),
    PROVIDER_FTP: ProviderSpec(
        PROVIDER_FTP,
        "FTP (unencrypted)",
        [
            FieldSpec("host", "Host", required=True),
            FieldSpec("port", "Port", "int", default=21),
            FieldSpec("username", "Username", required=True),
            FieldSpec("base_path", "Base path"),
            FieldSpec("passive", "Passive mode", "bool", default=True),
            _INSECURE,
            FieldSpec("password", "Password", "secret"),
        ],
    ),
    PROVIDER_WEBDAV: ProviderSpec(
        PROVIDER_WEBDAV,
        "WebDAV",
        [
            FieldSpec(
                "url",
                "Collection URL",
                required=True,
                help="e.g. https://cloud.example.org/remote.php/dav/files/me/",
            ),
            FieldSpec("username", "Username"),
            _INSECURE,
            FieldSpec("password", "Password / app password", "secret"),
        ],
    ),
    PROVIDER_S3: ProviderSpec(
        PROVIDER_S3,
        "S3-compatible storage",
        [
            FieldSpec("bucket", "Bucket", required=True),
            FieldSpec("prefix", "Key prefix", help="Optional folder inside the bucket."),
            FieldSpec("region", "Region", help="e.g. eu-west-1 (leave empty for the default)."),
            FieldSpec(
                "endpoint_url",
                "Endpoint URL",
                help="Only for non-AWS services, e.g. https://minio.example.org:9000",
            ),
            FieldSpec(
                "addressing_style",
                "Addressing style",
                "choice",
                default="auto",
                choices=["auto", "path", "virtual"],
            ),
            FieldSpec("access_key_id", "Access key ID"),
            _INSECURE,
            FieldSpec("secret_access_key", "Secret access key", "secret"),
        ],
    ),
    PROVIDER_GDRIVE: ProviderSpec(
        PROVIDER_GDRIVE,
        "Google Drive",
        [
            FieldSpec("client_id", "OAuth client ID", required=True),
            FieldSpec(
                "folder_id",
                "Folder ID",
                default="root",
                help="Drive folder all paths are relative to ('root' = My Drive).",
            ),
            FieldSpec("client_secret", "OAuth client secret", "secret"),
            FieldSpec("refresh_token", "Refresh token", "secret", managed=True),
        ],
        oauth=True,
    ),
}


def provider_spec(provider: str) -> ProviderSpec:
    spec = PROVIDER_SPECS.get(provider)
    if spec is None:
        raise BadRequestError(
            "Unknown provider", details={"provider": provider, "known": sorted(PROVIDER_SPECS)}
        )
    return spec


def specs_payload() -> list[dict[str, Any]]:
    payload = []
    for spec in PROVIDER_SPECS.values():
        item = asdict(spec)
        item["fields"] = [{**asdict(f), "secret": f.secret} for f in spec.fields if not f.managed]
        payload.append(item)
    return payload


def secret_names(provider: str) -> list[str]:
    return [f.name for f in provider_spec(provider).fields if f.secret]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _host(value: str) -> str:
    value = value.strip()
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if not _HOST_RE.match(value):
        raise BadRequestError("Invalid host name", details={"host": value})
    return value


def validate_host_port(host: str, port: int) -> tuple[str, int]:
    return _host(host), _coerce(FieldSpec("port", "Port", "int"), port)


def _url(value: str, *, allow_insecure: bool, label: str) -> str:
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise BadRequestError(f"{label} must be an http(s) URL")
    if parts.username or parts.password:
        raise BadRequestError(f"{label} must not embed credentials; use the credential fields")
    if parts.query or parts.fragment:
        raise BadRequestError(f"{label} must not contain a query or fragment")
    if parts.scheme == "http" and not allow_insecure:
        raise BadRequestError(
            f"{label} uses plain HTTP; enable 'Allow unencrypted connection' to accept that"
        )
    return value


def _coerce(spec: FieldSpec, value: Any) -> Any:
    if spec.kind == "int":
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise BadRequestError(f"{spec.name} must be an integer") from exc
        if not 1 <= number <= 65535:
            raise BadRequestError(f"{spec.name} must be a port number (1-65535)")
        return number
    if spec.kind == "bool":
        if isinstance(value, bool):
            return value
        raise BadRequestError(f"{spec.name} must be true or false")
    if not isinstance(value, str):
        raise BadRequestError(f"{spec.name} must be a string")
    if spec.kind == "choice" and value not in spec.choices:
        raise BadRequestError(f"{spec.name} must be one of {', '.join(spec.choices)}")
    if spec.kind in {"text", "choice"} and value and not _SIMPLE_RE.match(value):
        raise BadRequestError(f"{spec.name} must be a single line of at most 256 characters")
    if spec.secret and len(value) > 16384:
        raise BadRequestError(f"{spec.name} is too long")
    return value


def validate_config(
    provider: str,
    config: dict[str, Any],
    secrets_in: dict[str, str | None],
    existing_secrets: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate a connection definition; returns ``(config, secrets)``.

    ``secrets_in`` follows PATCH semantics: a missing key keeps the stored
    value, ``None`` or ``""`` clears it, any other string replaces it.
    """
    spec = provider_spec(provider)
    known = {f.name: f for f in spec.fields}
    unknown = sorted((set(config) | set(secrets_in)) - set(known))
    if unknown:
        raise BadRequestError("Unknown connection fields", details={"fields": unknown})

    clean: dict[str, Any] = {}
    secrets: dict[str, Any] = {
        k: v for k, v in existing_secrets.items() if k in known and known[k].secret
    }
    for f in spec.fields:
        if f.secret:
            if f.name in config:
                raise BadRequestError(f"{f.name} is a secret; send it in 'secrets'")
            if f.name in secrets_in:
                if f.managed:
                    raise BadRequestError(f"{f.name} is set by the server")
                value = secrets_in[f.name]
                if value is None or value == "":
                    secrets.pop(f.name, None)
                else:
                    secrets[f.name] = _coerce(f, value)
            continue
        if f.name in secrets_in:
            raise BadRequestError(f"{f.name} is not a secret; send it in 'config'")
        value = config.get(f.name, f.default)
        if value is None or value == "":
            if f.required:
                raise BadRequestError(f"{f.name} is required")
            if f.default is not None:
                clean[f.name] = f.default
            continue
        clean[f.name] = _coerce(f, value)

    allow_insecure = bool(clean.get("allow_insecure", False))
    if "host" in clean:
        clean["host"] = _host(clean["host"])
    if "base_path" in clean:
        clean["base_path"] = _base_path(clean["base_path"], provider)
    if provider == PROVIDER_SFTP:
        from app.services.remote.sftp import load_private_key, parse_host_key

        parse_host_key(clean["host_key"])
        if not secrets.get("password") and not secrets.get("private_key"):
            raise BadRequestError("SFTP needs a password or a private key")
        if secrets.get("private_key"):
            load_private_key(secrets["private_key"], secrets.get("passphrase"))
    elif provider == PROVIDER_FTP and not allow_insecure:
        raise BadRequestError(
            "Plain FTP sends the password unencrypted; use FTPS, or enable"
            " 'Allow unencrypted connection'"
        )
    elif provider == PROVIDER_WEBDAV:
        clean["url"] = _url(clean["url"], allow_insecure=allow_insecure, label="url")
    elif provider == PROVIDER_S3:
        if not _BUCKET_RE.match(clean["bucket"]):
            raise BadRequestError("Invalid bucket name")
        if "prefix" in clean:
            clean["prefix"] = normalize_remote_path(clean["prefix"])
        if clean.get("region") and not _REGION_RE.match(clean["region"]):
            raise BadRequestError("Invalid region")
        if clean.get("endpoint_url"):
            clean["endpoint_url"] = _url(
                clean["endpoint_url"], allow_insecure=allow_insecure, label="endpoint_url"
            )
        if bool(clean.get("access_key_id")) != bool(secrets.get("secret_access_key")):
            raise BadRequestError("Set both access_key_id and secret_access_key, or neither")
    elif provider == PROVIDER_GDRIVE and not secrets.get("client_secret"):
        raise BadRequestError("client_secret is required")
    return clean, secrets


def _base_path(value: str, provider: str) -> str:
    """Base paths may be absolute on SFTP/FTP servers (``/srv/backup``)."""
    absolute = value.strip().startswith("/")
    normalized = normalize_remote_path(value)
    if absolute and provider in {PROVIDER_SFTP, PROVIDER_FTP, PROVIDER_FTPS}:
        return "/" + normalized
    return normalized


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def open_provider(
    provider: str, config: dict[str, Any], secrets: dict[str, Any], settings: Settings
) -> RemoteProvider:
    """Build (not yet connect) a provider for a stored connection."""
    timeout = settings.remote_connect_timeout_seconds
    if provider == PROVIDER_SFTP:
        from app.services.remote.sftp import SftpProvider

        return SftpProvider(config, secrets, timeout=timeout)
    if provider in (PROVIDER_FTP, PROVIDER_FTPS):
        from app.services.remote.ftp import FtpProvider

        return FtpProvider(config, secrets, timeout=timeout, tls=provider == PROVIDER_FTPS)
    if provider == PROVIDER_WEBDAV:
        from app.services.remote.webdav import WebDavProvider

        return WebDavProvider(config, secrets, timeout=timeout)
    if provider == PROVIDER_S3:
        from app.services.remote.s3 import S3Provider

        return S3Provider(config, secrets, timeout=timeout)
    if provider == PROVIDER_GDRIVE:
        from app.services.remote.gdrive import GoogleDriveProvider

        return GoogleDriveProvider(config, secrets, timeout=timeout)
    raise BadRequestError("Unknown provider", details={"provider": provider})
