"""Template-based button detection for ad screens."""
import gc
import cv2
import numpy as np
from pathlib import Path
from utils.logger import setup_logger

logger = setup_logger("buttons")

TEMPLATE_DIR = Path("templates")

# Shared buttons (same across all apps)
SHARED_GROUPS = ["x_button", "continue_btn"]

# Per-app buttons (different per app)
APP_GROUPS = ["ok_button", "watch_now", "daily_limit", "coin_icon"]


class ButtonMatcher:
    """Find buttons on screen using template matching."""

    def __init__(self, app_name: str = ""):
        self._templates: dict[str, list[np.ndarray]] = {}
        self._app_name = app_name.lower()
        self._load_templates()

    def _load_templates(self):
        if not TEMPLATE_DIR.exists():
            logger.warning(f"Template directory not found: {TEMPLATE_DIR}")
            return

        loaded = 0

        # Load shared templates (x_button-1.png, continue_btn-1.png, etc.)
        for group in SHARED_GROUPS:
            variants = self._load_group(group)
            if variants:
                self._templates[group] = variants
                loaded += len(variants)

        # Load per-app templates (ok_button-getsms-1.png, watch_now-tempsms-1.png, etc.)
        for group in APP_GROUPS:
            # App-specific first: ok_button-getsms-1.png
            if self._app_name:
                variants = self._load_group(f"{group}-{self._app_name}")
                if variants:
                    key = f"{group}_{self._app_name}"
                    self._templates[key] = variants
                    loaded += len(variants)

            # Fallback: generic ok_button-1.png
            variants = self._load_group(group)
            if variants:
                self._templates[group] = variants
                loaded += len(variants)

        logger.info(f"Loaded {loaded} templates for {len(self._templates)} groups (app={self._app_name})")

    def _load_group(self, prefix: str) -> list[np.ndarray]:
        """Load all variants of a template group (prefix-1.png, prefix-2.png, ... prefix.png)."""
        variants = []
        for i in range(1, 20):
            path = TEMPLATE_DIR / f"{prefix}-{i}.png"
            if path.exists():
                img = cv2.imread(str(path), cv2.IMREAD_COLOR)
                if img is not None:
                    variants.append(img)
                    logger.info(f"  Loaded: {prefix}-{i}.png ({img.shape[1]}x{img.shape[0]})")
        # Also try base name
        base = TEMPLATE_DIR / f"{prefix}.png"
        if base.exists():
            img = cv2.imread(str(base), cv2.IMREAD_COLOR)
            if img is not None:
                variants.append(img)
                logger.info(f"  Loaded: {prefix}.png ({img.shape[1]}x{img.shape[0]})")
        return variants

    def _find_best(self, frame: np.ndarray, group: str, threshold: float) -> tuple[int, int, float] | None:
        """Find best match in a group. Returns (x, y, confidence) or None."""
        variants = self._templates.get(group, [])
        best_val = 0
        best_loc = None

        for template in variants:
            result = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            del result
            if max_val >= threshold and max_val > best_val:
                best_val = max_val
                h, w = template.shape[:2]
                best_loc = (max_loc[0] + w // 2, max_loc[1] + h // 2)

        gc.collect()
        if best_loc:
            return (best_loc[0], best_loc[1], best_val)
        return None

    def find(self, frame: np.ndarray, group: str, threshold: float = 0.7) -> tuple[int, int] | None:
        """Find any variant of a button group. Returns center (x, y) or None."""
        # Try app-specific first: ok_button_tempsms
        app_key = f"{group}_{self._app_name}"
        if app_key in self._templates:
            result = self._find_best(frame, app_key, threshold)
            if result:
                return (result[0], result[1])

        # Fallback to generic: ok_button
        result = self._find_best(frame, group, threshold)
        if result:
            return (result[0], result[1])
        return None

    def find_any(self, frame: np.ndarray, groups: list[str], threshold: float = 0.7) -> tuple[str, int, int] | None:
        """Find any of the named button groups. Returns (group_name, x, y) or None."""
        best_group = None
        best_val = 0
        best_loc = None

        for group in groups:
            # Try app-specific key
            app_key = f"{group}_{self._app_name}"
            for key in [app_key, group]:
                result = self._find_best(frame, key, threshold)
                if result and result[2] > best_val:
                    best_val = result[2]
                    best_group = group
                    best_loc = (result[0], result[1])

        if best_group and best_loc:
            return (best_group, best_loc[0], best_loc[1])
        return None

    @property
    def available(self) -> list[str]:
        return list(self._templates.keys())
