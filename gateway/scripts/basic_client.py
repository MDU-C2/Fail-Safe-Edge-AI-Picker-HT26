"""
Basic client: client_template.py driving the robot over the RWS API.

What it does:
    1. Acquires robot control and keeps the lease alive in a thread.
    2. Stops RAPID, replaces the program in both arm tasks with the one
       in gateway/rapid and starts it (PUT/DELETE /robot/rapid/...,
       POST /robot/rapid/start). Skip this with --no-load.
    3. Reads the camera intrinsics and, in a loop, an RGB + depth frame
       pair as NumPy arrays, passes them to run_model() and sends the
       moves it returns with POST /robot/rws/move and /robot/rws/home.
    4. Releases control on exit. RAPID is left running.

What to change:
    run_model()   Put the model here. It returns the moves to make.
                  By default it returns a short demo path (home, two
                  targets, home) for the first frame only.

Moves are dicts:
    {"x": 325, "y": 200, "z": 300}                           keep orientation
    {"x": 325, "y": 200, "z": 300, "rx": 90, "ry": 0, "rz": 90}
    "HOME"
Positions are tool0 in wobj0 in mm, rotations in degrees (RAPID
OrientZYX). The RAPID program rejects targets outside its safe zone.

The robot that moves is whichever controller the gateway's
GATEWAY_ROBOT_HOST points at. Use the RobotStudio virtual controller
(GATEWAY_ROBOT_HOST=127.0.0.1) unless you mean to move the real YuMi.
Loading the program replaces every program module in T_ROB_L and
T_ROB_R.

Usage (from the repository root):
    python gateway/scripts/basic_client.py --client operator        # development mode
    python gateway/scripts/basic_client.py --token <token>          # strict mode
    python gateway/scripts/basic_client.py --no-load --frames 0     # program already running, run until Ctrl+C

Needs: requests, numpy
"""

import argparse
import re
import threading
import time
from pathlib import Path

import numpy as np
import requests


DEFAULT_URL = "http://127.0.0.1:8000/api/v1"
DEFAULT_TOKEN = "BAD_TOKEN_OPERATOR"

RENEW_INTERVAL_SECONDS = 2.0
LOOP_INTERVAL_SECONDS = 1.0

RAPID_DIR = Path(__file__).resolve().parents[1] / "rapid"

#
# The program in gateway/rapid, as upload_rapid.py loads it.
#
PROGRAM = {
    "T_ROB_L": ["LegacyShared.mod", "LegacyMotion.mod"],
    "T_ROB_R": ["LegacyShared.mod", "RightArmIdle.mod"],
}

#
# Inside the RAPID safe zone (X 200..450, Y 50..350, Z 250..350).
#
DEMO_PATH = [
    "HOME",
    {"x": 325, "y": 200, "z": 300, "rx": 90, "ry": 0, "rz": 90},
    {"x": 300, "y": 150, "z": 280},
    "HOME",
]

stop = threading.Event()


class Gateway:
    """The few gateway calls this client needs."""

    def __init__(
        self,
        url: str,
        token: str,
        client: str,
    ) -> None:
        self.url = url

        #
        # Strict mode reads the token, development mode the client id;
        # each ignores the other header.
        #
        self.headers = {
            "Authorization": f"Bearer {token}",
            "X-Dev-Client": client,
        }

    def request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> requests.Response:
        response = requests.request(
            method,
            f"{self.url}{path}",
            headers=self.headers,
            timeout=kwargs.pop("timeout", 30),
            **kwargs,
        )

        if response.status_code >= 400:
            raise RuntimeError(
                f"{method} {path} -> {response.status_code}: {response.text}"
            )

        return response


# ================================================================
# Model
# ================================================================

def run_model(
    rgb: np.ndarray,
    depth: np.ndarray,
    intrinsics: dict,
    frame: int,
) -> list:
    """
    Turn one RGB + depth frame pair into robot moves.

    A pixel (u, v) with depth z (mm) is at this point in the camera frame:

        x = (u - intrinsics["cx"]) * z / intrinsics["fx"]
        y = (v - intrinsics["cy"]) * z / intrinsics["fy"]

    Returns the moves to make, in order (see the module docstring).
    """

    if frame == 1:
        return DEMO_PATH

    return []


# ================================================================
# Control
# ================================================================

def acquire_control(
    gateway: Gateway,
) -> None:
    gateway.request("POST", "/control/acquire")

    print("Control acquired")


def renew_lease(
    gateway: Gateway,
) -> None:
    #
    # The lease lasts 5 seconds by default, so it is renewed well
    # before it runs out. If renewing fails, control has been lost
    # and the main loop is stopped.
    #
    while not stop.wait(RENEW_INTERVAL_SECONDS):
        try:
            gateway.request("POST", "/control/renew")

        except (requests.RequestException, RuntimeError) as error:
            print(f"Lost control: {error}")

            stop.set()


# ================================================================
# RAPID program
# ================================================================

def module_name(
    source: str,
) -> str:
    #
    # The controller names a module after its MODULE line.
    #
    return re.search(r"^\s*MODULE\s+(\w+)", source, re.MULTILINE).group(1)


