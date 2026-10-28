"""API contracts for disk/storage/RAID management (Phase 5)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DiskPartition(BaseModel):
    name: str
    path: str
    size_bytes: int = Field(ge=0)
    fs_type: str | None = None
    label: str | None = None
    partlabel: str | None = None
    uuid: str | None = None
    mountpoints: list[str] = []


class PhysicalDisk(BaseModel):
    name: str
    path: str
    size_bytes: int = Field(ge=0)
    rotational: bool | None = None
    transport: str | None = None
    model: str | None = None
    serial: str | None = None
    wwn: str | None = None
    mountpoints: list[str] = []
    partitions: list[DiskPartition] = []


class SmartAttribute(BaseModel):
    id: int | None = None
    name: str | None = None
    value: int | None = None
    worst: int | None = None
    threshold: int | None = None
    raw: str | None = None


class SmartData(BaseModel):
    device: str
    available: bool = False
    error: str | None = None
    health: str | None = None
    model: str | None = None
    serial: str | None = None
    firmware: str | None = None
    interface: str | None = None
    capacity_bytes: int | None = None
    temperature: int | None = None
    attributes: list[SmartAttribute] = []
    messages: list[str] = []


class Volume(BaseModel):
    target: str
    source: str
    fs_type: str | None = None
    options: list[str] = []
    used_bytes: int | None = None
    avail_bytes: int | None = None
    size_bytes: int | None = None


class RaidMember(BaseModel):
    device: str
    state: str


class RaidArray(BaseModel):
    name: str
    path: str
    level: str | None = None
    state: str | None = None
    size_bytes: int | None = None
    active_disks: int | None = None
    total_disks: int | None = None
    sync_action: str | None = None
    degraded: bool = False
    members: list[RaidMember] = []


class RaidList(BaseModel):
    arrays: list[RaidArray] = []
    detail_scan: str = ""


class HelperStatus(BaseModel):
    enabled: bool
    available: bool
    socket: str
    error: str | None = None


class MountRequest(BaseModel):
    mountpoint: str = Field(min_length=1, max_length=1024)
    fstype: str = "ext4"
    options: str = "defaults"
    confirm: bool = False
    intent: str = Field(default="", max_length=128)


class MountPreviewRequest(BaseModel):
    mountpoint: str = Field(min_length=1, max_length=1024)
    fstype: str = "ext4"
    options: str = "defaults"


class UnmountRequest(BaseModel):
    mountpoint: str | None = Field(default=None, min_length=1, max_length=1024)
    confirm: bool = False
    intent: str = Field(default="", max_length=128)


class ScrubRequest(BaseModel):
    confirm: bool = False
    intent: str = Field(default="", max_length=128)


class MountPreview(BaseModel):
    action: str
    device: str
    mountpoint: str
    fstype: str
    options: str
    intent: str
    warnings: list[str] = []


class UnmountPreview(BaseModel):
    action: str
    device: str
    mountpoint: str
    intent: str
    warnings: list[str] = []


class ScrubPreview(BaseModel):
    action: str
    name: str
    sync_action: str
    intent: str
    warnings: list[str] = []


class OperationResult(BaseModel):
    ok: bool = True
    message: str
    details: dict[str, object] | None = None
