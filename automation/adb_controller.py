import subprocess
import shutil
import re
import datetime
import time
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Optional
from utils.logger import setup_logger

logger = setup_logger("adb")

ADB_PATH = r"C:\Program Files\BlueStacks_nxt\HD-Adb.exe"


class ADBController:
    def __init__(self, name: str, device_id: str):
        self.name = name
        self.device_id = device_id
        self.connected = False
        self.last_error = ""
        self.timeout_count = 0
        self._adb = self._resolve_adb()

    @staticmethod
    def _resolve_adb() -> str:
        if Path(ADB_PATH).exists():
            return ADB_PATH
        found = shutil.which("adb")
        if found:
            return found
        raise FileNotFoundError("adb not found")

    def _run(self, args: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
        cmd = [self._adb] + args
        logger.debug(f"[{self.name}] Running: {' '.join(cmd)}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            self.last_error = ""
            return result
        except subprocess.TimeoutExpired:
            self.timeout_count += 1
            self.connected = False
            self.last_error = f"ADB timeout after {timeout}s: {' '.join(args)}"
            logger.error(f"[{self.name}] {self.last_error}")
            return subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr=self.last_error)
        except OSError as e:
            self.connected = False
            self.last_error = str(e)
            logger.error(f"[{self.name}] ADB command failed: {e}")
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr=str(e))

    def _recover_after_timeout(self) -> bool:
        logger.info(f"[{self.name}] Rechecking ADB after timeout")
        result = self._run(["-s", self.device_id, "get-state"], timeout=3)
        if "device" in result.stdout.strip():
            self.connected = True
            logger.info(f"[{self.name}] ADB recovered")
            return True
        return False

    def connect(self) -> bool:
        # TCP BlueStacks endpoints need an explicit connect before get-state.
        if ":" in self.device_id:
            self._run(["connect", self.device_id], timeout=5)
        result = self._run(["-s", self.device_id, "get-state"], timeout=5)
        output = result.stdout.strip() or result.stderr.strip()
        if "device" in result.stdout.strip():
            self.connected = True
            self.last_error = ""
            logger.info(f"[{self.name}] Connected ({self.device_id})")
            return True
        self.connected = False
        self.last_error = output or "not online"
        logger.warning(f"[{self.name}] Not online: {output}")
        return False

    def disconnect(self):
        self.connected = False
        logger.info(f"[{self.name}] Disconnected")

    def is_online(self) -> bool:
        if ":" in self.device_id:
            self._run(["connect", self.device_id], timeout=5)
        result = self._run(["-s", self.device_id, "get-state"], timeout=5)
        if "device" in result.stdout.strip():
            self.connected = True
            return True
        if result.returncode != 0:
            self.last_error = result.stderr.strip() or self.last_error
        self.connected = False
        return False

    def shell(self, command: str, timeout: int = 10) -> str:
        result = self._run(["-s", self.device_id, "shell", command], timeout=timeout)
        if result.returncode == 124:
            self._recover_after_timeout()
        return result.stdout.strip()

    def tap(self, x: int, y: int):
        self.shell(f"input tap {x} {y}", timeout=4)
        logger.debug(f"[{self.name}] Tap ({x}, {y})")

    def tap_many(self, x: int, y: int, count: int = 5, delay: float = 0.05):
        """Send several taps in one shell call to avoid per-tap ADB startup cost."""
        delay_cmd = ""
        if delay > 0:
            delay_cmd = f"; sleep {delay}"
        command = "; ".join([f"input tap {x} {y}{delay_cmd}" for _ in range(count)])
        self.shell(command, timeout=6)
        logger.debug(f"[{self.name}] Tap many ({x}, {y}) x{count}")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int = 300):
        self.shell(f"input swipe {x1} {y1} {x2} {y2} {duration}", timeout=5)

    def press_key(self, key: str) -> bool:
        result = self.shell(f"input keyevent {key}", timeout=4)
        return not self.last_error

    def launch_app(self, package: str, activity: str = ""):
        if activity:
            self.shell(f"am start -n {package}/{activity}", timeout=8)
        else:
            self.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1", timeout=8)
        logger.info(f"[{self.name}] Launched {package}")

    def close_app(self, package: str) -> bool:
        self.shell(f"am force-stop {package}", timeout=5)
        if self.last_error:
            logger.warning(f"[{self.name}] Close app may have failed for {package}: {self.last_error}")
            return False
        logger.info(f"[{self.name}] Closed {package}")
        return True

    def screenshot(self, local_path: str = "current.png") -> bool:
        cmd = [self._adb, "-s", self.device_id, "exec-out", "screencap", "-p"]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=15)
            if result.returncode == 0 and len(result.stdout) > 100:
                Path(local_path).parent.mkdir(parents=True, exist_ok=True)
                with open(local_path, "wb") as f:
                    f.write(result.stdout)
                return True
        except Exception as e:
            logger.error(f"[{self.name}] Screenshot failed: {e}")
        return False

    def get_current_package(self) -> str:
        result = self._run(["-s", self.device_id, "shell", "dumpsys", "window", "windows"])
        for line in result.stdout.splitlines():
            if "mCurrentFocus" in line or "mFocusedApp" in line:
                match = re.search(r'u0 ([^\s}]+)', result.stdout if "mFocusedApp" in line else line)
                if match:
                    return match.group(1).split("/")[0]
        return ""

    def get_focused_activity(self) -> str:
        """Get the full package/activity of the current focus. ~10ms."""
        result = self._run(["-s", self.device_id, "shell", "dumpsys", "window", "windows"])
        for line in result.stdout.splitlines():
            if "mCurrentFocus" in line:
                match = re.search(r'u0 ([^\s}]+)', line)
                if match:
                    return match.group(1)
        return ""

    def is_focus_on(self, package: str) -> bool:
        """Check if the focused window belongs to the given package. ~10ms."""
        return package in self.get_focused_activity()

    def get_screen_resolution(self) -> tuple[int, int]:
        result = self._run(["-s", self.device_id, "shell", "wm", "size"])
        match = re.search(r'(\d+)x(\d+)', result.stdout)
        if match:
            return int(match.group(1)), int(match.group(2))
        return 1080, 1920

    def dump_ui_xml(self) -> str:
        """Dump the current UI hierarchy via UIAutomator (best-effort)."""
        return self.shell(
            "uiautomator dump /sdcard/window.xml >/dev/null && cat /sdcard/window.xml",
            timeout=8,
        )

    def has_ui_text(self, texts: list[str]) -> bool:
        """Return True if any UIAutomator node text/desc contains one of the values."""
        xml = self.dump_ui_xml()
        if not xml:
            return False
        lowered = xml.lower()
        return any(text.lower() in lowered for text in texts)

    def tap_ui_text(self, texts: list[str], timeout: float = 5.0) -> bool:
        """Tap the first visible UIAutomator node whose text contains any value."""
        deadline = time.time() + timeout
        wanted = [text.lower() for text in texts]

        while time.time() < deadline:
            xml = self.dump_ui_xml()
            if not xml:
                time.sleep(0.5)
                continue

            try:
                root = ET.fromstring(xml)
            except ET.ParseError:
                time.sleep(0.5)
                continue

            for node in root.iter("node"):
                node_text = (node.attrib.get("text") or "").strip()
                node_desc = (node.attrib.get("content-desc") or "").strip()
                haystack = f"{node_text} {node_desc}".lower()
                if not any(text in haystack for text in wanted):
                    continue

                bounds = node.attrib.get("bounds", "")
                match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
                if not match:
                    continue

                x1, y1, x2, y2 = map(int, match.groups())
                cx = (x1 + x2) // 2
                cy = (y1 + y2) // 2
                logger.info(f"[{self.name}] Tapping UI text '{node_text or node_desc}' at ({cx}, {cy})")
                self.tap(cx, cy)
                return True

            time.sleep(0.5)

        logger.warning(f"[{self.name}] UI text not found: {texts}")
        return False

    def get_date(self) -> str:
        """Return the device's current date as YYYY-MM-DD."""
        return self.shell("date +%Y-%m-%d").strip()

    def get_epoch_seconds(self) -> int | None:
        """Return the device's current Unix timestamp."""
        value = self.shell("date +%s").strip()
        try:
            return int(value)
        except ValueError:
            logger.warning(f"[{self.name}] Could not read device epoch seconds: {value}")
            return None

    def set_timezone(self, timezone: str) -> None:
        """Best-effort timezone update for Android/BlueStacks."""
        self.shell("settings put global auto_time_zone 0")
        self.shell(f"cmd alarm set-timezone {timezone}")
        self.shell(f"setprop persist.sys.timezone {timezone}")

    def restore_time(self, epoch_seconds: int, timezone: str = "Africa/Cairo", expected_date: str | None = None):
        """Restore the emulator to an exact API-provided timestamp and timezone."""
        epoch_ms = epoch_seconds * 1000

        self.shell("settings put global auto_time 0")
        self.set_timezone(timezone)
        cmd = f"cmd alarm set-time {epoch_ms}"

        for attempt in range(3):
            self.shell(cmd)
            time.sleep(1.5)

            actual_epoch = self.get_epoch_seconds()
            actual_date = self.get_date()
            date_ok = expected_date is None or actual_date == expected_date
            time_ok = actual_epoch is not None and abs(actual_epoch - epoch_seconds) <= 120
            if date_ok and time_ok:
                logger.info(f"[{self.name}] Time restored to {actual_date} {timezone} (cmd: {cmd})")
                return True
            logger.warning(
                f"[{self.name}] Time restore attempt {attempt + 1}/3 failed: "
                f"expected_epoch={epoch_seconds}, actual_epoch={actual_epoch}, "
                f"expected_date={expected_date}, actual_date={actual_date}"
            )

        logger.error(f"[{self.name}] Failed to restore API time after 3 attempts")
        return False

    def restore_date(self, date_str: str):
        """Restore the emulator to a real date using the host PC's current time."""
        now = datetime.datetime.now()
        target = datetime.datetime.fromisoformat(date_str).replace(
            hour=now.hour,
            minute=now.minute,
            second=now.second,
        )
        epoch_ms = int(target.timestamp() * 1000)

        self.shell("settings put global auto_time 0")
        self.shell("settings put global auto_time_zone 0")
        cmd = f"cmd alarm set-time {epoch_ms}"
        self.shell(cmd)
        time.sleep(0.5)

        actual = self.get_date()
        if actual != date_str:
            logger.error(f"[{self.name}] Failed to restore date to {date_str}; actual={actual}; cmd={cmd}")
            return False

        logger.info(f"[{self.name}] Date restored to {date_str} (cmd: {cmd})")
        return True

    def set_date(self, date_str: str):
        """Set the emulator date. date_str must be YYYY-MM-DD."""
        self.shell("settings put global auto_time 0")
        self.shell("settings put global auto_time_zone 0")

        hour = int((self.shell("date +%H") or "12").strip()[:2])
        minute = int((self.shell("date +%M") or "00").strip()[:2])
        second = int((self.shell("date +%S") or "00").strip()[:2])
        offset = (self.shell("date +%z") or "+0200").strip()
        sign = 1 if offset.startswith("+") else -1
        offset_hours = int(offset[1:3])
        offset_minutes = int(offset[3:5])
        tzinfo = datetime.timezone(sign * datetime.timedelta(hours=offset_hours, minutes=offset_minutes))
        target = datetime.datetime.fromisoformat(date_str).replace(
            hour=hour,
            minute=minute,
            second=second,
            tzinfo=tzinfo,
        )
        epoch_ms = int(target.timestamp() * 1000)

        # Plain `date MMDDhhmmYYYY` prints success on BlueStacks but does not
        # persist. Android's alarm service setter does persist for this emulator.
        cmd = f"cmd alarm set-time {epoch_ms}"

        # Retry up to 3 times — alarm service may need time to take effect
        for attempt in range(3):
            self.shell(cmd)
            time.sleep(1.5)

            actual = self.get_date()
            if actual == date_str:
                logger.info(f"[{self.name}] Date verified as {date_str} (cmd: {cmd})")
                return True
            logger.warning(f"[{self.name}] Date set attempt {attempt + 1}/3: expected {date_str}, got {actual}")

        logger.error(f"[{self.name}] Failed to set date to {date_str} after 3 attempts; actual={actual}")
        return False

    @staticmethod
    def discover_devices() -> list[str]:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        adb = ADBController._resolve_adb()

        # First check already-connected devices
        result = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=10)
        found = set()
        online = []
        offline = []
        for line in result.stdout.strip().splitlines()[1:]:
            parts = line.split()
            if len(parts) != 2:
                continue
            # Skip stale emulator-* entries from adb devices
            if parts[0].startswith("emulator-"):
                continue
            if parts[1] == "device":
                online.append(parts[0])
                found.add(parts[0])
            elif parts[1] == "offline":
                offline.append(parts[0])
                found.add(parts[0])

        # Scan every port in range (5500–5700) in parallel for speed
        def try_connect(port):
            addr = f"127.0.0.1:{port}"
            if addr in found:
                return None
            try:
                conn = subprocess.run(
                    [adb, "connect", addr],
                    capture_output=True, text=True, timeout=0.5,
                )
                if "connected" in conn.stdout.lower():
                    return addr
            except (subprocess.TimeoutExpired, Exception):
                pass
            return None

        with ThreadPoolExecutor(max_workers=50) as executor:
            futures = {executor.submit(try_connect, p): p for p in range(5500, 5701)}
            for future in as_completed(futures):
                addr = future.result()
                if addr:
                    logger.info(f"Auto-discovered BlueStacks instance: {addr}")
                    online.append(addr)

        if offline:
            logger.warning(f"Offline ADB device(s) detected but not runnable: {', '.join(offline)}")

        logger.info(f"Discovered {len(online)} online device(s)")
        return online + offline
