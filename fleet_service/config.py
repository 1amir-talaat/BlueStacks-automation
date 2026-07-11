"""Configuration for the loopback-only fleet service."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class FleetSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="fleet_service/.env",
        env_file_encoding="utf-8",
        env_prefix="FLEET_SERVICE_",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8110
    token: str = ""
    max_probe_workers: int = 8
    adb_timeout_seconds: int = 10
    instance_start_timeout_seconds: int = 90
    provider_launch_timeout_seconds: int = 12
    ocr_min_confidence: float = 0.65


@lru_cache
def get_settings() -> FleetSettings:
    return FleetSettings()
