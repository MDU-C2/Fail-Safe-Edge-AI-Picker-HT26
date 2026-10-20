"""
Send ONE job to one arm through the gateway's RWS API and print the
result: a move, home, or the gripper.

    python gateway/scripts/rws_move.py --x 540 --y 306 --z 348
    python gateway/scripts/rws_move.py --arm right --x 420 --y -150 --z 310 --rx 180 --ry 0 --rz 0
    python gateway/scripts/rws_move.py --home
    python gateway/scripts/rws_move.py --grip          # or --open
    python gateway/scripts/rws_move.py --x 540 --y 306 --z 338 --no-wait
    python gateway/scripts/rws_move.py --rz 90                 # turn in place
    python gateway/scripts/rws_move.py --rx 180 --ry 0 --rz 180  # point down, in place
    python gateway/scripts/rws_move.py --z 300                 # only change the height

Positions are tool0 in wobj0 in mm, rotations in degrees (RAPID
OrientZYX(rz, ry, rx)). Every value left out is taken from the arm's
current pose: without --x/--y/--z the flange stays where it is (the
gripper tip, 159 mm further out, swings around it), without
--rx/--ry/--rz the orientation is kept. The arm's pose is printed
before and after, with its orientation as rx/ry/rz. --no-wait starts the job
(STARTED) and polls GET /robot/rws/status until it is done, the way
clients of the team's CommServer do.

The robot that moves is the one the gateway at --url controls; the
default is the gateway on the Raspberry Pi, i.e. the real YuMi.

Needs: requests
"""

import argparse
import math
import sys
import threading
import time

import requests


DEFAULT_URL = "http://169.254.125.202:8000/api/v1"
DEFAULT_TOKEN = "BAD_TOKEN_OPERATOR"

#
# Longer than the gateway's own move timeout (60 s).
#
JOB_TIMEOUT_SECONDS = 90


def zyx_angles(
    pose: dict,
) -> tuple[float, float, float]:
    """(rx, ry, rz) in degrees of the pose's quaternion: the inverse
    of RAPID's OrientZYX(rz, ry, rx) (and of the gateway's orient_zyx)."""

    w, x, y, z = (pose[name] for name in ("q1", "q2", "q3", "q4"))

    rx = math.atan2(2 * (y * z + w * x), 1 - 2 * (x * x + y * y))
    ry = math.asin(max(-1.0, min(1.0, 2 * (w * y - x * z))))
    rz = math.atan2(2 * (x * y + w * z), 1 - 2 * (y * y + z * z))

    return tuple(round(math.degrees(angle), 3) + 0.0 for angle in (rx, ry, rz))


def get_pose(
    base: str,
    headers: dict,
    arm: str,
) -> dict:
    response = requests.get(
        f"{base}/robot/arms/{arm}/pose",
        headers=headers,
        params={"tool": "tool0", "wobj": "wobj0"},
        timeout=10,
    )

    if not response.ok:
        sys.exit(f"Could not read the {arm} arm's pose: {response.status_code} {response.text}")

    return response.json()


def show_pose(
    pose: dict,
    arm: str,
    label: str,
) -> None:
    rx, ry, rz = zyx_angles(pose)

    print(
        f"{label}: {arm} arm at x={pose['x']:.1f} y={pose['y']:.1f} z={pose['z']:.1f} "
        f"rx={rx:.1f} ry={ry:.1f} rz={rz:.1f} "
        f"q=({pose['q1']:.3f}, {pose['q2']:.3f}, {pose['q3']:.3f}, {pose['q4']:.3f})"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send one move / home / gripper job over the gateway's RWS API",
    )

    parser.add_argument("--url", default=DEFAULT_URL, help="Gateway API base URL")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="Bearer token (strict mode)")
    parser.add_argument("--client", default="operator", help="Client id (development mode)")
    parser.add_argument("--arm", choices=("left", "right"), default="left")

    parser.add_argument("--x", type=float)
    parser.add_argument("--y", type=float)
    parser.add_argument("--z", type=float)
    parser.add_argument("--rx", type=float)
    parser.add_argument("--ry", type=float)
    parser.add_argument("--rz", type=float)

    parser.add_argument("--home", action="store_true", help="Move the arm home")
    parser.add_argument("--grip", action="store_true", help="Close the gripper")
    parser.add_argument("--open", action="store_true", help="Open the gripper")
    parser.add_argument("--no-wait", action="store_true", help="Answer STARTED at once and poll /robot/rws/status")

    args = parser.parse_args()

    position = (args.x, args.y, args.z)
    rotation = (args.rx, args.ry, args.rz)

    headers = {
        "Authorization": f"Bearer {args.token}",
        "X-Dev-Client": args.client,
    }

    before = get_pose(args.url, headers, args.arm)

    show_pose(before, args.arm, "Before")

    if args.home:
        path, body = "/robot/rws/home", {}

    elif args.grip or args.open:
        path, body = "/robot/rws/gripper", {"action": "grip" if args.grip else "open"}

    elif any(value is not None for value in position + rotation):
        #
        # Whatever is not given stays as it is now.
        #
        current = (before["x"], before["y"], before["z"])

        path, body = "/robot/rws/move", {
            name: given if given is not None else now
            for name, given, now in zip(("x", "y", "z"), position, current)
        }

        if any(value is not None for value in rotation):
            body.update(
                (name, given if given is not None else now)
                for name, given, now in zip(("rx", "ry", "rz"), rotation, zyx_angles(before))
            )

    else:
        parser.error("give --x/--y/--z and/or --rx/--ry/--rz, --home, --grip or --open")

    body.update(arm=args.arm, wait=not args.no_wait)

    response = requests.post(f"{args.url}/control/acquire", headers=headers, timeout=10)

    if not response.ok:
        sys.exit(f"Could not take control: {response.status_code} {response.text}")

    #
    # The lease (5 s by default) must outlive the job, so it is
    # renewed in a thread while the request blocks.
    #
    stop = threading.Event()

    def renew() -> None:
        while not stop.wait(2.0):
            try:
                requests.post(f"{args.url}/control/renew", headers=headers, timeout=5)

            except requests.RequestException as error:
                print(f"Lease renewal failed: {error}")

    threading.Thread(target=renew, daemon=True).start()

    try:
        print(f"POST {path} {body}")

        response = requests.post(
            f"{args.url}{path}",
            headers=headers,
            json=body,
            timeout=JOB_TIMEOUT_SECONDS,
        )

        if not response.ok:
            sys.exit(f"Refused: {response.status_code} {response.text}")

        result = response.json()

        print(f"-> {result['message']}")

        if result["message"] == "STARTED":
            deadline = time.monotonic() + JOB_TIMEOUT_SECONDS

            while True:
                result = requests.get(
                    f"{args.url}/robot/rws/status",
                    headers=headers,
                    params={"arm": args.arm},
                    timeout=10,
                ).json()

                if result["message"] != "BUSY" or time.monotonic() > deadline:
                    break

                time.sleep(0.1)

            print(f"STATUS -> {result['message']}")

    finally:
        stop.set()

        requests.post(f"{args.url}/control/release", headers=headers, timeout=10)

    show_pose(get_pose(args.url, headers, args.arm), args.arm, "After")


if __name__ == "__main__":
    main()
