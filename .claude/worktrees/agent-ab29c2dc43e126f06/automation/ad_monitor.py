import subprocess
import threading
import time
import re
from utils.logger import setup_logger

logger = setup_logger("ad_monitor")


class AdMonitor:
    """Placeholder — this app doesn't log AdMob events to logcat."""

    def __init__(self, adb_controller):
        self.adb = adb_controller
        self._running = False

        self.ad_loading = False
        self.ad_shown = False
        self.ad_failed = False
        self.ad_rewarded = False
        self.ad_finished = False
        self.no_ad_available = False

    def start(self):
        self._running = True

    def stop(self):
        self._running = False

    def reset(self):
        self.ad_loading = False
        self.ad_shown = False
        self.ad_failed = False
        self.ad_rewarded = False
        self.ad_finished = False
        self.no_ad_available = False

    def is_ad_playing(self) -> bool:
        return False

    def is_ad_ready(self) -> bool:
        return False

    def has_reward(self) -> bool:
        return False

    def has_no_ad(self) -> bool:
        return False
