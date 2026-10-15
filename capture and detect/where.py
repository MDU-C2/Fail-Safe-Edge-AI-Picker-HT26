"""
Print where the YuMi left gripper is right now (read-only, the robot does not move).

Asks the robot controller's own web service (ABB Robot Web Services) for the current
position of tool tGrip in wobj0 - the same coordinates capture_and_detect.py sends.

The controller needs a login. Save it once (new PowerShell windows pick it up):
    setx RWS_USER "<controller user>"
    setx RWS_PASSWORD "<password>"

Usage:
    py where.py
    py where.py --loop        print every second until Ctrl+C
"""

import argparse
import os
import time

import requests
from requests.auth import HTTPDigestAuth

CONTROLLER = "http://192.168.125.1"
URL = (CONTROLLER + "/rw/motionsystem/mechunits/ROB_L/robtarget"
       "?tool=tGrip&wobj=wobj0&coordinate=Wobj&json=1")


def saved(name):
    """Environment variable, or the value saved with setx (Windows) if this window is older."""
    value = os.environ.get(name)
    if not value and os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value = winreg.QueryValueEx(key, name)[0]
        except OSError:
            pass
    return value


def where(session):
    """Current gripper position (x, y, z) in mm."""
    r = session.get(URL, timeout=5)
    if r.status_code == 401:
        raise SystemExit("The controller refused the login - check RWS_USER / RWS_PASSWORD.")
    r.raise_for_status()
    state = r.json()["_embedded"]["_state"][0]
    return float(state["x"]), float(state["y"]), float(state["z"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="print every second until Ctrl+C")
    args = parser.parse_args()

    user, password = saved("RWS_USER"), saved("RWS_PASSWORD")
    if not user or not password:
        raise SystemExit('Save the controller login first:  setx RWS_USER "<user>"  and  setx RWS_PASSWORD "<password>"')

    session = requests.Session()
    session.auth = HTTPDigestAuth(user, password)

    try:
        while True:
            x, y, z = where(session)
            print(f"Gripper (tGrip, wobj0):  X={x:7.1f}  Y={y:7.1f}  Z={z:7.1f} mm", flush=True)
            if not args.loop:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