def load_program(
    gateway: Gateway,
) -> None:
    gateway.request("POST", "/robot/rapid/stop")

    rapid = gateway.request("GET", "/robot/rapid").json()

    for task in rapid["tasks"]:
        if task["name"] not in PROGRAM:
            continue

        for module in task["modules"]:
            gateway.request("DELETE", f"/robot/rapid/tasks/{task['name']}/modules/{module}")

            print(f"{task['name']}: unloaded {module}")

    for task, files in PROGRAM.items():
        for file in files:
            source = (RAPID_DIR / file).read_text(encoding="utf-8")

            gateway.request(
                "PUT",
                f"/robot/rapid/tasks/{task}/modules/{module_name(source)}",
                json={"source": source},
            )

            print(f"{task}: loaded {file}")

    rapid = gateway.request(
        "POST",
        "/robot/rapid/start",
        json={"reset_program_pointer": True},
    ).json()

    print(f"RAPID {rapid['execution_state']}")


# ================================================================
# Camera
# ================================================================

def get_frame(
    gateway: Gateway,
    name: str,
) -> tuple[bytes, int, int, int]:
    response = gateway.request("GET", f"/camera/{name}")

    return (
        response.content,
        int(response.headers["X-Frame-Width"]),
        int(response.headers["X-Frame-Height"]),
        int(response.headers["X-Frame-Sequence"]),
    )


def get_images(
    gateway: Gateway,
) -> tuple[np.ndarray, np.ndarray, int]:
    """
    Read one RGB + depth pair that belongs to the same capture.

    The two images are fetched with separate requests, so the
    sequence numbers are compared and the read repeated until
    they match.
    """

    while True:
        rgb_data, rgb_width, rgb_height, rgb_sequence = get_frame(gateway, "rgb")
        depth_data, depth_width, depth_height, depth_sequence = get_frame(gateway, "depth")

        if rgb_sequence == depth_sequence:
            break

    #
    # BGR888p (planar) -> (height, width, 3) RGB.
    #
    rgb = np.ascontiguousarray(
        np.frombuffer(rgb_data, np.uint8)
        .reshape(3, rgb_height, rgb_width)
        .transpose(1, 2, 0)[:, :, ::-1]
    )

    depth = np.frombuffer(depth_data, np.uint16).reshape(
        depth_height,
        depth_width,
    )

    return rgb, depth, rgb_sequence


# ================================================================
# Robot
# ================================================================

def send_move(
    gateway: Gateway,
    move,
) -> bool:
    """
    Make one move and wait for it to finish.

    Returns True if the robot carried it out.
    """

    if move == "HOME":
        response = gateway.request("POST", "/robot/rws/home", timeout=90)

    else:
        response = gateway.request("POST", "/robot/rws/move", json=move, timeout=90)

    result = response.json()

    print(f"Robot: {move} -> {result['message']}")

    return result["accepted"]


def show_pose(
    gateway: Gateway,
) -> None:
    pose = gateway.request(
        "GET",
        "/robot/arms/left/pose",
        params={"tool": "tool0", "wobj": "wobj0"},
    ).json()

    print(f"Left arm at x={pose['x']:.1f} y={pose['y']:.1f} z={pose['z']:.1f}")


# ================================================================
# Main loop
# ================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load the RAPID program and move the robot over the RWS API",
    )

    parser.add_argument("--url", default=DEFAULT_URL, help="Gateway API base URL")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="Bearer token (strict mode)")
    parser.add_argument("--client", default="operator", help="Client id (development mode)")
    parser.add_argument("--no-load", action="store_true", help="Use the RAPID program already running")
    parser.add_argument("--frames", type=int, default=1, help="Stop after N frames (0 = until Ctrl+C)")

    args = parser.parse_args()

    gateway = Gateway(args.url, args.token, args.client)

    acquire_control(gateway)

    thread = threading.Thread(
        target=renew_lease,
        args=(gateway,),
        daemon=True,
    )

    thread.start()

    try:
        if not args.no_load:
            load_program(gateway)

        intrinsics = gateway.request("GET", "/camera/intrinsics").json()

        print(f"Intrinsics: {intrinsics}")

        frames = 0
        last_sequence = None

        while not stop.is_set():
            started = time.monotonic()

            rgb, depth, sequence = get_images(gateway)

            #
            # Only run the model on frames it has not seen yet.
            #
            if sequence != last_sequence:
                last_sequence = sequence
                frames += 1

                print(
                    f"Frame {sequence}: "
                    f"rgb {rgb.shape} {rgb.dtype}, "
                    f"depth {depth.shape} {depth.dtype}"
                )

                for move in run_model(rgb, depth, intrinsics, frames):
                    if stop.is_set():
                        break

                    if not send_move(gateway, move):
                        print("Move was not carried out, skipping the rest")

                        break

                    show_pose(gateway)

            if args.frames and frames >= args.frames:
                break

            remaining = LOOP_INTERVAL_SECONDS - (time.monotonic() - started)

            if remaining > 0:
                stop.wait(remaining)

    except KeyboardInterrupt:
        pass

    finally:
        stop.set()
        thread.join()

        #
        # Hand control back so another client can take over at
        # once instead of waiting for the lease to run out.
        #
        try:
            gateway.request("POST", "/control/release")

        except (requests.RequestException, RuntimeError) as error:
            print(f"Could not release control: {error}")

    print("Stopped")


if __name__ == "__main__":
    main()
