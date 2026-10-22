from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class DatabaseHealth(BaseModel):
    reachable: bool
    schema_current: bool
    revision: str | None
    head_revision: str | None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "unhealthy"]
    version: str
    environment: str
    database: DatabaseHealth
