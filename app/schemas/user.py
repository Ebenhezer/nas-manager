from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.identity import GroupSummary, RoleSummary, UserSummary


class UserDetail(UserSummary):
    email: str | None = None
    is_active: bool
    is_locked: bool
    last_login_at: datetime | None = None
    created_at: datetime | None = None
    roles: list[RoleSummary]
    groups: list[GroupSummary]


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)
    display_name: str | None = Field(default=None, max_length=128)
    email: str | None = Field(default=None, max_length=255)
    roles: list[str] = Field(default_factory=list)
    groups: list[int] = Field(default_factory=list)


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=128)
    email: str | None = Field(default=None, max_length=255)


class UserActiveUpdate(BaseModel):
    active: bool


class UserRolesUpdate(BaseModel):
    roles: list[str] = Field(default_factory=list)


class PasswordResetRequest(BaseModel):
    new_password: str = Field(min_length=1, max_length=512)
