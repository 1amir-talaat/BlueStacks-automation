"""Root-based ad-limit bypass for GetSMS / TempSMS.

Uses adb root shell (su) to directly modify SharedPreferences,
resetting daily ad limits, loader state, and ad counters.

Usage:
    python -m automation.ad_reset_root [--device DEVICE] [--app getsms|tempsms|both]
"""
from __future__ import annotations

import re
import sys
import time
import uuid
from typing import Optional

from automation.adb_controller import ADBController


# --- package / path constants ---------------------------------------------------
PACKAGES = {
    "getsms": "com.virtualnumber.sms",
    "tempsms": "com.secondphone.tempsms",
}

# Keys that control daily ad limits in each app's default.xml
# GetSMS uses different key names than TempSMS
RESET_KEYS_GETSMS = {
    "daily_reward_max_reached": "false",
    "the_daily_reward_day": "0",
}

RESET_KEYS_TEMPSMS = {
    "ad_watch_daily_limit_reached": "false",
    "ad_watch_daily_limit_day": "0",
    "ad_watch_progress_current": "0",
    "ad_watch_last_reward_at": "0",
}

# AdMob counters to reset in admob.xml
ADMOB_RESET_KEYS = {
    "request_in_session_count": "0",
}


# --- helpers --------------------------------------------------------------------
def _su(adb: ADBController, cmd: str, timeout: int = 10) -> str:
    """Run a command as root via su -c."""
    return adb.shell(f"su -c '{cmd}'", timeout=timeout)


def _read_prefs(adb: ADBController, package: str, filename: str) -> str:
    """Read a SharedPreferences XML file."""
    return _su(adb, f"cat /data/data/{package}/shared_prefs/{filename}")


def _write_prefs(adb: ADBController, package: str, filename: str, xml: str) -> bool:
    """Write a SharedPreferences XML file via a temp file on /sdcard."""
    tmp = f"/sdcard/_prefs_{int(time.time())}.xml"
    # Write to temp file
    write_cmd = f"echo '{xml}' > {tmp}"
    _su(adb, write_cmd, timeout=10)
    # Copy to app's shared_prefs with correct ownership
    copy_cmd = (
        f"cp {tmp} /data/data/{package}/shared_prefs/{filename} && "
        f"chown $(stat -c '%u:%g' /data/data/{package}/shared_prefs/{filename}.bak 2>/dev/null || "
        f"echo 'u0_a$(dumpsys package {package} | grep userId= | head -1 | sed \"s/.*userId=\\([0-9]*\\).*/\\1/\"):u0_a"
        f"$(dumpsys package {package} | grep userId= | head -1 | sed \"s/.*userId=\\([0-9]*\\).*/\\1/\")') "
        f"/data/data/{package}/shared_prefs/{filename} && "
        f"chmod 660 /data/data/{package}/shared_prefs/{filename} && "
        f"rm {tmp}"
    )
    result = _su(adb, copy_cmd, timeout=10)
    return "No such file" not in (result or "")


def _replace_key_value(xml: str, key: str, new_value: str) -> str:
    """Replace a value for a given key in SharedPreferences XML."""
    # Match various types: <boolean name="key" value="..." />, <int name="key" value="..." />, etc.
    pattern = re.compile(
        r'(<(?:boolean|int|long|string) name="' + re.escape(key) + r'"[^>]*value=)"[^"]*"',
        re.DOTALL,
    )
    new_xml, count = pattern.subn(r'\1"' + new_value + '"', xml, count=1)
    return new_xml


def _force_stop(adb: ADBController, package: str) -> None:
    """Force-stop an app."""
    adb.shell(f"am force-stop {package}", timeout=5)


def _launch_app(adb: ADBController, package: str) -> None:
    """Launch an app by its main activity."""
    adb.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1", timeout=5)


