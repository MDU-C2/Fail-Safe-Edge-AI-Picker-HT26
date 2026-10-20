"""
Template for a client that drives the robot from camera images.

What it does:
    1. Acquires robot control and keeps the lease alive in a thread.
    2. Reads the camera intrinsics once.
    3. In a loop: reads an RGB + depth frame pair as NumPy arrays,
       passes them to run_model() and carries out the jobs it returns.
    4. Releases control on exit.

What to change:
    run_model()   Put the model here. It returns the robot jobs to run.
                  It returns nothing by default, so the robot does not
                  move until this is filled in.

Arrays handed to run_model():
    rgb     uint8,  shape (height, width, 3), channel order RGB
    depth   uint16, shape (height, width), millimetres, 0 = unknown

Depth is aligned to the RGB image, so depth[v, u] is the distance of
whatever is at rgb[v, u].

Usage:
    python gateway/scripts/client_template.py

Needs: requests, numpy


Robot API (RWS)
===============

All paths are under URL (http://<gateway>:8000/api/v1); full schemas
are at http://<gateway>:8000/docs. Every request carries the token
(Authorization: Bearer <token>). Calls that move the robot also need
the control lease (POST /control/acquire, then /control/renew more
often than every 5 s, as this template does).

Jobs (each one for one arm, "left" by default):

    POST /robot/rws/move      {"arm": "left", "x": 300, "y": 150, "z": 280}
                              {"x": 300, "y": 150, "z": 280, "rx": 90, "ry": 0, "rz": 90}
    POST /robot/rws/home      {"arm": "right"}            (body optional)
    POST /robot/rws/gripper   {"arm": "left", "action": "grip" | "open"}

    x, y, z     tool0 position in wobj0 (the robot base), millimetres
    rx, ry, rz  orientation in degrees, RAPID OrientZYX(rz, ry, rx);
                all three or none (none = keep the current orientation)
    "wait"      true (default): the reply comes when the job is done
                false: the reply comes at once, then poll the status

Replies (HTTP 200):

    {"accepted": true,  "message": "DONE"}                   job done
    {"accepted": true,  "message": "STARTED"}                "wait": false
    {"accepted": false, "message": "ERR move failed"}        unreachable target
    {"accepted": false, "message": "ERR outside safe zone"}  refused, nothing moved

Status of an arm (no lease needed), like CommServer's STATUS:

    GET /robot/rws/status?arm=left   ->  "BUSY" | "DONE" | "ERR move failed"

Errors (HTTP status, reason in "detail"):

    409  no lease / another client holds it, RAPID not running,
         that arm is still busy with an earlier job
    422  bad body (e.g. only rx given)
    503  the controller cannot be reached
    504  the job took longer than the gateway's timeout (60 s)

The two arms are independent: send a job to each with "wait": false
and both move at once (see run_both_arms() below). A second job for a
busy arm is refused with 409, never queued.

Reading the robot (no lease needed):

    GET /robot/arms/left/pose?tool=tool0&wobj=wobj0   {"x", "y", "z", "q1".."q4", ...}
    GET /robot/arms/left/joints
    GET /robot/rapid                                  RAPID state and loaded modules

The RAPID program from rapid/ must be running on the controller
(python rapid/rapidctl.py status / upload). Which robot moves depends
on the gateway: the one on the Raspberry Pi drives the real YuMi.

The old POST /robot/command (raw CommServer text such as
"300,150,280" or "R,HOME", then "STATUS" polling) still works but is
deprecated; use the calls above in new clients.
"""

import threading
import time

import numpy as np
import requests


URL = "http://127.0.0.1:8000/api/v1"
TOKEN = "BAD_TOKEN_OPERATOR"

RENEW_INTERVAL_SECONDS = 2.0
LOOP_INTERVAL_SECONDS = 1.0

