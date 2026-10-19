"""
Send ONE job to one arm through the gateway's RWS API and print the
result: a move, home, or the gripper.

    python gateway/scripts/rws_move.py --x 540 --y 306 --z 348
    python gateway/scripts/rws_move.py --arm right --x 420 --y -150 --z 310 --rx 180 --ry 0 --rz 0
    python gateway/scripts/rws_move.py --home
    python gateway/scripts/rws_move.py --grip          # or --open
    python gateway/scripts/rws_move.py --x 540 --y 306 --z 338 --no-wait

Positions are tool0 in wobj0 in mm, rotations in degrees (RAPID
OrientZYX); without --rx/--ry/--rz the orientation is kept. The
arm's pose is printed before and after. --no-wait starts the job
(STARTED) and polls GET /robot/rws/status until it is done, the way
clients of the team's CommServer do.

The robot that moves is the one the gateway at --url controls; the
default is the gateway on the Raspberry Pi, i.e. the real YuMi.

Needs: requests
"""

import argparse
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


def show_pose(
    base: str,
    headers: dict,
    arm: str,
    label: str,
) -> None:
    pose = requests.get(
        f"{base}/robot/arms/{arm}/pose",
        headers=headers,
        params={"tool": "tool0", "wobj": "wobj0"},
        timeout=10,
    ).json()

    print(
        f"{label}: {arm} arm at x={pose['x']:.1f} y={pose['y']:.1f} z={pose['z']:.1f} "
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

    if args.home:
        path, body = "/robot/rws/home", {}

    elif args.grip or args.open:
        path, body = "/robot/rws/gripper", {"action": "grip" if args.grip else "open"}

    elif None not in position:
        path, body = "/robot/rws/move", dict(zip(("x", "y", "z"), position))

        if any(value is not None for value in rotation):
            if None in rotation:
                parser.error("give all of --rx --ry --rz, or none")

            body.update(zip(("rx", "ry", "rz"), rotation))

    else:
        parser.error("give --x --y --z (optionally --rx --ry --rz), --home, --grip or --open")

    body.update(arm=args.arm, wait=not args.no_wait)

    headers = {
        "Authorization": f"Bearer {args.token}",
        "X-Dev-Client": args.client,
    }

    show_pose(args.url, headers, args.arm, "Before")

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

    show_pose(args.url, headers, args.arm, "After")


if __name__ == "__main__":
    main()
