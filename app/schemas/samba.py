"""API contracts for Samba share management (Phase 3)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SambaShareCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=1024)
    comment: str = Field(default="", max_length=4000)
    read_only: bool = False
    browseable: bool = True
    guest_ok: bool = False
    valid_users: str = Field(default="", max_length=4000)
    write_list: str = Field(default="", max_length=4000)
    enabled: bool = True


class SambaShareUpdate(BaseModel):
    path: str | None = Field(default=None, min_length=1, max_length=1024)
    comment: str | None = Field(default=None, max_length=4000)
    read_only: bool | None = None
    browseable: bool | None = None
    guest_ok: bool | None = None
    valid_users: str | None = Field(default=None, max_length=4000)
    write_list: str | None = Field(default=None, max_length=4000)
    enabled: bool | None = None


class SambaShareDetail(SambaShareCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


class SambaStatus(BaseModel):
    config_path: str
    config_exists: bool
    managed_markers_present: bool
    managed_share_count: int
    backup_dir: str
    pending_changes: bool
    reload_required: bool
    message: str


class SambaApplyResult(BaseModel):
    config_path: str
    backup_path: str | None
    reload_required: bool
    message: str
