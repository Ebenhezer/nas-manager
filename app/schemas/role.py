from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.schemas.identity import RoleSummary


class RoleDetail(RoleSummary):
    permissions: list[str]

    @field_validator("permissions", mode="before")
    @classmethod
    def _flatten_permissions(cls, value: object) -> list[str]:
        if isinstance(value, list) and value and hasattr(value[0], "permission"):
            return [item.permission for item in value]
        if isinstance(value, list):
            return [str(item) for item in value]
        raise ValueError("permissions must be a list of strings")


class RoleCreate(BaseModel):
    name: str = Field(min_length=2, max_length=32)
    description: str | None = Field(default=None, max_length=4000)
    permissions: list[str] = Field(default_factory=list)


class RoleDescriptionUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=4000)


class RolePermissionsUpdate(BaseModel):
    permissions: list[str] = Field(default_factory=list)
