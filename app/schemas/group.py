from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.identity import GroupSummary, RoleSummary, UserSummary


class GroupDetail(GroupSummary):
    roles: list[RoleSummary]
    members: list[UserSummary]


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=4000)


class GroupUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=4000)


class GroupMembersUpdate(BaseModel):
    user_ids: list[int]


class GroupRolesUpdate(BaseModel):
    roles: list[str]
