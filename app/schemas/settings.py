"""API contracts for runtime (database-backed) settings (Phase 3/4)."""

from __future__ import annotations

from pydantic import BaseModel


class RuntimeSettings(BaseModel):
    """The runtime policy switches an admin may flip."""

    operator_view_files_samba: bool
    public_upload_enabled: bool


class RuntimeSettingsUpdate(BaseModel):
    """Partial update: ``None`` fields are left unchanged."""

    operator_view_files_samba: bool | None = None
    public_upload_enabled: bool | None = None
