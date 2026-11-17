"""The legacy /robot/command path against the REAL YuMi, alone and
side by side with the RWS API (run with --real-robot).

These tests MOVE THE REAL ROBOT, under the same rules as
test_real_robot.py: through the running gateway (--real-robot-url)
and the RAPID program already running, nothing loaded or started;
every move straight up from the arm's current pose (at most
MAX_OFFSET_MM) and the arm put back afterwards; the refusals move
nothing.

/robot/command goes to the team's CommServer (rapid/CommServer.mod,
task communication, port 5001): a job answers STARTED, STATUS
answers BUSY / DONE / ERR move failed, "R," addresses the right arm.
CommServer and the RWS API write the same PERS variables, so each
sees the other's jobs: the other path's job makes CommServer answer
ERR busy and the RWS API 409, and both status queries report it.
"""

import time

import pytest

from gateway.tests.real_robot import (
    ARMS,
    DONE,
    JOB_TIMEOUT_SECONDS,
    MAX_OFFSET_MM,
    MOVE_FAILED,
    OUT_OF_REACH,
    OUTSIDE_SAFE_ZONE,
    SMALL_OFFSET_MM,
    STARTED,
    above,
    assert_at,
    move,
    admin,
    pose,
    position,
    real_robot,
    returns_to_start,
    wait_until_idle,
)
from gateway.tests.support import Client, expect


pytestmark = pytest.mark.real_robot

BUSY = {"accepted": True, "message": "BUSY"}
ERR_BUSY = {"accepted": False, "message": "ERR busy"}


# ================================================================
# Helpers
# ================================================================

def prefix(
    arm: str,
) -> str:
    return "R," if arm == "right" else ""


def command(
    client: Client,
    text: str,
) -> dict:
    return expect(
        client.post("/robot/command", json={"command": text}),
        200,
    )


def target_command(
    arm: str,
    target: tuple[float, float, float],
) -> str:
    return prefix(arm) + ",".join(f"{value:.1f}" for value in target)


def legacy_status(
    client: Client,
    arm: str,
) -> dict:
    return command(client, prefix(arm) + "STATUS")


def legacy_wait(
    client: Client,
    arm: str,
) -> dict:
    """Poll STATUS like rapid/tests/handover.py until the arm is done."""

    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS

    while (status := legacy_status(client, arm)) == BUSY:
        assert time.monotonic() < deadline, f"{arm} arm still busy"

        time.sleep(0.1)

    return status


def legacy_move(
    client: Client,
    arm: str,
    target: tuple[float, float, float],
) -> dict:
    started = command(client, target_command(arm, target))

    if started != STARTED:
        return started

    return legacy_wait(client, arm)


# ================================================================
# Legacy alone
# ================================================================

@pytest.mark.parametrize("arm", ARMS)
def test_legacy_move_up_and_back(
    admin: Client,
    arm: str,
) -> None:
    with returns_to_start(admin, arm) as start:
        target = above(position(start), SMALL_OFFSET_MM)

        assert legacy_move(admin, arm, target) == DONE

        assert_at(admin, arm, target, start)

        assert legacy_move(admin, arm, position(start)) == DONE

        assert_at(admin, arm, position(start), start)


def test_legacy_both_arms_at_once(
    admin: Client,
) -> None:
    with returns_to_start(admin, "left") as left, returns_to_start(admin, "right") as right:
        targets = {
            "left": above(position(left), MAX_OFFSET_MM),
            "right": above(position(right), MAX_OFFSET_MM),
        }

        for arm, target in targets.items():
            assert command(admin, target_command(arm, target)) == STARTED

        for arm in ARMS:
            assert legacy_status(admin, arm) == BUSY, arm

        for arm, start in (("left", left), ("right", right)):
            assert legacy_wait(admin, arm) == DONE

            assert_at(admin, arm, targets[arm], start)


def test_legacy_busy_arm_refuses_a_second_job(
    admin: Client,
) -> None:
    with returns_to_start(admin, "left") as start:
        target = above(position(start), MAX_OFFSET_MM)

        assert command(admin, target_command("left", target)) == STARTED

        assert command(admin, target_command("left", target)) == ERR_BUSY

        assert legacy_wait(admin, "left") == DONE

        assert_at(admin, "left", target, start)


def test_legacy_refusals_do_not_move(
    admin: Client,
) -> None:
    start = pose(admin, "left")

    assert command(admin, "1,2") == {"accepted": False, "message": "ERR bad format"}

    #
    # CommServer's own safe zone (its X/Y/Z_MIN/MAX).
    #
    assert command(admin, target_command("left", OUTSIDE_SAFE_ZONE)) == {
        "accepted": False,
        "message": "ERR outside safe zone",
    }

    #
    # Accepted by CommServer, refused by the arm task.
    #
    assert legacy_move(admin, "left", OUT_OF_REACH) == MOVE_FAILED

    assert_at(admin, "left", position(start), start)


# ================================================================
# Legacy and RWS side by side
# ================================================================

def test_rws_job_blocks_legacy_on_that_arm(
    admin: Client,
) -> None:
    with returns_to_start(admin, "left") as start:
        target = above(position(start), MAX_OFFSET_MM)

        assert move(admin, "left", target, wait=False) == STARTED

        #
        # CommServer sees the RWS job.
        #
        assert legacy_status(admin, "left") == BUSY

        assert command(admin, target_command("left", target)) == ERR_BUSY

        assert legacy_wait(admin, "left") == DONE

        assert_at(admin, "left", target, start)


def test_legacy_job_blocks_rws_on_that_arm(
    admin: Client,
) -> None:
    with returns_to_start(admin, "left") as start:
        target = above(position(start), MAX_OFFSET_MM)

        assert command(admin, target_command("left", target)) == STARTED

        #
        # The RWS API sees the CommServer job.
        #
        assert expect(admin.get("/robot/rws/status", params={"arm": "left"}), 200) == BUSY

        response = admin.post(
            "/robot/rws/move",
            json={"arm": "left", "wait": False, **dict(zip("xyz", target))},
        )

        assert expect(response, 409)["detail"] == "The left arm is busy with an earlier job"

        assert wait_until_idle(admin, "left") == DONE

        assert_at(admin, "left", target, start)


def test_rws_and_legacy_on_different_arms_at_once(
    admin: Client,
) -> None:
    with returns_to_start(admin, "left") as left, returns_to_start(admin, "right") as right:
        left_target = above(position(left), MAX_OFFSET_MM)
        right_target = above(position(right), MAX_OFFSET_MM)

        assert move(admin, "left", left_target, wait=False) == STARTED
        assert command(admin, target_command("right", right_target)) == STARTED

        assert expect(admin.get("/robot/rws/status", params={"arm": "left"}), 200) == BUSY
        assert legacy_status(admin, "right") == BUSY

        assert wait_until_idle(admin, "left") == DONE
        assert legacy_wait(admin, "right") == DONE

        assert_at(admin, "left", left_target, left)
        assert_at(admin, "right", right_target, right)
