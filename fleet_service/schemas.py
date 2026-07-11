"""Public DTOs for fleet-service consumers."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ProviderStatus(BaseModel):
    provider_key: str
    package_name: str
    installed: bool
    foreground: bool
    state: str
    last_error: str | None = None


class InstanceHealth(BaseModel):
    score: int = Field(ge=0, le=100)
    failures_last_15m: int = Field(ge=0)
    last_error: str | None = None


class FleetInstance(BaseModel):
    instance_id: str
    display_name: str
    adb_endpoint: str | None = None
    state: str
    adb_connected: bool
    adb_latency_ms: int | None = None
    bluestacks_process_running: bool
    last_seen_at: datetime
    apps: list[ProviderStatus]
    health: InstanceHealth


class FleetSnapshot(BaseModel):
    generated_at: datetime
    instances: list[FleetInstance]


class ProviderActionResult(BaseModel):
    instance_id: str
    provider_key: str
    package_name: str
    action: str
    foreground: bool


class BalanceReading(BaseModel):
    instance_id: str
    provider_key: str
    balance_coins: float | None
    status: str
    method: str
    observed_at: datetime
    error: str | None = None


class BalanceScanRequest(BaseModel):
    threshold: float = Field(default=20, ge=0, le=100000)


class BalanceScanResult(BaseModel):
    threshold: float
    eligible: bool
    selected_instance_id: str | None
    action: str
    readings: list[BalanceReading]
