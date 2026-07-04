"""Test fast focus detection via dumpsys window + Watch Now click loop."""
import sys
import time
import argparse

sys.path.insert(0, ".")

from automation.adb_controller import ADBController
from apps.base_app import BaseApp, AppState
from apps.tempsms import TempSMSApp
from apps.getsms import GetSMSApp
from config import APPS

APP_CLASSES = {"getsms": GetSMSApp, "tempsms": TempSMSApp}


def _make_app(device_id: str, app_key: str):
    adb = ADBController(name=app_key, device_id=device_id)
    return APP_CLASSES[app_key](adb), adb


def test_focus_detection(device_id: str, app_key: str):
    """Test that _check_focus() returns correct values."""
    app, adb = _make_app(device_id, app_key)

    print(f"=== Focus Detection Test ({app_key} / {device_id}) ===")

    # 1. Check initial focus 10 times, measure speed
    t0 = time.time()
    for i in range(10):
        focus = app._check_focus()
        elapsed = (time.time() - t0) * 1000 / (i + 1)
        print(f"  [{i+1}] focus={focus}  ({elapsed:.0f}ms avg)")

    # 2. Check if we can see the app
    running = app.is_app_running()
    print(f"  is_app_running={running}")

    # 3. If app is on ad page, try clicking Watch Now
    if running:
        state = app.detect_state()
        print(f"  state={state.value}")

        if state == AppState.AD_PAGE:
            print("  Clicking Watch Now + monitoring focus...")
            t0 = time.time()
            adb.tap(*app.WATCH_NOW)
            for i in range(15):
                time.sleep(0.5)
                focus = app._check_focus()
                elapsed = time.time() - t0
                print(f"  [{elapsed:.1f}s] focus={focus}")
                if focus != "ours":
                    print(f"  -> Focus changed to '{focus}' after {elapsed:.1f}s!")
                    break

    print()


def test_watch_now_loop(device_id: str, app_key: str, max_clicks: int = 10):
    """Test the aggressive Watch Now click loop for a few iterations."""
    app, adb = _make_app(device_id, app_key)

    print(f"=== Watch Now Loop Test ({app_key} / {device_id}, max {max_clicks} clicks) ===")

    if not app.is_app_running():
        print("  App not running, launching...")
        app.launch()

    app.go_to_ad_page()

    t0 = time.time()
    for i in range(max_clicks):
        focus = app._check_focus()
        elapsed = time.time() - t0

        if focus == "ad":
            print(f"  [{elapsed:.1f}s] Click #{i+1}: AD DETECTED!")
            print("  -> Ad window appeared, entering wait loop...")
            app._wait_for_ad_finish()
            print("  -> Ad finished.")
            break
        elif focus == "google_play":
            print(f"  [{elapsed:.1f}s] Click #{i+1}: Google Play popup, dismissing...")
            adb.tap(*app.GOOGLE_PLAY_X)
            time.sleep(1)
        elif focus == "other":
            print(f"  [{elapsed:.1f}s] Click #{i+1}: Focus LEFT app ({focus})")
            time.sleep(2)
            state = app.detect_state()
            print(f"  -> State after 2s: {state.value}")
            if state in (AppState.REWARD_RECEIVED, AppState.REWARD_GRANTED):
                print("  -> REWARD DETECTED! Collecting...")
                app.collect_reward()
                break
        else:
            print(f"  [{elapsed:.1f}s] Click #{i+1}: focus={focus}, clicking Watch Now")
            adb.tap(*app.WATCH_NOW)
            time.sleep(2)

    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="emulator-5554")
    parser.add_argument("--app", default="tempsms", choices=["getsms", "tempsms"])
    parser.add_argument("--mode", default="focus", choices=["focus", "loop"])
    parser.add_argument("--clicks", type=int, default=10)
    args = parser.parse_args()

    if args.mode == "focus":
        test_focus_detection(args.device, args.app)
    else:
        test_watch_now_loop(args.device, args.app, args.clicks)


if __name__ == "__main__":
    main()
