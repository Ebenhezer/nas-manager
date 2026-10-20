from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.identity import GroupSummary, UserSummary


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=512)


class LoginResponse(BaseModel):
    """Successful login. ``csrf_token`` must be echoed back by the client on
    every state-changing request (header ``X-CSRF-Token`` or form field)."""

    user: UserSummary
    roles: list[str]
    permissions: list[str]
    csrf_token: str


class SessionUser(BaseModel):
    id: int
    username: str
    display_name: str


class MeResponse(BaseModel):
    user: SessionUser
    roles: list[str]
    groups: list[GroupSummary]
    permissions: list[str]
    session_expires_at: str | None = None
