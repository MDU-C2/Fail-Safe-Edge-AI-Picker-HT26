import argparse

import requests

DEFAULT_URL = "http://127.0.0.1:8000/api/v1"
DEFAULT_TOKEN = "BAD_TOKEN_OPERATOR"
TIMEOUT = 100                    # s, longer than ARM_TIMEOUT (90 s) in CommServer.mod

# Tilt used for every pose that has no own rotation: (rx, ry, rz) in degrees.
CALIB_ROT = (90, 0, 90)

# 24 calibration poses, mm, gripper TCP (tGrip) in wobj0, table = z 0.
# Mirrored left/right around y = 0: pose n and pose 25-n share x and z.
POSES = [
    # Left side (+y)
    (350, 60, 260), (350, 145, 300), (350, 230, 300),
    (375, 230, 300), (375, 145, 300), (375, 60, 260),
    (400, 60, 340), (400, 145, 260), (400, 230, 300),
    (425, 230, 260), (425, 145, 300), (425, 60, 340),
    # Right side (-y), mirror of the left side
    (425, -60, 340), (425, -145, 300), (425, -230, 260),
    (400, -230, 300), (400, -145, 260), (400, -60, 340),
    (375, -60, 260), (375, -145, 300), (375, -230, 300),
    (350, -230, 300), (350, -145, 300), (350, -60, 260),
]


def acquire(url: str, headers: dict) -> None:
    response = requests.post(f"{url}/control/acquire", headers=headers, timeout=10)
    response.raise_for_status()
    print("Control:", response.json())


def send(url: str, headers: dict, command: str) -> str:
    response = requests.post(
        f"{url}/robot/command",
        headers=headers,
        json={"command": command, "parameters": {}},
        timeout=TIMEOUT,
    )
    reply = str(response.json()) if response.ok else f"HTTP {response.status_code}: {response.text}"
    print(f"Sent: {command}  →  {reply}")
    return reply


def send_target(url: str, headers: dict, x: float, y: float, z: float, rot=None) -> str:
    values = (x, y, z) + (tuple(rot) if rot else ())
    return send(url, headers, "GOTO," + ",".join(f"{v:g}" for v in values))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send calibration poses to YuMi through the API")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--x", type=float, help="X in mm")
    parser.add_argument("--y", type=float, help="Y in mm")
    parser.add_argument("--z", type=float, help="Z in mm")
    parser.add_argument("--rx", type=float, help="Rotation about X in degrees")
    parser.add_argument("--ry", type=float, help="Rotation about Y in degrees")
    parser.add_argument("--rz", type=float, help="Rotation about Z in degrees")
    parser.add_argument("--poses", action="store_true", help="Go to calibration position, then run POSES")
    parser.add_argument("--home", action="store_true", help="Go to the YuMi calibration position")
    parser.add_argument("--limits", action="store_true", help="Print the height limit (zMax)")
    args = parser.parse_args()

    single = None not in (args.x, args.y, args.z)
    if not (args.poses or args.home or args.limits or single):
        parser.error("give --x --y --z (optionally --rx --ry --rz), --home, --limits or --poses")

    headers = {"Authorization": f"Bearer {args.token}"}
    acquire(args.url, headers)

    if args.poses:
        if "DONE" not in send(args.url, headers, "HOME"):
            parser.exit(1, "Could not reach calibration position, stopping\n")
        failed = []
        for i, pose in enumerate(POSES, 1):
            x, y, z = pose[:3]
            rot = pose[3:] or CALIB_ROT
            print(f"[{i}/{len(POSES)}] ", end="")
            reply = send_target(args.url, headers, x, y, z, rot)
            if "DONE" not in reply:
                failed.append((i, pose, reply))
            if "arm not ready" in reply or "arm timeout" in reply:
                print("Arm is not responding, stopping")
                break
        print(f"\n{len(POSES) - len(failed)}/{len(POSES)} poses reached")
        for i, pose, reply in failed:
            print(f"  failed: pose {i} {pose}  ({reply})")
    elif args.home:
        send(args.url, headers, "HOME")
    elif args.limits:
        send(args.url, headers, "LIMITS")
    else:
        rot = (args.rx, args.ry, args.rz)
        if None in rot:
            if any(r is not None for r in rot):
                parser.error("give all of --rx --ry --rz, or none")
            rot = None
        send_target(args.url, headers, args.x, args.y, args.z, rot)