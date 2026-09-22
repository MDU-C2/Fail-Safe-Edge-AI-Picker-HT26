

import socket
import argparse
import json

DEFAULT_HOST = "192.168.1.10"   # YuMi controller IP 
DEFAULT_PORT = 5000             # must match the RAPID Socket server port


def send_target(host: str, port: int, x: float, y: float, z: float) -> None:
    payload = json.dumps({"x": x, "y": y, "z": z}) + "\n"
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(payload.encode())
        response = sock.recv(1024).decode().strip()
    print(f"Sent: x={x} y={y} z={z}  →  Response: {response}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send XYZ coordinate to YuMi")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--x", type=float, required=True, help="X in mm")
    parser.add_argument("--y", type=float, required=True, help="Y in mm")
    parser.add_argument("--z", type=float, required=True, help="Z in mm")
    args = parser.parse_args()

    send_target(args.host, args.port, args.x, args.y, args.z)
