"""API contracts for personal API tokens (Phase 6)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ApiTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    scopes: list[str] = Field(min_length=1, max_length=64)
    # None: the configured default lifetime. 0: never expires (only when the
    # configured maximum lifetime is 0).
    expires_in_days: int | None = Field(default=None, ge=0, le=3650)


class ApiTokenOut(BaseModel):
    """Token metadata. The secret itself is never part of this shape."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    token_prefix: str
    scopes: list[str]
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    last_used_ip: str | None
    revoked_at: datetime | None
    active: bool


class ApiTokenAdminOut(ApiTokenOut):
    username: str


class ApiTokenCreated(ApiTokenOut):
    """Returned once by ``POST /tokens``: ``token`` is the only copy."""

    token: str


class GrantableScopes(BaseModel):
    scopes: list[str]
    default_lifetime_days: int
    max_lifetime_days: int
    max_per_user: int
