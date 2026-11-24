"""Finite, native Lynx takeover behind the gateway's cloud/Pi priorities."""

import time

from cup_inventory import SelectionError
from handover_plan import right_camera_park_jobs
from robot_client import RobotClient, RobotCommandError


def _named(step, name):
    # QueueRobotClient prefixes each original plan name with a submission ID.
    return step.get("name") == name or step.get("name", "").endswith("_" + name)


def _finish_inherited_parking(session, queue):
    """Only complete the known camera-clear tail of a successful cup queue."""
    steps = queue.get("steps", [])
    if queue.get("state") != "done" or not any(_named(s, "right_clear") for s in steps):
        return
    required = {"left_release", "left_park", "right_release", "right_clear"}
    if not all(any(_named(s, name) and s.get("state") == "done" for s in steps) for name in required):
        raise RobotCommandError("Previous cup queue does not confirm release and camera clearance; inspect the robot.")
    if any(_named(s, "right_park") for s in steps):
        return
    jobs = right_camera_park_jobs(speed=session.speed, phase_speeds=session.phase_speeds)
    record = {"action": "takeover_camera_parking", "pending_jobs": [], "completed_steps": []}

    def before(_index, job):
        session.robot.check_lease()
        session.motion_uncertain = True
        record["pending_jobs"].append(job["name"])
        session.recovery.begin(record)

    def done(_index, job):
        record["pending_jobs"].remove(job["name"])
        record["completed_steps"].append(job["name"])
        session.recovery.begin(record)

    session.robot.execute_jobs(jobs, before_send=before, after_done=done)
    if record["pending_jobs"] or record["completed_steps"] != [job["name"] for job in jobs]:
        raise RobotCommandError("Inherited camera parking completion is unknown.")
    session._finish_confirmed_motion()


def run_standby(session, *, output=print, sleep=time.sleep, poll=1.0):
    """Wait for tertiary ownership, finish one fresh batch, then exit.

    A previously accepted cup is allowed to finish at the gateway. Recovery
    records and failed queues are never cleared automatically. Losing the lease
    cannot cause a second submission or an automatic Stop against a new owner.
    """
    robot = session.robot
    if robot.dry_run or not session.between_cup_grid_check or session.continuous_batch:
        raise SelectionError("Lynx standby requires live finite workspace/grid refresh mode.")
    if session.recovery is None or session.motion_uncertain:
        raise SelectionError("Inspect the previous movement and resolve recovery before arming Lynx standby.")
    output("Lynx standby: waiting behind cloud (300) and Pi (200), using tertiary priority 100.")
    try:
        while not robot.try_begin_standby(expected_client_id="tertiary", expected_priority=100):
            sleep(poll)
        output("Lynx owns control. Waiting for the previous queued cup to finish.")
        queue = robot.wait_for_idle_queue()
        # Queue history cannot tell whether somebody subsequently jogged a
        # gripper. Read current telemetry before any takeover parking or pick.
        # These are the same empty/open thresholds as the original queue runner.
        for arm in ("left", "right"):
            robot.check_lease()
            position = RobotClient.read_gripper_position(robot, arm)
            if position is None or 1.0 < position < 23.0:
                raise RobotCommandError(f"The {arm} gripper may hold a cup or its position is unknown; inspect before takeover.")
        robot.check_lease()
        _finish_inherited_parking(session, queue)
        robot.check_lease()
        inventory = session.scan()
        robot.check_lease()
        jobs = session.execute_observed_batch(capture_id=inventory["capture"])
        reason = session.batch_progress.get("completion_reason")
        if reason == "workspace_empty":
            output("Workspace empty. Both arms are Home; this Lynx batch is finished.")
        else:
            output("Lynx batch finished: " + str(reason or "complete") + ".")
        return jobs
    finally:
        robot.end_standby()
