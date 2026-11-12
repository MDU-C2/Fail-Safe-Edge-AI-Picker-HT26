"""
Get the robot back to a clean start:

    1. rapidctl reset (stops RAPID, resets SharedVars, starts RAPID again)
    2. both arms home at 200 mm/s, one after the other
    3. both grippers closed

    py gateway/scripts/reset_home.py

MOVES THE REAL YUMI. rapidctl asks for confirmation before the reset.
The arms go straight home, without going up first, so check the path is clear.
It stops at the first step that fails.

Needs: requests
"""

import subprocess
import sys
import threading
from pathlib import Path

import requests


URL = "http://169.254.219.127:8000/api/v1"   # C++ gateway on the Lynx (was the Pi 169.254.125.202 until 2026-11-11)
HEADERS = {"Authorization": "Bearer BAD_TOKEN_OPERATOR"}
RAPIDCTL = Path(__file__).resolve().parents[2] / "rapid" / "rapidctl.py"
HOME_SPEED = 200
JOB_TIMEOUT_SECONDS = 90   # longer than the gateway's own move timeout (60 s)


def job(path: str, body: dict) -> None:
    """Send one job, wait for it, and stop the script unless it is DONE."""
    response = requests.post(
        f"{URL}/robot/rws/{path}",
        headers=HEADERS,
        json=body,
        timeout=JOB_TIMEOUT_SECONDS,
    )

    reply = response.json()["message"] if response.ok else f"HTTP {response.status_code}: {response.text}"

    print(f"{path} {body} -> {reply}")

    if reply != "DONE":
        sys.exit(f"Stopped: {path} {body['arm']} failed")


def main() -> None:
    print("Reset RAPID")
    subprocess.run([sys.executable, str(RAPIDCTL), "reset"], check=True)

    response = requests.post(f"{URL}/control/acquire", headers=HEADERS, timeout=10)

    if not response.ok:
        sys.exit(f"Could not take control: {response.status_code} {response.text}")

    #
    # The lease (5 s by default) must outlive the jobs.
    #
    stop = threading.Event()

    def renew() -> None:
        while not stop.wait(2.0):
            try:
                requests.post(f"{URL}/control/renew", headers=HEADERS, timeout=5)

            except requests.RequestException as error:
                print(f"Lease renewal failed: {error}")

    threading.Thread(target=renew, daemon=True).start()

    try:
        for arm in ("left", "right"):
            job("home", {"arm": arm, "speed": HOME_SPEED})

        for arm in ("left", "right"):
            job("gripper", {"arm": arm, "action": "grip"})

        print("Done: both arms home, both grippers closed")

    finally:
        stop.set()

        requests.post(f"{URL}/control/release", headers=HEADERS, timeout=10)


if __name__ == "__main__":
    main()
