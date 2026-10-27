"""Structured selection interface shared by the terminal and a future language UI."""

import math
import time

from cup_inventory import SelectionError, hover_moves, match_observation, selected_cup
from handover_plan import handover_jobs


class CupSession:
    def __init__(self, capture, save, robot, calibration_id, *, max_result_age=15.0,
                 match_mm=20.0, match_px=40.0, tag_offset=(0, 0, 0), speed=50.0,
                 clock=time.monotonic, recovery=None):
        for name, value in (("max_result_age", max_result_age), ("match_mm", match_mm), ("match_px", match_px)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        self.capture = capture
        self.save = save
        self.robot = robot
        self.calibration_id = calibration_id
        self.max_result_age = max_result_age
        self.match_mm = match_mm
        self.match_px = match_px
        self.clock = clock
        self.tag_offset = tuple(tag_offset)
        self.speed = speed
        self.inventory = None
        self.scan_usable = False
        self.recovery = recovery
        self.motion_uncertain = recovery.required() if recovery is not None else False

    def scan(self):
        # A failed scan must not leave the previous scan authorized for motion.
        self.scan_usable = False
        inventory, _ = self.capture("scan")
        self.inventory = inventory
        self.scan_usable = True
        return inventory

    def execute_selection(self, *, action, capture_id, cup_id):
        """A future LLM can supply this bounded request, never raw XYZ or commands."""
        if action not in ("goto", "hover"):
            raise SelectionError("Supported actions: goto (pick, hand over, place and home) or hover.")
        if self.motion_uncertain:
            raise SelectionError("Previous movement was not confirmed. Check the robot, then use reset.")
        if not self.scan_usable:
            raise SelectionError("Run scan before selecting a cup; scans are invalidated after movement.")
        original = selected_cup(self.inventory, capture_id, cup_id)
        if (self.inventory["calibration_sha256"] != self.calibration_id
                or tuple(self.inventory.get("tag_offset_mm", (0, 0, 0))) != self.tag_offset):
            raise SelectionError("Scan calibration does not match this session. Run scan again.")
        self.scan_usable = False
        # Every attempted selection needs a new scan if validation fails.
        sequence = self.inventory["frame"]["sequence"]

        def plan(target):
            if action == "goto":
                return handover_jobs(target, tag_offset=self.tag_offset, speed=self.speed)
            return [{"name": name, "endpoint": "/robot/rws/move", "body": body}
                    for name, body in zip(("left_ready", "approach_cup"),
                                          hover_moves(target, tag_offset=self.tag_offset, speed=self.speed))]

        # Validate the saved observation too, before taking a fresh image or
        # opening either gripper. Picking requires a cup on the calibrated table.
        plan(original)

        def verify():
            nonlocal sequence
            fresh, started = self.capture("verification")
            if fresh["calibration_sha256"] != self.calibration_id or tuple(fresh.get("tag_offset_mm", (0, 0, 0))) != self.tag_offset:
                raise SelectionError("Calibration changed. Restart with the intended calibration and scan again.")
            if fresh["frame"]["sequence"] <= sequence:
                raise SelectionError("Camera sequence did not advance or the gateway restarted. Scan again.")
            sequence = fresh["frame"]["sequence"]
            target = match_observation(original, fresh["cups"], self.match_mm, self.match_px,
                                       tag_offset=self.tag_offset)
            jobs = plan(target)
            original["last_verification"] = {
                "capture": fresh["capture"], "frame": fresh["frame"],
                "matched_cup_id": target["id"],
                "robot_mm": {axis: target[axis] for axis in ("x", "y", "z")},
                "action": action, "jobs": jobs,
            }
            return jobs, started

        jobs, started = verify()

        def check_age():
            age = self.clock() - started
            if not math.isfinite(age) or age < 0 or age > self.max_result_age:
                raise SelectionError(f"Verification image is {age:.1f}s old; motion blocked "
                                     f"(limit {self.max_result_age:g}s).")

        check_age()
        if self.robot.dry_run:
            self.robot.execute_jobs(jobs)
            original["last_action"] = f"dry-run {action}; no robot command sent"
            self.save(self.inventory)
            self.scan_usable = True
            return jobs

        for cup in self.inventory["cups"]:
            cup["status"] = "needs_rescan"
        original["status"] = "command_pending"
        original["execution"] = {"action": action, "state": "pending", "completed_steps": []}
        execution = original["execution"]
        self.save(self.inventory)  # If persistence fails, do not dispatch motion.
        if self.recovery is not None:
            self.recovery.begin({"capture": capture_id, "cup_id": cup_id, "action": action})

        def before_job(index, job):
            nonlocal started
            if job["name"] == "approach_cup":
                # The ready move may take longer than the observation deadline.
                # Reobserve while at ready, with the lease still held, before
                # approaching the cup. Do not silently use the old coordinates.
                print("At ready position. Rechecking the selected cup before approaching ...", flush=True)
                verified_jobs, started = verify()
                jobs[:] = verified_jobs
            # After gripping, the observed cup is intentionally moved and may
            # be hidden by the arm. Its old camera age must not interrupt the
            # transfer while either gripper is holding it. Fixed handover steps
            # still require a live lease and confirmed completion of each job.
            if index == 0 or job["name"] in ("approach_cup", "descend_to_cup", "grip_cup"):
                check_age()
            job = jobs[index]
            execution.update(state="running", pending_step=job["name"], pending_job=job)
            self.save(self.inventory)
            if index == 0 or job["name"] in ("approach_cup", "descend_to_cup", "grip_cup"):
                check_age()
            print(f"Step {index + 1}/{len(jobs)}: {job['name'].replace('_', ' ')}", flush=True)
            return job

        def after_job(index, job):
            execution["completed_steps"].append(job)
            execution["last_completed_step"] = job["name"]
            execution.pop("pending_step", None)
            execution.pop("pending_job", None)
            self.save(self.inventory)

        try:
            self.robot.execute_jobs(jobs, before_send=before_job, after_done=after_job)
            original["status"] = "placed" if action == "goto" else "at_hover"
            execution["state"] = "completed"
            self.save(self.inventory)
            if self.recovery is not None:
                self.recovery.clear()
        except BaseException as error:
            # A timeout/interrupt does not tell us whether the robot actually moved.
            self.motion_uncertain = True
            original["status"] = "movement_unconfirmed"
            execution.update(state="interrupted", error=str(error))
            try:
                self.save(self.inventory)
            except OSError as save_error:
                # Keep the original failure, and keep the in-memory lockout.
                print(f"Could not save interrupted sequence: {save_error}", flush=True)
            raise
        return jobs

    def acknowledge_recovery(self):
        """Operator acknowledgement only; does not stop, home, or otherwise move hardware."""
        if self.recovery is not None:
            self.recovery.clear()
        self.motion_uncertain = False
        self.scan_usable = False
