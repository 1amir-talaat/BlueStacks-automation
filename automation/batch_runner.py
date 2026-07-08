"""Queue-based multi-instance batch orchestrator.

Keys 1 / 2 in the TUI start this runner with concurrency 1 or 2:
  - Fill up to N BlueStacks instances at a time
  - Run full ad automation on each until the worker exits
  - Tear down the finished VM and start the next pending instance
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from automation.bluestacks_manager import BlueStacksInstance, BlueStacksManager
from automation.instance_manager import InstanceManager, InstanceTracker
from automation.runtime import default_app_assignments, refresh_manager, start_tracker, stop_tracker
from utils.logger import setup_logger

logger = setup_logger("batch")

DEFAULT_SETTLE_SECONDS = 20


class SlotState(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class BatchSlot:
    instance: BlueStacksInstance
    state: SlotState = SlotState.STARTING
    device_id: str | None = None
    tracker_name: str | None = None
    error: str = ""
    started_at: float = field(default_factory=time.time)


class BatchRunner:
    """Orchestrate sequential/parallel BlueStacks automation slots."""

    def __init__(
        self,
        bs_manager: BlueStacksManager,
        get_manager: Callable[[], InstanceManager | None],
        set_manager: Callable[[InstanceManager], None],
        on_message: Callable[[str], None] | None = None,
        settle_seconds: int = DEFAULT_SETTLE_SECONDS,
    ):
        self.bs_manager = bs_manager
        self._get_manager = get_manager
        self._set_manager = set_manager
        self._on_message = on_message or (lambda _msg: None)
        self.settle_seconds = settle_seconds

        self._lock = threading.RLock()
        self._worker_lock = threading.Lock()
        self._active = False
        self._stopping = False
        self._fill_in_progress = False
        self._concurrency = 1
        self._queue: list[BlueStacksInstance] = []
        self._slots: dict[str, BatchSlot] = {}
        self._completed: list[str] = []
        self._failed: list[str] = []
        self._total = 0

    @property
    def active(self) -> bool:
        return self._active

    @property
    def concurrency(self) -> int:
        return self._concurrency

    def status_text(self) -> str:
        with self._lock:
            if not self._active and not self._completed and not self._failed:
                return "batch idle"
            done = len(self._completed) + len(self._failed)
            running = sum(1 for s in self._slots.values() if s.state in (SlotState.STARTING, SlotState.RUNNING))
            pending = len(self._queue)
            mode = "serial" if self._concurrency == 1 else f"x{self._concurrency}"
            parts = [f"batch {mode}: {done}/{self._total} finished"]
            if running:
                names = [
                    s.instance.display_name
                    for s in self._slots.values()
                    if s.state in (SlotState.STARTING, SlotState.RUNNING)
                ]
                parts.append(f"{running} active ({', '.join(names)})")
            if pending:
                parts.append(f"{pending} queued")
            if self._failed:
                parts.append(f"{len(self._failed)} failed")
            if self._stopping:
                parts.append("stopping")
            elif not self._active and self._total:
                parts.append("complete")
            return " | ".join(parts)

    def start(self, concurrency: int = 1) -> str:
        with self._lock:
            if self._active:
                return "A batch is already running. Stop it before starting another."

            manager = self._get_manager()
            if manager and manager.has_running():
                running = [t.adb.name for t in manager.get_all() if t.running]
                return f"Stop running automation first: {', '.join(running)}"

            instances = self.bs_manager.list_instances()
            if not instances:
                return "No BlueStacks instances found."

            self._concurrency = max(1, int(concurrency))
            self._queue = list(instances)
            self._slots = {}
            self._completed = []
            self._failed = []
            self._total = len(instances)
            self._active = True
            self._stopping = False
            self._fill_in_progress = False

            msg = (
                f"Batch started ({self._concurrency} at a time) with {self._total} instance(s)."
            )
            logger.info(msg)
            self._on_message(msg)

        self._schedule_fill()
        return msg

    def stop(self) -> None:
        with self._lock:
            if not self._active and not self._slots:
                return
            self._stopping = True
            self._queue.clear()
            slots = list(self._slots.values())

        logger.info("Batch stop requested")
        for slot in slots:
            self._teardown_slot(slot, mark_failed=False, reason="batch stopped")

        with self._lock:
            self._slots.clear()
            self._active = False
            self._stopping = False
            self._fill_in_progress = False
        self._on_message("Batch stopped.")

    def tick(self) -> None:
        """Poll active slots; free finished ones and refill concurrency."""
        if not self._active and not self._slots:
            return

        finished: list[BatchSlot] = []
        with self._lock:
            if self._stopping:
                return
            for slot in list(self._slots.values()):
                if slot.state != SlotState.RUNNING:
                    continue
                # Worker sets running=True after the thread starts; avoid false "finished".
                if time.time() - slot.started_at < 5.0:
                    continue
                tracker = self._resolve_tracker(slot)
                if tracker is None:
                    slot.state = SlotState.FAILED
                    slot.error = "tracker missing"
                    finished.append(slot)
                    continue
                if not tracker.running:
                    # Prefer thread exit when available.
                    thread = tracker.thread
                    if thread is not None and thread.is_alive():
                        continue
                    slot.state = SlotState.DONE
                    finished.append(slot)

        for slot in finished:
            self._finalize_finished_slot(slot)

        if self._active and not self._stopping:
            self._schedule_fill()

        with self._lock:
            if self._active and not self._queue and not self._slots and not self._fill_in_progress:
                self._active = False
                msg = (
                    f"Batch complete: {len(self._completed)} done"
                    + (f", {len(self._failed)} failed" if self._failed else "")
                    + f" of {self._total}."
                )
                logger.info(msg)
                self._on_message(msg)

    def _schedule_fill(self) -> None:
        with self._lock:
            if not self._active or self._stopping or self._fill_in_progress:
                return
            open_slots = self._concurrency - len(self._slots)
            if open_slots <= 0 or not self._queue:
                return
            self._fill_in_progress = True

        thread = threading.Thread(target=self._fill_slots_worker, daemon=True, name="batch-fill")
        thread.start()

    def _fill_slots_worker(self) -> None:
        if not self._worker_lock.acquire(blocking=False):
            with self._lock:
                self._fill_in_progress = False
            return
        try:
            while True:
                with self._lock:
                    if not self._active or self._stopping:
                        break
                    open_slots = self._concurrency - len(self._slots)
                    if open_slots <= 0 or not self._queue:
                        break
                    inst = self._queue.pop(0)
                    slot = BatchSlot(instance=inst, state=SlotState.STARTING)
                    self._slots[inst.name] = slot
                    self._on_message(f"Batch: starting {inst.display_name}...")

                ok = self._start_slot(slot)
                if not ok:
                    with self._lock:
                        self._slots.pop(inst.name, None)
                        self._failed.append(inst.display_name)
                    continue
        finally:
            with self._lock:
                self._fill_in_progress = False
            self._worker_lock.release()
            # In case slots finished while we were starting others.
            if self._active and not self._stopping:
                # Avoid recursive thread spam: tick will schedule again.
                pass

    def _start_slot(self, slot: BatchSlot) -> bool:
        inst = slot.instance
        name = inst.display_name
        try:
            if self._stopping:
                return False

            ready = self.bs_manager.resolve_online_instance(inst)
            if not ready:
                self._on_message(f"Batch: launching {name}; waiting for ADB...")
                self.bs_manager.start_instance(inst.name)
                ready = self.bs_manager.wait_for_instance(inst)
            if not ready or not ready.device_id:
                slot.state = SlotState.FAILED
                slot.error = "ADB timeout"
                logger.warning(f"Batch: timed out waiting for {name}")
                self._on_message(f"Batch: timed out waiting for {name}")
                return False

            slot.device_id = ready.device_id
            slot.instance = ready
            self._settle([ready])

            if self._stopping:
                self.bs_manager.stop_instance(
                    ready.name,
                    display_name=ready.display_name,
                    device_id=ready.device_id,
                )
                return False

            manager = refresh_manager(self._get_manager(), default_app_assignments(), connect=True)
            self._set_manager(manager)

            tracker = manager.ensure_tracker(
                name=ready.display_name,
                device_id=ready.device_id,
                app_assignments=default_app_assignments(),
            )
            if not tracker.adb.connect():
                slot.state = SlotState.FAILED
                slot.error = "ADB connect failed"
                logger.warning(f"Batch: could not connect ADB for {name}")
                self.bs_manager.stop_instance(
                    ready.name,
                    display_name=ready.display_name,
                    device_id=ready.device_id,
                )
                return False

            if not tracker.app:
                tracker.assign_app(default_app_assignments().get("default", "getsms"))

            if tracker.running:
                logger.warning(f"Batch: {name} already running; treating as active slot")
            elif not start_tracker(tracker):
                slot.state = SlotState.FAILED
                slot.error = "failed to start automation"
                logger.warning(f"Batch: failed to start automation on {name}")
                self.bs_manager.stop_instance(
                    ready.name,
                    display_name=ready.display_name,
                    device_id=ready.device_id,
                )
                return False

            slot.tracker_name = tracker.adb.name
            slot.state = SlotState.RUNNING
            slot.started_at = time.time()
            logger.info(f"Batch: automation running on {name} ({ready.device_id})")
            self._on_message(f"Batch: running {name}")
            return True
        except Exception as e:
            slot.state = SlotState.FAILED
            slot.error = str(e)
            logger.error(f"Batch: error starting {name}: {e}")
            self._on_message(f"Batch: failed to start {name}: {e}")
            try:
                self.bs_manager.stop_instance(
                    inst.name,
                    display_name=inst.display_name,
                    device_id=inst.device_id,
                )
            except Exception:
                pass
            return False

    def _settle(self, instances: list[BlueStacksInstance]) -> None:
        if not instances or self.settle_seconds <= 0:
            return
        names = ", ".join(i.display_name for i in instances)
        for remaining in range(self.settle_seconds, 0, -1):
            if self._stopping:
                return
            self._on_message(f"Waiting {remaining}s for BlueStacks to finish loading: {names}")
            time.sleep(1)

    def _resolve_tracker(self, slot: BatchSlot) -> InstanceTracker | None:
        manager = self._get_manager()
        if not manager:
            return None
        if slot.tracker_name:
            tracker = manager.find_by_name(slot.tracker_name)
            if tracker:
                return tracker
        if slot.device_id:
            return manager.find_by_device(slot.device_id)
        return None

    def _finalize_finished_slot(self, slot: BatchSlot) -> None:
        name = slot.instance.display_name
        result = "failed" if slot.state == SlotState.FAILED else "done"
        tracker = self._resolve_tracker(slot)
        if tracker and tracker.last_result:
            result = tracker.last_result

        self._teardown_slot(slot, mark_failed=(slot.state == SlotState.FAILED), reason=result)

        with self._lock:
            self._slots.pop(slot.instance.name, None)
            if slot.state == SlotState.FAILED or result in ("error",):
                if name not in self._failed:
                    self._failed.append(name)
            else:
                if name not in self._completed:
                    self._completed.append(name)
            msg = f"Batch: {name} finished ({result}). {len(self._completed) + len(self._failed)}/{self._total}"
        logger.info(msg)
        self._on_message(msg)

    def _teardown_slot(self, slot: BatchSlot, mark_failed: bool, reason: str) -> None:
        name = slot.instance.display_name
        tracker = self._resolve_tracker(slot)
        if tracker:
            try:
                if tracker.running or tracker.stop_event.is_set():
                    stop_tracker(tracker)
            except Exception as e:
                logger.warning(f"Batch: stop_tracker failed for {name}: {e}")

        # Always close the BlueStacks window for this slot before starting the next one.
        self._on_message(f"Batch: closing {name}...")
        try:
            stopped = self.bs_manager.stop_instance(
                slot.instance.name,
                display_name=slot.instance.display_name,
                device_id=slot.device_id or slot.instance.device_id,
            )
            if stopped:
                logger.info(f"Batch: closed BlueStacks instance {name}")
            else:
                logger.warning(f"Batch: could not confirm close for {name}")
        except Exception as e:
            logger.warning(f"Batch: stop_instance failed for {name}: {e}")

        # Brief pause so the OS releases RAM/ADB before the next instance boots.
        time.sleep(2)

        if mark_failed:
            slot.state = SlotState.FAILED
            if not slot.error:
                slot.error = reason
        else:
            slot.state = SlotState.DONE
