"""
Send the YuMi left arm to calibration poses or to a single point, through the gateway API.

Usage:
    py SendCalibrationPoses.py --x 500 --y 100 --z 150 --rx 90 --ry 0 --rz 90    one point
    py SendCalibrationPoses.py --home                                             HOME
    py SendCalibrationPoses.py --limits                                           height limit (zMax)
    py SendCalibrationPoses.py --poses                                            HOME, then all POSES, Enter before each
    py SendCalibrationPoses.py --poses --auto                                     same, without stopping
    py SendCalibrationPoses.py --poses --start 7                                  begin at pose 7
    py SendCalibrationPoses.py --poses --no-home                                  skip HOME first

Always give the rotation for single points. Without it the arm module uses its own default
orientation (pointing down) and goes via its start pose first.
"""
import argparse
import threading

import requests

DEFAULT_URL = "http://192.168.125.10:8000/api/v1"
DEFAULT_TOKEN = "BAD_TOKEN_OPERATOR"
ROBOT_TIMEOUT = 100              # s, longer than ARM_TIMEOUT (90 s) in CommServer.mod

# Gripper orientation for every calibration pose: horizontal, flat side with the tag up.
CALIB_ROT = (90, 0, 90)

# Gripper TCP positions, mm, wobj0, table = z 0. The first pose stays high, because the
# move from HOME turns the gripper into CALIB_ROT.
POSES = [
    (600, 410, 140), (600, 250, 90), (600, 90, 170), (600, -70, 70), (600, -230, 110),
    (520, -230, 140), (520, -70, 90), (520, 90, 110), (520, 250, 70), (520, 410, 90),
    (440, 410, 90), (440, 250, 170), (440, 90, 70), (440, -70, 110), (440, -230, 140),
    (380, -150, 170), (350, -70, 110), (350, 90, 90), (380, 250, 90), (350, 410, 140),
]


def acquire(url, headers):
    """Take control and keep it. The lease lasts 5 s, so a background thread renews it every 2 s."""
    response = requests.post(f"{url}/control/acquire", headers=headers, timeout=10)
    response.raise_for_status()
    print("Control:", response.json())
    stop = threading.Event()

    def renew():
        while not stop.wait(2):
            try:
                requests.post(f"{url}/control/renew", headers=headers, timeout=5).raise_for_status()
            except requests.RequestException as e:
                print(f"  lease renew failed: {e}")

    threading.Thread(target=renew, daemon=True).start()
    return stop


def send(url, headers, command):
    """Send one command and wait for the robot. True if it reports DONE."""
    response = requests.post(f"{url}/robot/command", headers=headers,
                             json={"command": command, "parameters": {}}, timeout=ROBOT_TIMEOUT)
    reply = response.json() if response.ok else f"HTTP {response.status_code}: {response.text}"
    print(f"Sent: {command}  →  {reply}")
    return "DONE" in str(reply)


def goto(url, headers, x, y, z, rot=None):
    values = (x, y, z) + (tuple(rot) if rot else ())
    return send(url, headers, "GOTO," + ",".join(f"{v:g}" for v in values))


def run_poses(url, headers, auto, start, home):
    def ask(what):
        if auto:
            return True
        return input(f"Enter = {what}, q = stop: ").strip().lower() != "q"

    if home:
        if not ask("go HOME"):
            return
        print("Going to the YuMi home position")
        if not send(url, headers, "HOME"):
            print("HOME failed, stopping.")
            return

    reached, failed = [], []
    for n, pose in enumerate(POSES, 1):
        if n < start:
            continue
        if not ask(f"pose {n}/{len(POSES)} {pose}"):
            break
        print(f"Going to pose {n}/{len(POSES)} {pose}")
        if goto(url, headers, *pose, rot=CALIB_ROT):
            reached.append(n)
        else:
            print(f"Pose {n} failed.")
            failed.append((n, pose))

    print(f"\n{len(reached)} poses reached: {reached}")
    for n, pose in failed:
        print(f"  failed: pose {n} {pose}")


def main():
    parser = argparse.ArgumentParser(description="Send the YuMi left arm to calibration poses")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--x", type=float, help="X in mm")
    parser.add_argument("--y", type=float, help="Y in mm")
    parser.add_argument("--z", type=float, help="Z in mm")
    parser.add_argument("--rx", type=float, help="Rotation about X in degrees")
    parser.add_argument("--ry", type=float, help="Rotation about Y in degrees")
    parser.add_argument("--rz", type=float, help="Rotation about Z in degrees")
    parser.add_argument("--home", action="store_true", help="Go to the YuMi home position")
    parser.add_argument("--limits", action="store_true", help="Print the height limit (zMax)")
    parser.add_argument("--poses", action="store_true", help="HOME, then all POSES")
    parser.add_argument("--auto", action="store_true", help="With --poses: don't wait for Enter")
    parser.add_argument("--start", type=int, default=1, help="With --poses: first pose number")
    parser.add_argument("--no-home", action="store_true", help="With --poses: skip HOME first")
    args = parser.parse_args()

    single = None not in (args.x, args.y, args.z)
    if not (args.poses or args.home or args.limits or single):
        parser.error("give --poses, --home, --limits or --x --y --z")

    rot = (args.rx, args.ry, args.rz)
    if None in rot:
        if any(r is not None for r in rot):
            parser.error("give all of --rx --ry --rz, or none")
        rot = None

    url = args.url.rstrip("/")
    headers = {"Authorization": f"Bearer {args.token}"}
    lease = acquire(url, headers)
    try:
        if args.poses:
            run_poses(url, headers, args.auto, args.start, not args.no_home)
        elif args.home:
            send(url, headers, "HOME")
        elif args.limits:
            send(url, headers, "LIMITS")
        else:
            goto(url, headers, args.x, args.y, args.z, rot)
    finally:
        lease.set()


if __name__ == "__main__":
    main()