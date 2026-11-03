from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, computed_field


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
    chain_hash: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sealed(self) -> bool:
        return self.chain_hash is not None


class AuditPage(BaseModel):
    events: list[AuditEventOut]
    total: int
    limit: int
    offset: int


class AuditFacets(BaseModel):
    categories: list[str]
    events: list[str]


class AuditVerifyResult(BaseModel):
    ok: bool
    checked: int
    sealed_up_to: int | None = None
    unsealed: int
    first_break_id: int | None = None
    reason: str | None = None
    verified_at: str | None = None


class AuditIntegrity(BaseModel):
    anchor_id: int
    sealed_up_to: int | None = None
    unsealed: int
    retention_days: int
    expired: int
    prunable_up_to: int | None = None
    prune_held_back_by_forwarding: bool
    prune_held_back_by_sealing: bool
    last_verify: AuditVerifyResult | None = None
