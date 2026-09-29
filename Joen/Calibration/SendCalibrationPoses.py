import socket
import argparse

DEFAULT_HOST = "192.168.125.1"   # YuMi controller IP
DEFAULT_PORT = 5000              # must match the RAPID Socket server port

# Tilt used for every pose that has no own rotation: (rx, ry, rz) in degrees.
CALIB_ROT = (90, 0, 90)

# Calibration area, mm, flange (tool0) in wobj0. Adjust to what the camera sees.
X_RANGE = (350, 425)        # near the body -> far from the body
Y_RANGE = (-50, 200)        # right -> left
Z_LEVELS = (260, 300, 340)  # low, middle, high



# 24 calibration poses, mm, flange (tool0) in wobj0.
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