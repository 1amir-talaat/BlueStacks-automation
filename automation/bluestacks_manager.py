import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from utils.logger import setup_logger

logger = setup_logger("bluestacks")

BLUESTACKS_DIR = Path(r"C:\Program Files\BlueStacks_nxt")
CONFIG_PATH = Path(r"C:\ProgramData\BlueStacks_nxt\bluestacks.conf")
PLAYER_PATH = BLUESTACKS_DIR / "HD-Player.exe"
MIM_PATH = BLUESTACKS_DIR / "HD-MultiInstanceManager.exe"
ADB_PATH = BLUESTACKS_DIR / "HD-Adb.exe"


@dataclass
class BlueStacksInstance:
    name: str
    display_name: str
    adb_port: int | None

    @property
    def device_id(self) -> str | None:
        return f"127.0.0.1:{self.adb_port}" if self.adb_port else None


class BlueStacksManager:
    def __init__(self, config_path: Path = CONFIG_PATH):
        self.config_path = config_path

    def list_instances(self) -> list[BlueStacksInstance]:
        if not self.config_path.exists():
            logger.warning(f"BlueStacks config not found: {self.config_path}")
            return []

        instances: dict[str, dict[str, str]] = {}
        pattern = re.compile(r'^bst\.instance\.([^.]+)\.([^=]+)="?(.*?)"?$')
        for raw_line in self.config_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            match = pattern.match(raw_line.strip())
            if not match:
                continue
            name, key, value = match.groups()
            instances.setdefault(name, {})[key] = value.strip('"')

        result = []
        for name, values in sorted(instances.items()):
            port = values.get("adb_port") or values.get("status.adb_port")
            try:
                adb_port = int(port) if port else None
            except ValueError:
                adb_port = None
            result.append(
                BlueStacksInstance(
                    name=name,
                    display_name=values.get("display_name") or name,
                    adb_port=adb_port,
                )
            )
        return result

    def start_instance(self, name: str) -> bool:
        if not PLAYER_PATH.exists():
            logger.error(f"HD-Player.exe not found: {PLAYER_PATH}")
            return False
        logger.info(f"Starting BlueStacks instance {name}")
        subprocess.Popen([str(PLAYER_PATH), "--instance", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True

    def wait_for_device(self, device_id: str, timeout: float = 90.0) -> bool:
        if not ADB_PATH.exists():
            logger.error(f"HD-Adb.exe not found: {ADB_PATH}")
            return False

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                subprocess.run([str(ADB_PATH), "connect", device_id], capture_output=True, text=True, timeout=5)
                state = subprocess.run([str(ADB_PATH), "-s", device_id, "get-state"], capture_output=True, text=True, timeout=5)
                if "device" in state.stdout.strip():
                    logger.info(f"BlueStacks ADB ready: {device_id}")
                    return True
            except (subprocess.TimeoutExpired, OSError):
                pass
            time.sleep(3)

        logger.warning(f"Timed out waiting for BlueStacks ADB: {device_id}")
        return False

    def start_and_wait(self, instance: BlueStacksInstance, timeout: float = 90.0) -> bool:
        if not self.start_instance(instance.name):
            return False
        if not instance.device_id:
            logger.warning(f"BlueStacks instance has no ADB port: {instance.name}")
            return False
        return self.wait_for_device(instance.device_id, timeout=timeout)

    def open_multi_instance_manager(self) -> bool:
        if not MIM_PATH.exists():
            logger.error(f"HD-MultiInstanceManager.exe not found: {MIM_PATH}")
            return False
        subprocess.Popen([str(MIM_PATH)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True

    def start_instances(self, names: list[str], delay: float = 3.0) -> int:
        started = 0
        for name in names:
            if self.start_instance(name):
                started += 1
                time.sleep(delay)
        return started
