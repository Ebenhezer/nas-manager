"""API contracts for runtime (database-backed) settings (Phase 3)."""

from __future__ import annotations

from pydantic import BaseModel


class RuntimeSettings(BaseModel):
    """The runtime policy switches an admin may flip."""

    operator_view_files_samba: bool


class RuntimeSettingsUpdate(BaseModel):
    operator_view_files_samba: bool
