"""
Check that a deployed gateway works, without moving the robot.

Usage (from the repository root):
    python gateway/scripts/smoke_test.py --url http://169.254.219.127:8000/api/v1
    python gateway/scripts/smoke_test.py --url ... --token <robot.control token> --read-token <camera.read token>
    python gateway/scripts/smoke_test.py --url ... --write-calibration   # also POST + GET /calibration

Checks /health, /openapi.json, that requests without a token are refused
(strict mode), camera status, intrinsics and an RGB + depth frame (sizes,
headers, valid depth), robot status, and the control lease (acquire,
renew, release; skipped with --read-only). --write-calibration replaces
the gateway's stored calibration with a test object, so only use it on a
gateway whose calibration does not matter.

Works against either implementation (Python or C++). Exit code 0 when
every check passed. Tokens default to the GATEWAY_TOKEN environment
variable (see client_tokens.json, local only).

Needs: requests
"""

import argparse
import array
import os
import sys
import time

import requests


DEFAULT_URL = "http://127.0.0.1:8000/api/v1"
DEFAULT_TOKEN = os.environ.get("GATEWAY_TOKEN", "")


class Checks:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        print(f"{'ok  ' if ok else 'FAIL'}  {name}{f': {detail}' if detail else ''}")

        if not ok:
            self.failed += 1

        return ok


def main() -> None:
    parser = argparse.ArgumentParser(description="Check a deployed gateway without moving the robot")

    parser.add_argument("--url", default=DEFAULT_URL, help="Gateway API base URL")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="Token with robot.control (control lease)")
    parser.add_argument("--read-token", default="", help="Token with camera.read (default: --token)")
    parser.add_argument("--read-only", action="store_true", help="Skip the control lease")
    parser.add_argument("--write-calibration", action="store_true", help="Also replace the stored calibration with a test object")
    parser.add_argument("--depth-seconds", type=float, default=15, help="How long to wait for valid depth")

    args = parser.parse_args()

    read_token = args.read_token or args.token
    base = args.url.rstrip("/")
    root = base.rsplit("/api/", 1)[0]
    checks = Checks()

    def get(path: str, token: str | None = None, **kwargs) -> requests.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return requests.get(f"{base}{path}", headers=headers, timeout=10, **kwargs)

    def post(path: str, token: str, body: object | None = None) -> requests.Response:
        return requests.post(f"{base}{path}", headers={"Authorization": f"Bearer {token}"}, json=body, timeout=10)

    # ------------------------------------------------------------
    # Unauthenticated
    # ------------------------------------------------------------

    health = get("/health")

    if not checks.check("GET /health", health.status_code == 200, f"HTTP {health.status_code}"):
        sys.exit(1)

    body = health.json()

    print(
        f"      {body.get('implementation', 'python')} {body['version']}, status {body['status']}, "
        f"security {body['security_mode']}, up {body['uptime_seconds']:.0f} s"
    )
    checks.check("camera connected", body["camera"]["connected"], body["camera"].get("error") or "")

    robot = body["robot_controller"]
    print(f"      robot controller: {'reachable' if robot['reachable'] else robot['error']}")

    openapi = requests.get(f"{root}/openapi.json", timeout=10)
    checks.check("GET /openapi.json", openapi.status_code == 200 and "paths" in openapi.json(), f"HTTP {openapi.status_code}")

    if body["security_mode"] == "strict":
        refused = get("/camera/status")
        checks.check("no token is refused", refused.status_code == 401, f"HTTP {refused.status_code}")

    # ------------------------------------------------------------
    # Camera
    # ------------------------------------------------------------

    status = get("/camera/status", read_token)
    checks.check("GET /camera/status", status.status_code == 200, status.text[:200])

    intrinsics = get("/camera/intrinsics", read_token)

    if checks.check("GET /camera/intrinsics", intrinsics.status_code == 200, f"HTTP {intrinsics.status_code}"):
        values = intrinsics.json()
        print(f"      {values['width']}x{values['height']}, fx {values['fx']:.1f}, fy {values['fy']:.1f}")

    rgb = get("/camera/rgb", read_token)

    if checks.check("GET /camera/rgb", rgb.status_code == 200, f"HTTP {rgb.status_code}"):
        width = int(rgb.headers["X-Frame-Width"])
        height = int(rgb.headers["X-Frame-Height"])

        checks.check(
            "RGB frame is BGR888p of its size",
            rgb.headers["X-Pixel-Format"] == "BGR888p" and len(rgb.content) == 3 * width * height,
            f"{width}x{height}, {len(rgb.content)} bytes, frame {rgb.headers.get('X-Frame-Sequence')}",
        )

    #
    # Stereo needs a few seconds after the camera starts.
    #
    deadline = time.monotonic() + args.depth_seconds
    valid = 0.0

    while True:
        depth = get("/camera/depth", read_token)

        if depth.status_code != 200:
            break

        width = int(depth.headers["X-Frame-Width"])
        height = int(depth.headers["X-Frame-Height"])
        values = array.array("H")
        values.frombytes(depth.content)
        valid = sum(1 for value in values[::97] if value) / len(values[::97])

        if valid > 0 or time.monotonic() > deadline:
            break

        time.sleep(1)

    if checks.check("GET /camera/depth", depth.status_code == 200, f"HTTP {depth.status_code}"):
        checks.check(
            "depth frame is RAW16 of its size",
            depth.headers["X-Pixel-Format"] == "RAW16" and len(depth.content) == 2 * width * height,
            f"{width}x{height}, {len(depth.content)} bytes",
        )
        checks.check("depth has valid pixels", valid > 0, f"{100 * valid:.0f} % of sampled pixels")

    # ------------------------------------------------------------
    # Robot and control (no moves)
    # ------------------------------------------------------------

    robot_status = get("/robot/status", read_token)
    checks.check("GET /robot/status", robot_status.status_code == 200, robot_status.text[:200])

    control = get("/control", read_token)
    checks.check("GET /control", control.status_code == 200, control.text[:200])

    if not args.read_only:
        acquired = post("/control/acquire", args.token)

        if checks.check("POST /control/acquire", acquired.status_code == 200, acquired.text[:200]):
            try:
                renewed = post("/control/renew", args.token)
                checks.check("POST /control/renew", renewed.status_code == 200, f"HTTP {renewed.status_code}")

                if args.write_calibration:
                    sample = {"smoke_test": time.time()}
                    stored = post("/calibration", args.token, sample)
                    checks.check("POST /calibration", stored.status_code == 200, stored.text[:200])

                    read_back = get("/calibration", read_token)
                    checks.check(
                        "GET /calibration returns it",
                        read_back.status_code == 200 and read_back.json()["calibration"] == sample,
                        read_back.text[:200],
                    )
            finally:
                released = post("/control/release", args.token)
                checks.check("POST /control/release", released.status_code == 200, f"HTTP {released.status_code}")

    print(f"\n{'all checks passed' if checks.failed == 0 else f'{checks.failed} check(s) FAILED'}")
    sys.exit(1 if checks.failed else 0)


if __name__ == "__main__":
    main()
