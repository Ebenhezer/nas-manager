"""Configuration export/import API schemas (Phase 12)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConfigExportRequest(BaseModel):
    # Without a passphrase the export omits every secret (password hashes,
    # credentials, channel secrets, backup passphrases).
    passphrase: str | None = Field(default=None, min_length=12, max_length=1024)


class ConfigImportRequest(BaseModel):
    document: dict[str, Any]
    passphrase: str | None = Field(default=None, max_length=1024)
    mode: str = "merge"
    source_name: str = Field(default="", max_length=255)
    confirm: bool = False


class ConfigImportPlan(BaseModel):
    sections: dict[str, Any]
    warnings: list[str]
    errors: list[dict[str, str]]
    samba_apply_required: bool
    secrets_included: bool = False


class ConfigImportRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    mode: str
    source_name: str
    exported_at: str | None
    sections: dict[str, Any]
    warnings: list[str]
    database_copy: str | None
    actor_username: str
    created_at: datetime
    rolled_back_at: datetime | None
    rolled_back_by: str | None


class ConfigImportResult(BaseModel):
    run: ConfigImportRunOut
    plan: ConfigImportPlan


class ConfigRollbackRequest(BaseModel):
    confirm: bool = False
