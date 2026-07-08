import time
from enum import Enum
from automation.adb_controller import ADBController
from automation.screen import ScreenCapture
from automation.buttons import ButtonMatcher
from config import AD_TIMING, APPS
from utils.logger import setup_logger

logger = setup_logger("base_app")


class AppState(Enum):
    NOT_LAUNCHED = "not_launched"
    AD_PAGE = "ad_page"
    AD_PLAYING = "ad_playing"
    REWARD_GRANTED = "reward_granted"
    REWARD_RECEIVED = "reward_received"
    GOOGLE_PLAY = "google_play"
    DAILY_LIMIT = "daily_limit"
    EXIT_DIALOG = "exit_dialog"
    LOADING_DIALOG = "loading_dialog"
    UNKNOWN = "unknown"


class BaseApp:
    PACKAGE_NAME = ""
    ACTIVITY_NAME = ""
    APP_NAME = ""
    IS_DARK = False

    def __init__(self, adb: ADBController):
        self.adb = adb
        self.screen = ScreenCapture(adb)
        self.buttons = ButtonMatcher(app_name=self.APP_NAME)
        self.ads_watched = 0
        self.watch_now_clicks = 0
        self.failed_ad_load_batches = 0
        self._ad_reset_attempted_at = 0
        self.date_trick_offset_days = 1
        self.post_ad_loading_retries = 0
        # TempSMS: consecutive post-ad outcomes with loader and no coins earned.
        # After 2, treat as daily rate-limit (date changes will not help).
        self.post_ad_no_reward_streak = 0
        self._no_reward_counted_for_current_ad = False
        self._in_post_ad_retry = False
        self._last_loading_seen_at = 0
        self._last_reward_x = None
        self.stop_event = None
        self._daily_limit_grace_until = 0
        self._post_ad_grace_until = 0
        self._started_at = 0

        app_cfg = APPS.get(self.APP_NAME.lower(), {})
        self.COIN_ICON = app_cfg.get("coin_icon_coords", (427, 82))
        self.WATCH_NOW = app_cfg.get("watch_now_coords", (420, 910))
        self.REWARD_X = app_cfg.get("reward_x_coords", (490, 35))
        self.GOOGLE_PLAY_X = app_cfg.get("google_play_x_coords", (490, 260))
        self.OK_BTN = app_cfg.get("ok_button_coords", (310, 565))

    def should_stop(self) -> bool:
        return bool(self.stop_event and self.stop_event.is_set())

    def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleep in short chunks. Returns True if stop was requested."""
        deadline = time.time() + max(0.0, seconds)
        while time.time() < deadline:
            if self.should_stop():
                return True
            time.sleep(min(0.25, max(0.0, deadline - time.time())))
        return self.should_stop()

    def is_app_running(self) -> bool:
        pkg = self.adb.get_current_package()
        return self.PACKAGE_NAME in pkg

    def close_all_bg_apps(self):
        logger.info(f"[{self.adb.name}] Closing all background apps")
        if not self.adb.press_key("HOME"):
            logger.warning(f"[{self.adb.name}] HOME key timed out; continuing cleanup best-effort")
        time.sleep(0.5)
        for package in ("com.virtualnumber.sms", "com.secondphone.tempsms", "com.android.vending"):
            self.adb.close_app(package)
        time.sleep(0.5)
        self.adb.press_key("KEYCODE_HOME")
        time.sleep(0.5)

    def launch(self) -> bool:
        if self.should_stop():
            return False
        self.close_all_bg_apps()
        if self._interruptible_sleep(0.5) or self.should_stop():
            return False
        self.adb.launch_app(self.PACKAGE_NAME, self.ACTIVITY_NAME)
        if self.adb.last_error:
            logger.warning(f"[{self.adb.name}] {self.APP_NAME}: Launch command may have failed: {self.adb.last_error}")
            return False
        # Wait up to 15s for the app to appear in the foreground
        for _ in range(30):
            if self.should_stop():
                return False
            time.sleep(0.5)
            if self.is_app_running():
                logger.info(f"[{self.adb.name}] {self.APP_NAME}: App is in foreground, waiting 10s for full load")
                if self._interruptible_sleep(10):  # let splash/loading finish fully
                    return False
                return not self.should_stop()
        logger.warning(f"[{self.adb.name}] {self.APP_NAME}: App did not appear in foreground after 15s")
        return False

    def force_restart(self):
        if self.should_stop():
            return
        logger.info(f"[{self.adb.name}] {self.APP_NAME}: FORCE RESTART")
        self.adb.close_app(self.PACKAGE_NAME)
        if self._interruptible_sleep(1) or self.should_stop():
            return
        self.adb.press_key("HOME")
        if self._interruptible_sleep(0.5) or self.should_stop():
            return
        self.adb.launch_app(self.PACKAGE_NAME, self.ACTIVITY_NAME)
        for _ in range(20):
            if self.should_stop():
                return
            time.sleep(0.5)
            if self.is_app_running():
                logger.info(f"[{self.adb.name}] {self.APP_NAME}: App back in foreground after restart, waiting 10s")
                self._interruptible_sleep(10)
                return
        logger.warning(f"[{self.adb.name}] {self.APP_NAME}: App did not appear after restart")

    def go_back(self):
        self.adb.press_key("BACK")
        time.sleep(0.3)

    def _get_frame(self):
        import gc
        import cv2
        img = self.screen.take_screenshot("detect", force=True)
        if not img:
            return None
        from pathlib import Path
        debug_dir = Path("screenshots/debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        img.save(debug_dir / f"{self.adb.name}_latest.png")
        frame = self.screen.screenshot_to_cv2(img)
        gc.collect()
        return frame

    def _verify_button(self, frame, cx, cy, btn_type) -> bool:
        """Verify a detected button by checking pixel content around it."""
        import cv2
        import numpy as np
        h, w = frame.shape[:2]
        # Extract region around detected button
        pad = 15
        x1 = max(0, cx - pad)
        y1 = max(0, cy - pad)
        x2 = min(w, cx + pad)
        y2 = min(h, cy + pad)
        roi = frame[y1:y2, x1:x2]
        if roi.size == 0:
            return False

        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

        if btn_type == "x_button":
            # X button should have contrast (white on dark or dark on white)
            std = cv2.meanStdDev(gray)[1][0][0]
            return std > 30  # needs some contrast

        if btn_type == "continue_btn":
            # Continue button should have some white/bright content
            bright = cv2.inRange(gray, 180, 255)
            return cv2.countNonZero(bright) > 10

        if btn_type == "ok_button":
            # OK button can be red/pink (TempSMS) or blue (GetSMS)
            hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            r1 = cv2.inRange(hsv, np.array([0, 120, 150]), np.array([12, 255, 255]))
            r2 = cv2.inRange(hsv, np.array([168, 120, 150]), np.array([180, 255, 255]))
            blue = cv2.inRange(hsv, np.array([95, 80, 120]), np.array([130, 255, 255]))
            return cv2.countNonZero(r1 | r2 | blue) > 10

        return True

    def _find_button(self, frame, groups: list[str], threshold: float = 0.8):
        """Find button with region constraints and verification."""
        import cv2
        h, w = frame.shape[:2]

        # Region constraints for each button type
        regions = {
            "x_button": (0, 0, w, int(h * 0.15)),                      # top strip; ads may put X left or right
            "continue_btn": (int(w * 0.5), 0, w, int(h * 0.12)),        # top-right 50%
            "ok_button": (int(w * 0.1), int(h * 0.4), int(w * 0.9), h), # bottom half, centered
            "watch_now": (int(w * 0.4), int(h * 0.85), w, h),           # bottom-right
            "daily_limit": (int(w * 0.25), int(h * 0.40), int(w * 0.90), h),  # centered dialog button only
        }

        for group in groups:
            result = self.buttons.find(frame, group, threshold)
            if result is None:
                continue
            cx, cy = result

            # Check region constraint
            if group in regions:
                rx1, ry1, rx2, ry2 = regions[group]
                if not (rx1 <= cx <= rx2 and ry1 <= cy <= ry2):
                    continue

            # Verify pixel content
            if self._verify_button(frame, cx, cy, group):
                return (group, cx, cy)

        return None

    def _find_daily_limit_button(self, frame):
        """Find the daily-limit dismiss button.

        Template matching handles GetSMS and most TempSMS dialogs. TempSMS can
        miss the template on dark theme, so fall back to detecting the pink
        Recharge button and tapping the left "Come back tomorrow" button.
        """
        import cv2
        import numpy as np

        thresholds = [0.68]
        if self.APP_NAME == "tempsms":
            thresholds.append(0.55)

        for threshold in thresholds:
            result = self._find_button(frame, ["daily_limit"], threshold=threshold)
            if result:
                _, cx, cy = result
                logger.info(f"[{self.adb.name}] {self.APP_NAME}: Daily-limit template matched at threshold {threshold}")
                return (cx, cy)

        if self.APP_NAME != "tempsms":
            return None

        h, w = frame.shape[:2]
        x0 = int(w * 0.50)
        y0 = int(h * 0.48)
        x1 = int(w * 0.93)
        y1 = int(h * 0.70)
        roi = frame[y0:y1, x0:x1]
        if roi.size == 0:
            return None

        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        pink = cv2.inRange(hsv, np.array([150, 80, 120]), np.array([180, 255, 255]))
        pink |= cv2.inRange(hsv, np.array([0, 80, 120]), np.array([10, 255, 255]))
        contours, _ = cv2.findContours(pink, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        best = None
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < 1200:
                continue
            x, y, cw, ch = cv2.boundingRect(contour)
            aspect = cw / max(ch, 1)
            if 1.8 <= aspect <= 5.5 and 28 <= ch <= 70:
                if best is None or area > best[0]:
                    best = (area, x, y, cw, ch)

        if not best:
            return None

        _, x, y, cw, ch = best
        recharge_cx = x0 + x + cw // 2
        recharge_cy = y0 + y + ch // 2

        # TempSMS daily-limit dialog has Recharge on the right and the safe
        # dismiss action, "Come back tomorrow", on the same row to the left.
        dismiss_x = int(w * 0.34)
        dismiss_y = recharge_cy
        logger.info(
            f"[{self.adb.name}] {self.APP_NAME}: TempSMS daily-limit fallback detected Recharge at "
            f"({recharge_cx},{recharge_cy}); dismiss at ({dismiss_x},{dismiss_y})"
        )
        return (dismiss_x, dismiss_y)

    def detect_state(self) -> AppState:
        import cv2
        import numpy as np

        if not self.is_app_running():
            return AppState.NOT_LAUNCHED

        frame = self._get_frame()
        if frame is None:
            return AppState.UNKNOWN

        h, w = frame.shape[:2]

        # ===== Exit dialog ("Are you sure you want to exit?") =====
        # TempSMS has similar pink/gray UI on the normal screen, which caused a
        # repeated false-positive CANCEL loop. Only use this detector for GetSMS.
        if self.APP_NAME != "tempsms":
            # Signature: pink EXIT button on the right plus a gray CANCEL button on the left.
            # Requiring both avoids confusing reward/daily-limit OK buttons with exit dialogs.
            exit_region = frame[int(h*0.50):int(h*0.70), int(w*0.35):int(w*0.95)]
            hsv_exit = cv2.cvtColor(exit_region, cv2.COLOR_BGR2HSV)
            pink_exit = cv2.inRange(hsv_exit, np.array([150, 80, 80]), np.array([180, 255, 255]))
            pink_exit |= cv2.inRange(hsv_exit, np.array([0, 80, 80]), np.array([10, 255, 255]))
            contours, _ = cv2.findContours(pink_exit, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            cancel_region = frame[int(h*0.50):int(h*0.70), int(w*0.08):int(w*0.48)]
            hsv_cancel = cv2.cvtColor(cancel_region, cv2.COLOR_BGR2HSV)
            cancel_gray = cv2.inRange(hsv_cancel, np.array([0, 0, 25]), np.array([180, 80, 120]))
            has_cancel_button = cv2.countNonZero(cancel_gray) > 1200

            for contour in contours:
                area = cv2.contourArea(contour)
                if 3000 < area < 20000:
                    x, y, cw, ch = cv2.boundingRect(contour)
                    aspect = cw / max(ch, 1)
                    if has_cancel_button and 1.8 < aspect < 5.5:
                        logger.info(f"[{self.adb.name}] {self.APP_NAME}: Exit dialog detected")
                        return AppState.EXIT_DIALOG

        # ===== Template matching with region + verification =====
        if self.buttons.available:
            result = self._find_button(frame, ["x_button", "continue_btn"], threshold=0.8)
            if result:
                name, cx, cy = result
                logger.info(f"[{self.adb.name}] {self.APP_NAME}: Verified '{name}' at ({cx},{cy})")
                if name == "x_button":
                    self._last_reward_x = (cx, cy)
                    return AppState.REWARD_GRANTED
                if name == "continue_btn":
                    return AppState.AD_PLAYING

            # Check for daily limit dialog ("Got it" / "Come back tomorrow")
            result = self._find_daily_limit_button(frame)
            if result:
                cx, cy = result
                logger.info(f"[{self.adb.name}] {self.APP_NAME}: Daily limit dialog detected at ({cx},{cy})")
                return AppState.DAILY_LIMIT

        if self._find_in_app_redirect_continue(frame):
            logger.info(f"[{self.adb.name}] {self.APP_NAME}: In-app redirect overlay detected")
            return AppState.AD_PLAYING

        if self._is_in_app_ad_overlay(frame):
            logger.info(f"[{self.adb.name}] {self.APP_NAME}: In-app ad overlay detected")
            return AppState.AD_PLAYING

        if self._is_blocking_loading_dialog(frame) or (
            self.APP_NAME == "tempsms" and self._is_tempsms_inline_loading(frame)
        ):
            self._last_loading_seen_at = time.time()
            logger.info(f"[{self.adb.name}] {self.APP_NAME}: Loading dialog detected")
            return AppState.LOADING_DIALOG

        # ===== Pixel-based detection =====

        # 1. "Reward received!" dialog
        # Check center region for a dialog (rounded rect with OK button)
        # Use a tighter vertical range — reward dialog sits in the middle of the screen
        center_region = frame[int(h*0.35):int(h*0.7), int(w*0.1):int(w*0.9)]
        hsv_center = cv2.cvtColor(center_region, cv2.COLOR_BGR2HSV)
        gray_center = cv2.cvtColor(center_region, cv2.COLOR_BGR2GRAY)

        # Look for red OK button — must be a single large contour (a button), not scattered icons
        r1 = cv2.inRange(hsv_center, np.array([0, 150, 150]), np.array([10, 255, 255]))
        r2 = cv2.inRange(hsv_center, np.array([170, 150, 150]), np.array([180, 255, 255]))
        red_mask = r1 | r2
        red_contours, _ = cv2.findContours(red_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in red_contours:
            area = cv2.contourArea(c)
            if 2500 < area < 20000:  # single button-sized red blob
                x, y, cw, ch = cv2.boundingRect(c)
                aspect = cw / max(ch, 1)
                if 1.5 < aspect < 6:  # wide button shape
                    return AppState.REWARD_RECEIVED

        # Look for blue/teal OK button (light theme)
        b1 = cv2.inRange(hsv_center, np.array([95, 100, 100]), np.array([115, 255, 255]))
        blue_contours, _ = cv2.findContours(b1, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in blue_contours:
            area = cv2.contourArea(c)
            if 2500 < area < 15000:
                x, y, cw, ch = cv2.boundingRect(c)
                aspect = cw / max(ch, 1)
                if 1.5 < aspect < 6:  # wide button shape
                    return AppState.REWARD_RECEIVED

        # 2. Ad timer (AD_PLAYING)
        timer = frame[0:int(h*0.06), int(w*0.55):]
        hsv_t = cv2.cvtColor(timer, cv2.COLOR_BGR2HSV)
        white_t = cv2.inRange(hsv_t, np.array([0, 0, 180]), np.array([180, 40, 255]))
        t_ratio = cv2.countNonZero(white_t) / max(1, white_t.size)
        if 0.06 < t_ratio < 0.35:
            return AppState.AD_PLAYING

        # 3. "Reward granted" X pill
        pill = frame[0:int(h*0.07), int(w*0.55):]
        hsv_pill = cv2.cvtColor(pill, cv2.COLOR_BGR2HSV)
        white_pill = cv2.inRange(hsv_pill, np.array([0, 0, 180]), np.array([180, 40, 255]))
        pill_ratio = cv2.countNonZero(white_pill) / max(1, white_pill.size)

        if 0.15 < pill_ratio < 0.6:
            x_area = frame[int(h*0.01):int(h*0.05), int(w*0.85):]
            gray_x = cv2.cvtColor(x_area, cv2.COLOR_BGR2GRAY)
            _, thresh_x = cv2.threshold(gray_x, 80, 255, cv2.THRESH_BINARY_INV)
            if cv2.countNonZero(thresh_x) > 25:
                return AppState.REWARD_GRANTED

        # 4. Watch Now button (AD_PAGE) — bottom 15%, right half
        # GetSMS: blue button (hue 100-130)
        # TempSMS: pink/red button (hue 155-180 or 0-5)
        watch_y1 = int(h * 0.85)
        watch_region = frame[watch_y1:, w // 2:]
        hsv_w = cv2.cvtColor(watch_region, cv2.COLOR_BGR2HSV)

        # Warm/pink (original)
        mask = cv2.inRange(hsv_w, np.array([0, 60, 130]), np.array([25, 255, 255]))
        mask |= cv2.inRange(hsv_w, np.array([155, 60, 130]), np.array([180, 255, 255]))
        # Blue (GetSMS Watch Now)
        mask |= cv2.inRange(hsv_w, np.array([100, 100, 100]), np.array([130, 255, 255]))

        if cv2.countNonZero(mask) > 300:
            return AppState.AD_PAGE

        return AppState.UNKNOWN

    def _is_loading_dialog(self, frame) -> bool:
        """Detect the centered Loading dialog — works for both GetSMS and TempSMS."""
        import cv2
        import numpy as np

        h, w = frame.shape[:2]
        # Wider region to cover both themes
        x1 = int(w * 0.18)
        x2 = int(w * 0.82)
        y1 = int(h * 0.30)
        y2 = int(h * 0.58)
        roi = frame[y1:y2, x1:x2]
        if roi.size == 0:
            return False

        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

        if getattr(self, "APP_NAME", "") != "tempsms":
            # --- GetSMS theme: white dialog + blue spinner ---
            # The whole screen is dimmed behind the dialog, so the white popup can
            # appear gray in screenshots. Keep the threshold lower than pure white.
            bright = cv2.inRange(gray, 150, 255)
            bright_contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for contour in bright_contours:
                area = cv2.contourArea(contour)
                if area < 2500:
                    continue
                x, y, cw, ch = cv2.boundingRect(contour)
                aspect = cw / max(ch, 1)
                if 1.8 <= aspect <= 4.5 and 40 <= ch <= 140:
                    blue = cv2.inRange(hsv, np.array([90, 40, 70]), np.array([140, 255, 255]))
                    if cv2.countNonZero(blue) > 20:
                        return True

            # Fallback for GetSMS: centered white/gray popup with blue spinner.
            center = frame[int(h * 0.36):int(h * 0.52), int(w * 0.25):int(w * 0.75)]
            if center.size:
                center_hsv = cv2.cvtColor(center, cv2.COLOR_BGR2HSV)
                center_gray = cv2.cvtColor(center, cv2.COLOR_BGR2GRAY)
                light_ratio = cv2.countNonZero(cv2.inRange(center_gray, 145, 255)) / max(1, center_gray.size)
                blue = cv2.inRange(center_hsv, np.array([90, 35, 60]), np.array([140, 255, 255]))
                if light_ratio > 0.18 and cv2.countNonZero(blue) > 15:
                    return True

        # --- TempSMS theme: dark dialog + red/pink spinner + white "Loading..." text ---
        temp_roi = frame[int(h * 0.40):int(h * 0.67), int(w * 0.25):int(w * 0.85)]
        if temp_roi.size == 0:
            return False
        temp_hsv = cv2.cvtColor(temp_roi, cv2.COLOR_BGR2HSV)
        temp_gray = cv2.cvtColor(temp_roi, cv2.COLOR_BGR2GRAY)

        text_roi = frame[int(h * 0.48):int(h * 0.56), int(w * 0.34):int(w * 0.68)]
        spinner_roi = frame[int(h * 0.47):int(h * 0.55), int(w * 0.28):int(w * 0.46)]
        if text_roi.size and spinner_roi.size:
            text_gray = cv2.cvtColor(text_roi, cv2.COLOR_BGR2GRAY)
            spinner_hsv = cv2.cvtColor(spinner_roi, cv2.COLOR_BGR2HSV)
            popup_gray = cv2.cvtColor(frame[int(h * 0.46):int(h * 0.59), int(w * 0.32):int(w * 0.70)], cv2.COLOR_BGR2GRAY)
            text_pixels = cv2.countNonZero(cv2.inRange(text_gray, 150, 255))
            dark_popup_ratio = cv2.countNonZero(cv2.inRange(popup_gray, 0, 45)) / max(1, popup_gray.size)
            spinner_red = cv2.inRange(spinner_hsv, np.array([0, 45, 60]), np.array([18, 255, 255]))
            spinner_red |= cv2.inRange(spinner_hsv, np.array([140, 45, 60]), np.array([180, 255, 255]))
            if dark_popup_ratio > 0.45 and text_pixels > 35 and 6 <= cv2.countNonZero(spinner_red) <= 350:
                return True

        # The purchase page is dimmed while the popup is open, so the text often
        # captures as gray rather than pure white.
        white_text = cv2.inRange(temp_gray, 165, 255)
        white_ratio = cv2.countNonZero(white_text) / max(1, white_text.size)
        has_loading_text = 0.004 < white_ratio < 0.15

        # The TempSMS popup itself is a centered dark rounded rectangle. Checking
        # this keeps the looser text/spinner thresholds from matching normal UI.
        popup = frame[int(h * 0.46):int(h * 0.59), int(w * 0.32):int(w * 0.70)]
        has_dark_popup = False
        if popup.size:
            popup_gray = cv2.cvtColor(popup, cv2.COLOR_BGR2GRAY)
            dark_ratio = cv2.countNonZero(cv2.inRange(popup_gray, 0, 45)) / max(1, popup_gray.size)
            has_dark_popup = dark_ratio > 0.45

        # Look for a small red/pink arc (the spinner).
        red1 = cv2.inRange(temp_hsv, np.array([0, 20, 35]), np.array([18, 255, 255]))
        red2 = cv2.inRange(temp_hsv, np.array([140, 20, 35]), np.array([180, 255, 255]))
        red_count = cv2.countNonZero(red1 | red2)
        if red_count > 10 and has_loading_text and has_dark_popup:
            return True

        # Inline spinner over the coin grid (Watch Now still visible).
        if self.APP_NAME == "tempsms" and self._is_tempsms_inline_loading(frame):
            return True

        return False

    def _is_tempsms_inline_loading(self, frame) -> bool:
        """Detect TempSMS centered 'Loading...' spinner on the Purchase Coins page.

        Matches the bug case: small pink/red arc + Loading text over coin packages
        while Watch Now remains visible at the bottom (no coins earned).
        """
        import cv2
        import numpy as np

        # UIAutomator is the most reliable signal for the literal "Loading..." label.
        try:
            if self.adb.has_ui_text(["Loading", "Loading..."]):
                if frame is None or self._is_ad_page_behind_loading(frame):
                    return True
        except Exception:
            pass

        if frame is None:
            return False

        h, w = frame.shape[:2]
        # Tight center band where the spinner sits between coin package rows.
        y1, y2 = int(h * 0.34), int(h * 0.52)
        x1, x2 = int(w * 0.22), int(w * 0.78)
        roi = frame[y1:y2, x1:x2]
        if roi.size == 0:
            return False

        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

        # Loading label can be white or light gray depending on capture quality.
        light = cv2.inRange(gray, 140, 255)
        light_count = cv2.countNonZero(light)
        light_ratio = light_count / max(1, light.size)

        # Small pink/red spinner arc (exclude large orange coin-package borders).
        red = cv2.inRange(hsv, np.array([0, 50, 60]), np.array([15, 255, 255]))
        red |= cv2.inRange(hsv, np.array([155, 50, 60]), np.array([180, 255, 255]))
        red_contours, _ = cv2.findContours(red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        small_red = 0
        for contour in red_contours:
            area = cv2.contourArea(contour)
            if 8 <= area <= 600:
                small_red += area

        has_spinner = small_red >= 12
        has_label = 25 <= light_count <= int(light.size * 0.25) and light_ratio < 0.25
        if not (has_spinner and has_label):
            # Spinner alone over ad page is still a strong signal.
            if not (has_spinner and self._is_ad_page_behind_loading(frame) and small_red < 900):
                return False

        return self._is_ad_page_behind_loading(frame)

    def _is_ad_page_behind_loading(self, frame) -> bool:
        """Confirm the loading popup is over the Purchase Coins/ad page."""
        import cv2
        import numpy as np

        h, w = frame.shape[:2]

        # The Watch Now pill remains visible near the lower-right of the
        # Purchase Coins page behind the loading overlay. Keep this region tight
        # so TempSMS home-screen tabs/arrows/bottom-nav pink UI do not count.
        watch_region = frame[int(h * 0.78):int(h * 0.95), int(w * 0.50):int(w * 0.98)]
        if watch_region.size == 0:
            return False

        hsv = cv2.cvtColor(watch_region, cv2.COLOR_BGR2HSV)
        blue = cv2.inRange(hsv, np.array([95, 35, 40]), np.array([135, 255, 255]))
        pink = cv2.inRange(hsv, np.array([140, 20, 30]), np.array([180, 255, 255]))
        pink |= cv2.inRange(hsv, np.array([0, 20, 30]), np.array([18, 255, 255]))
        mask = blue | pink
        if cv2.countNonZero(mask) >= 120:
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if any(cv2.contourArea(c) > 200 for c in contours):
                return True

        if getattr(self, "APP_NAME", "") == "tempsms":
            purchase_region = frame[int(h * 0.28):int(h * 0.75), 0:w]
            if purchase_region.size == 0:
                return False

            hsv_purchase = cv2.cvtColor(purchase_region, cv2.COLOR_BGR2HSV)
            yellow = cv2.inRange(hsv_purchase, np.array([18, 20, 20]), np.array([45, 255, 255]))
            contours, _ = cv2.findContours(yellow, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            large_buttons = 0
            for contour in contours:
                area = cv2.contourArea(contour)
                if area < 1200:
                    continue
                _, _, cw, ch = cv2.boundingRect(contour)
                aspect = cw / max(ch, 1)
                if 1.2 <= aspect <= 5.5 and 24 <= ch <= 90:
                    large_buttons += 1

            if large_buttons >= 2:
                return True

        return False

    def _is_in_app_ad_overlay(self, frame) -> bool:
        """Detect ad webviews that keep focus inside the app package."""
        import cv2
        import numpy as np

        h, w = frame.shape[:2]

        top_bar = frame[0:int(h * 0.06), int(w * 0.10):int(w * 0.98)]
        ad_badge = frame[int(h * 0.055):int(h * 0.095), 0:int(w * 0.35)]
        if top_bar.size == 0 or ad_badge.size == 0:
            return False

        top_gray = cv2.cvtColor(top_bar, cv2.COLOR_BGR2GRAY)
        top_bright_ratio = cv2.countNonZero(cv2.inRange(top_gray, 185, 255)) / max(1, top_gray.size)

        badge_hsv = cv2.cvtColor(ad_badge, cv2.COLOR_BGR2HSV)
        badge_gray = cv2.inRange(badge_hsv, np.array([0, 0, 90]), np.array([180, 70, 190]))
        badge_ratio = cv2.countNonZero(badge_gray) / max(1, badge_gray.size)

        top_right = frame[0:int(h * 0.07), int(w * 0.65):w]
        top_right_hsv = cv2.cvtColor(top_right, cv2.COLOR_BGR2HSV)
        top_blue = cv2.inRange(top_right_hsv, np.array([95, 70, 90]), np.array([135, 255, 255]))

        bottom = frame[int(h * 0.72):h, int(w * 0.05):int(w * 0.95)]
        bottom_hsv = cv2.cvtColor(bottom, cv2.COLOR_BGR2HSV)
        bottom_blue = cv2.inRange(bottom_hsv, np.array([95, 70, 90]), np.array([135, 255, 255]))

        has_ad_chrome = top_bright_ratio > 0.45 and badge_ratio > 0.12
        has_ad_action = cv2.countNonZero(top_blue) > 25 or cv2.countNonZero(bottom_blue) > 1500
        return has_ad_chrome and has_ad_action

    def _is_blocking_loading_dialog(self, frame) -> bool:
        return self._is_loading_dialog(frame) and self._is_ad_page_behind_loading(frame)

    def _loading_dialog_persisted(self, duration: float = 5.0) -> bool:
        deadline = time.time() + duration
        saw_loading = False
        while time.time() < deadline:
            frame = self._get_frame()
            if frame is None or not self._is_blocking_loading_dialog(frame):
                return False
            saw_loading = True
            time.sleep(1)
        return saw_loading

    def _post_ad_loading_stuck(self, duration: float = 15.0) -> bool:
        """Wait out normal post-ad loading; return True only if it stays stuck."""
        deadline = time.time() + duration
        saw_loading = False
        while time.time() < deadline:
            frame = self._get_frame()
            if frame is None:
                time.sleep(1)
                continue

            if not self._is_blocking_loading_dialog(frame):
                return False

            saw_loading = True
            time.sleep(1)
        return saw_loading

    def _dismiss_exit_dialog(self):
        """Tap CANCEL on the 'Are you sure you want to exit?' dialog."""
        frame = self._get_frame()
        if frame is None:
            return
        h, w = frame.shape[:2]
        # CANCEL is on the left half of the dialog, vertically in the button row.
        # Measured at approximately (160, 565) on 540x960.
        cancel_x = int(w * 0.30)
        cancel_y = int(h * 0.59)
        logger.info(f"[{self.adb.name}] {self.APP_NAME}: Tapping CANCEL on exit dialog at ({cancel_x},{cancel_y})")
        self.adb.tap(cancel_x, cancel_y)
        time.sleep(1)

    def clear_stuck_dialogs(self):
        for _ in range(3):
            state = self.detect_state()

            if state == AppState.REWARD_RECEIVED:
                self.adb.tap(*self.OK_BTN)
                time.sleep(1.5)
                continue

            if state == AppState.REWARD_GRANTED:
                self._tap_reward_granted_close()
                time.sleep(1.5)
                continue

            if state == AppState.DAILY_LIMIT:
                # Do not dismiss this here. The main loop must run
                # handle_daily_limit() so the date-switch flow executes.
                break

            if state == AppState.EXIT_DIALOG:
                self._dismiss_exit_dialog()
                continue

            break

    def _find_and_tap_coin_icon(self) -> bool:
        """Find the coin icon via template match and tap it. Returns True if found and tapped."""
        frame = self._get_frame()
        if frame is None:
            return False
        h, w = frame.shape[:2]
        # Coin icon lives in the top-right ~40% of the screen, top 15%
        result = self.buttons.find(frame, "coin_icon", threshold=0.7)
        if result is None:
            logger.warning(f"[{self.adb.name}] {self.APP_NAME}: Coin icon template not found; tapping configured coordinate {self.COIN_ICON}")
            self.adb.tap(*self.COIN_ICON)
            return True
        cx, cy = result
        # Sanity check: must be in top strip
        if cy > int(h * 0.15) or cx < int(w * 0.4):
            logger.warning(
                f"[{self.adb.name}] {self.APP_NAME}: Coin icon found at ({cx},{cy}) outside expected region; "
                f"tapping configured coordinate {self.COIN_ICON}"
            )
            self.adb.tap(*self.COIN_ICON)
            return True
        logger.info(f"[{self.adb.name}] {self.APP_NAME}: Coin icon found at ({cx},{cy}), tapping")
        self.adb.tap(cx, cy)
        return True

    def go_to_ad_page(self) -> bool:
        raise NotImplementedError

    def click_watch_ad(self) -> bool:
        raise NotImplementedError

    def handle_ad_result(self) -> bool:
        raise NotImplementedError

    def collect_reward(self) -> bool:
        raise NotImplementedError

    def handle_daily_limit(self):
        """Handle daily limit dialog by moving to the next rotating fake day."""
        logger.info(f"[{self.adb.name}] {self.APP_NAME}: Daily limit hit — starting date trick")

        # 1. Dismiss the dialog by tapping the button (already detected via template).
        #    Re-detect to get the exact tap coordinates.
        frame = self._get_frame()
        dismissed = False
        if frame is not None:
            result = self._find_daily_limit_button(frame)
            if result:
                cx, cy = result
                logger.info(f"[{self.adb.name}] {self.APP_NAME}: Tapping dismiss button at ({cx},{cy})")
                self.adb.tap(cx, cy)
                time.sleep(1.5)
                dismissed = True

        if not dismissed:
            # Fallback: tap the configured OK position
            logger.warning(f"[{self.adb.name}] {self.APP_NAME}: Could not find dismiss button — fallback tap")
            self.adb.tap(*self.OK_BTN)
            time.sleep(1.5)

        if not self._advance_fake_date("daily limit"):
            logger.error(f"[{self.adb.name}] {self.APP_NAME}: Date trick aborted; could not set fake date")
            return

        self._daily_limit_grace_until = time.time() + 45

        logger.info(f"[{self.adb.name}] {self.APP_NAME}: Waiting 3s after fake-date change before retrying")
        time.sleep(3)

        # Do not press BACK here. On TempSMS home screen that opens the exit
        # confirmation dialog. Daily-limit dismissal is handled explicitly above.
        time.sleep(1)

        ads_this_cycle = 0
        max_ads_per_cycle = 3
        while ads_this_cycle < max_ads_per_cycle:
            if self.should_stop():
                return "stopped"
            if self.go_to_ad_page():
                logger.info(f"[{self.adb.name}] {self.APP_NAME}: Continuing ads after date trick (attempt {ads_this_cycle + 1}/{max_ads_per_cycle})")
                ads_before = self.ads_watched
                result = self._aggressive_watch_now()
                if result in ("switch_app", "date_trick_blocked", "rate_limited", "stopped"):
                    return result
                if self.ads_watched > ads_before:
                    ads_this_cycle += self.ads_watched - ads_before
                # Check if daily limit appeared again — that means this fake date is exhausted
                frame = self._get_frame()
                if frame is not None:
                    dl = self._find_daily_limit_button(frame)
                    if dl:
                        logger.info(f"[{self.adb.name}] {self.APP_NAME}: Daily limit reached again after date trick")
                        break
                if self._interruptible_sleep(1):
                    return "stopped"
            else:
                logger.warning(f"[{self.adb.name}] {self.APP_NAME}: Could not reach ad page after date trick")
                break

        logger.info(f"[{self.adb.name}] {self.APP_NAME}: Date trick complete; collected {ads_this_cycle} ad(s)")
        return None

    def run_loop(self):
        logger.info(f"[{self.adb.name}] {self.APP_NAME}: Starting loop (no limit)")
        self._started_at = time.time()

        # Always start from a clean app session.
        if self.should_stop():
            return "stopped"
        self.launch()

        while True:
            if self.should_stop():
                logger.info(f"[{self.adb.name}] {self.APP_NAME}: Stop requested")
                return "stopped"

            self.clear_stuck_dialogs()
            if self.should_stop():
                return "stopped"
            state = self.detect_state()
            logger.info(f"[{self.adb.name}] {self.APP_NAME}: State: {state.value}")

            # Already in ad — wait for it
            if state == AppState.AD_PLAYING:
                result = self.handle_ad_result()
                if self.should_stop() or result == "stopped":
                    return "stopped"
                if result in ("switch_app", "date_trick_blocked", "rate_limited"):
                    return result
                if result:
                    return result
                continue

            # Daily limit dialog
            if state == AppState.DAILY_LIMIT:
                result = self.handle_daily_limit()
                if self.should_stop() or result == "stopped":
                    return "stopped"
                if result:
                    return result
                continue

            if state == AppState.LOADING_DIALOG:
                if time.time() < self._post_ad_grace_until:
                    logger.info(
                        f"[{self.adb.name}] {self.APP_NAME}: Loading during post-ad grace; "
                        "waiting for reward/ad page to settle"
                    )
                    if self._interruptible_sleep(3):
                        return "stopped"
                    continue

                if time.time() < self._daily_limit_grace_until:
                    logger.info(
                        f"[{self.adb.name}] {self.APP_NAME}: Loading after daily-limit date change; "
                        "waiting instead of switching app"
                    )
                    if self._interruptible_sleep(3):
                        return "stopped"
                    self.go_to_ad_page()
                    continue

                # After an ad attempt, stuck loader ⇒ no-reward path for GetSMS/TempSMS
                # (2x loader/no coins = daily rate limit; date change will not help).
                if self._is_coin_farm_app() and (
                    self.ads_watched > 0 or self.post_ad_no_reward_streak > 0 or self.watch_now_clicks > 0
                ):
                    result = self._handle_post_ad_no_reward("loading dialog in main loop")
                    if result:
                        return result
                    # Already counted this ad's no-reward; wait, then try another Watch Now.
                    if self._interruptible_sleep(2):
                        return "stopped"
                    continue

                self.post_ad_loading_retries += 1
                if self.post_ad_loading_retries >= 2:
                    reason = "startup blocked loading" if time.time() - self._started_at < 90 else "repeated loading"
                    logger.warning(
                        f"[{self.adb.name}] {self.APP_NAME}: Loading dialog detected twice ({reason}); "
                        "treating as rate limit / blocked"
                    )
                    self.post_ad_loading_retries = 0
                    self._in_post_ad_retry = False
                    # Startup-only loading with no ad attempts: still exit this app.
                    if self._is_coin_farm_app():
                        return "rate_limited"
                    return "date_trick_blocked"

                logger.info(
                    f"[{self.adb.name}] {self.APP_NAME}: Loading dialog detected; "
                    "waiting briefly (1/2)"
                )
                if self._interruptible_sleep(1):
                    return "stopped"
                continue

            # Exit confirmation dialog
            if state == AppState.EXIT_DIALOG:
                self._dismiss_exit_dialog()
                continue

            # Reward pending
            if state in (AppState.REWARD_RECEIVED, AppState.REWARD_GRANTED):
                self.collect_reward()
                continue

            # Need to navigate to ad page
            if state != AppState.AD_PAGE:
                if not self.is_app_running():
                    # stop_tracker closes the app; do not relaunch while stopping
                    if self.should_stop():
                        return "stopped"
                    self.launch()
                    continue
                if not self.go_to_ad_page():
                    logger.warning(f"[{self.adb.name}] {self.APP_NAME}: Could not navigate to ad page, retrying after delay")
                    if self._interruptible_sleep(3):
                        return "stopped"
                    continue
                state = self.detect_state()

            if self.should_stop():
                return "stopped"

            # We should be on AD_PAGE now — try to click Watch Now aggressively
            if state == AppState.AD_PAGE:
                if self._in_post_ad_retry:
                    logger.info(f"[{self.adb.name}] {self.APP_NAME}: Changing date one more time before next Watch Now")
                    if not self._advance_fake_date("post-loading retry"):
                        return "date_trick_blocked"
                    self._in_post_ad_retry = False
                    if self._interruptible_sleep(1):
                        return "stopped"
                result = self._aggressive_watch_now()
                if self.should_stop() or result == "stopped":
                    return "stopped"
                if result in ("switch_app", "date_trick_blocked", "rate_limited"):
                    return result
            else:
                # Unknown state — retry navigation on next loop. Do not press BACK here;
                # on the home screen that opens the exit confirmation dialog.
                if self._interruptible_sleep(2):
                    return "stopped"

    def _record_ad_load_success(self):
        if self.failed_ad_load_batches:
            logger.info(f"[{self.adb.name}] {self.APP_NAME}: Resetting failed ad-load counter")
        self.failed_ad_load_batches = 0
        self._ad_reset_attempted_at = 0
        self.post_ad_no_reward_streak = 0
        self.post_ad_loading_retries = 0
        self._no_reward_counted_for_current_ad = False

    def _is_coin_farm_app(self) -> bool:
        return self.APP_NAME in ("getsms", "tempsms")

    def _is_post_ad_loading_ui(self, frame) -> bool:
        """True when a loading popup/spinner is shown on the purchase/ad page."""
        if frame is None:
            return False
        if self._is_blocking_loading_dialog(frame) or self._is_loading_dialog(frame):
            return True
        if self.APP_NAME == "tempsms" and self._is_tempsms_inline_loading(frame):
            return True
        # GetSMS often exposes the literal Loading label via UIAutomator.
        if self.APP_NAME == "getsms":
            try:
                if self.adb.has_ui_text(["Loading", "Loading..."]):
                    return True
            except Exception:
                pass
        return False

    def _handle_post_ad_no_reward(self, reason: str) -> str | None:
        """Handle post-ad loader / no-coin outcome.

        Counts at most once per finished ad. GetSMS/TempSMS: 2 consecutive
        no-reward outcomes => daily rate limit (exit app; do not date-trick).
        """
        if self._no_reward_counted_for_current_ad:
            return None

        self._no_reward_counted_for_current_ad = True
        self.post_ad_no_reward_streak += 1
        count = self.post_ad_no_reward_streak
        self.post_ad_loading_retries = count
        logger.warning(
            f"[{self.adb.name}] {self.APP_NAME}: Post-ad no reward ({reason}) "
            f"streak {count}/2"
        )

        if count < 2:
            logger.info(
                f"[{self.adb.name}] {self.APP_NAME}: Allowing one more ad attempt after no-reward loading"
            )
            # Do not advance fake dates for this case — user reports it has no effect.
            self._in_post_ad_retry = False
            # Leave the stuck loader so the next Watch Now can run.
            try:
                self.go_back()
                time.sleep(0.5)
                self.go_to_ad_page()
            except Exception:
                pass
            return None

        self.post_ad_no_reward_streak = 0
        self.post_ad_loading_retries = 0
        self._in_post_ad_retry = False

        logger.warning(
            f"[{self.adb.name}] {self.APP_NAME}: Rate limit reached for today "
            f"(loader after ad with no coins x2); date change will not help — exiting app"
        )
        return "rate_limited"

    def _record_ad_load_failure(self) -> str | None:
        self.failed_ad_load_batches += 1
        count = self.failed_ad_load_batches
        logger.warning(f"[{self.adb.name}] {self.APP_NAME}: Failed ad-load batch #{count}")

        if count >= 15:
            logger.warning(f"[{self.adb.name}] {self.APP_NAME}: Failed ad-load threshold reached; switching app")
            self.failed_ad_load_batches = 0
            self._ad_reset_attempted_at = 0
            return "switch_app"

        if count % 2 == 0:
            logger.info(f"[{self.adb.name}] {self.APP_NAME}: Re-entering ad page after {count} failed batches")
            self.go_back()
            time.sleep(0.5)
            if not self.is_app_running():
                self.force_restart()
            self.go_to_ad_page()

        if count % 10 == 0:
            logger.info(f"[{self.adb.name}] {self.APP_NAME}: Cooldown after {count} failed batches")
            time.sleep(10)

        return None

    def _advance_fake_date(self, reason: str = "date trick") -> bool:
        """Move to the next rotating fake date using host date as the anchor."""
        import datetime

        real_date = datetime.date.today()
        offset = self.date_trick_offset_days
        fake_date = real_date + datetime.timedelta(days=offset)
        fake_date_str = fake_date.isoformat()
        logger.info(f"[{self.adb.name}] {self.APP_NAME}: Advancing date to {fake_date_str} (+{offset}d) for {reason}")
        if not self.adb.set_date(fake_date_str):
            return False
        self.date_trick_offset_days = 1 if offset >= 10 else offset + 1
        return True

    def _reset_ad_environment(self):
        """Best-effort ad reset without root or destructive Google account changes."""
        logger.info(f"[{self.adb.name}] {self.APP_NAME}: Attempting ad environment reset")
        if self._reset_google_ad_id():
            logger.info(f"[{self.adb.name}] {self.APP_NAME}: Google Advertising ID reset attempted")
        else:
            logger.warning(f"[{self.adb.name}] {self.APP_NAME}: Google Advertising ID reset UI not available; using fallback reset")

        self.adb.close_app("com.android.vending")
        self.adb.shell("am force-stop com.google.android.gms")
        self.adb.shell("am force-stop com.google.android.gsf")
        self.force_restart()
        self.go_to_ad_page()

    def _reset_google_ad_id(self) -> bool:
        """Open Google Ads settings and reset the Advertising ID via UI text."""
        logger.info(f"[{self.adb.name}] {self.APP_NAME}: Opening Google Ads settings")

        self.adb.shell("am start -a com.google.android.gms.ads.settings.ADS_SETTINGS")
        time.sleep(2)

        if not self.adb.tap_ui_text([
            "reset advertising id",
            "reset advertising ID",
            "reset ad id",
            "reset",
        ], timeout=6):
            self.adb.shell("am start -n com.google.android.gms/com.google.android.gms.ads.settings.AdsSettingsActivity")
            time.sleep(2)
            if not self.adb.tap_ui_text([
                "reset advertising id",
                "reset advertising ID",
                "reset ad id",
                "reset",
            ], timeout=6):
                return False

        time.sleep(1)
        self.adb.tap_ui_text(["ok", "reset", "confirm"], timeout=4)
        time.sleep(1)
        self.adb.press_key("BACK")
        time.sleep(1)
        return True

    def _focus_state(self) -> str:
        """Classify current focused window: ours, ad, google_play, browser, or other."""
        activity = self.adb.get_focused_activity()
        if not activity:
            return "other"
        if self.PACKAGE_NAME in activity:
            # Ad runs inside our app (AdActivity) — treat as ad playing
            if "AdActivity" in activity or "ads" in activity.lower():
                return "ad"
            return "ours"
        if "com.android.vending" in activity or "phonesky" in activity.lower():
            return "google_play"
        if "com.android.chrome" in activity or "browser" in activity.lower():
            return "browser"
        # Launcher and settings overlays are transient — treat as still "ours"
        # so we don't press BACK and break the current flow.
        if "launcher" in activity.lower() or "settings" in activity.lower():
            return "ours"
        return "other"

    def _check_focus(self) -> str:
        """Backwards-compatible focus check. Returns 'ours' or 'away'."""
        return "ours" if self._focus_state() == "ours" else "away"

    def _recover_from_redirect(self, focus_state: str):
        """Recover when an ad redirects to Google Play/browser/launcher."""
        logger.info(f"[{self.adb.name}] {self.APP_NAME}: Redirect/opened {focus_state}, going back to app")

        # Press BACK first to return to our app
        self.adb.press_key("BACK")
        time.sleep(1.5)

        # Check if we're back in our app
        if self.is_app_running():
            return

        # If still not in our app, force-close the redirect app
        if focus_state == "google_play":
            self.adb.close_app("com.android.vending")
        time.sleep(1)

        # Relaunch our app if needed
        if not self.is_app_running():
            self.adb.launch_app(self.PACKAGE_NAME, self.ACTIVITY_NAME)
            time.sleep(3)

    def _find_in_app_redirect_continue(self, frame) -> tuple[int, int] | None:
        """Detect Play-style in-app redirect overlays with a top Continue-to-app bar."""
        import cv2
        import numpy as np

        h, w = frame.shape[:2]
        if h < 600 or w < 300:
            return None

        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        # The overlay in the reported screenshot has a large Google-blue Install
        # CTA near the bottom. This keeps the detector from firing on normal ads.
        bottom = hsv[int(h * 0.82):int(h * 0.97), int(w * 0.05):int(w * 0.95)]
        blue = cv2.inRange(bottom, np.array([95, 90, 90]), np.array([125, 255, 255]))
        blue_pixels = cv2.countNonZero(blue)
        if blue_pixels < int(w * h * 0.015):
            return None

        # The top bar is mostly white/light gray and contains a small blue arrow
        # on the right. Tap the arrow area to return to the app immediately.
        top = hsv[0:int(h * 0.08), :]
        white = cv2.inRange(top, np.array([0, 0, 180]), np.array([180, 55, 255]))
        if cv2.countNonZero(white) < int(w * h * 0.035):
            return None

        right_top = hsv[0:int(h * 0.08), int(w * 0.75):w]
        arrow_blue = cv2.inRange(right_top, np.array([95, 80, 80]), np.array([125, 255, 255]))
        if cv2.countNonZero(arrow_blue) < 20:
            return None

        return (int(w * 0.92), int(h * 0.035))

    def _dismiss_in_app_redirect_overlay(self, frame=None) -> bool:
        if frame is None:
            frame = self._get_frame()
        if frame is None:
            return False

        target = self._find_in_app_redirect_continue(frame)
        if not target:
            return False

        logger.info(f"[{self.adb.name}] {self.APP_NAME}: In-app redirect overlay detected, tapping Continue to app at {target}")
        self.adb.tap(*target)
        time.sleep(1)
        return True

    def _guarded_watch_now_taps(self, x: int, y: int, tap_count: int) -> str:
        """Tap Watch Now quickly, but stop as soon as the ad/redirect opens."""
        for index in range(tap_count):
            self.adb.tap(x, y)
            if index == tap_count - 1:
                return "tapped"

            time.sleep(0.12)
            focus_state = self._focus_state()
            if focus_state == "ad":
                logger.info(f"[{self.adb.name}] {self.APP_NAME}: Ad opened after Watch Now tap {index + 1}/{tap_count}")
                return "ad"
            if focus_state in ("google_play", "browser"):
                logger.info(f"[{self.adb.name}] {self.APP_NAME}: Redirect opened after Watch Now tap {index + 1}/{tap_count}")
                return focus_state
            if focus_state != "ours":
                return focus_state

            frame = self._get_frame()
            if frame is None:
                return "unknown"
            if self._dismiss_in_app_redirect_overlay(frame):
                return "ad"
            if self._find_button(frame, ["watch_now"], threshold=0.65):
                continue
            if self._find_daily_limit_button(frame):
                return "daily_limit"
            return "ad_page_changed"

        return "tapped"

    def _tap_watch_now(self, tap_count: int = 1) -> bool | str:
        """Tap Watch Now only when its app-specific template is confidently visible."""
        import cv2
        import numpy as np

        frame = self._get_frame()
        if frame is None:
            return False

        if self._is_blocking_loading_dialog(frame) or (
            self.APP_NAME == "tempsms" and self._is_tempsms_inline_loading(frame)
        ):
            logger.info(f"[{self.adb.name}] {self.APP_NAME}: Watch Now blocked by loading dialog")
            return "loading_dialog"

        # Daily-limit dialog can appear over the ad page right after Watch Now.
        # Do not click anything else while it is visible.
        daily_limit = self._find_daily_limit_button(frame)
        if daily_limit:
            cx, cy = daily_limit
            logger.info(f"[{self.adb.name}] {self.APP_NAME}: Daily limit button found at ({cx},{cy}) while looking for Watch Now")
            return "daily_limit"

        result = self._find_button(frame, ["watch_now"], threshold=0.65)
        if result:
            _, cx, cy = result
            logger.info(f"[{self.adb.name}] {self.APP_NAME}: Watch Now template found at ({cx},{cy}), guarded tapping up to x{tap_count}")
            if tap_count > 1:
                tap_status = self._guarded_watch_now_taps(cx, cy, tap_count)
                if tap_status in ("ad", "google_play", "browser", "daily_limit"):
                    return tap_status
            else:
                self.adb.tap(cx, cy)
            return True

        # Fallback: detect the colored button itself in the bottom-right area.
        # GetSMS button is blue; TempSMS button is pink/red. Restricting to this
        # region avoids tapping purchase/package buttons elsewhere on the page.
        h, w = frame.shape[:2]
        x0 = int(w * 0.55)
        y0 = int(h * 0.84)
        roi = frame[y0:h, x0:w]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

        blue = cv2.inRange(hsv, np.array([100, 100, 120]), np.array([130, 255, 255]))
        pink = cv2.inRange(hsv, np.array([150, 80, 120]), np.array([180, 255, 255]))
        pink |= cv2.inRange(hsv, np.array([0, 80, 120]), np.array([10, 255, 255]))
        mask = blue | pink

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best = None
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < 800:
                continue
            x, y, cw, ch = cv2.boundingRect(contour)
            aspect = cw / max(ch, 1)
            if 1.4 <= aspect <= 6 and 24 <= ch <= 80:
                if best is None or area > best[0]:
                    best = (area, x, y, cw, ch)

        if best:
            _, x, y, cw, ch = best
            cx = x0 + x + cw // 2
            cy = y0 + y + ch // 2
            logger.info(f"[{self.adb.name}] {self.APP_NAME}: Watch Now color button found at ({cx},{cy}), guarded tapping up to x{tap_count}")
            if tap_count > 1:
                tap_status = self._guarded_watch_now_taps(cx, cy, tap_count)
                if tap_status in ("ad", "google_play", "browser", "daily_limit"):
                    return tap_status
            else:
                self.adb.tap(cx, cy)
            return True

        logger.warning(f"[{self.adb.name}] {self.APP_NAME}: Watch Now button not found safely")
        return False

    def _aggressive_watch_now(self) -> str | None:
        """Tap Watch Now in fast bursts, then check whether the ad opened."""
        clicks_per_batch = 5
        self.watch_now_clicks += 1

        logger.info(f"[{self.adb.name}] {self.APP_NAME}: Starting fast Watch Now spam ({clicks_per_batch} taps)")
        for attempt in range(2):
            focus_state = self._focus_state()

            if focus_state == "ad":
                logger.info(f"[{self.adb.name}] {self.APP_NAME}: Focus left app; ad started")
                self._record_ad_load_success()
                return self._wait_for_ad_finish()

            if focus_state in ("google_play", "browser"):
                self._recover_from_redirect(focus_state)
                return None

            # "other" may be a transient overlay/notification. Wait briefly
            # for focus to return before treating it as a real redirect.
            if focus_state == "other":
                time.sleep(1)
                focus_state = self._focus_state()
                if focus_state in ("ours", "ad"):
                    pass  # focus came back, keep spamming
                elif focus_state in ("google_play", "browser"):
                    self._recover_from_redirect(focus_state)
                    return None
                else:
                    # Still "other" — press BACK once and continue spamming
                    logger.info(f"[{self.adb.name}] {self.APP_NAME}: Focus still away after 1s, pressing BACK")
                    try:
                        self.adb.press_key("BACK")
                    except Exception:
                        pass
                    time.sleep(1)

            frame = self._get_frame()
            if frame is not None and self._find_daily_limit_button(frame):
                return self.handle_daily_limit()

            logger.info(f"[{self.adb.name}] {self.APP_NAME}: Finding Watch Now for fast tap burst")
            tap_result = self._tap_watch_now(tap_count=clicks_per_batch)
            if tap_result == "daily_limit":
                return self.handle_daily_limit()
            if tap_result == "loading_dialog":
                # Watch Now while loader is up — count as no-earn for coin farm apps.
                if self._is_coin_farm_app():
                    result = self._handle_post_ad_no_reward("Watch Now blocked by Loading...")
                    if result:
                        return result
                return None
            if tap_result == "ad":
                self._record_ad_load_success()
                return self._wait_for_ad_finish()
            if tap_result in ("google_play", "browser"):
                self._recover_from_redirect(tap_result)
                return None
            if not tap_result:
                time.sleep(0.2)
                continue

            # Check once after the whole burst instead of waiting after every tap.
            time.sleep(0.35)
            focus_state = self._focus_state()
            if focus_state == "ad":
                logger.info(f"[{self.adb.name}] {self.APP_NAME}: Focus left app; ad started")
                self._record_ad_load_success()
                return self._wait_for_ad_finish()
            if focus_state in ("google_play", "browser"):
                self._recover_from_redirect(focus_state)
                return None

            frame = self._get_frame()
            if frame is not None and self._find_daily_limit_button(frame):
                return self.handle_daily_limit()

            if attempt == 0:
                time.sleep(0.2)

        time.sleep(0.3)
        focus_state = self._focus_state()
        if focus_state == "ad":
            logger.info(f"[{self.adb.name}] {self.APP_NAME}: Ad started after spam")
            self._record_ad_load_success()
            return self._wait_for_ad_finish()
        if focus_state in ("google_play", "browser"):
            self._recover_from_redirect(focus_state)
            return None

        return self._record_ad_load_failure()

    def _tap_reward_granted_x(self, frame) -> bool:
        """Fallback: tap potential close buttons in the top corners."""
        import cv2
        import numpy as np

        h, w = frame.shape[:2]
        top_bar = frame[0:int(h * 0.12), :]
        gray = cv2.cvtColor(top_bar, cv2.COLOR_BGR2GRAY)
        
        tap_y = int(h * 0.05)

        # 1. Try right corner first
        tap_x = int(w * 0.95)
        logger.info(f"[{self.adb.name}] {self.APP_NAME}: Fallback tapping top-right corner at ({tap_x},{tap_y})")
        self.adb.tap(tap_x, tap_y)
        time.sleep(3)

        # Check if tap triggered a redirect (Google Play / browser)
        focus = self._focus_state()
        if focus in ("google_play", "browser"):
            self._recover_from_redirect(focus)
            return True

        # Handle any popup that appeared (continue, reward OK, etc.)
        frame2 = self._get_frame()
        if frame2 is not None:
            result = self._find_button(frame2, ["continue_btn"], threshold=0.7)
            if result:
                _, cx, cy = result
                logger.info(f"[{self.adb.name}] {self.APP_NAME}: Found continue_btn after right tap, tapping")
                self.adb.tap(cx, cy)
                time.sleep(2)
                return True
            if self._tap_reward_ok(timeout=3):
                return True

        # 2. Try left corner
        tap_x = int(w * 0.05)
        logger.info(f"[{self.adb.name}] {self.APP_NAME}: Fallback tapping top-left corner at ({tap_x},{tap_y})")
        self.adb.tap(tap_x, tap_y)
        time.sleep(3)

        # Check if tap triggered a redirect
        focus = self._focus_state()
        if focus in ("google_play", "browser"):
            self._recover_from_redirect(focus)
            return True

        # Handle any popup after left tap
        frame3 = self._get_frame()
        if frame3 is not None:
            result = self._find_button(frame3, ["continue_btn"], threshold=0.7)
            if result:
                _, cx, cy = result
                logger.info(f"[{self.adb.name}] {self.APP_NAME}: Found continue_btn after left tap, tapping")
                self.adb.tap(cx, cy)
                time.sleep(2)
                return True
            if self._tap_reward_ok(timeout=3):
                return True

        return False

    def _tap_reward_granted_close(self) -> bool:
        """Close reward-granted screens using the detected X position when possible."""
        frame = self._get_frame()
        if frame is not None:
            result = self._find_button(frame, ["x_button"], threshold=0.74)
            if result:
                _, cx, cy = result
                self._last_reward_x = (cx, cy)
                logger.info(f"[{self.adb.name}] {self.APP_NAME}: Tapping detected reward X at ({cx},{cy})")
                self.adb.tap(cx, cy)
                return True

        if self._last_reward_x:
            cx, cy = self._last_reward_x
            logger.info(f"[{self.adb.name}] {self.APP_NAME}: Tapping last detected reward X at ({cx},{cy})")
            self.adb.tap(cx, cy)
            return True

        logger.info(f"[{self.adb.name}] {self.APP_NAME}: Tapping configured reward X fallback {self.REWARD_X}")
        self.adb.tap(*self.REWARD_X)
        return True

    def _tap_reward_ok(self, timeout: float = 8.0) -> bool:
        """Find and tap the reward OK button — template first, then pixel fallback."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            frame = self._get_frame()
            if frame is None:
                time.sleep(0.5)
                continue

            result = self._find_button(frame, ["ok_button"], threshold=0.55)
            if result:
                name, cx, cy = result
                logger.info(f"[{self.adb.name}] {self.APP_NAME}: Reward dialog found, tapping {name} at ({cx},{cy})")
                self.adb.tap(cx, cy)
                time.sleep(2)
                return True

            # Pixel fallback: look for a centered dark dialog with a red/pink OK button
            if self._tap_reward_ok_pixel(frame):
                time.sleep(2)
                return True

            time.sleep(0.5)

        return False

    def _tap_reward_ok_pixel(self, frame) -> bool:
        """Pixel-based fallback for reward dialog OK button."""
        import cv2
        import numpy as np

        h, w = frame.shape[:2]
        # Look for a large red/pink blob in the center-bottom area (the OK button)
        x1 = int(w * 0.35)
        x2 = int(w * 0.85)
        y1 = int(h * 0.45)
        y2 = int(h * 0.72)
        roi = frame[y1:y2, x1:x2]
        if roi.size == 0:
            return False

        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        # Red/pink OK button
        r1 = cv2.inRange(hsv, np.array([0, 120, 150]), np.array([12, 255, 255]))
        r2 = cv2.inRange(hsv, np.array([168, 120, 150]), np.array([180, 255, 255]))
        mask = r1 | r2

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            area = cv2.contourArea(contour)
            if 2000 < area < 25000:
                x, y, cw, ch = cv2.boundingRect(contour)
                aspect = cw / max(ch, 1)
                if 1.5 < aspect < 6 and 25 <= ch <= 80:
                    cx = x1 + x + cw // 2
                    cy = y1 + y + ch // 2
                    logger.info(f"[{self.adb.name}] {self.APP_NAME}: Pixel fallback found red OK button at ({cx},{cy}), tapping")
                    self.adb.tap(cx, cy)
                    return True

        return False

    def _wait_for_x_after_continue(self, timeout: float = 12.0) -> bool:
        """After Continue, the final X often appears a few seconds later."""
        start = time.time()
        deadline = time.time() + timeout
        fallback_tapped = False
        while time.time() < deadline:
            focus_state = self._focus_state()
            if focus_state in ("google_play", "browser"):
                self._recover_from_redirect(focus_state)
                return True
            if focus_state == "ours":
                return False

            frame = self._get_frame()
            if frame is not None:
                result = self._find_button(frame, ["x_button"], threshold=0.74)
                if result:
                    _, cx, cy = result
                    logger.info(f"[{self.adb.name}] {self.APP_NAME}: Found x_button after Continue at ({cx},{cy}), tapping")
                    self.adb.tap(cx, cy)
                    time.sleep(0.5)
                    return True
                if not fallback_tapped and time.time() - start >= 5:
                    h, w = frame.shape[:2]
                    tap_x = int(w * 0.95)
                    tap_y = int(h * 0.05)
                    logger.info(f"[{self.adb.name}] {self.APP_NAME}: X not matched 5s after Continue; fallback tapping top-right at ({tap_x},{tap_y})")
                    self.adb.tap(tap_x, tap_y)
                    fallback_tapped = True
                    time.sleep(1)
                    continue
            time.sleep(0.35)
        return False

    def _wait_for_ad_finish(self):
        """Wait for ad — look for X/Continue buttons to skip, then OK to collect reward."""
        logger.info(f"[{self.adb.name}] {self.APP_NAME}: Waiting for ad to finish...")
        # New ad attempt — allow one no-reward count for this completion.
        self._no_reward_counted_for_current_ad = False
        start = time.time()
        last_click_pos = None
        last_click_time = 0
        reward_x_clicks = 0

        while time.time() - start < 90:
            if self.should_stop():
                logger.info(f"[{self.adb.name}] {self.APP_NAME}: Stop requested during ad wait")
                return "stopped"

            elapsed = time.time() - start
            focus_state = self._focus_state()

            if focus_state in ("google_play", "browser"):
                self._recover_from_redirect(focus_state)
                if self._interruptible_sleep(2):
                    return "stopped"
                continue

            if focus_state == "other":
                # Transient overlay/notification — wait briefly for it to pass
                if self._interruptible_sleep(1):
                    return "stopped"
                focus_state = self._focus_state()
                if focus_state in ("ours", "ad"):
                    continue  # came back, keep going
                if focus_state in ("google_play", "browser"):
                    self._recover_from_redirect(focus_state)
                    if self._interruptible_sleep(2):
                        return "stopped"
                    continue
                # Still "other" — press BACK once
                logger.info(f"[{self.adb.name}] {self.APP_NAME}: Focus still away, pressing BACK")
                try:
                    self.adb.press_key("BACK")
                except Exception:
                    pass
                if self._interruptible_sleep(2):
                    return "stopped"
                continue

            if focus_state == "ours":
                frame = self._get_frame()
                if frame is not None and self._is_in_app_ad_overlay(frame):
                    focus_state = "ad"
                elif frame is not None and self._dismiss_in_app_redirect_overlay(frame):
                    focus_state = "ad"
                else:
                    # Back to our app — look for reward dialog
                    logger.info(f"[{self.adb.name}] {self.APP_NAME}: Focus returned after {elapsed:.0f}s")
                    if self._interruptible_sleep(2):
                        return "stopped"

                    ads_before = self.ads_watched

                    # Check reward FIRST — it sits on top of daily limit dialog
                    if self._tap_reward_ok(timeout=6):
                        self.ads_watched += 1
                        self._record_ad_load_success()
                        self._post_ad_grace_until = 0
                        logger.info(f"[{self.adb.name}] {self.APP_NAME}: Ad #{self.ads_watched} collected!")
                        return

                    if self.should_stop():
                        return "stopped"

                    state = self.detect_state()
                    if state in (AppState.REWARD_RECEIVED, AppState.REWARD_GRANTED):
                        self.collect_reward()
                        if self.ads_watched > ads_before:
                            self._record_ad_load_success()
                        return

                    if state == AppState.DAILY_LIMIT:
                        return self.handle_daily_limit()

                    # Inline / popup Loading... (Watch Now may still be visible).
                    if self._is_post_ad_loading_ui(frame):
                        return self._handle_post_ad_no_reward("loading UI after ad (no coins)")

                    if state == AppState.LOADING_DIALOG:
                        return self._handle_post_ad_no_reward("loading dialog after ad")

                    if state == AppState.AD_PAGE:
                        logger.info(f"[{self.adb.name}] {self.APP_NAME}: Ad finished, back on ad page")
                        self._post_ad_grace_until = time.time() + 20
                        if self._is_coin_farm_app():
                            # Re-check loader after a short settle — it often appears slightly late.
                            if self._interruptible_sleep(1.5):
                                return "stopped"
                            settle_frame = self._get_frame()
                            if self._is_post_ad_loading_ui(settle_frame):
                                return self._handle_post_ad_no_reward(
                                    "Loading after ad settle (no coins)"
                                )
                            # Full ad finished with no reward UI ⇒ no earn (both apps).
                            return self._handle_post_ad_no_reward("ad finished without reward/coins")
                        return

                    if self._is_post_ad_loading_ui(frame):
                        logger.info(
                            f"[{self.adb.name}] {self.APP_NAME}: Loading popup visible after ad; "
                            "counting as no-reward instead of OK fallback"
                        )
                        return self._handle_post_ad_no_reward("loading popup before OK fallback")

                    # Fallback: try OK once; only count real rewards for coin farm apps.
                    logger.info(f"[{self.adb.name}] {self.APP_NAME}: Tapping OK position (fallback)")
                    self.adb.tap(*self.OK_BTN)
                    if self._interruptible_sleep(2):
                        return "stopped"

                    if self._is_coin_farm_app():
                        post_frame = self._get_frame()
                        post_state = self.detect_state()
                        if post_state in (AppState.REWARD_RECEIVED, AppState.REWARD_GRANTED):
                            self.collect_reward()
                            if self.ads_watched > ads_before:
                                self._record_ad_load_success()
                            return
                        if post_state == AppState.LOADING_DIALOG or self._is_post_ad_loading_ui(post_frame):
                            return self._handle_post_ad_no_reward("loader after OK fallback")
                        # No coins confirmed — do not fake-increment ads_watched.
                        return self._handle_post_ad_no_reward("no coins after ad (OK fallback)")

                    self.ads_watched += 1
                    self._record_ad_load_success()
                    self._post_ad_grace_until = 0
                    logger.info(f"[{self.adb.name}] {self.APP_NAME}: Ad #{self.ads_watched} collected!")
                    return

            # Still in ad — look for X or Continue button to skip
            if self.buttons.available:
                frame = self._get_frame()
                if frame is not None:
                    if self._dismiss_in_app_redirect_overlay(frame):
                        continue

                    # First only click explicit known skip/close templates.
                    result = self._find_button(frame, ["x_button", "continue_btn"], threshold=0.8)
                    if result:
                        name, cx, cy = result
                        h, w = frame.shape[:2]
                        tap_x, tap_y = cx, cy
                        if name == "continue_btn":
                            # Some ads require tapping the small arrow on the right side
                            # of the Continue pill, not the text center.
                            tap_x = min(w - 15, cx + 50)
                        # Don't click same position twice within 3s
                        pos = (tap_x, tap_y)
                        now = time.time()
                        if pos == last_click_pos and now - last_click_time < 3:
                            if self._interruptible_sleep(0.3):
                                return "stopped"
                            continue
                        logger.info(f"[{self.adb.name}] {self.APP_NAME}: Found {name} at ({cx},{cy}), tapping ({tap_x},{tap_y})...")
                        self.adb.tap(tap_x, tap_y)
                        last_click_pos = pos
                        last_click_time = now
                        if name == "continue_btn":
                            if self._wait_for_x_after_continue(timeout=8):
                                continue
                        if self._interruptible_sleep(0.5):
                            return "stopped"
                        continue

                    # Fallback: if ad is stuck for > 40s and template matching hasn't found a close button,
                    # try tapping the top corners (e.g. for dark/unrecognized reward pills).
                    if elapsed > 70 and self._tap_reward_granted_x(frame):
                        reward_x_clicks += 1
                        if self._interruptible_sleep(1):
                            return "stopped"
                        if reward_x_clicks >= 3 and self._check_focus() == "away":
                            logger.info(f"[{self.adb.name}] {self.APP_NAME}: Reward X did not close ad, pressing BACK")
                            self.go_back()
                            reward_x_clicks = 0
                            if self._interruptible_sleep(2):
                                return "stopped"
                        continue

            if self._interruptible_sleep(1):
                return "stopped"

        logger.warning(f"[{self.adb.name}] {self.APP_NAME}: Ad wait timed out (90s)")
        self.go_back()
