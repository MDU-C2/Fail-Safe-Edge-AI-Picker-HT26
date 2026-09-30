import socket
import argparse

DEFAULT_HOST = "192.168.125.1"   # sim: 127.0.0.1
DEFAULT_PORT = 5001              # must match PORT in CommServer

EXPLAIN = {
    "OK": "Reachable and allowed.",
    "BLOCKED": "Reachable, but outside the safe zone or above the ceiling.",
    "NO_REACH": "The arm can't reach this point with the gripper pointing down.",
    "ERR outside safe zone": "Rejected: outside the safe zone or above the ceiling.",
    "ERR approach above max height": "Rejected: the point 100 mm above the target is over the ceiling.",
    "ERR bad format": "Rejected: the message could not be parsed.",
    "ERR unknown command": "Rejected: RAPID doesn't know this command.",
    "ERR arm not ready": "The left arm isn't ready. Wait for 'Left arm ready' on the pendant.",
    "ERR arm timeout": "The arm didn't answer. Check the pendant for errors.",
    "ERR home blocked": "HOME stopped early. Move the arm clear by hand.",
}

NEEDS_XYZ = ("pick", "place", "move", "check")


def send(host: str, port: int, payload: str) -> str:
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.settimeout(90)  # pick/place and HOME take a while
        sock.sendall(payload.encode())
        response = sock.recv(1024).decode().strip()
    note = EXPLAIN.get(response, "")
    print(f"Sent: {payload}  ->  Response: {response}" + (f"  ({note})" if note else ""))
    return response


def send_cmd(host: str, port: int, cmd: str, x: float, y: float, z: float) -> str:
    return send(host, port, f"{cmd},{x},{y},{z}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send commands to YuMi (left arm)")
    parser.add_argument("cmd", choices=["pick", "place", "move", "check", "home", "limits"],
                        help="check = test a point without moving, home = fold arm to home pose")
    parser.add_argument("x", type=float, nargs="?", help="X in mm")
    parser.add_argument("y", type=float, nargs="?", help="Y in mm")
    parser.add_argument("z", type=float, nargs="?", help="Z in mm")
    parser.add_argument("--to", nargs=3, type=float, metavar=("X", "Y", "Z"),
                        help="After a successful pick, place the object here")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    if args.cmd in NEEDS_XYZ and None in (args.x, args.y, args.z):
        parser.error(f"'{args.cmd}' needs x y z, e.g. {args.cmd} 400 200 150")

    if args.cmd == "home":
        send(args.host, args.port, "HOME")
    elif args.cmd == "limits":
        send(args.host, args.port, "LIMITS")
    else:
        resp = send_cmd(args.host, args.port, args.cmd.upper(), args.x, args.y, args.z)

        if args.to:
            if args.cmd != "pick":
                print("--to only works together with pick.")
            elif resp != "DONE":
                print("Pick failed, not placing.")
            else:
                send_cmd(args.host, args.port, "PLACE", *args.to)