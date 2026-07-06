import datetime
import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from automation.instance_manager import InstanceManager
from utils.logger import setup_logger

logger = setup_logger("runtime")

CAIRO_TIMEZONE = "Africa/Cairo"
CAIRO_OFFSET = datetime.timedelta(hours=3)


def _parse_iso_datetime(value: str) -> datetime.datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.datetime.fromisoformat(value)


def get_cairo_time() -> dict:
    """Fetch Cairo time, falling back locally if public APIs are unavailable."""
    providers = [
        f"https://timeapi.io/api/time/current/zone?timeZone={CAIRO_TIMEZONE}",
        f"https://worldtimeapi.org/api/timezone/{CAIRO_TIMEZONE}",
    ]

    for url in providers:
        for attempt in range(2):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "BlueStacks-automation/1.0"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    data = json.loads(response.read().decode("utf-8"))

                if "dateTime" in data:
                    cairo_time = _parse_iso_datetime(data["dateTime"])
                    if cairo_time.tzinfo is None:
                        cairo_time = cairo_time.replace(tzinfo=datetime.timezone(CAIRO_OFFSET))
                    return {
                        "epoch_seconds": int(cairo_time.timestamp()),
                        "date": cairo_time.date().isoformat(),
                        "timezone": CAIRO_TIMEZONE,
                        "source": url,
                    }

                if "currentLocalTime" in data:
                    cairo_time = _parse_iso_datetime(data["currentLocalTime"])
                    if cairo_time.tzinfo is None:
                        cairo_time = cairo_time.replace(tzinfo=datetime.timezone(CAIRO_OFFSET))
                    return {
                        "epoch_seconds": int(cairo_time.timestamp()),
                        "date": cairo_time.date().isoformat(),
                        "timezone": CAIRO_TIMEZONE,
                        "source": url,
                    }

                if "unixtime" in data:
                    epoch_seconds = int(data["unixtime"])
                    date_str = data["datetime"].split("T", 1)[0]
                    return {
                        "epoch_seconds": epoch_seconds,
                        "date": date_str,
                        "timezone": data.get("timezone", CAIRO_TIMEZONE),
                        "source": url,
                    }
            except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, ValueError) as e:
                logger.warning(f"Cairo time API failed ({url}, attempt {attempt + 1}/2): {e}")
                time.sleep(1)

    cairo_time = datetime.datetime.now(datetime.timezone.utc).astimezone(datetime.timezone(CAIRO_OFFSET))
    logger.warning("All Cairo time APIs failed; falling back to local system clock with Cairo UTC+03:00 offset")
    return {
        "epoch_seconds": int(cairo_time.timestamp()),
        "date": cairo_time.date().isoformat(),
        "timezone": CAIRO_TIMEZONE,
        "source": "local-fallback",
    }


def run_instance(tracker):
    name = tracker.adb.name

    if not tracker.app_key:
        logger.warning(f"[{name}] No app assigned, skipping")
        tracker.last_result = "no_app"
        return

    logger.info(f"[{name}] Starting {tracker.app_key} automation")
    tracker.prepare_start()
    tracker.running = True

    try:
        while tracker.app and not tracker.stop_event.is_set():
            running_app = tracker.app_key
            ads_before = tracker.app.ads_watched if tracker.app else 0
            result = tracker.app.run_loop()
            ads_after = tracker.app.ads_watched if tracker.app else ads_before
            if running_app:
                tracker.ads_by_app[running_app] = tracker.ads_by_app.get(running_app, 0) + max(0, ads_after - ads_before)
            tracker.last_result = result or "loop_complete"
            if result == "stopped" or tracker.stop_event.is_set():
                break
            if result not in ("switch_app", "date_trick_blocked"):
                break

            old_app = tracker.app_key
            new_app = "tempsms" if old_app == "getsms" else "getsms"

            if result == "date_trick_blocked":
                tracker.disabled_apps.add(old_app)
                if tracker.app:
                    logger.info(f"[{name}] Closing blocked app {old_app}")
                    tracker.adb.close_app(tracker.app.PACKAGE_NAME)

                if new_app in tracker.disabled_apps:
                    logger.warning(f"[{name}] Both apps are blocked by fake-date loading; stopping this instance for today")
                    tracker.last_result = "done_today"
                    break

                cairo_time = get_cairo_time()
                logger.info(f"[{name}] Restoring device time to Cairo time before switching app ({cairo_time['source']})")
                tracker.adb.restore_time(
                    cairo_time["epoch_seconds"],
                    cairo_time["timezone"],
                    cairo_time["date"],
                )

            reason = "fake-date loading dialog" if result == "date_trick_blocked" else "repeated ad-load failures"
            logger.warning(f"[{name}] Switching from {old_app} to {new_app} after {reason}")
            tracker.assign_app(new_app)
            time.sleep(2)
    except Exception as e:
        tracker.last_error = str(e)
        logger.error(f"[{name}] Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        tracker.running = False
        if tracker.last_result == "starting":
            tracker.last_result = "stopped"


def default_app_assignments() -> dict[str, str]:
    return {
        "default": "getsms",
        "instance_1": "getsms",
        "instance_2": "getsms",
        "instance_3": "getsms",
        "instance_4": "getsms",
    }


def build_manager(app_assignments: dict[str, str] | None = None) -> InstanceManager:
    return InstanceManager.auto_discover(app_assignments or default_app_assignments())


def connect_manager(manager: InstanceManager) -> dict[str, bool]:
    online = manager.connect_all()
    connected_count = sum(1 for ok in online.values() if ok)
    logger.info(f"Connected: {connected_count}/{len(online)}")
    return online


def reset_online_dates(manager: InstanceManager, online: dict[str, bool] | None = None) -> None:
    online = online or {tracker.adb.name: tracker.adb.is_online() for tracker in manager.get_all()}
    cairo_time = get_cairo_time()
    logger.info(f"Resetting all instance dates to Cairo time {cairo_time['date']} ({cairo_time['source']})")
    online_trackers = [manager.get(name) for name, ok in online.items() if ok]
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(online_trackers)))) as executor:
        futures = {
            executor.submit(
                tracker.adb.restore_time,
                cairo_time["epoch_seconds"],
                cairo_time["timezone"],
                cairo_time["date"],
            ): tracker
            for tracker in online_trackers
        }
        for future in as_completed(futures):
            tracker = futures[future]
            if future.result():
                logger.info(f"[{tracker.adb.name}] Date reset verified")
            else:
                logger.warning(f"[{tracker.adb.name}] Date reset failed; continuing automation")


def start_tracker(tracker) -> bool:
    if tracker.running:
        logger.info(f"[{tracker.adb.name}] Already running")
        return False
    if not tracker.app:
        logger.warning(f"[{tracker.adb.name}] Select an app before starting")
        return False
    thread = threading.Thread(target=run_instance, args=(tracker,), daemon=True)
    tracker.thread = thread
    thread.start()
    return True


def stop_tracker(tracker) -> None:
    tracker.request_stop()
    if tracker.app:
        try:
            tracker.adb.close_app(tracker.app.PACKAGE_NAME)
        except Exception as e:
            logger.warning(f"[{tracker.adb.name}] Stop close-app failed: {e}")
