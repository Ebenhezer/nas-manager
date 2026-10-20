from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    actor_user_id: int | None = None
    actor_username: str
    event: str
    category: str
    outcome: str
    target_type: str | None = None
    target_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    request_id: str | None = None
    details: dict[str, Any] | None = None


class AuditPage(BaseModel):
    events: list[AuditEventOut]
    total: int
    limit: int
    offset: int
