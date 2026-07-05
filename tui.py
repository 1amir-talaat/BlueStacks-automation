import datetime
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rich.align import Align
from rich import box
from rich.columns import Columns
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from automation.instance_manager import APP_CLASSES
from automation.runtime import build_manager, connect_manager, reset_online_dates, start_tracker, stop_tracker
from utils.logger import LOG_STORE, set_console_logging, setup_logger

logger = setup_logger("tui")

LOG_EXPORT_DIR = Path("logs")


class AutomationTUI:
    def __init__(self):
        self.manager = None
        self.selected = 0
        self.status_rows = []
        self.command_message = "Starting dashboard..."
        self.action_state = "Idle"
        self.log_mode = "selected"
        self.executor = ThreadPoolExecutor(max_workers=6)
        self._status_lock = threading.Lock()
        self._status_refreshing = False
        self._last_status_refresh = 0.0
        self._busy_actions = set()
        self._quit = False

    def run(self):
        import msvcrt

        set_console_logging(False)
        logger.info("BlueStacks Ad Automation TUI starting")
        self.submit_action("discover", self.discover, True)

        with Live(self.render(), refresh_per_second=8, screen=True) as live:
            while not self._quit:
                self._refresh_status_async()
                if msvcrt.kbhit():
                    key = msvcrt.getwch().lower()
                    self.handle_key(key)
                live.update(self.render())
                time.sleep(0.12)

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
                func(*args)
                self.command_message = f"Done: {name}."
            except Exception as e:
                self.command_message = f"Failed {name}: {e}"
                logger.error(f"TUI action '{name}' failed: {e}")
            finally:
                self._busy_actions.discard(name)
                self.action_state = "Idle" if not self._busy_actions else f"Running: {', '.join(sorted(self._busy_actions))}"

        self.executor.submit(wrapped)
        return True

    def discover(self, connect: bool = False):
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
            self.export_selected_logs(copy=True)
            return
        if key == "v":
            self.log_mode = "selected"
            self.command_message = "Log view: selected instance."
            return
        if key == "b":
            self.log_mode = "all"
            self.command_message = "Log view: all instances."
            return
        if key == "w":
            self.log_mode = "problems"
            self.command_message = "Log view: warnings and errors."
            return
        self.command_message = f"Unknown key: {key}"

    def move_selection(self, delta: int):
        count = len(self.status_rows)
        if count:
            self.selected = (self.selected + delta) % count
            row = self.selected_status()
            self.command_message = f"Selected {row.get('name')}."

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
        if start_tracker(tracker):
            self.command_message = f"Started {tracker.adb.name}."

    def stop_selected(self):
        tracker = self.selected_tracker()
        if not tracker:
            self.command_message = "No selected instance."
            return
        self.submit_action(f"stop {tracker.adb.name}", stop_tracker, tracker)

    def reset_dates(self):
        if not self.manager:
            return
        reset_online_dates(self.manager)

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

    def _refresh_status_sync(self, force: bool = False):
        if not self.manager:
            self.status_rows = []
            return
        if not force and time.time() - self._last_status_refresh < 2.0:
            return
        with self._status_lock:
            try:
                self.status_rows = self.manager.status()
                self._last_status_refresh = time.time()
            except Exception as e:
                self.command_message = f"Status refresh failed: {e}"

    def _refresh_status_async(self):
        if self._status_refreshing or not self.manager:
            return
        if time.time() - self._last_status_refresh < 2.0:
            return
        self._status_refreshing = True

        def refresh():
            try:
                self._refresh_status_sync(force=True)
            finally:
                self._status_refreshing = False

        self.executor.submit(refresh)

    def render(self):
        return Padding(
            Group(
                self.hero_panel(),
                Columns([self.instances_panel(), self.selected_panel()], expand=True, equal=False, padding=(0, 1)),
                self.log_panel(),
                self.command_panel(),
            ),
            (0, 1),
        )

    def hero_panel(self):
        total = len(self.status_rows)
        running = sum(1 for row in self.status_rows if row.get("running"))
        online = sum(1 for row in self.status_rows if row.get("online"))
        title = Text()
        title.append("BlueStacks", style="bold bright_cyan")
        title.append(" Automation", style="bold white")
        subtitle = Text("multi-instance ad runner", style="dim")

        metrics = Table.grid(expand=True)
        metrics.add_column(justify="center", ratio=1)
        metrics.add_column(justify="center", ratio=1)
        metrics.add_column(justify="center", ratio=1)
        metrics.add_row(
            self.metric_card("ONLINE", f"{online}/{total}", "green"),
            self.metric_card("RUNNING", str(running), "magenta"),
            self.metric_card("ACTION", self.action_state, "yellow" if self._busy_actions else "cyan"),
        )

        return Panel(
            Group(Align.center(title), Align.center(subtitle), Text(""), metrics),
            border_style="bright_blue",
            box=box.DOUBLE_EDGE,
            padding=(1, 2),
        )

    def metric_card(self, label: str, value: str, color: str):
        text = Text()
        text.append(label + "\n", style="dim")
        text.append(value, style=f"bold {color}")
        return Panel(Align.center(text), border_style=color, box=box.ROUNDED, padding=(0, 1))

    def instances_panel(self):
        table = Table.grid(expand=True)
        table.add_column(ratio=1)
        if not self.status_rows:
            empty = Text("No instances found\n", style="bold yellow")
            empty.append("Start BlueStacks, enable ADB, then press d", style="dim")
            table.add_row(Align.center(empty, vertical="middle"))
            return Panel(table, title="Instances", border_style="cyan", height=14, box=box.ROUNDED)

        for index, row in enumerate(self.status_rows):
            selected = index == self.selected
            card_style = "bright_cyan" if selected else "blue"
            name_style = "bold black on bright_cyan" if selected else "bold white"
            online = self.badge("ONLINE", "green") if row.get("online") else self.badge("OFFLINE", "red")
            running = self.badge("RUNNING", "magenta") if row.get("running") else self.badge("IDLE", "grey50")

            top = Text()
            top.append("▶ " if selected else "  ", style="bold bright_cyan")
            top.append(row.get("name", ""), style=name_style)
            top.append("  ")
            top.append_text(online)
            top.append(" ")
            top.append_text(running)

            bottom = Text()
            bottom.append("app ", style="dim")
            bottom.append(row.get("app") or "none", style="cyan")
            bottom.append("   ads ", style="dim")
            bottom.append(str(row.get("ads_watched", 0)), style="bold green")
            bottom.append("   state ", style="dim")
            bottom.append(row.get("state", ""), style="yellow" if row.get("state") not in ("idle", "stopped") else "dim")
            if row.get("adb_timeouts"):
                bottom.append("   timeouts ", style="dim")
                bottom.append(str(row.get("adb_timeouts")), style="bold red")

            table.add_row(Panel(Group(top, bottom), border_style=card_style, box=box.ROUNDED, padding=(0, 1)))
        return Panel(table, title="Instances", border_style="cyan", height=14, box=box.ROUNDED)

    def badge(self, label: str, color: str) -> Text:
        text = Text()
        text.append(f" {label} ", style=f"bold white on {color}")
        return text

    def selected_panel(self):
        row = self.selected_status()
        if not row:
            return Panel(Align.center("No selection", vertical="middle"), title="Selected", border_style="blue", height=14, box=box.ROUNDED)

        header = Text(row.get("name", ""), style="bold bright_cyan")
        header.append("  ")
        header.append_text(self.badge("ONLINE", "green") if row.get("online") else self.badge("OFFLINE", "red"))

        body = Table.grid(expand=True, padding=(0, 1))
        body.add_column(justify="right", style="dim", width=9)
        body.add_column(ratio=1)
        body.add_row("device", row.get("device_id", ""))
        body.add_row("app", row.get("app") or "none")
        body.add_row("ads", f"[bold green]{row.get('ads_watched', 0)}[/]")
        body.add_row("timeouts", f"[bold red]{row.get('adb_timeouts', 0)}[/]" if row.get("adb_timeouts") else "0")
        body.add_row("worker", "[bold magenta]running[/]" if row.get("running") else "[dim]stopped[/]")
        body.add_row("state", row.get("state", ""))

        hints = Text()
        hints.append("s", style="bold cyan")
        hints.append(" start   ")
        hints.append("x", style="bold cyan")
        hints.append(" stop   ")
        hints.append("t", style="bold cyan")
        hints.append(" switch app")

        return Panel(Group(header, Text(""), body, Text(""), hints), title="Selected Instance", border_style="bright_blue", height=14, box=box.ROUNDED)

    def log_panel(self):
        tracker = self.selected_tracker()
        instance = tracker.adb.name if tracker else None
        rows, title = self.visible_logs(instance)

        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(width=11)
        table.add_column(ratio=1)
        table.add_column(width=12)
        for level, message in rows:
            table.add_row(self.log_badge(level), self.format_log_message(message), self.log_category(message, level))
        if not rows:
            table.add_row("", Text("No logs yet.", style="dim"))
        return Panel(table, title=title, border_style="magenta", height=20, box=box.ROUNDED)

    def visible_logs(self, instance: str | None) -> tuple[list[tuple[str, str]], str]:
        if self.log_mode == "all":
            return LOG_STORE.latest(limit=16), "Live Logs - all instances"
        if self.log_mode == "problems":
            rows = LOG_STORE.latest(limit=250)
            problems = [(level, message) for level, message in rows if level in ("WARNING", "ERROR", "CRITICAL")]
            return problems[-16:], "Live Logs - warnings/errors"
        return (LOG_STORE.latest(instance, limit=16) if instance else LOG_STORE.latest(limit=16), f"Live Logs - {instance or 'system'}")

    def log_badge(self, level: str) -> Text:
        color = "cyan"
        label = level[:4]
        if level == "INFO":
            color = "blue"
            label = "INFO"
        elif level == "WARNING":
            color = "yellow"
            label = "WARN"
        elif level == "ERROR":
            color = "red"
            label = "ERR"
        elif level == "CRITICAL":
            color = "bright_red"
            label = "CRIT"
        return self.badge(label, color)

    def log_category(self, message: str, level: str) -> Text:
        lower = message.lower()
        if level in ("ERROR", "CRITICAL") or "timeout" in lower or "failed" in lower:
            return self.badge("PROBLEM", "red")
        if "connected" in lower or "recovered" in lower:
            return self.badge("ADB", "green")
        if "ad #" in lower or "reward" in lower or "collected" in lower:
            return self.badge("REWARD", "green")
        if "state:" in lower:
            return self.badge("STATE", "blue")
        if "watch now" in lower or "tapping" in lower or "tap" in lower:
            return self.badge("ACTION", "cyan")
        if "google_play" in lower or "redirect" in lower:
            return self.badge("REDIRECT", "magenta")
        if level == "WARNING":
            return self.badge("WARN", "yellow")
        return self.badge("EVENT", "grey50")

    def format_log_message(self, message: str) -> Text:
        text = Text()
        tokens = re.split(r"(\[[^\]]+\]|\bgetsms\b|\btempsms\b|\bState:\s*\w+|\btimeout\b|\bfailed\b|\bConnected\b|\bStarting\b|\bcollected\b|\bWatch Now\b|\bGoogle Play\b|\bgoogle_play\b|\bredirect\b)", message, flags=re.IGNORECASE)
        for token in tokens:
            if not token:
                continue
            lower = token.lower()
            style = "bright_white"
            if token.startswith("[") and token.endswith("]"):
                style = "bold bright_cyan"
            elif lower in ("getsms", "tempsms"):
                style = "bold cyan"
            elif lower.startswith("state:"):
                style = "bold blue"
            elif lower in ("timeout", "failed"):
                style = "bold red"
            elif lower in ("connected", "collected"):
                style = "bold green"
            elif lower in ("starting", "watch now"):
                style = "bold yellow"
            elif lower in ("google play", "google_play", "redirect"):
                style = "bold magenta"
            text.append(token, style=style)
        return text

    def command_panel(self):
        keys = Table.grid(expand=True, padding=(0, 2))
        keys.add_column(ratio=1)
        keys.add_column(ratio=1)
        keys.add_column(ratio=1)
        keys.add_column(ratio=1)
        keys.add_row(
            self.key_hint("d", "discover"),
            self.key_hint("a", "start all"),
            self.key_hint("s", "start"),
            self.key_hint("x", "stop"),
        )
        keys.add_row(
            self.key_hint("t", "switch app"),
            self.key_hint("g", "reset dates"),
            self.key_hint("l", "close app"),
            self.key_hint("n/p", "select"),
        )
        keys.add_row(
            self.key_hint("e", "export selected"),
            self.key_hint("y", "copy selected"),
            self.key_hint("f", "export all"),
            self.key_hint("v/b/w", "logs"),
        )
        keys.add_row("", "", "", self.key_hint("q", "quit"))
        status = Text(self.command_message, style="bold yellow")
        return Panel(Group(keys, Text(""), status), title="Command Bar", border_style="green", box=box.ROUNDED)

    def key_hint(self, key: str, label: str) -> Text:
        text = Text()
        text.append(f" {key} ", style="bold black on bright_cyan")
        text.append(f" {label}", style="white")
        return text

    def shutdown(self):
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
