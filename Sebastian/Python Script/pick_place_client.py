import socket
import argparse

DEFAULT_HOST = "192.168.125.1"   # sim. Real YuMi: 192.168.125.1
DEFAULT_PORT = 5000          # must match PORT in the RAPID module


def send_cmd(host: str, port: int, cmd: str, x: float, y: float, z: float) -> str:
    payload = f"{cmd},{x},{y},{z}"
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.settimeout(60)  # pick/place sequences take a while
        sock.sendall(payload.encode())
        response = sock.recv(1024).decode().strip()
    print(f"Sent: {payload}  ->  Response: {response}")
    return response


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send PICK/PLACE/MOVE commands to YuMi")
    parser.add_argument("cmd", choices=["pick", "place", "move"])
    parser.add_argument("x", type=float, help="X in mm")
    parser.add_argument("y", type=float, help="Y in mm")
    parser.add_argument("z", type=float, help="Z in mm")
    parser.add_argument("--to", nargs=3, type=float, metavar=("X", "Y", "Z"),
                        help="After a successful pick, place the object here")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    resp = send_cmd(args.host, args.port, args.cmd.upper(), args.x, args.y, args.z)

    if args.to:
        if resp != "DONE":
            print("Pick failed, not placing.")
        else:
            send_cmd(args.host, args.port, "PLACE", *args.to)