"""In-memory provider balance monitoring for manual fleet operations."""

from __future__ import annotations

import re
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from automation.adb_controller import ADBController
from automation.bluestacks_manager import BlueStacksInstance, BlueStacksManager
from config import APPS
from fleet_service.config import FleetSettings
from fleet_service.schemas import BalanceReading, BalanceScanResult

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_TEMPSMS_COIN_ID = "com.secondphone.tempsms:id/tv_coins"


class BalanceMonitor:
    """Scans visible provider balance labels and holds results in process memory."""

    def __init__(self, settings: FleetSettings) -> None:
        self._settings = settings
        self._manager = BlueStacksManager()
        self._cache: dict[tuple[str, str], BalanceReading] = {}
        self._scan_lock = threading.Lock()
        self._ocr_reader = None
        self._ocr_error: str | None = None
        self._load_ocr_reader()

    def cached_balances(self) -> list[BalanceReading]:
        return sorted(
            self._cache.values(),
            key=lambda item: (item.instance_id.casefold(), item.provider_key),
        )

    def status(self) -> dict:
        return {
            "getsms_ocr": {
                "ready": self._ocr_reader is not None,
                "error": self._ocr_error,
            }
        }

    def scan_until_eligible(self, threshold: float) -> BalanceScanResult:
        if not self._scan_lock.acquire(blocking=False):
            raise RuntimeError("A balance scan is already in progress.")
        try:
            return self._scan_until_eligible(threshold)
        finally:
            self._scan_lock.release()

    def _scan_until_eligible(self, threshold: float) -> BalanceScanResult:
        candidates = self._manager.list_instances()
        if not candidates:
            return BalanceScanResult(
                threshold=threshold,
                eligible=False,
                selected_instance_id=None,
                action="no_instances",
                readings=[],
            )

        opened = self._first_online(candidates)
        if opened:
            ordered = [opened] + [item for item in candidates if item.name != opened.name]
            action = "used_open_instance"
        else:
            ordered = candidates
            action = "started_instance"

        all_readings: list[BalanceReading] = []
        for candidate in ordered:
            ready = candidate if candidate.device_id and self._manager.resolve_online_instance(candidate) else None
            if ready is None:
                if not self._manager.start_and_wait(candidate, timeout=self._settings.instance_start_timeout_seconds):
                    continue
                ready = self._manager.wait_for_instance(
                    candidate, timeout=self._settings.instance_start_timeout_seconds
                )
            if ready is None:
                continue

            readings = self._scan_instance(ready)
            all_readings.extend(readings)
            if any(
                item.status == "measured"
                and item.balance_coins is not None
                and item.balance_coins >= threshold
                for item in readings
            ):
                return BalanceScanResult(
                    threshold=threshold,
                    eligible=True,
                    selected_instance_id=ready.name,
                    action=action,
                    readings=all_readings,
                )

            if any(item.status != "measured" for item in readings):
                return BalanceScanResult(
                    threshold=threshold,
                    eligible=False,
                    selected_instance_id=ready.name,
                    action="balance_unreadable_keep_open",
                    readings=all_readings,
                )

            self._manager.stop_instance(
                ready.name,
                display_name=ready.display_name,
                device_id=ready.device_id,
            )
            action = "rotated_below_threshold"

        return BalanceScanResult(
            threshold=threshold,
            eligible=False,
            selected_instance_id=None,
            action="no_instance_met_threshold",
            readings=all_readings,
        )

    def _first_online(self, instances: list[BlueStacksInstance]) -> BlueStacksInstance | None:
        for instance in instances:
            ready = self._manager.resolve_online_instance(instance)
            if ready:
                return ready
        return None

    def _scan_instance(self, instance: BlueStacksInstance) -> list[BalanceReading]:
        if not instance.device_id:
            return []
        adb = ADBController(instance.display_name, instance.device_id)
        readings: list[BalanceReading] = []
        for provider_key in ("tempsms", "getsms"):
            readings.append(self._read_provider_balance(adb, instance.name, provider_key))
        return readings

    def _read_provider_balance(
        self, adb: ADBController, instance_id: str, provider_key: str
    ) -> BalanceReading:
        provider = APPS[provider_key]
        observed_at = datetime.now(UTC)
        try:
            adb.launch_app(provider["package"], provider["activity"])
            if provider_key == "getsms":
                if self._ocr_reader is None:
                    raise RuntimeError(f"GetSMS OCR is unavailable: {self._ocr_error}")
            if not self._wait_for_foreground(adb, provider["package"]):
                raise RuntimeError("Provider app did not reach the foreground.")
            if provider_key == "tempsms":
                balance = self._read_tempsms_accessibility(adb)
                method = "accessibility"
            else:
                balance = self._read_getsms_ocr(adb)
                method = "ocr"
            reading = BalanceReading(
                instance_id=instance_id,
                provider_key=provider_key,
                balance_coins=balance,
                status="measured",
                method=method,
                observed_at=observed_at,
            )
        except Exception as exc:
            reading = BalanceReading(
                instance_id=instance_id,
                provider_key=provider_key,
                balance_coins=None,
                status="unreadable",
                method="none",
                observed_at=observed_at,
                error=str(exc)[:240],
            )
        self._cache[(instance_id, provider_key)] = reading
        return reading

    def _wait_for_foreground(self, adb: ADBController, package_name: str) -> bool:
        deadline = time.monotonic() + self._settings.provider_launch_timeout_seconds
        while time.monotonic() < deadline:
            if adb.get_current_package() == package_name:
                return True
            time.sleep(0.4)
        return False

    def _read_tempsms_accessibility(self, adb: ADBController) -> float:
        deadline = time.monotonic() + self._settings.provider_launch_timeout_seconds
        while time.monotonic() < deadline:
            xml = adb.shell(
                "uiautomator dump /sdcard/window.xml >/dev/null && cat /sdcard/window.xml"
            )
            if xml:
                root = ET.fromstring(xml)
                for node in root.iter("node"):
                    if node.attrib.get("resource-id") == _TEMPSMS_COIN_ID:
                        return _parse_balance(node.attrib.get("text", ""))
            time.sleep(0.35)
        raise RuntimeError("TempSMS balance label was not found.")

    def _read_getsms_ocr(self, adb: ADBController) -> float:
        deadline = time.monotonic() + self._settings.provider_launch_timeout_seconds
        while time.monotonic() < deadline:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
                screenshot_path = Path(handle.name)
            try:
                if not adb.screenshot(str(screenshot_path)):
                    raise RuntimeError("GetSMS screenshot failed.")
                from PIL import Image
                import numpy as np

                with Image.open(screenshot_path) as image:
                    width, height = image.size
                    crop = image.crop(
                        (
                            int(width * 0.68),
                            int(height * 0.04),
                            int(width * 0.99),
                            int(height * 0.13),
                        )
                    )
                    entries = self._ocr_reader.readtext(np.array(crop), detail=1)
                candidates = [
                    _parse_balance(text)
                    for _, text, confidence in entries
                    if confidence >= self._settings.ocr_min_confidence and _NUMBER.search(text)
                ]
                if candidates:
                    return candidates[-1]
            finally:
                screenshot_path.unlink(missing_ok=True)
            time.sleep(0.4)
        raise RuntimeError("GetSMS balance could not be read from the coin badge.")

    def _load_ocr_reader(self) -> None:
        try:
            import easyocr

            self._ocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        except Exception as exc:
            self._ocr_error = str(exc)

def _parse_balance(value: str) -> float:
    match = _NUMBER.search(value.replace(",", "."))
    if not match:
        raise RuntimeError("Balance text is not numeric.")
    return float(match.group(0).replace(",", "."))