#
# Longer than the gateway's own 60 s job timeout, so a job that waits
# gets the gateway's answer instead of a client timeout.
#
JOB_TIMEOUT_SECONDS = 90
STATUS_POLL_SECONDS = 0.1

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
) -> list[dict]:
    """
    Turn one RGB + depth frame pair into robot jobs.

    TensorFlow example:

        batch = tf.convert_to_tensor(rgb[np.newaxis], tf.float32) / 255.0
        prediction = model(batch)

    A pixel (u, v) with depth z (mm) is at this point in the camera frame:

        x = (u - intrinsics["cx"]) * z / intrinsics["fx"]
        y = (v - intrinsics["cy"]) * z / intrinsics["fy"]

    Returns the jobs to run, in order, each carried out by run_job():

        [
            {"gripper": "open"},
            {"x": 300, "y": 150, "z": 280},
            {"x": 300, "y": 150, "z": 250, "rx": 90, "ry": 0, "rz": 90},
            {"gripper": "grip"},
            {"home": True},
            {"arm": "right", "x": 420, "y": -150, "z": 300},
        ]
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

def robot_job(
    path: str,
    body: dict,
) -> tuple[bool, str]:
    """
    POST one job to /robot/rws/<path>.

    Returns (accepted, message): (True, "DONE") or (True, "STARTED")
    when it ran or started, (False, reason) when the robot or the
    gateway refused it. Only a missing permission or token raises.
    """

    response = requests.post(
        f"{URL}/robot/rws/{path}",
        headers=headers,
        json=body,
        timeout=JOB_TIMEOUT_SECONDS,
    )

    if response.status_code in (401, 403):
        response.raise_for_status()

    if response.status_code != 200:
        #
        # 409 busy / no lease / RAPID stopped, 422 bad body,
        # 503 controller unreachable, 504 timeout.
        #
        return False, f"HTTP {response.status_code}: {response.json().get('detail')}"

    result = response.json()

    return result["accepted"], result["message"]


def move(
    x: float,
    y: float,
    z: float,
    rotation: tuple[float, float, float] | None = None,
    arm: str = "left",
    wait: bool = True,
) -> tuple[bool, str]:
    """
    Move tool0 of one arm to (x, y, z) mm in the robot base frame.

    rotation is (rx, ry, rz) in degrees (RAPID OrientZYX(rz, ry, rx));
    None keeps the current orientation.
    """

    body = {
        "arm": arm,
        "x": x,
        "y": y,
        "z": z,
        "wait": wait,
    }

    if rotation is not None:
        body.update(zip(("rx", "ry", "rz"), rotation))

    return robot_job("move", body)


def home(
    arm: str = "left",
    wait: bool = True,
) -> tuple[bool, str]:
    """Move one arm to its home position."""

    return robot_job("home", {"arm": arm, "wait": wait})


def gripper(
    action: str,
    arm: str = "left",
    wait: bool = True,
) -> tuple[bool, str]:
    """Close ("grip") or open ("open") the gripper of one arm."""

    return robot_job("gripper", {"arm": arm, "action": action, "wait": wait})


def job_status(
    arm: str = "left",
) -> str:
    """"BUSY" while the arm works, then "DONE" or "ERR move failed"."""

    response = requests.get(
        f"{URL}/robot/rws/status",
        headers=headers,
        params={"arm": arm},
    )

    response.raise_for_status()

    return response.json()["message"]


def wait_for(
    arm: str = "left",
) -> bool:
    """Wait for a job started with wait=False. True if it succeeded."""

    while (status := job_status(arm)) == "BUSY":
        time.sleep(STATUS_POLL_SECONDS)

    return status == "DONE"


def get_pose(
    arm: str = "left",
) -> dict:
    """The arm's tool0 pose in the base frame: x, y, z (mm), q1..q4, ..."""

    response = requests.get(
        f"{URL}/robot/arms/{arm}/pose",
        headers=headers,
        params={"tool": "tool0", "wobj": "wobj0"},
    )

    response.raise_for_status()

    return response.json()


def run_both_arms(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> bool:
    """
    Example: move both arms at the same time.

    Each job is started with wait=False, then both are waited for.
    """

    for arm, target in (("left", left), ("right", right)):
        accepted, message = move(*target, arm=arm, wait=False)

        if not accepted:
            print(f"Robot: {arm} {target} -> {message}")

            return False

    return all([wait_for("left"), wait_for("right")])


def run_job(
    job: dict,
) -> bool:
    """
    Carry out one job from run_model() and wait for it.

    Returns True if the robot did it.
    """

    arm = job.get("arm", "left")

    if job.get("home"):
        accepted, message = home(arm)

    elif "gripper" in job:
        accepted, message = gripper(job["gripper"], arm)

    else:
        rotation = None

        if "rx" in job:
            rotation = (job["rx"], job["ry"], job["rz"])

        accepted, message = move(job["x"], job["y"], job["z"], rotation, arm)

    print(f"Robot: {job} -> {message}")

    return accepted


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

                jobs = run_model(rgb, depth, intrinsics)

                for job in jobs:
                    if stop.is_set():
                        break

                    if not run_job(job):
                        print("Job was not carried out, skipping the rest")

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

        #
        # Hand control back so another client can take over at
        # once instead of waiting for the lease to run out.
        #
        requests.post(
            f"{URL}/control/release",
            headers=headers,
        )

    print("Stopped")


if __name__ == "__main__":
    main()
