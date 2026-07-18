import time
from apps.base_app import BaseApp, AppState
from utils.logger import setup_logger

logger = setup_logger("tempsms")


class TempSMSApp(BaseApp):
    PACKAGE_NAME = "com.secondphone.tempsms"
    ACTIVITY_NAME = "com.temp.sms.activity.SplashActivity"
    APP_NAME = "tempsms"
    IS_DARK = True

    def go_to_ad_page(self) -> bool:
        state = self.detect_state()
        if state in (AppState.AD_PAGE, AppState.AD_PLAYING, AppState.REWARD_GRANTED, AppState.REWARD_RECEIVED):
            return True

        # Find coin icon by template and tap it
        found = self._find_and_tap_coin_icon()
        if not found:
            logger.warning(f"[{self.adb.name}] TempSMS: Coin icon not found, cannot navigate to ad page")
            return False

        time.sleep(1.0)
        state = self.detect_state()
        if state == AppState.AD_PAGE:
            return True

        # One extra retry after a short wait
        time.sleep(0.5)
        return self.detect_state() == AppState.AD_PAGE

    def click_watch_ad(self) -> bool:
        pass

    def handle_ad_result(self) -> bool:
        return self._wait_for_ad_finish()

    def collect_reward(self) -> bool:
        state = self.detect_state()
        if state == AppState.REWARD_RECEIVED:
            if not self._tap_reward_ok(timeout=6):
                logger.info(f"[{self.adb.name}] TempSMS: Tapping OK fallback {self.OK_BTN}")
                self.adb.tap(*self.OK_BTN)
                time.sleep(2)

            time.sleep(2)
            self.clear_stuck_dialogs()
            self.ads_watched += 1
            self._record_ad_load_success()
            logger.info(f"[{self.adb.name}] TempSMS: Ad #{self.ads_watched} collected!")
            return True
        if state == AppState.REWARD_GRANTED:
            self._tap_reward_granted_close()
            time.sleep(2)
            self.clear_stuck_dialogs()
            return True
        return True
