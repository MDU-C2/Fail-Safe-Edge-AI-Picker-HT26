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
    # x = 600 (pose 1 unchanged: the first move turns the gripper, so it stays high)
    (600, 410, 140), (600, 250, 90), (600, 90, 170), (600, -70, 70), (600, -230, 110),
    # x = 520
    (520, -230, 140), (520, -70, 90), (520, 90, 110), (520, 250, 70), (520, 410, 90),
    # x = 440
    (440, 410, 90), (440, 250, 170), (440, 90, 70), (440, -70, 110), (440, -230, 140),
    # x = 350
    (380, -150, 170), (350, -70, 110), (350, 90, 90), (380, 250, 90), (350, 410, 140),
]


def acquire(url, headers):
    response = requests.post(f"{url}/control/acquire", headers=headers, timeout=10)
    response.raise_for_status()
    print("Control:", response.json())


def send(url, headers, command):
    response = requests.post(
        f"{url}/robot/command",
        headers=headers,
        json={"command": command, "parameters": {}},
        timeout=TIMEOUT,
    )
    reply = str(response.json()) if response.ok else f"HTTP {response.status_code}: {response.text}"
    print(f"Sent: {command}  →  {reply}")
    return reply


def send_target(url, headers, x, y, z, rot=None):
    values = (x, y, z) + (tuple(rot) if rot else ())
    return send(url, headers, "GOTO," + ",".join(f"{v:g}" for v in values))


def confirm(next_step, auto):
    """Wait for the user unless running automatically. True = go on, False = quit."""
    if auto:
        print(f"Going to {next_step}")
        return True
    answer = input(f"Press Enter to go to {next_step} (q = quit): ")
    return answer.strip().lower() != "q"


def run_poses(url, headers, start, auto, home):
    if home:
        if not confirm("the YuMi home position", auto):
            print("Stopped.")
            return
        if "DONE" not in send(url, headers, "HOME"):
            print("Could not reach home position, stopping")
            return

    reached, failed = [], []
    for i in range(start, len(POSES) + 1):
        pose = POSES[i - 1]
        if not confirm(f"pose {i}/{len(POSES)} {pose}", auto):
            print("Stopped.")
            break

        x, y, z = pose[:3]
        rot = pose[3:] or CALIB_ROT
        reply = send_target(url, headers, x, y, z, rot)
        if "DONE" in reply:
            reached.append(i)
            if not auto:
                print(f"At pose {i}. Do your measurement now.")
        else:
            failed.append((i, pose, reply))
            print(f"Pose {i} failed.")
            if "arm not ready" in reply or "arm timeout" in reply:
                print("Arm is not responding, stopping")
                break

    print(f"\n{len(reached)} poses reached: {reached}")
    for i, pose, reply in failed:
        print(f"  failed: pose {i} {pose}  ({reply})")


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
    parser.add_argument("--poses", action="store_true", help="Go home, then walk through POSES")
    parser.add_argument("--auto", action="store_true", help="with --poses: run without waiting for Enter")
    parser.add_argument("--start", type=int, default=1, help="with --poses: pose number to start from (1-24)")
    parser.add_argument("--no-home", action="store_true", help="with --poses: skip HOME before the first pose")
    parser.add_argument("--home", action="store_true", help="Go to the YuMi calibration position")
    parser.add_argument("--limits", action="store_true", help="Print the height limit (zMax)")
    args = parser.parse_args()

    single = None not in (args.x, args.y, args.z)
    if not (args.poses or args.home or args.limits or single):
        parser.error("give --poses, --home, --limits or --x --y --z (optionally --rx --ry --rz)")
    if not 1 <= args.start <= len(POSES):
        parser.error(f"--start must be between 1 and {len(POSES)}")

    headers = {"Authorization": f"Bearer {args.token}"}
    acquire(args.url, headers)

    if args.poses:
        run_poses(args.url, headers, args.start, args.auto, not args.no_home)
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