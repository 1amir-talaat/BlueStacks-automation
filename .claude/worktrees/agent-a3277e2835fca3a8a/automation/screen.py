import gc
import os
import time
from pathlib import Path
from PIL import Image
import cv2
import numpy as np
from utils.logger import setup_logger

logger = setup_logger("screen")

os.environ["PYTHONIOENCODING"] = "utf-8"


class ScreenCapture:
    def __init__(self, adb_controller):
        self.adb = adb_controller
        self.screenshot_dir = Path("screenshots")
        self.screenshot_dir.mkdir(exist_ok=True)
        self._count = 0
        self._last_screenshot: Image.Image | None = None
        self._last_screenshot_time = 0

    def take_screenshot(self, label: str = "", force: bool = False) -> Image.Image | None:
        now = time.time()
        if not force and self._last_screenshot and (now - self._last_screenshot_time) < 0.5:
            return self._last_screenshot

        self._count += 1
        filename = f"{self.adb.name}_{self._count}_{label}.png"
        local_path = str(self.screenshot_dir / filename)

        if self.adb.screenshot(local_path):
            try:
                if self._last_screenshot:
                    self._last_screenshot.close()
                    self._last_screenshot = None
                gc.collect()
                img = Image.open(local_path)
                self._last_screenshot = img
                self._last_screenshot_time = now
                return img
            except Exception as e:
                logger.error(f"Failed to open screenshot: {e}")
        return None

    def screenshot_to_cv2(self, img: Image.Image) -> np.ndarray:
        arr = np.array(img)
        del img
        frame = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        del arr
        gc.collect()
        return frame

    def find_template(self, template_path: str, threshold: float = 0.7) -> tuple[int, int] | None:
        img = self.take_screenshot("template")
        if not img:
            return None

        template = cv2.imread(template_path, cv2.IMREAD_COLOR)
        if template is None:
            return None

        frame = self.screenshot_to_cv2(img)
        result = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        del result
        del frame

        if max_val >= threshold:
            h, w = template.shape[:2]
            cx = max_loc[0] + w // 2
            cy = max_loc[1] + h // 2
            logger.info(f"Template match: {template_path} at ({cx}, {cy}) conf={max_val:.2f}")
            return (cx, cy)

        return None

    def find_color_region(self, color_bgr: tuple, tolerance: int = 30,
                          region: tuple | None = None) -> tuple[int, int] | None:
        img = self.take_screenshot("color")
        if not img:
            return None

        frame = self.screenshot_to_cv2(img)
        if region:
            x1, y1, x2, y2 = region
            frame = frame[y1:y2, x1:x2]

        lower = np.array([max(0, c - tolerance) for c in color_bgr], dtype=np.uint8)
        upper = np.array([min(255, c + tolerance) for c in color_bgr], dtype=np.uint8)
        mask = cv2.inRange(frame, lower, upper)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            largest = max(contours, key=cv2.contourArea)
            M = cv2.moments(largest)
            if M["m00"] > 0:
                cx = int(M["m10"] / M["m00"])
                cy = int(M["m01"] / M["m00"])
                if region:
                    cx += region[0]
                    cy += region[1]
                logger.info(f"Color match at ({cx}, {cy})")
                return (cx, cy)

        return None

    def is_text_on_screen(self, text: str) -> bool:
        img = self.take_screenshot("text_check")
        if not img:
            return False

        try:
            import easyocr
            reader = easyocr.Reader(["en"], gpu=False, verbose=False)
            results = reader.readtext(np.array(img))
            for _, detected_text, conf in results:
                if conf > 0.4 and text.lower() in detected_text.lower():
                    return True
        except Exception:
            pass

        return False

    def find_button_by_position(self, position: str, screen_size: tuple[int, int] = (540, 960)) -> tuple[int, int]:
        w, h = screen_size
        positions = {
            "top_right": (int(w * 0.9), int(h * 0.05)),
            "top_left": (int(w * 0.1), int(h * 0.05)),
            "bottom_center": (w // 2, int(h * 0.9)),
            "center": (w // 2, h // 2),
            "bottom_right": (int(w * 0.85), int(h * 0.9)),
        }
        return positions.get(position, (w // 2, h // 2))

    def wait_for_template(self, template_path: str, timeout: int = 15, interval: float = 1.0) -> tuple[int, int] | None:
        start = time.time()
        while time.time() - start < timeout:
            coords = self.find_template(template_path)
            if coords:
                return coords
            time.sleep(interval)
        return None

    def wait_and_click_template(self, template_path: str, timeout: int = 15) -> bool:
        coords = self.wait_for_template(template_path, timeout)
        if coords:
            self.adb.tap(coords[0], coords[1])
            return True
        return False
