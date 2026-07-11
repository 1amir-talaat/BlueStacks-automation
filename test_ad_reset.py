from __future__ import annotations

import re
import sys
import time

from automation.adb_controller import ADBController

ADS_PRIVACY_ACTION = "com.google.android.gms.settings.ADS_PRIVACY"
ADS_PRIVACY_COMPONENT = "com.google.android.gms/.adid.settings.AdsSettingsActivity"
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}"
)

RESET_TEXTS = [
    "reset advertising id",
    "delete advertising id",
    "reset ad id",
    "delete ad id",
    "reset",
    "delete",
]
CONFIRM_TEXTS = [
    "ok",
    "confirm",
    "yes",
]


def _uiautomator_dump(adb: ADBController, timeout: int = 15) -> str:
    return adb.shell("uiautomator dump /sdcard/window.xml >/dev/null && cat /sdcard/window.xml", timeout=timeout)


def _extract_uuids(xml: str) -> list[str]:
    return list(dict.fromkeys(UUID_RE.findall(xml or "")))


def _open_ads_privacy_screen(adb: ADBController) -> bool:
    for command in (
        f"am start -W -a {ADS_PRIVACY_ACTION}",
        f"am start -W -n {ADS_PRIVACY_COMPONENT}",
    ):
        adb.shell(command, timeout=10)
        time.sleep(2)
        if _wait_for_ad_id(adb, timeout=8) is not None:
            return True
    return False


def _wait_for_ad_id(adb: ADBController, timeout: float = 20.0) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        xml = _uiautomator_dump(adb, timeout=15)
        ad_ids = _extract_uuids(xml)
        if ad_ids:
            if len(ad_ids) > 1:
                print(f"[{adb.name}] multiple UUIDs seen on screen: {ad_ids}")
            return ad_ids[0]
        time.sleep(0.75)
    return None


def test_device(device_id: str) -> dict[str, str]:
    adb = ADBController(name=device_id, device_id=device_id)
    if not adb.connect():
        return {
            "device": device_id,
            "status": "offline",
        }

    print(f"[{device_id}] opening Google Play services ads privacy screen")
    if not _open_ads_privacy_screen(adb):
        print(f"[{device_id}] failed to open ads privacy screen")
        return {
            "device": device_id,
            "status": "screen-not-found",
        }

    before_id = _wait_for_ad_id(adb, timeout=20)
    print(f"[{device_id}] before ad_id={before_id or 'unavailable'}")
    if not before_id:
        return {
            "device": device_id,
            "status": "before-id-missing",
        }

    reset_clicked = adb.tap_ui_text(RESET_TEXTS, timeout=8)
    print(f"[{device_id}] reset_clicked={reset_clicked}")
    if not reset_clicked:
        return {
            "device": device_id,
            "status": "reset-button-missing",
            "before_id": before_id,
        }

    time.sleep(1)

    confirm_clicked = adb.tap_ui_text(CONFIRM_TEXTS, timeout=5)
    print(f"[{device_id}] confirm_clicked={confirm_clicked}")
    if not confirm_clicked:
        print(f"[{device_id}] no confirmation dialog; checking whether reset was applied directly")

    time.sleep(2)
    if not _open_ads_privacy_screen(adb):
        print(f"[{device_id}] could not reopen ads privacy screen after reset")
        return {
            "device": device_id,
            "status": "reopen-failed",
            "before_id": before_id,
        }

    after_id = _wait_for_ad_id(adb, timeout=20)
    print(f"[{device_id}] after ad_id={after_id or 'unavailable'}")

    changed = bool(before_id and after_id and before_id != after_id)
    print(f"[{device_id}] changed={changed}")

    return {
        "device": device_id,
        "status": "changed" if changed else "unchanged",
        "before_id": before_id,
        "after_id": after_id or "",
    }


def _resolve_devices(argv: list[str]) -> list[str]:
    if argv:
        return argv
    return [
        device
        for device in ADBController.discover_devices()
        if device != "127.0.0.1:5555"
    ]


def main(argv: list[str]) -> int:
    devices = _resolve_devices(argv)
    if not devices:
        print("No online BlueStacks ADB devices found.")
        return 1

    results = []
    for device_id in devices:
        results.append(test_device(device_id))

    print("\nSummary:")
    for result in results:
        print(result)

    return 0 if any(result.get("status") == "changed" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
