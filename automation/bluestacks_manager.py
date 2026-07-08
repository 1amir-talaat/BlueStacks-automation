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
    # Extra candidate ports from conf (configured vs runtime status).
    alt_adb_ports: tuple[int, ...] = ()

    @property
    def device_id(self) -> str | None:
        return f"127.0.0.1:{self.adb_port}" if self.adb_port else None

    def candidate_ports(self) -> list[int]:
        ports: list[int] = []
        for port in (self.adb_port, *self.alt_adb_ports):
            if port is not None and port not in ports:
                ports.append(port)
        return ports

    def candidate_device_ids(self) -> list[str]:
        return [f"127.0.0.1:{port}" for port in self.candidate_ports()]

    def with_port(self, port: int) -> "BlueStacksInstance":
        alts = tuple(p for p in self.candidate_ports() if p != port)
        return BlueStacksInstance(
            name=self.name,
            display_name=self.display_name,
            adb_port=port,
            alt_adb_ports=alts,
        )


class BlueStacksManager:
    def __init__(self, config_path: Path = CONFIG_PATH):
        self.config_path = config_path

    @staticmethod
    def _parse_ports(values: dict[str, str]) -> tuple[int | None, tuple[int, ...]]:
        """Return (preferred_port, alternate_ports).

        BlueStacks often stores a configured adb_port and a live status.adb_port
        that differ (e.g. 5555 vs 5556). Prefer the runtime status port first.
        """
        parsed: list[int] = []
        for key in ("status.adb_port", "adb_port"):
            raw = values.get(key)
            if not raw:
                continue
            try:
                port = int(raw)
            except ValueError:
                continue
            if port not in parsed:
                parsed.append(port)
        if not parsed:
            return None, ()
        return parsed[0], tuple(parsed[1:])

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
            preferred, alts = self._parse_ports(values)
            result.append(
                BlueStacksInstance(
                    name=name,
                    display_name=values.get("display_name") or name,
                    adb_port=preferred,
                    alt_adb_ports=alts,
                )
            )
        return result

    def get_instance(self, name: str) -> BlueStacksInstance | None:
        for inst in self.list_instances():
            if inst.name == name:
                return inst
        return None

    def start_instance(self, name: str) -> bool:
        if not PLAYER_PATH.exists():
            logger.error(f"HD-Player.exe not found: {PLAYER_PATH}")
            return False
        logger.info(f"Starting BlueStacks instance {name}")
        subprocess.Popen([str(PLAYER_PATH), "--instance", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True

    def stop_instance(
        self,
        name: str,
        display_name: str | None = None,
        device_id: str | None = None,
    ) -> bool:
        """Best-effort stop of one BlueStacks instance by killing its HD-Player process.

        CommandLine is often unavailable without elevation, so we also match by
        window title (display name) and ADB listen port.
        """
        inst = self.get_instance(name)
        title = display_name or (inst.display_name if inst else name)
        ports: list[int] = list(inst.candidate_ports()) if inst else []
        if device_id and ":" in device_id:
            try:
                port = int(device_id.rsplit(":", 1)[1])
                if port not in ports:
                    ports.append(port)
            except ValueError:
                pass

        pids = self._find_player_pids(name, display_name=title, ports=ports)
        if not pids:
            # Last resort: filter taskkill by exact window title.
            if self._taskkill_by_window_title(title):
                logger.info(f"Stopped BlueStacks instance {name} via window title '{title}'")
                return True
            logger.info(f"No running HD-Player process found for instance {name}")
            return True

        killed = 0
        for pid in pids:
            if self._kill_pid(pid):
                killed += 1
                logger.info(f"Stopped BlueStacks instance {name} (pid {pid})")
        return killed > 0

    def _kill_pid(self, pid: int) -> bool:
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                return True
            detail = (result.stderr or result.stdout or "").strip()
            logger.warning(f"taskkill failed for pid {pid}: {detail}")
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"Failed to kill pid {pid}: {e}")
        return False

    def _taskkill_by_window_title(self, title: str) -> bool:
        if not title:
            return False
        try:
            # Exact window title match (e.g. "BlueStacks App Player").
            result = subprocess.run(
                ["taskkill", "/FI", f"WINDOWTITLE eq {title}", "/IM", "HD-Player.exe", "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            out = (result.stdout or "") + (result.stderr or "")
            return result.returncode == 0 and "SUCCESS" in out.upper()
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"taskkill by window title failed for '{title}': {e}")
            return False

    def _find_player_pids(
        self,
        instance_name: str,
        display_name: str | None = None,
        ports: list[int] | None = None,
    ) -> list[int]:
        """Return HD-Player PIDs for one instance using several matching strategies."""
        player_pids = set(self._all_hd_player_pids())
        found: list[int] = []

        def add(pid: int) -> None:
            # Never kill non-player PIDs that happen to listen on a conf port.
            if pid in player_pids and pid not in found:
                found.append(pid)

        # 1) ADB listen port → owning HD-Player PID (most reliable here).
        for port in ports or []:
            for pid in self._pids_listening_on_port(port):
                add(pid)
        if found:
            return found

        # 2) Exact MainWindowTitle == display name.
        if display_name:
            for pid in self._pids_by_window_title(display_name):
                add(pid)
        if found:
            return found

        # 3) Command line --instance <name> (works when process ACL exposes cmdline).
        for pid in self._pids_by_command_line_instance(instance_name):
            add(pid)
        return found

    def _all_hd_player_pids(self) -> list[int]:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq HD-Player.exe", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (subprocess.TimeoutExpired, OSError):
            return []

        pids: list[int] = []
        for line in result.stdout.splitlines():
            # "HD-Player.exe","38884","Console","1","..."
            parts = [p.strip().strip('"') for p in line.split(",")]
            if len(parts) < 2 or parts[0].lower() != "hd-player.exe":
                continue
            try:
                pids.append(int(parts[1]))
            except ValueError:
                continue
        return pids

    def _pids_listening_on_port(self, port: int) -> list[int]:
        pids: list[int] = []
        try:
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"netstat failed while looking for port {port}: {e}")
            return []

        needle = f"127.0.0.1:{port}"
        for line in result.stdout.splitlines():
            if "LISTENING" not in line.upper() or needle not in line:
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            try:
                pid = int(parts[-1])
            except ValueError:
                continue
            if pid > 0 and pid not in pids:
                pids.append(pid)
        return pids

    def _pids_by_window_title(self, display_name: str) -> list[int]:
        # Escape single quotes for PowerShell string.
        safe = display_name.replace("'", "''")
        ps_script = (
            f"Get-Process HD-Player -ErrorAction SilentlyContinue | "
            f"Where-Object {{ $_.MainWindowTitle -eq '{safe}' }} | "
            f"Select-Object -ExpandProperty Id"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"Could not query HD-Player window titles: {e}")
            return []

        pids: list[int] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pids.append(int(line))
            except ValueError:
                continue
        return pids

    def _pids_by_command_line_instance(self, instance_name: str) -> list[int]:
        """Match --instance <name> with word-boundary so Pie64 does not match Pie64_4."""
        import json

        ps_script = (
            "Get-CimInstance Win32_Process -Filter \"Name = 'HD-Player.exe'\" | "
            "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"Could not query HD-Player processes: {e}")
            return []

        if result.returncode != 0 or not result.stdout.strip():
            return []

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []

        if isinstance(data, dict):
            data = [data]

        # Require exact instance token after --instance (space or =), not a prefix of another name.
        pattern = re.compile(
            rf"(?:--instance[= ]|--instance\s+){re.escape(instance_name)}(?:\s|$)",
            re.IGNORECASE,
        )
        pids: list[int] = []
        for entry in data:
            cmdline = entry.get("CommandLine") or ""
            if not cmdline or not pattern.search(cmdline):
                continue
            try:
                pids.append(int(entry["ProcessId"]))
            except (KeyError, TypeError, ValueError):
                continue
        return pids

    def is_device_online(self, device_id: str) -> bool:
        if not ADB_PATH.exists():
            return False
        try:
            subprocess.run([str(ADB_PATH), "connect", device_id], capture_output=True, text=True, timeout=5)
            state = subprocess.run(
                [str(ADB_PATH), "-s", device_id, "get-state"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return "device" in state.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            return False

    def resolve_online_instance(self, instance: BlueStacksInstance) -> BlueStacksInstance | None:
        """Return a refreshed instance if any of its ADB ports is already online."""
        current = self.get_instance(instance.name) or instance
        for device_id in current.candidate_device_ids():
            if self.is_device_online(device_id):
                port = int(device_id.rsplit(":", 1)[1])
                ready = current.with_port(port)
                logger.info(f"BlueStacks instance online: {ready.display_name} ({ready.device_id})")
                return ready
        return None

    def wait_for_device(self, device_id: str, timeout: float = 90.0) -> bool:
        if not ADB_PATH.exists():
            logger.error(f"HD-Adb.exe not found: {ADB_PATH}")
            return False

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_device_online(device_id):
                logger.info(f"BlueStacks ADB ready: {device_id}")
                return True
            time.sleep(3)

        logger.warning(f"Timed out waiting for BlueStacks ADB: {device_id}")
        return False

    def wait_for_instance(
        self, instance: BlueStacksInstance, timeout: float = 90.0
    ) -> BlueStacksInstance | None:
        """Wait until any known ADB port for the instance is online."""
        if not ADB_PATH.exists():
            logger.error(f"HD-Adb.exe not found: {ADB_PATH}")
            return None

        deadline = time.time() + timeout
        while time.time() < deadline:
            ready = self.resolve_online_instance(instance)
            if ready:
                logger.info(f"BlueStacks instance ready: {ready.display_name} ({ready.device_id})")
                return ready
            time.sleep(3)

        logger.warning(f"Timed out waiting for BlueStacks instance: {instance.display_name}")
        return None

    def start_and_wait(self, instance: BlueStacksInstance, timeout: float = 90.0) -> bool:
        if not self.start_instance(instance.name):
            return False
        return self.wait_for_instance(instance, timeout=timeout) is not None

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
