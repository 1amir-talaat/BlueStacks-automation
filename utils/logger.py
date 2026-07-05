import logging
import re
import threading
from collections import deque


class UILogStore(logging.Handler):
    def __init__(self, max_lines: int = 1200):
        super().__init__()
        self.records = deque(maxlen=max_lines)
        self.by_instance = {}
        self._lock = threading.Lock()
        self._instance_pattern = re.compile(r"\[(instance_\d+)\]")

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        match = self._instance_pattern.search(record.getMessage())
        instance = match.group(1) if match else "system"
        with self._lock:
            self.records.append((instance, record.levelname, message))
            self.by_instance.setdefault(instance, deque(maxlen=400)).append((record.levelname, message))

    def latest(self, instance: str | None = None, limit: int = 80) -> list[tuple[str, str]]:
        with self._lock:
            if instance:
                return list(self.by_instance.get(instance, []))[-limit:]
            return [(level, message) for _, level, message in list(self.records)[-limit:]]


LOG_STORE = UILogStore()
LOG_STORE.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%H:%M:%S"))
CONSOLE_LOGGING_ENABLED = True


def set_console_logging(enabled: bool) -> None:
    global CONSOLE_LOGGING_ENABLED
    CONSOLE_LOGGING_ENABLED = enabled
    for logger in logging.Logger.manager.loggerDict.values():
        if not isinstance(logger, logging.Logger):
            continue
        for handler in logger.handlers:
            if getattr(handler, "_automation_console", False):
                handler.setLevel(logging.INFO if enabled else logging.CRITICAL + 1)


def setup_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    if not any(isinstance(handler, UILogStore) for handler in logger.handlers):
        logger.addHandler(LOG_STORE)

    if not any(getattr(handler, "_automation_console", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler._automation_console = True
        handler.setLevel(logging.INFO if CONSOLE_LOGGING_ENABLED else logging.CRITICAL + 1)
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger
