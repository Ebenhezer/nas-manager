from __future__ import annotations

from pydantic import BaseModel, Field


class HostInfo(BaseModel):
    hostname: str
    os: str
    kernel: str
    uptime_seconds: float = Field(ge=0)


class CpuInfo(BaseModel):
    percent: float = Field(ge=0)
    logical_cores: int | None
    load_average: list[float] | None


class MemoryInfo(BaseModel):
    total: int
    used: int
    available: int
    percent: float
    swap_total: int
    swap_used: int
    swap_percent: float


class DiskUsageInfo(BaseModel):
    path: str
    total: int
    used: int
    free: int
    percent: float


class NetworkInfo(BaseModel):
    bytes_sent: int
    bytes_received: int


class SystemSummary(BaseModel):
    host: HostInfo
    cpu: CpuInfo
    memory: MemoryInfo
    disks: list[DiskUsageInfo]
    network: NetworkInfo
