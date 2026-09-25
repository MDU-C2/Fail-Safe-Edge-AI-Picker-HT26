import socket
import argparse

DEFAULT_HOST = "192.168.125.1"   # YuMi controller IP
DEFAULT_PORT = 5000              # must match the RAPID Socket server port

# Tilt used for every pose that has no own rotation: (rx, ry, rz) in degrees.
CALIB_ROT = (90, 0, 90)

# 25 calibration poses: (x, y, z) in mm, or (x, y, z, rx, ry, rz) with degrees.
# Flange (tool0) in wobj0; positions must stay inside the RAPID safe zone.
POSES = [
    # Low layer, z = 260
    (250, 100, 260), (325, 100, 260), (400, 100, 260),
    (400, 200, 260), (325, 200, 260), (250, 200, 260),
    (250, 300, 260), (325, 300, 260), (400, 300, 260),
    # Middle layer, z = 300
    (360, 250, 300), (290, 250, 300), (325, 200, 300), (360, 150, 300),
    (290, 150, 300), (325, 100, 300), (325, 300, 300),
    # High layer, z = 340 (x >= 290 near the body to avoid self-collision)
    (400, 300, 340), (325, 300, 340), (250, 300, 340),
    (290, 200, 340), (325, 200, 340), (400, 200, 340),
    (400, 100, 340), (325, 100, 340), (360, 250, 340),
]


def send(host: str, port: int, payload: str) -> str:
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.settimeout(60)
        sock.sendall(payload.encode())
        response = sock.recv(1024).decode().strip()
    print(f"Sent: {payload}  →  Response: {response}")
    return response


def send_target(host: str, port: int, x: float, y: float, z: float, rot=None) -> str:
    values = (x, y, z) + (tuple(rot) if rot else ())
    return send(host, port, ",".join(f"{v:g}" for v in values))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send XYZ (and rotation) to YuMi")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--x", type=float, help="X in mm")
    parser.add_argument("--y", type=float, help="Y in mm")
    parser.add_argument("--z", type=float, help="Z in mm")
    parser.add_argument("--rx", type=float, help="Rotation about X in degrees")
    parser.add_argument("--ry", type=float, help="Rotation about Y in degrees")
    parser.add_argument("--rz", type=float, help="Rotation about Z in degrees")
    parser.add_argument("--poses", action="store_true", help="Go to calibration position, then run POSES")
    parser.add_argument("--home", action="store_true", help="Go to the YuMi calibration position")
    args = parser.parse_args()

    if args.poses:
        if send(args.host, args.port, "HOME") != "DONE":
            parser.exit(1, "Could not reach calibration position, stopping\n")
        failed = []
        for i, pose in enumerate(POSES, 1):
            x, y, z = pose[:3]
            rot = pose[3:] or CALIB_ROT
            print(f"[{i}/{len(POSES)}] ", end="")
            if send_target(args.host, args.port, x, y, z, rot) != "DONE":
                failed.append((i, pose))
        print(f"\n{len(POSES) - len(failed)}/{len(POSES)} poses reached")
        for i, pose in failed:
            print(f"  failed: pose {i} {pose}")
    elif args.home:
        send(args.host, args.port, "HOME")
    elif None not in (args.x, args.y, args.z):
        rot = (args.rx, args.ry, args.rz)
        if None in rot:
            if any(r is not None for r in rot):
                parser.error("give all of --rx --ry --rz, or none")
            rot = None
        send_target(args.host, args.port, args.x, args.y, args.z, rot)
    else:
        parser.error("give --x --y --z (optionally --rx --ry --rz), --home, or --poses")