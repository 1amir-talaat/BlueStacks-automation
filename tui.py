import datetime
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rich.align import Align
from rich import box
from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from automation.batch_runner import BatchRunner
from automation.bluestacks_manager import BlueStacksManager
from automation.instance_manager import APP_CLASSES
from automation.runtime import (
    build_manager,
    connect_manager,
    refresh_manager,
    reset_online_dates,
    start_tracker,
    stop_tracker,
)
from utils.logger import LOG_STORE, set_console_logging, setup_logger

logger = setup_logger("tui")

LOG_EXPORT_DIR = Path("logs")
BLUESTACKS_SETTLE_SECONDS = 20

THEME = {
    "border": "#334155",
    "border_soft": "#1e293b",
    "cyan": "#38bdf8",
    "cyan_soft": "#0e7490",
    "gold": "#f59e0b",
    "green": "#22c55e",
    "red": "#ef4444",
    "purple": "#a78bfa",
    "muted": "#94a3b8",
    "text": "#e5e7eb",
}


class AutomationTUI:
    def __init__(self):
        self.manager = None
        self.bs_manager = BlueStacksManager()
        self.bs_instances = []
        self.selected_bs = 0
        self.batch_runner = BatchRunner(
            bs_manager=self.bs_manager,
            get_manager=lambda: self.manager,
            set_manager=self._set_manager,
            on_message=self._batch_message,
            settle_seconds=BLUESTACKS_SETTLE_SECONDS,
        )
        self.selected = 0
        self.status_rows = []
        self.command_message = "Starting dashboard..."
        self.action_state = "Idle"
        self.log_mode = "selected"
        self.log_follow = True
        self.log_scroll = 0
        self._visible_log_rows = []
        self.executor = ThreadPoolExecutor(max_workers=6)
        self._status_lock = threading.Lock()
        self._status_refreshing = False
        self._last_status_refresh = 0.0
        self._last_render = 0.0
        self._dirty = True
        self._busy_actions = set()
        self._quit = False

    def _set_manager(self, manager):
        self.manager = manager
        self._dirty = True

    def _batch_message(self, message: str):
        self.command_message = message
        self._dirty = True

    def run(self):
        import msvcrt

        set_console_logging(False)
        logger.info("BlueStacks Ad Automation TUI starting")
        self.submit_action("discover", self.discover, True)

        with Live(self.render(), refresh_per_second=4, screen=True, auto_refresh=False) as live:
            while not self._quit:
                self._refresh_status_async()
                handled = 0
                while msvcrt.kbhit() and handled < 25:
                    key = msvcrt.getwch().lower()
                    if key in ("\x00", "\xe0"):
                        key = msvcrt.getwch()
                    self.handle_key(key)
                    handled += 1
                now = time.time()
                self.batch_runner.tick()
                if self._dirty or now - self._last_render >= 0.25:
                    live.update(self.render(), refresh=True)
                    self._last_render = now
                    self._dirty = False
                time.sleep(0.03)

        self.shutdown()

    def submit_action(self, name: str, func, *args):
        if name in self._busy_actions:
            self.command_message = f"{name} is already running."
            return False

        self._busy_actions.add(name)
        self.action_state = f"Running: {name}"
        self.command_message = f"Queued {name}."

        def wrapped():
            try:
                result = func(*args)
                if result is not None:
                    self.command_message = str(result)
                else:
                    self.command_message = f"Done: {name}."
            except Exception as e:
                self.command_message = f"Failed {name}: {e}"
                logger.error(f"TUI action '{name}' failed: {e}")
            finally:
                self._busy_actions.discard(name)
                self.action_state = "Idle" if not self._busy_actions else f"Running: {', '.join(sorted(self._busy_actions))}"
                self._dirty = True

        self.executor.submit(wrapped)
        self._dirty = True
        return True

    def discover(self, connect: bool = False):
        self.bs_instances = self.bs_manager.list_instances()
        keep_alive = (
            self.batch_runner.active
            or (self.manager is not None and self.manager.has_running())
        )
        if keep_alive and self.manager is not None:
            # Never rebuild trackers while automation or a batch is live.
            self.manager = refresh_manager(self.manager, connect=connect)
            self.command_message = "Rediscovered instances (kept live trackers)."
        else:
            self.manager = build_manager()
            self.selected = 0
            if connect:
                self.connect_all()
        self._refresh_status_sync(force=True)

    def connect_all(self):
        if not self.manager:
            self.manager = build_manager()
        online = connect_manager(self.manager)
        connected = sum(1 for ok in online.values() if ok)
        self.command_message = f"Connected {connected}/{len(online)} instance(s)."

    def selected_tracker(self):
        if not self.manager:
            return None
        trackers = self.manager.get_all()
        if not trackers:
            return None
        self.selected = max(0, min(self.selected, len(trackers) - 1))
        return trackers[self.selected]

    def selected_status(self):
        if not self.status_rows:
            return None
        self.selected = max(0, min(self.selected, len(self.status_rows) - 1))
        return self.status_rows[self.selected]

    def handle_key(self, key: str):
        self._dirty = True
        if key == "q":
            self.command_message = "Shutting down..."
            self._quit = True
            return
        if key == "d":
            self.submit_action("discover", self.discover, True)
            return
        if key == "c":
            self.submit_action("connect", self.connect_all)
            return
        if key == "m":
            self.submit_action("start selected BlueStacks", self.start_selected_bluestacks)
            return
        if key == "h":
            self.submit_action("start all BlueStacks", self.start_all_bluestacks)
            return
        if key == "j":
            self.move_bluestacks_selection(1)
            return
        if key == "k":
            self.move_bluestacks_selection(-1)
            return
        if key == "z":
            self.submit_action("open multi-instance manager", self.bs_manager.open_multi_instance_manager)
            return
        if key in ("1", "2", "3", "4", "5", "6"):
            self.start_batch(int(key))
            return
        if key == "0":
            if self.batch_runner.active:
                self.submit_action("stop batch", self.batch_runner.stop)
            else:
                self.command_message = "No batch is running."
            return
        if key == "n":
            self.move_selection(1)
            return
        if key == "p":
            self.move_selection(-1)
            return
        if key == "r":
            self._refresh_status_sync(force=True)
            self.command_message = "Status refreshed."
            return
        if key == "a":
            self.start_all()
            return
        if key == "s":
            self.start_selected()
            return
        if key == "x":
            self.stop_selected()
            return
        if key == "g":
            self.submit_action("reset dates", self.reset_dates)
            return
        if key == "t":
            self.toggle_app()
            return
        if key == "l":
            self.close_selected_app()
            return
        if key == "e":
            self.export_selected_logs(copy=False)
            return
        if key == "f":
            self.export_all_logs(copy=False)
            return
        if key == "y":
            self.copy_current_logs(full=True)
            return
        if key == "u":
            self.copy_current_logs(full=False)
            return
        if key == "o":
            self.log_follow = not self.log_follow
            if self.log_follow:
                self.log_scroll = 0
            self.command_message = "Log follow on." if self.log_follow else "Log follow paused."
            return
        if key == "v":
            self.log_mode = "selected"
            self.log_scroll = 0
            self.log_follow = True
            self.command_message = "Log view: selected instance."
            return
        if key == "b":
            self.log_mode = "all"
            self.log_scroll = 0
            self.log_follow = True
            self.command_message = "Log view: all instances."
            return
        if key == "w":
            self.log_mode = "problems"
            self.log_scroll = 0
            self.log_follow = True
            self.command_message = "Log view: warnings and errors."
            return
        if key in ("H", "P", "I", "Q", "G", "O"):
            self.handle_special_key(key)
            return
        self.command_message = f"Unknown key: {key}"

    def handle_special_key(self, key: str):
        # Windows msvcrt extended keys: H up, P down, I page up, Q page down, G home, O end.
        if key == "H":
            self.scroll_logs(1)
        elif key == "P":
            self.scroll_logs(-1)
        elif key == "I":
            self.scroll_logs(10)
        elif key == "Q":
            self.scroll_logs(-10)
        elif key == "G":
            self.scroll_logs(9999)
        elif key == "O":
            self.log_scroll = 0
            self.log_follow = True
            self.command_message = "Jumped to newest logs."

    def scroll_logs(self, amount: int):
        total = len(self.current_log_rows())
        max_scroll = max(0, total - 1)
        self.log_follow = False
        self.log_scroll = max(0, min(max_scroll, self.log_scroll + amount))
        self.command_message = f"Log scroll: {self.log_scroll} row(s) from newest."

    def move_selection(self, delta: int):
        count = len(self.status_rows)
        if count:
            self.selected = (self.selected + delta) % count
            row = self.selected_status()
            self.command_message = f"Selected {row.get('name')}."

    def move_bluestacks_selection(self, delta: int):
        if not self.bs_instances:
            self.bs_instances = self.bs_manager.list_instances()
        count = len(self.bs_instances)
        if count:
            self.selected_bs = (self.selected_bs + delta) % count
            inst = self.bs_instances[self.selected_bs]
            self.command_message = f"Selected BlueStacks {inst.display_name} ({inst.name})."

    def start_selected_bluestacks(self):
        if not self.bs_instances:
            self.bs_instances = self.bs_manager.list_instances()
        if not self.bs_instances:
            self.command_message = "No BlueStacks instances found."
            return
        inst = self.bs_instances[self.selected_bs]
        self.command_message = f"Starting {inst.display_name}; waiting for ADB..."
        self.bs_manager.start_instance(inst.name)
        ready_inst = self.bs_manager.wait_for_instance(inst)
        if ready_inst:
            self.wait_for_bluestacks_settle([ready_inst])
            self.command_message = f"{ready_inst.display_name} is ready on {ready_inst.device_id}."
            self.discover(connect=True)
        else:
            self.command_message = f"Timed out waiting for {inst.display_name}."

    def start_all_bluestacks(self):
        if not self.bs_instances:
            self.bs_instances = self.bs_manager.list_instances()
        ready = 0
        ready_instances = []
        for inst in self.bs_instances:
            self.command_message = f"Starting {inst.display_name}; waiting for ADB..."
            self.bs_manager.start_instance(inst.name)
            ready_inst = self.bs_manager.wait_for_instance(inst)
            if ready_inst:
                ready += 1
                ready_instances.append(ready_inst)
        self.wait_for_bluestacks_settle(ready_instances)
        self.command_message = f"Ready {ready}/{len(self.bs_instances)} BlueStacks instance(s)."
        self.discover(connect=True)

    def start_batch(self, concurrency: int):
        if self.batch_runner.active:
            self.command_message = "A batch is already running. Press 0 to stop it."
            return
        # start() is non-blocking for long work (fill runs on its own thread)
        message = self.batch_runner.start(concurrency)
        self.command_message = message
        self.bs_instances = self.bs_manager.list_instances()
        self._dirty = True

    def find_bluestacks_by_device(self, device_id: str):
        if not self.bs_instances:
            self.bs_instances = self.bs_manager.list_instances()
        for inst in self.bs_instances:
            if inst.device_id == device_id:
                return inst
        return None

    def start_all(self):
        if not self.manager:
            self.submit_action("discover", self.discover, True)
            return
        started = 0
        skipped = 0
        for tracker in self.manager.get_all():
            if tracker.running:
                skipped += 1
                continue
            if start_tracker(tracker):
                started += 1
        self.command_message = f"Start all: {started} started, {skipped} already running."

    def start_selected(self):
        tracker = self.selected_tracker()
        if not tracker:
            self.command_message = "No selected instance."
            return
        if tracker.running:
            self.command_message = f"{tracker.adb.name} is already running."
            return
        if not tracker.adb.is_online():
            bs_inst = self.find_bluestacks_by_device(tracker.adb.device_id)
            if not bs_inst:
                self.command_message = f"{tracker.adb.name} is offline and has no BlueStacks mapping."
                return
            self.command_message = f"Starting {bs_inst.display_name}; waiting for ADB..."
            self.bs_manager.start_instance(bs_inst.name)
            ready_inst = self.bs_manager.wait_for_instance(bs_inst)
            if not ready_inst:
                self.command_message = f"Timed out waiting for {bs_inst.display_name}."
                return
            self.wait_for_bluestacks_settle([ready_inst])
            tracker.adb.connect()
        if start_tracker(tracker):
            self.command_message = f"Started {tracker.adb.name}."

    def wait_for_bluestacks_settle(self, instances):
        if not instances:
            return
        names = ", ".join(inst.display_name for inst in instances)
        for remaining in range(BLUESTACKS_SETTLE_SECONDS, 0, -1):
            self.command_message = f"Waiting {remaining}s for BlueStacks to finish loading: {names}"
            time.sleep(1)

    def stop_selected(self):
        tracker = self.selected_tracker()
        if not tracker:
            self.command_message = "No selected instance."
            return
        if not tracker.running and not tracker.stop_event.is_set():
            self.command_message = f"{tracker.adb.name} is already stopped."
            return
        self.command_message = f"Stopping {tracker.adb.name}..."
        self.submit_action(f"stop {tracker.adb.name}", self._stop_tracker_and_refresh, tracker)

    def _stop_tracker_and_refresh(self, tracker):
        stop_tracker(tracker)
        self._refresh_status_sync(force=True)
        if tracker.running:
            return f"{tracker.adb.name} is stopping..."
        return f"Stopped {tracker.adb.name}."

    def reset_dates(self):
        if not self.manager:
            return
        if not reset_online_dates(self.manager):
            return "Date reset blocked: external API time could not be applied to every instance."
        return "All online emulator clocks synchronized from external API time."

    def toggle_app(self):
        tracker = self.selected_tracker()
        if not tracker:
            self.command_message = "No selected instance."
            return
        if tracker.running:
            self.command_message = f"Stop {tracker.adb.name} before switching apps."
            return
        app_keys = list(APP_CLASSES)
        current = tracker.app_key if tracker.app_key in app_keys else app_keys[0]
        next_app = app_keys[(app_keys.index(current) + 1) % len(app_keys)]
        tracker.assign_app(next_app)
        self.command_message = f"Assigned {tracker.adb.name} to {next_app}."
        self._refresh_status_sync(force=True)

    def close_selected_app(self):
        tracker = self.selected_tracker()
        if not tracker or not tracker.app:
            self.command_message = "No app selected to close."
            return
        self.submit_action(f"close {tracker.adb.name}", tracker.adb.close_app, tracker.app.PACKAGE_NAME)

    def export_selected_logs(self, copy: bool = False):
        tracker = self.selected_tracker()
        instance = tracker.adb.name if tracker else None
        if not instance:
            self.command_message = "No selected instance logs to export."
            return
        path = self._write_logs(instance, LOG_STORE.latest(instance, limit=400))
        if copy:
            self._copy_file_to_clipboard(path)
            self.command_message = f"Copied {instance} logs to clipboard and saved {path}."
        else:
            self.command_message = f"Exported {instance} logs to {path}."

    def export_all_logs(self, copy: bool = False):
        path = self._write_logs("all", LOG_STORE.latest(limit=1200))
        if copy:
            self._copy_file_to_clipboard(path)
            self.command_message = f"Copied all logs to clipboard and saved {path}."
        else:
            self.command_message = f"Exported all logs to {path}."

    def copy_current_logs(self, full: bool):
        rows = self.current_log_rows()
        if not full:
            rows = self._visible_log_rows
        if not rows:
            self.command_message = "No logs to copy."
            return

        name = f"{self.log_mode}-visible" if not full else f"{self.log_mode}-full"
        path = self._write_logs(name, rows)
        self._copy_file_to_clipboard(path)
        scope = "full current log view" if full else "visible log rows"
        self.command_message = f"Copied {scope} to clipboard and saved {path}."

    def _write_logs(self, name: str, rows: list[tuple[str, str]]) -> Path:
        LOG_EXPORT_DIR.mkdir(exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = LOG_EXPORT_DIR / f"{name}-{stamp}.log"
        path.write_text("\n".join(message for _, message in rows), encoding="utf-8")
        return path

    def _copy_file_to_clipboard(self, path: Path):
        try:
            text = path.read_text(encoding="utf-8")
            subprocess.run("clip", input=text, text=True, check=True, timeout=5)
        except Exception as e:
            self.command_message = f"Saved {path}, clipboard copy failed: {e}"

    def current_log_rows(self) -> list[tuple[str, str]]:
        tracker = self.selected_tracker()
        instance = tracker.adb.name if tracker else None
        if self.log_mode == "all":
            return LOG_STORE.latest(limit=1200)
        if self.log_mode == "problems":
            rows = LOG_STORE.latest(limit=1200)
            return [(level, message) for level, message in rows if level in ("WARNING", "ERROR", "CRITICAL")]
        return LOG_STORE.latest(instance, limit=400) if instance else LOG_STORE.latest(limit=400)

    def _refresh_status_sync(self, force: bool = False):
        if not self.manager:
            self.status_rows = []
            return
        if not force and time.time() - self._last_status_refresh < 5.0:
            return
        with self._status_lock:
            try:
                self.status_rows = self.manager.status()
                self._last_status_refresh = time.time()
                self._dirty = True
            except Exception as e:
                self.command_message = f"Status refresh failed: {e}"

    def _refresh_status_async(self):
        if self._status_refreshing or not self.manager:
            return
        if time.time() - self._last_status_refresh < 5.0:
            return
        self._status_refreshing = True

        def refresh():
            try:
                self._refresh_status_sync(force=True)
            finally:
                self._status_refreshing = False

        self.executor.submit(refresh)

    def render(self):
        layout = Layout(name="root")
        layout.split_column(
            Layout(self.hero_panel(), name="header", size=6),
            Layout(name="body", ratio=1),
            Layout(self.status_bar(), name="status", size=4),
        )
        layout["body"].split_row(
            Layout(self.instances_panel(), name="instances", size=34),
            Layout(self.log_panel(), name="logs", ratio=1, minimum_size=80),
            Layout(name="side", size=38),
        )
        layout["side"].split_column(
            Layout(self.selected_panel(), name="selected", size=17),
            Layout(self.bluestacks_panel(), name="bluestacks", ratio=1),
            Layout(self.command_panel(), name="commands", size=17),
        )
        return Padding(layout, (0, 1))

    def hero_panel(self):
        total = len(self.status_rows)
        running = sum(1 for row in self.status_rows if row.get("running"))
        online = sum(1 for row in self.status_rows if row.get("online"))
        metrics = Table.grid(expand=True, padding=(0, 2))
        metrics.add_column(ratio=2)
        metrics.add_column(justify="center", ratio=1)
        metrics.add_column(justify="center", ratio=1)
        metrics.add_column(justify="center", ratio=2)

        title = Text()
        title.append("BlueStacks", style=f"bold {THEME['cyan']}")
        title.append(" Automation", style=f"bold {THEME['text']}")
        title.append("\n")
        title.append("multi-instance ad runner", style="dim")
        metrics.add_row(
            title,
            self.metric_card("ONLINE", f"{online}/{total}", THEME["green"]),
            self.metric_card("RUNNING", str(running), THEME["purple"]),
            self.metric_card("ACTION", self.action_state, THEME["gold"] if self._busy_actions else THEME["cyan"]),
        )

        return Panel(metrics, border_style=THEME["border"], box=box.ROUNDED, padding=(0, 1))

    def metric_card(self, label: str, value: str, color: str):
        text = Text()
        text.append(label + "  ", style=THEME["muted"])
        text.append(value, style=f"bold {color}")
        return Align.center(text)

    def instances_panel(self):
        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(ratio=1)
        if not self.status_rows:
            empty = Text("No instances found\n", style=f"bold {THEME['gold']}")
            empty.append("Start BlueStacks, enable ADB, then press d", style=THEME["muted"])
            table.add_row(Align.center(empty, vertical="middle"))
            return Panel(table, title="Instances", border_style=THEME["border"], box=box.ROUNDED)

        for index, row in enumerate(self.status_rows):
            selected = index == self.selected
            name_style = f"bold {THEME['cyan']}" if selected else f"bold {THEME['text']}"
            online = self.badge("ONLINE", THEME["green"]) if row.get("online") else self.badge("OFFLINE", THEME["red"])
            running = self.badge("RUNNING", THEME["purple"]) if row.get("running") else self.badge("IDLE", "#475569")

            line = Text()
            line.append("▌ " if selected else "  ", style=f"bold {THEME['gold']}")
            line.append(row.get("name", ""), style=name_style)
            line.append("\n   ")
            line.append_text(online)
            line.append(" ")
            line.append_text(running)
            line.append("\n   ")
            line.append(row.get("app") or "none", style=THEME["cyan"])
            line.append("  ads ", style=THEME["muted"])
            line.append(str(row.get("ads_watched", 0)), style=f"bold {THEME['green']}")
            ads_by_app = row.get("ads_by_app") or {}
            if ads_by_app:
                line.append(f"  g:{ads_by_app.get('getsms', 0)} t:{ads_by_app.get('tempsms', 0)}", style=THEME["muted"])
            state = row.get("state", "")
            if row.get("online") or state not in ("error: device offline", "error: device not found", "device offline"):
                line.append("\n   ")
                line.append(state, style=THEME["gold"] if state not in ("idle", "stopped") else THEME["muted"])
            if row.get("adb_timeouts"):
                line.append("  timeouts ", style=THEME["muted"])
                line.append(str(row.get("adb_timeouts")), style=f"bold {THEME['red']}")

            table.add_row(line)
            table.add_row(Text("─" * 28, style=THEME["border_soft"]))
        return Panel(table, title="Instances", border_style=THEME["border"], box=box.ROUNDED)

    def badge(self, label: str, color: str) -> Text:
        text = Text()
        text.append(f" {label} ", style=f"bold #020617 on {color}")
        return text

    def selected_panel(self):
        row = self.selected_status()
        if not row:
            return Panel(Align.center("No selection", vertical="middle"), title="Selected", border_style=THEME["border"], box=box.ROUNDED)

        header = Text(row.get("name", ""), style=f"bold {THEME['cyan']}")
        header.append("  ")
        header.append_text(self.badge("ONLINE", THEME["green"]) if row.get("online") else self.badge("OFFLINE", THEME["red"]))

        body = Table.grid(expand=True, padding=(0, 1))
        body.add_column(justify="right", style=THEME["muted"], width=9)
        body.add_column(ratio=1)
        body.add_row("device", row.get("device_id", ""))
        body.add_row("app", row.get("app") or "none")
        body.add_row("ads", f"[bold {THEME['green']}]{row.get('ads_watched', 0)}[/]")
        ads_by_app = row.get("ads_by_app") or {}
        body.add_row("getsms", str(ads_by_app.get("getsms", 0)))
        body.add_row("tempsms", str(ads_by_app.get("tempsms", 0)))
        body.add_row("timeouts", f"[bold {THEME['red']}]{row.get('adb_timeouts', 0)}[/]" if row.get("adb_timeouts") else "0")
        body.add_row("worker", f"[bold {THEME['purple']}]running[/]" if row.get("running") else f"[{THEME['muted']}]stopped[/]")
        state = row.get("state", "")
        clean_state = "offline" if not row.get("online") and "device" in state else state
        body.add_row("state", clean_state)

        flow = Table.grid(expand=True, padding=(0, 1))
        flow.add_column(ratio=1)
        flow.add_row(Text("Flow", style=f"bold {THEME['gold']}"))
        flow.add_row(Text("start -> watch -> ad -> reward", style=THEME["muted"]))
        flow.add_row(Text(""))
        flow.add_row(Text("Health", style=f"bold {THEME['gold']}"))
        health = "clean" if not row.get("adb_timeouts") else "adb timeouts detected"
        flow.add_row(Text(health, style=THEME["green"] if health == "clean" else THEME["red"]))

        return Panel(Group(header, Text(""), body, Text(""), flow), title="Selected", border_style=THEME["border"], box=box.ROUNDED)

    def bluestacks_panel(self):
        if not self.bs_instances:
            self.bs_instances = self.bs_manager.list_instances()
        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(ratio=1)
        if not self.bs_instances:
            table.add_row(Text("No installed instances found", style=THEME["muted"]))
        else:
            online_devices = {row.get("device_id") for row in self.status_rows if row.get("online")}
            for index, inst in enumerate(self.bs_instances):
                selected = index == self.selected_bs
                line = Text()
                line.append("▌ " if selected else "  ", style=f"bold {THEME['gold']}")
                line.append(inst.display_name, style=f"bold {THEME['cyan']}" if selected else THEME["text"])
                line.append("\n   ")
                line.append(inst.name, style=THEME["muted"])
                if inst.device_id:
                    line.append(f"  {inst.device_id}", style=THEME["muted"])
                    if inst.device_id in online_devices:
                        line.append("  online", style=f"bold {THEME['green']}")
                table.add_row(line)
        return Panel(table, title="BlueStacks", border_style=THEME["border"], box=box.ROUNDED)

    def log_panel(self):
        tracker = self.selected_tracker()
        instance = tracker.adb.name if tracker else None
        rows, title = self.visible_logs(instance)
        self._visible_log_rows = rows

        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(width=7)
        table.add_column(width=9)
        table.add_column(width=10)
        table.add_column(ratio=1)
        table.add_column(width=11)
        for level, message in rows:
            timestamp, source, body = self.parse_log_message(message)
            table.add_row(
                self.log_badge(level),
                Text(timestamp, style=THEME["muted"]),
                Text(source, style=THEME["muted"]),
                self.format_log_message(body),
                self.log_category(body, level),
            )
        if not rows:
            table.add_row("", Text("No logs yet.", style="dim"))
        return Panel(table, title=title, border_style=THEME["border"], box=box.ROUNDED)

    def parse_log_message(self, message: str) -> tuple[str, str, str]:
        parts = [part.strip() for part in message.split(" | ", 3)]
        if len(parts) == 4:
            return parts[0], parts[2], parts[3]
        return "", "", message

    def visible_logs(self, instance: str | None) -> tuple[list[tuple[str, str]], str]:
        rows = self.current_log_rows()
        visible_count = 55
        if self.log_follow:
            self.log_scroll = 0
        max_scroll = max(0, len(rows) - visible_count)
        self.log_scroll = max(0, min(max_scroll, self.log_scroll))
        end = len(rows) - self.log_scroll
        start = max(0, end - visible_count)
        visible = rows[start:end]

        if self.log_mode == "all":
            title = "Live Logs - all instances"
        elif self.log_mode == "problems":
            title = "Live Logs - warnings/errors"
        else:
            title = f"Live Logs - {instance or 'system'}"
        marker = "follow" if self.log_follow else f"scroll {self.log_scroll}"
        return visible, f"{title} ({marker}, {len(rows)} total)"

    def log_badge(self, level: str) -> Text:
        color = "cyan"
        label = level[:4]
        if level == "INFO":
            color = THEME["cyan_soft"]
            label = "INFO"
        elif level == "WARNING":
            color = THEME["gold"]
            label = "WARN"
        elif level == "ERROR":
            color = THEME["red"]
            label = "ERR"
        elif level == "CRITICAL":
            color = "#f87171"
            label = "CRIT"
        return self.badge(label, color)

    def log_category(self, message: str, level: str) -> Text:
        lower = message.lower()
        if level in ("ERROR", "CRITICAL") or "timeout" in lower or "failed" in lower:
            return self.badge("PROBLEM", THEME["red"])
        if "connected" in lower or "recovered" in lower:
            return self.badge("ADB", THEME["green"])
        if "ad #" in lower or "reward" in lower or "collected" in lower:
            return self.badge("REWARD", THEME["green"])
        if "state:" in lower:
            return self.badge("STATE", THEME["cyan_soft"])
        if "watch now" in lower or "tapping" in lower or "tap" in lower:
            return self.badge("ACTION", THEME["cyan"])
        if "google_play" in lower or "redirect" in lower:
            return self.badge("REDIRECT", THEME["purple"])
        if level == "WARNING":
            return self.badge("WARN", THEME["gold"])
        return self.badge("EVENT", "grey50")

    def format_log_message(self, message: str) -> Text:
        lower = message.lower()
        style = THEME["text"]
        if "timeout" in lower or "failed" in lower:
            style = f"bold {THEME['red']}"
        elif "collected" in lower or "connected" in lower:
            style = f"bold {THEME['green']}"
        elif "watch now" in lower or "starting" in lower:
            style = f"bold {THEME['gold']}"
        elif "state:" in lower:
            style = f"bold {THEME['cyan_soft']}"
        elif "google_play" in lower or "redirect" in lower:
            style = f"bold {THEME['purple']}"
        return Text(message, style=style, overflow="fold")

    def command_panel(self):
        keys = Table.grid(expand=True, padding=(0, 1))
        keys.add_column(ratio=1)
        for key, label in (
            ("s", "start selected"),
            ("x", "stop selected"),
            ("a", "start all"),
            ("t", "switch app"),
            ("g", "reset dates"),
            ("l", "close app"),
            ("n/p", "select instance"),
            ("v/b/w", "log views"),
            ("↑/↓", "scroll logs"),
            ("PgUp/PgDn", "page logs"),
            ("o", "pause/follow logs"),
            ("u/y", "copy visible/full"),
            ("e/f", "export logs"),
            ("j/k", "select BS"),
            ("m/h", "start BS/all"),
            ("1-6", "batch N-at-a-time"),
            ("0", "stop batch"),
            ("z", "multi-instance mgr"),
            ("d/c", "discover/connect"),
            ("q", "quit"),
        ):
            keys.add_row(self.key_hint(key, label))
        return Panel(keys, title="Actions", border_style=THEME["border"], box=box.ROUNDED)

    def status_bar(self):
        text = Text()
        text.append(" STATUS ", style=f"bold #020617 on {THEME['gold']}")
        text.append(" ")
        text.append(self.command_message, style=f"bold {THEME['gold']}")
        text.append("   ")
        text.append(" BATCH ", style=f"bold #020617 on {THEME['green']}")
        text.append(f" {self.batch_runner.status_text()}", style=THEME["green"])
        text.append("   ")
        text.append(" LOGS ", style=f"bold #020617 on {THEME['purple']}")
        text.append(f" {self.log_mode}", style=THEME["purple"])
        text.append("   ")
        text.append(" SHORTCUTS ", style=f"bold #020617 on {THEME['cyan']}")
        text.append(" 1-6 batch  0 stop batch  m start BS  q quit", style=THEME["cyan"])
        return Panel(text, border_style=THEME["border"], box=box.ROUNDED)

    def key_hint(self, key: str, label: str) -> Text:
        text = Text()
        text.append(f" {key} ", style=f"bold #020617 on {THEME['cyan']}")
        text.append(f" {label}", style=THEME["text"])
        return text

    def shutdown(self):
        try:
            if self.batch_runner.active:
                self.batch_runner.stop()
        except Exception:
            pass
        if self.manager:
            for tracker in self.manager.get_all():
                tracker.request_stop()
            try:
                self.manager.disconnect_all()
            except Exception:
                pass
        self.executor.shutdown(wait=False, cancel_futures=True)
        set_console_logging(True)


def run_tui():
    AutomationTUI().run()
