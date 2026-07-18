import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from automation.adb_controller import ADBController
from automation.bluestacks_manager import BlueStacksManager
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
        self.last_result = "idle"
        self.last_error = ""
        self.thread = None
        self.stop_event = threading.Event()
        self.results = {"watched": 0, "failed": 0}
        self.ads_by_app: dict[str, int] = {}
        self.disabled_apps: set[str] = set()
        self.lock = threading.Lock()

        if app_key and app_key in APP_CLASSES:
            self.app = APP_CLASSES[app_key](adb)
            self.app.stop_event = self.stop_event

    def assign_app(self, app_key: str):
        with self.lock:
            self.app_key = app_key
            self.app = APP_CLASSES[app_key](self.adb)
            self.app.stop_event = self.stop_event
            self.last_result = "assigned"

    def request_stop(self):
        self.stop_event.set()
        self.last_result = "stopping"

    def prepare_start(self):
        self.stop_event.clear()
        if self.app:
            self.app.stop_event = self.stop_event
        self.last_error = ""
        self.last_result = "starting"

    def status(self) -> dict:
        with self.lock:
            app_key = self.app_key
            app = self.app
            running = self.running
            last_result = self.last_result
            last_error = self.last_error
            adb_error = self.adb.last_error
            ads_by_app = dict(self.ads_by_app)
        return {
            "name": self.adb.name,
            "device_id": self.adb.device_id,
            "online": self.adb.is_online(),
            "app": app_key,
            "ads_watched": app.ads_watched if app else 0,
            "state": last_error or adb_error or last_result,
            "running": running,
            "adb_timeouts": self.adb.timeout_count,
            "ads_by_app": ads_by_app,
        }


class InstanceManager:
    def __init__(self):
        self.trackers: dict[str, InstanceTracker] = {}

    @classmethod
    def auto_discover(cls, app_assignments: dict[str, str] | None = None) -> "InstanceManager":
        manager = cls()

        bs_manager = BlueStacksManager()
        bs_instances = [inst for inst in bs_manager.list_instances() if inst.device_id]
        seen_devices = set()

        for i, bs_inst in enumerate(bs_instances, 1):
            bs_inst = bs_manager.resolve_online_instance(bs_inst) or bs_inst
            device_id = bs_inst.device_id
            if not device_id:
                continue
            seen_devices.update(bs_inst.candidate_device_ids())
            name = bs_inst.display_name
            adb = ADBController(name=name, device_id=device_id)
            app_key = app_assignments.get(name) if app_assignments else None
            if app_key is None and app_assignments:
                app_key = app_assignments.get(f"instance_{i}")
            if app_key is None and app_assignments:
                app_key = app_assignments.get("default")
            manager.trackers[name] = InstanceTracker(adb, app_key)

        # Include any connected ADB device that is not present in BlueStacks config.
        devices = ADBController.discover_devices()
        for device_id in devices:
            if device_id in seen_devices:
                continue
            name = f"adb_{len(manager.trackers) + 1}"
            adb = ADBController(name=name, device_id=device_id)
            app_key = app_assignments.get("default") if app_assignments else None
            manager.trackers[name] = InstanceTracker(adb, app_key)

        logger.info(f"Discovered {len(manager.trackers)} configured/connected instance(s)")
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

    def has_running(self) -> bool:
        return any(t.running for t in self.trackers.values())

    def find_by_device(self, device_id: str) -> InstanceTracker | None:
        for tracker in self.trackers.values():
            if tracker.adb.device_id == device_id:
                return tracker
        return None

    def find_by_name(self, name: str) -> InstanceTracker | None:
        return self.trackers.get(name)

    def ensure_tracker(
        self,
        name: str,
        device_id: str,
        app_key: str | None = None,
        app_assignments: dict[str, str] | None = None,
    ) -> InstanceTracker:
        """Return an existing tracker for this device/name, or create one.

        Never replaces a tracker that is still running.
        """
        existing = self.find_by_device(device_id) or self.find_by_name(name)
        if existing:
            if existing.adb.device_id != device_id:
                if existing.running:
                    logger.warning(
                        f"Keeping running tracker {existing.adb.name} on {existing.adb.device_id}; "
                        f"not rebinding to {device_id}"
                    )
                else:
                    existing.adb.device_id = device_id
            if name != existing.adb.name and name not in self.trackers and not existing.running:
                # Re-key under the preferred BlueStacks display name when idle.
                self.trackers.pop(existing.adb.name, None)
                existing.adb.name = name
                self.trackers[name] = existing
            return existing

        resolved_app = app_key
        if resolved_app is None and app_assignments:
            resolved_app = app_assignments.get(name) or app_assignments.get("default")
        adb = ADBController(name=name, device_id=device_id)
        tracker = InstanceTracker(adb, resolved_app)
        self.trackers[name] = tracker
        logger.info(f"Added tracker for {name} ({device_id})")
        return tracker

    def sync_from_bluestacks(self, app_assignments: dict[str, str] | None = None) -> int:
        """Merge BlueStacks/ADB devices into this manager without dropping live trackers.

        Returns the number of newly added trackers.
        """
        added = 0
        bs_instances = [inst for inst in BlueStacksManager().list_instances() if inst.device_id]
        seen_devices: set[str] = set()

        for i, bs_inst in enumerate(bs_instances, 1):
            device_id = bs_inst.device_id
            seen_devices.add(device_id)
            name = bs_inst.display_name
            before = len(self.trackers)
            app_key = None
            if app_assignments:
                app_key = app_assignments.get(name)
                if app_key is None:
                    app_key = app_assignments.get(f"instance_{i}")
                if app_key is None:
                    app_key = app_assignments.get("default")
            self.ensure_tracker(name, device_id, app_key=app_key, app_assignments=app_assignments)
            if len(self.trackers) > before:
                added += 1

        for device_id in ADBController.discover_devices():
            if device_id in seen_devices:
                continue
            seen_devices.add(device_id)
            if self.find_by_device(device_id):
                continue
            name = f"adb_{len(self.trackers) + 1}"
            app_key = app_assignments.get("default") if app_assignments else None
            self.ensure_tracker(name, device_id, app_key=app_key, app_assignments=app_assignments)
            added += 1

        if added:
            logger.info(f"Synced manager; added {added} tracker(s), total {len(self.trackers)}")
        return added

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
