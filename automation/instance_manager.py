import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from automation.adb_controller import ADBController
from apps.getsms import GetSMSApp
from apps.tempsms import TempSMSApp
from config import APPS
from utils.logger import setup_logger

logger = setup_logger("instances")

APP_CLASSES = {
    "getsms": GetSMSApp,
    "tempsms": TempSMSApp,
}


class InstanceTracker:
    def __init__(self, adb: ADBController, app_key: str | None = None):
        self.adb = adb
        self.app_key = app_key
        self.app = None
        self.running = False
        self.results = {"watched": 0, "failed": 0}
        self.disabled_apps: set[str] = set()

        if app_key and app_key in APP_CLASSES:
            self.app = APP_CLASSES[app_key](adb)

    def assign_app(self, app_key: str):
        self.app_key = app_key
        self.app = APP_CLASSES[app_key](self.adb)

    def status(self) -> dict:
        return {
            "name": self.adb.name,
            "device_id": self.adb.device_id,
            "online": self.adb.is_online(),
            "app": self.app_key,
            "ads_watched": self.app.ads_watched if self.app else 0,
            "state": "idle",
            "running": self.running,
        }


class InstanceManager:
    def __init__(self):
        self.trackers: dict[str, InstanceTracker] = {}

    @classmethod
    def auto_discover(cls, app_assignments: dict[str, str] | None = None) -> "InstanceManager":
        devices = ADBController.discover_devices()
        manager = cls()

        for i, device_id in enumerate(devices, 1):
            name = f"instance_{i}"
            adb = ADBController(name=name, device_id=device_id)
            app_key = app_assignments.get(name) if app_assignments else None
            manager.trackers[name] = InstanceTracker(adb, app_key)

        logger.info(f"Discovered {len(devices)} instance(s)")
        return manager

    def connect_all(self) -> dict[str, bool]:
        results = {}
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(self.trackers)))) as executor:
            futures = {executor.submit(tracker.adb.connect): name for name, tracker in self.trackers.items()}
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return results

    def disconnect_all(self):
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(self.trackers)))) as executor:
            futures = [executor.submit(tracker.adb.disconnect) for tracker in self.trackers.values()]
            for future in as_completed(futures):
                future.result()

    def get(self, name: str) -> InstanceTracker:
        return self.trackers[name]

    def get_all(self) -> list[InstanceTracker]:
        return list(self.trackers.values())

    def get_online(self) -> list[InstanceTracker]:
        return [t for t in self.trackers.values() if t.adb.is_online()]

    def get_running(self) -> list[InstanceTracker]:
        return [t for t in self.trackers.values() if t.running]

    def status(self) -> list[dict]:
        trackers = list(self.trackers.values())
        if not trackers:
            return []
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(trackers)))) as executor:
            futures = [executor.submit(t.status) for t in trackers]
            return [future.result() for future in futures]

    def print_status(self):
        print("\n" + "=" * 60)
        print("INSTANCE STATUS")
        print("=" * 60)
        for s in self.status():
            online = "ONLINE" if s["online"] else "OFFLINE"
            app = s["app"] or "none"
            watched = s["ads_watched"]
            state = s["state"]
            running = "RUNNING" if s["running"] else "stopped"
            print(f"  {s['name']:15} | {online:8} | {app:10} | ads: {watched:3} | {state:20} | {running}")
        print("=" * 60 + "\n")
