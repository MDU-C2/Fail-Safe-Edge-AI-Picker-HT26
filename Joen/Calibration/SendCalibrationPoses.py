import socket
import argparse
import json

DEFAULT_HOST = "192.168.125.1"   # YuMi controller IP
DEFAULT_PORT = 5000              # must match the RAPID Socket server port

# Tilt used for every pose that has no own rotation: (rx, ry, rz) in degrees.
# Read EX, EY, EZ from the FlexPendant (tool0, wobj0). None keeps the current orientation.
CALIB_ROT = None

# Calibration poses: (x, y, z) in mm, or (x, y, z, rx, ry, rz) with degrees.
# Flange (tool0) in wobj0; positions must stay inside the RAPID safe zone.
POSES = [
    (325, 200, 300),
    (250, 100, 260), (400, 100, 260), (250, 300, 260), (400, 300, 260),
    (250, 100, 340), (400, 100, 340), (250, 300, 340), (400, 300, 340),
    (325, 150, 280), (325, 250, 320), (300, 200, 340),
]


def send_target(host: str, port: int, x: float, y: float, z: float, rot=None) -> str:
    values = (x, y, z) + (tuple(rot) if rot else ())
    payload = ",".join(f"{v:g}" for v in values)
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.settimeout(30)
        sock.sendall(payload.encode())
        response = sock.recv(1024).decode().strip()
    print(f"Sent: {payload}  →  Response: {response}")
    return response


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
    parser.add_argument("--poses", action="store_true", help="Run the POSES list")
    args = parser.parse_args()

    if args.poses:
        for i, pose in enumerate(POSES, 1):
            x, y, z = pose[:3]
            rot = pose[3:] or CALIB_ROT
            print(f"[{i}/{len(POSES)}] ", end="")
            send_target(args.host, args.port, x, y, z, rot)
    elif None not in (args.x, args.y, args.z):
        rot = (args.rx, args.ry, args.rz)
        if None in rot:
            if any(r is not None for r in rot):
                parser.error("give all of --rx --ry --rz, or none")
            rot = None
        send_target(args.host, args.port, args.x, args.y, args.z, rot)
    else:
        parser.error("give --x --y --z (optionally --rx --ry --rz), or --poses")