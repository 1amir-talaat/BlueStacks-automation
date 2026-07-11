"""Read-only BlueStacks and ADB fleet probes.

This module does not operate provider application workflows. It reports only
instance health, ADB reachability, and installed/foreground package status.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from automation.adb_controller import ADBController
from automation.bluestacks_manager import BlueStacksInstance, BlueStacksManager
from fleet_service.config import FleetSettings
from fleet_service.schemas import (
    FleetInstance,
    FleetSnapshot,
    InstanceHealth,
    ProviderActionResult,
    ProviderStatus,
)

PROVIDERS = {
    "getsms": "com.virtualnumber.sms",
    "tempsms": "com.secondphone.tempsms",
}


class FleetProbeService:
    def __init__(self, settings: FleetSettings) -> None:
        self._settings = settings
        self._manager = BlueStacksManager()

    def snapshot(self) -> FleetSnapshot:
        instances = self._manager.list_instances()
        if not instances:
            return FleetSnapshot(generated_at=datetime.now(UTC), instances=[])

        workers = max(1, min(self._settings.max_probe_workers, len(instances)))
        results: list[FleetInstance] = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(self._probe_instance, instance): instance for instance in instances}
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda row: (row.display_name.casefold(), row.instance_id))
        return FleetSnapshot(generated_at=datetime.now(UTC), instances=results)

    def launch_provider(self, instance_id: str, provider_key: str) -> ProviderActionResult:
        package_name = PROVIDERS.get(provider_key)
        if package_name is None:
            raise ValueError("Unsupported provider.")
        instance = self._manager.get_instance(instance_id)
        if instance is None:
            raise LookupError("BlueStacks instance not found.")
        resolved = self._manager.resolve_online_instance(instance)
        if resolved is None or not resolved.device_id:
            raise RuntimeError("BlueStacks instance is not ADB-ready.")

        adb = ADBController(resolved.display_name, resolved.device_id)
        installed = bool(
            adb.shell(f"pm path {package_name}", timeout=self._settings.adb_timeout_seconds)
        )
        if not installed:
            raise RuntimeError("Provider app is not installed on this instance.")

        adb.launch_app(package_name)
        deadline = time.monotonic() + 8
        foreground = False
        while time.monotonic() < deadline:
            foreground = adb.get_current_package() == package_name
            if foreground:
                break
            time.sleep(0.4)
        if not foreground:
            raise RuntimeError("Provider app did not reach the foreground.")
        return ProviderActionResult(
            instance_id=resolved.name,
            provider_key=provider_key,
            package_name=package_name,
            action="launch",
            foreground=True,
        )

    def _probe_instance(self, instance: BlueStacksInstance) -> FleetInstance:
        started = time.monotonic()
        process_running = self._is_process_running(instance)
        resolved = self._manager.resolve_online_instance(instance)
        endpoint = resolved.device_id if resolved else instance.device_id
        connected = resolved is not None
        latency_ms = int((time.monotonic() - started) * 1000) if connected else None
        error: str | None = None
        apps: list[ProviderStatus] = []

        if connected and endpoint:
            try:
                adb = ADBController(instance.display_name, endpoint)
                apps = self._provider_statuses(adb)
            except (FileNotFoundError, OSError) as exc:
                connected = False
                error = str(exc)
        elif not process_running:
            error = "BlueStacks process is not running"
        else:
            error = "ADB endpoint is not reachable"

        failures = 0 if connected else 1
        if not process_running and not connected:
            state = "offline"
        elif not connected:
            state = "degraded"
        elif any(app.state == "degraded" for app in apps):
            state = "degraded"
        else:
            state = "idle"

        score = 100
        if state == "degraded":
            score = 60
        elif state == "offline":
            score = 0
        return FleetInstance(
            instance_id=instance.name,
            display_name=instance.display_name,
            adb_endpoint=endpoint,
            state=state,
            adb_connected=connected,
            adb_latency_ms=latency_ms,
            bluestacks_process_running=process_running,
            last_seen_at=datetime.now(UTC),
            apps=apps,
            health=InstanceHealth(score=score, failures_last_15m=failures, last_error=error),
        )

    def _provider_statuses(self, adb: ADBController) -> list[ProviderStatus]:
        foreground = adb.get_current_package()
        statuses: list[ProviderStatus] = []
        for key, package_name in PROVIDERS.items():
            installed = bool(adb.shell(f"pm path {package_name}", timeout=self._settings.adb_timeout_seconds))
            state = "available" if installed else "unavailable"
            if adb.timeout_count:
                state = "degraded"
            statuses.append(
                ProviderStatus(
                    provider_key=key,
                    package_name=package_name,
                    installed=installed,
                    foreground=foreground == package_name,
                    state=state,
                    last_error=adb.last_error or None,
                )
            )
        return statuses

    def _is_process_running(self, instance: BlueStacksInstance) -> bool:
        return bool(
            self._manager._find_player_pids(
                instance.name,
                display_name=instance.display_name,
                ports=instance.candidate_ports(),
            )
        )
