import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

from automation.adb_controller import ADBController


GETSMS_PACKAGE = "com.virtualnumber.sms"


def run_adb(adb: str, device_id: str, args: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        [adb, "-s", device_id] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        errors="replace",
    )


def discover_getsms_device(adb: str) -> str | None:
    devices = ADBController.discover_devices()
    for device_id in devices:
        state = run_adb(adb, device_id, ["get-state"], timeout=5).stdout.strip()
        if state != "device":
            continue

        pid = get_pid(adb, device_id)
        if pid:
            return device_id

        focus = run_adb(adb, device_id, ["shell", "dumpsys window windows"], timeout=10).stdout
        if GETSMS_PACKAGE in focus:
            return device_id

    return None


def get_pid(adb: str, device_id: str) -> str | None:
    result = run_adb(adb, device_id, ["shell", f"pidof {GETSMS_PACKAGE}"], timeout=5)
    pid = result.stdout.strip().split()
    if pid:
        return pid[0]
    return None


def package_uids(adb: str, device_id: str) -> set[str]:
    result = run_adb(adb, device_id, ["shell", f"dumpsys package {GETSMS_PACKAGE}"], timeout=10)
    return set(re.findall(r"userId=(\d+)|uid=(\d+)", result.stdout)[0]) if "userId=" in result.stdout or "uid=" in result.stdout else set()


def should_print(line: str, pid: str | None, extra_terms: list[str]) -> bool:
    lower = line.lower()

    if pid and re.search(rf"\b{re.escape(pid)}\b", line):
        return True

    terms = [
        GETSMS_PACKAGE,
        "getsms",
        "virtualnumber",
        "sms.activate",
        "okhttp",
        "retrofit",
        "http",
        "api",
        "firebase",
        "admob",
        "googleads",
        "reward",
        "error",
        "exception",
    ]
    terms.extend(extra_terms)
    return any(term.lower() in lower for term in terms)


def monitor(device_id: str | None, extra_terms: list[str]):
    adb = ADBController._resolve_adb()
    if not device_id:
        device_id = discover_getsms_device(adb)

    if not device_id:
        print("No online device running GetSMS was found.", file=sys.stderr, flush=True)
        print("Start GetSMS, then run this again or pass --device emulator-XXXX.", file=sys.stderr, flush=True)
        return 1

    print(f"Monitoring GetSMS on {device_id}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)

    run_adb(adb, device_id, ["logcat", "-c"], timeout=10)
    pid = get_pid(adb, device_id)
    if pid:
        print(f"Initial GetSMS PID: {pid}", flush=True)
    else:
        print("GetSMS PID not found yet; monitoring broad logs until it starts.", flush=True)

    cmd = [adb, "-s", device_id, "logcat", "-v", "time"]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    last_pid_check = 0.0

    try:
        assert process.stdout is not None
        for line in process.stdout:
            now = time.time()
            if now - last_pid_check > 5:
                last_pid_check = now
                new_pid = get_pid(adb, device_id)
                if new_pid and new_pid != pid:
                    pid = new_pid
                    print(f"\n--- GetSMS PID: {pid} ---", flush=True)

            if should_print(line, pid, extra_terms):
                print(line, end="", flush=True)
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()

    return 0


def main():
    parser = argparse.ArgumentParser(description="Monitor GetSMS logcat while using the app.")
    parser.add_argument("--device", help="ADB device id, for example emulator-5564")
    parser.add_argument(
        "--term",
        action="append",
        default=[],
        help="Extra case-insensitive text to include in the log filter. Can be repeated.",
    )
    args = parser.parse_args()
    raise SystemExit(monitor(args.device, args.term))


if __name__ == "__main__":
    main()
