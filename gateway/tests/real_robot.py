"""Fixtures and helpers for the tests that move the REAL YuMi
(test_real_robot.py, test_real_robot_legacy.py; run with --real-robot).

Every move is straight up from an arm's current pose, at most
MAX_OFFSET_MM, and returns_to_start() puts the arm back afterwards.
"""

import time
from contextlib import contextmanager
from typing import Iterator

import pytest

from gateway.tests.support import Client, RemoteGateway, expect


ARMS = ("left", "right")

#
# How far the tests move an arm, straight up from where it is.
#
SMALL_OFFSET_MM = 30.0
MAX_OFFSET_MM = 90.0

POSITION_TOLERANCE_MM = 1.0

#
# Far outside the gateway's safe zone, and inside it but beyond the
# YuMi's reach (about 560 mm).
#
OUTSIDE_SAFE_ZONE = (1500.0, 0.0, 300.0)
OUT_OF_REACH = (780.0, 0.0, 300.0)

DONE = {"accepted": True, "message": "DONE"}
STARTED = {"accepted": True, "message": "STARTED"}
MOVE_FAILED = {"accepted": False, "message": "ERR move failed"}

JOB_TIMEOUT_SECONDS = 60.0


# ================================================================
# Fixtures
# ================================================================

@pytest.fixture(scope="module")
def real_robot(
    pytestconfig: pytest.Config,
) -> RemoteGateway:
    """The gateway in front of the real robot, checked to be ready."""

    gateway = RemoteGateway(
        pytestconfig.getoption("real_robot_url"),
        pytestconfig.getoption("real_robot_token"),
    )

    client = gateway.client()

    controller = expect(client.get("/health"), 200)["robot_controller"]

    problems = [
        problem
        for problem, bad in (
            (f"controller unreachable: {controller['error']}", not controller["reachable"]),
            (f"operation mode is {controller['operation_mode']}, not AUTO", controller["operation_mode"] != "AUTO"),
            (f"motors are {controller['controller_state']}", controller["controller_state"] != "motoron"),
            (f"RAPID is {controller['rapid_execution_state']}", controller["rapid_execution_state"] != "running"),
        )
        if bad
    ]

    for arm in ARMS:
        response = client.get("/robot/rws/status", params={"arm": arm})

        if response.status_code != 200:
            problems.append(f"{arm} arm status: {response.status_code} {response.text[:200]}")

        elif response.json()["message"] == "BUSY":
            problems.append(f"{arm} arm is busy with a job")

    if problems:
        pytest.fail(
            "The real robot is not ready, nothing was moved: " + "; ".join(problems),
            pytrace=False,
        )

    return gateway


@pytest.fixture()
def operator(
    real_robot: RemoteGateway,
) -> Iterator[Client]:
    client = real_robot.client()

    with client.control():
        yield client


# ================================================================
# Helpers
# ================================================================

def pose(
    client: Client,
    arm: str,
) -> dict:
    return expect(
        client.get(
            f"/robot/arms/{arm}/pose",
            params={"tool": "tool0", "wobj": "wobj0"},
        ),
        200,
    )


def position(
    pose: dict,
) -> tuple[float, float, float]:
    return (pose["x"], pose["y"], pose["z"])


def above(
    start: tuple[float, float, float],
    offset: float,
) -> tuple[float, float, float]:
    assert 0 < offset <= MAX_OFFSET_MM

    x, y, z = start

    return (x, y, z + offset)


def assert_at(
    client: Client,
    arm: str,
    expected: tuple[float, float, float],
    orientation_of: dict,
) -> None:
    now = pose(client, arm)

    for axis, value in zip("xyz", expected):
        assert now[axis] == pytest.approx(value, abs=POSITION_TOLERANCE_MM), now

    #
    # q and -q are the same orientation.
    #
    dot = sum(
        now[q] * orientation_of[q]
        for q in ("q1", "q2", "q3", "q4")
    )

    assert abs(dot) == pytest.approx(1.0, abs=1e-3), now


def move(
    client: Client,
    arm: str,
    target: tuple[float, float, float],
    wait: bool = True,
) -> dict:
    return expect(
        client.post(
            "/robot/rws/move",
            json={"arm": arm, "wait": wait, **dict(zip("xyz", target))},
        ),
        200,
    )


def wait_until_idle(
    client: Client,
    arm: str,
) -> dict:
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS

    while (status := expect(client.get("/robot/rws/status", params={"arm": arm}), 200))["message"] == "BUSY":
        assert time.monotonic() < deadline, f"{arm} arm still busy"

        time.sleep(0.1)

    return status


@contextmanager
def returns_to_start(
    client: Client,
    arm: str,
) -> Iterator[dict]:
    """Yield the arm's pose, and move it back there afterwards."""

    start = pose(client, arm)

    try:
        yield start

    finally:
        wait_until_idle(client, arm)

        here = position(pose(client, arm))

        if max(abs(a - b) for a, b in zip(here, position(start))) > POSITION_TOLERANCE_MM:
            assert move(client, arm, position(start)) == DONE