# --- core reset logic -----------------------------------------------------------
def reset_app_ads(
    adb: ADBController,
    app_key: str,
    new_device_id: Optional[str] = None,
    preserve_device_id: bool = True,
) -> dict:
    """
    Reset ad-related state for one app via root.

    Returns a dict with:
      - status: 'ok' | 'error'
      - changes: list of strings describing what was changed
      - errors: list of strings describing what failed
    """
    package = PACKAGES[app_key]
    changes = []
    errors = []

    # 1. Force-stop the app first
    _force_stop(adb, package)
    time.sleep(1)

    # 2. Reset default.xml keys
    default_xml = _read_prefs(adb, package, "default.xml")
    if not default_xml or "No such file" in default_xml:
        errors.append(f"Could not read default.xml for {app_key}")
    else:
        reset_keys = RESET_KEYS_GETSMS if app_key == "getsms" else RESET_KEYS_TEMPSMS
        modified = default_xml
        for key, value in reset_keys.items():
            old_xml = modified
            modified = _replace_key_value(modified, key, value)
            if modified != old_xml:
                changes.append(f"default.xml: {key} -> {value}")
            else:
                # Key might not exist or already has this value; try to add it
                pass

        if changes:
            # Write back
            tmp = f"/sdcard/_default_reset.xml"
            _su(adb, f"rm -f {tmp}", timeout=5)
            # Write XML content to file using printf to handle special chars
            _su(adb, f"printf '%s' '{modified}' > {tmp}", timeout=10)
            _su(
                adb,
                f"cp {tmp} /data/data/{package}/shared_prefs/default.xml && chmod 660 /data/data/{package}/shared_prefs/default.xml && rm -f {tmp}",
                timeout=10,
            )

    # 3. Reset admob.xml session counters
    admob_xml = _read_prefs(adb, package, "admob.xml")
    if admob_xml and "No such file" not in admob_xml:
        modified_admob = admob_xml
        for key, value in ADMOB_RESET_KEYS.items():
            old = modified_admob
            modified_admob = _replace_key_value(modified_admob, key, value)
            if modified_admob != old:
                changes.append(f"admob.xml: {key} -> {value}")

        if modified_admob != admob_xml:
            tmp = f"/sdcard/_admob_reset.xml"
            _su(adb, f"rm -f {tmp}", timeout=5)
            _su(adb, f"printf '%s' '{modified_admob}' > {tmp}", timeout=10)
            _su(
                adb,
                f"cp {tmp} /data/data/{package}/shared_prefs/admob.xml && chmod 660 /data/data/{package}/shared_prefs/admob.xml && rm -f {tmp}",
                timeout=10,
            )

    # 4. Optionally change the device_id
    if not preserve_device_id and new_device_id:
        app_xml = _read_prefs(adb, package, f"{package}.xml")
        if app_xml and "No such file" not in app_xml:
            modified_app = _replace_key_value(app_xml, "key_device_id", new_device_id)
            if modified_app != app_xml:
                changes.append(f"{package}.xml: key_device_id -> {new_device_id}")
                tmp = f"/sdcard/_appid_reset.xml"
                _su(adb, f"rm -f {tmp}", timeout=5)
                _su(adb, f"printf '%s' '{modified_app}' > {tmp}", timeout=10)
                _su(
                    adb,
                    f"cp {tmp} /data/data/{package}/shared_prefs/{package}.xml && chmod 660 /data/data/{package}/shared_prefs/{package}.xml && rm -f {tmp}",
                    timeout=10,
                )

    # 5. Clear Google measurement prefs (ad tracking)
    _su(adb, f"rm -f /data/data/{package}/shared_prefs/com.google.android.gms.measurement.prefs.xml", timeout=5)
    changes.append("Cleared Google measurement prefs")

    # 6. Restart the app
    time.sleep(1)
    _launch_app(adb, package)
    time.sleep(3)

    return {
        "status": "ok" if not errors else "partial",
        "changes": changes,
        "errors": errors,
    }


def reset_all_ads(
    adb: ADBController,
    apps: list[str] | None = None,
    new_device_id: Optional[str] = None,
    preserve_device_id: bool = True,
) -> dict:
    """Reset ad state for multiple apps."""
    if apps is None:
        apps = ["getsms", "tempsms"]

    results = {}
    for app in apps:
        print(f"[{adb.name}] Resetting ads for {app}...")
        results[app] = reset_app_ads(
            adb,
            app,
            new_device_id=new_device_id,
            preserve_device_id=preserve_device_id,
        )
        print(f"[{adb.name}] {app}: {results[app]}")

    return results


# --- CLI entry point ------------------------------------------------------------
def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Reset ad limits via root")
    parser.add_argument("--device", default=None, help="ADB device ID")
    parser.add_argument("--app", choices=["getsms", "tempsms", "both"], default="both")
    parser.add_argument("--change-id", action="store_true", help="Generate a new device_id")
    args = parser.parse_args(argv)

    devices = [args.device] if args.device else ADBController.discover_devices()
    if not devices:
        print("No ADB devices found.")
        return 1

    for device_id in devices:
        adb = ADBController(name=device_id, device_id=device_id)
        if not adb.connect():
            print(f"[{device_id}] Failed to connect")
            continue

        apps = ["getsms", "tempsms"] if args.app == "both" else [args.app]
        new_id = str(uuid.uuid4()) if args.change_id else None

        results = reset_all_ads(adb, apps, new_device_id=new_id, preserve_device_id=not args.change_id)
        print(f"\n[{device_id}] Results:")
        for app, result in results.items():
            print(f"  {app}: {result['status']}")
            for c in result.get("changes", []):
                print(f"    + {c}")
            for e in result.get("errors", []):
                print(f"    ! {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
