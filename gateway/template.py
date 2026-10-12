"""
Template for a client that drives the robot from camera images.

What it does:
    1. Acquires robot control and keeps the lease alive in a thread.
    2. Reads the camera intrinsics once.
    3. In a loop: reads an RGB + depth frame pair as NumPy arrays,
       passes them to run_model() and sends the commands it returns.

What to change:
    run_model()   Put the model here. It returns the commands to send.
                  It returns nothing by default, so the robot does not
                  move until this is filled in.

Arrays handed to run_model():
    rgb     uint8,  shape (height, width, 3), channel order RGB
    depth   uint16, shape (height, width), millimetres, 0 = unknown

Depth is aligned to the RGB image, so depth[v, u] is the distance of
whatever is at rgb[v, u].

Usage:
    python gateway/template.py

Needs: requests, numpy
"""

import threading
import time

import numpy as np
import requests


URL = "http://127.0.0.1:8000/api/v1"
TOKEN = "BAD_TOKEN_OPERATOR"

RENEW_INTERVAL_SECONDS = 2.0
LOOP_INTERVAL_SECONDS = 1.0

# Stop after this many frames. None = run until Ctrl+C.
MAX_FRAMES = None

headers = {
    "Authorization": f"Bearer {TOKEN}",
}

stop = threading.Event()


# ================================================================
# Model
# ================================================================

def run_model(
    rgb: np.ndarray,
    depth: np.ndarray,
    intrinsics: dict,
) -> list[str]:
    """
    Turn one RGB + depth frame pair into robot commands.

    TensorFlow example:

        batch = tf.convert_to_tensor(rgb[np.newaxis], tf.float32) / 255.0
        prediction = model(batch)

    A pixel (u, v) with depth z (mm) is at this point in the camera frame:

        x = (u - intrinsics["cx"]) * z / intrinsics["fx"]
        y = (v - intrinsics["cy"]) * z / intrinsics["fy"]

    Returns the command strings to send, in order, for example:

        ["WAYEND,515.0,200.0,250.0"]
    """

    return []


# ================================================================
# Control
# ================================================================

def acquire_control() -> None:
    response = requests.post(
        f"{URL}/control/acquire",
        headers=headers,
    )

    response.raise_for_status()

    print("Control acquired")


def renew_lease() -> None:
    #
    # The lease lasts 5 seconds by default, so it is renewed well
    # before it runs out. If renewing fails, control has been lost
    # and the main loop is stopped.
    #
    while not stop.wait(RENEW_INTERVAL_SECONDS):
        try:
            response = requests.post(
                f"{URL}/control/renew",
                headers=headers,
            )

            response.raise_for_status()

        except requests.RequestException as error:
            print(f"Lost control: {error}")

            stop.set()


# ================================================================
# Camera
# ================================================================

def get_intrinsics() -> dict:
    response = requests.get(
        f"{URL}/camera/intrinsics",
        headers=headers,
    )

    response.raise_for_status()

    return response.json()


def get_frame(
    name: str,
) -> tuple[bytes, int, int, int]:
    response = requests.get(
        f"{URL}/camera/{name}",
        headers=headers,
    )

    response.raise_for_status()

    return (
        response.content,
        int(response.headers["X-Frame-Width"]),
        int(response.headers["X-Frame-Height"]),
        int(response.headers["X-Frame-Sequence"]),
    )


def get_images() -> tuple[np.ndarray, np.ndarray, int]:
    """
    Read one RGB + depth pair that belongs to the same capture.

    The two images are fetched with separate requests, so the camera
    may publish a new frame in between. The sequence numbers are
    compared and the read is repeated until they match.
    """

    while True:
        rgb_data, rgb_width, rgb_height, rgb_sequence = get_frame("rgb")
        depth_data, depth_width, depth_height, depth_sequence = get_frame("depth")

        if rgb_sequence == depth_sequence:
            break

    #
    # The gateway sends BGR888p: all blue pixels, then all green,
    # then all red. Convert to the usual (height, width, 3) RGB layout.
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

def send_command(
    command: str,
) -> bool:
    """
    Send one command to the robot and wait for its answer.

    Returns True if the robot accepted the command.
    """

    response = requests.post(
        f"{URL}/robot/command",
        headers=headers,
        json={
            "command": command,
            "parameters": {},
        },
    )

    response.raise_for_status()

    result = response.json()

    print(f"Robot: {command} -> {result['message']}")

    return result["accepted"]


# ================================================================
# Main loop
# ================================================================

def main() -> None:
    acquire_control()

    thread = threading.Thread(
        target=renew_lease,
        daemon=True,
    )

    thread.start()

    try:
        intrinsics = get_intrinsics()

        print(f"Intrinsics: {intrinsics}")

        frames = 0
        last_sequence = None

        while not stop.is_set():
            started = time.monotonic()

            rgb, depth, sequence = get_images()

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

                commands = run_model(rgb, depth, intrinsics)

                for command in commands:
                    if stop.is_set():
                        break

                    if not send_command(command):
                        print("Command was not accepted, skipping the rest")

                        break

            if MAX_FRAMES is not None and frames >= MAX_FRAMES:
                break

            remaining = LOOP_INTERVAL_SECONDS - (time.monotonic() - started)

            if remaining > 0:
                stop.wait(remaining)

    except KeyboardInterrupt:
        pass

    finally:
        stop.set()
        thread.join()

    print("Stopped")


if __name__ == "__main__":
    main()
