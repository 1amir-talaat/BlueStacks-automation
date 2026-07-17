import os
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"

import sys
import threading
import time

from automation.runtime import build_manager, connect_manager, reset_online_dates, run_instance
from utils.logger import setup_logger

logger = setup_logger("main")


def run_legacy():
    logger.info("BlueStacks Ad Automation starting in legacy mode...")

    manager = build_manager()
    online = connect_manager(manager)
    connected_count = sum(1 for ok in online.values() if ok)

    if connected_count == 0:
        logger.warning("No instances online. Start BlueStacks first.")
        return

    manager.print_status()
    if not reset_online_dates(manager, online):
        logger.error("Stopping startup because one or more emulator clocks could not be synchronized from an API")
        manager.disconnect_all()
        return

    for tracker in [manager.get(name) for name, ok in online.items() if ok]:
        if tracker.app:
            thread = threading.Thread(target=run_instance, args=(tracker,), daemon=True)
            tracker.thread = thread
            thread.start()

    try:
        while True:
            manager.print_status()
            time.sleep(10)
    except KeyboardInterrupt:
        logger.info("Stopping...")

    for tracker in manager.get_all():
        tracker.request_stop()

    try:
        manager.disconnect_all()
    except Exception:
        pass
    logger.info("All done.")


def main():
    if "--legacy" in sys.argv:
        run_legacy()
        return

    try:
        from tui import run_tui
    except ImportError as e:
        logger.warning(f"TUI dependencies missing ({e}); falling back to legacy mode")
        run_legacy()
        return

    run_tui()


if __name__ == "__main__":
    main()
