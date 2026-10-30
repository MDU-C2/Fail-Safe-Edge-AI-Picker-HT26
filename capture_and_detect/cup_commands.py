"""Structured selection interface shared by the terminal and a future language UI."""

import copy
import math
import threading
import time

from cup_inventory import SelectionError, hover_moves, match_observation, selected_cup
from handover_plan import (DEFAULT_SPEED, handover_jobs, handover_settings,
                           normalize_handover_offset, pick_position_problem)


class CupSession:
    def __init__(self, capture, save, robot, calibration_id, *, max_result_age=15.0,
                 match_mm=20.0, match_px=40.0, tag_offset=(0, 0, 0), speed=DEFAULT_SPEED,
                 clock=time.monotonic, recovery=None, placing_grid=None, handover_offset=(0, 0, 0)):
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
        self.handover_offset = normalize_handover_offset(handover_offset)
        self.inventory = None
        self.scan_usable = False
        self.recovery = recovery
        self.placing_grid = placing_grid
        self.batch_progress = None
        self.motion_uncertain = recovery.required() if recovery is not None else False
        self._stop_event = threading.Event()
        self._stop_pending = threading.Event()
        self._stop_call_lock = threading.Lock()
        self._activity_lock = threading.Lock()
        self._state_lock = threading.RLock()

    @property
    def stop_requested(self):
        return self._stop_event.is_set() or getattr(self.robot, "stop_requested", False) is True

    @property
    def motion_uncertain(self):
        # Stop can arrive between a successful completion check and its state
        # assignment. The latch must dominate that older assignment everywhere,
        # including callers that read this attribute without status_summary().
        event = getattr(self, "_stop_event", None)
        return self._motion_uncertain or (event is not None and event.is_set())

    @motion_uncertain.setter
    def motion_uncertain(self, value):
        self._motion_uncertain = value

    @property
    def scan_usable(self):
        event = getattr(self, "_stop_event", None)
        return self._scan_usable and not (event is not None and event.is_set())

    @scan_usable.setter
    def scan_usable(self, value):
        self._scan_usable = value

    def latch_stop(self):
        """Immediately inhibit selection and dispatch without network or disk I/O."""
        self._stop_pending.set()
        self._stop_event.set()
        hook = getattr(self.robot, "latch_stop", None)
        if callable(hook):
            hook()
        self.motion_uncertain = True
        self.scan_usable = False

    def _check_stop(self):
        if self.stop_requested:
            raise SelectionError("Software stop requested. Inspect the robot, acknowledge recovery, then scan again.")

    def status_summary(self):
        """Cached application state only; this does not query the controller."""
        return {"stop_requested": self.stop_requested,
                "recovery_required": self.motion_uncertain or self.stop_requested,
                "scan_usable": self.scan_usable and not self.stop_requested,
                "batch": copy.deepcopy(self.batch_progress)}

    def request_stop(self):
        """Attempt a physical stop even when saving recovery information fails."""
        self.latch_stop()
        if not self._stop_call_lock.acquire(blocking=False):
            raise SelectionError("A software stop request is already in progress.")
        result = {"dry_run": self.robot.dry_run, "stopped": False}

        def preserve_recovery():
            with self._state_lock:
                self.motion_uncertain = True
                self.scan_usable = False
                if self.recovery is not None and not self.robot.dry_run:
                    try:
                        self.recovery.begin({"action": "software_stop", "stopped": result["stopped"],
                                             "batch": copy.deepcopy(self.batch_progress)})
                    except Exception as error:
                        result["recovery_save_failed"] = True
                        result["recovery_error"] = str(error)

        try:
            # Preserve the lockout before dispatch, including if the app crashes
            # while waiting for an unconfirmed stop response. Disk failure must
            # still allow the physical stop attempt below.
            preserve_recovery()
            try:
                hook = getattr(self.robot, "request_stop", None)
                if not callable(hook):
                    raise SelectionError("This robot client does not support a software stop.")
                result.update(hook())
            except Exception as error:
                result["message"] = f"Software stop is unconfirmed: {error} Use the robot's physical stop if needed."
            finally:
                preserve_recovery()
                if result.get("recovery_save_failed"):
                    result["message"] = (result.get("message", "Software stop requested.")
                                         + f" Recovery could not be saved: {result.pop('recovery_error')}. "
                                         "Keep this app open until inspection.")
            return result
        finally:
            self._stop_pending.clear()
            self._stop_call_lock.release()

    def scan(self):
        if not self._activity_lock.acquire(blocking=False):
            raise SelectionError("Wait for the active operation to finish before scanning.")
        try:
            return self._scan_active()
        finally:
            self._activity_lock.release()

    def _scan_active(self):
        # A failed scan must not leave the previous scan authorized for motion.
        self.scan_usable = False
        self.batch_progress = None
        inventory, _ = self.capture("scan")
        self.inventory = inventory
        self._check_stop()
        observation = inventory.get("placement_observation")
        if observation is not None and self.placing_grid is not None and "grid_snapshot" not in observation:
            raise SelectionError("Placement area could not be checked. Inspect the grid configuration and scan again.")
        if observation and "grid_snapshot" in observation and self.placing_grid is not None:
            if self.robot.dry_run or self.motion_uncertain:
                observation["message"] = ("Placement check preview only; saved locations unchanged. "
                                          + ("Dry-run mode." if self.robot.dry_run else "Recovery requires inspection."))
            else:
                # Scan owns the activity lock; do not reconcile during execution.
                # Reserved locations are protected independently by the grid lock.
                with self._state_lock:
                    self._check_stop()
                    before = observation["grid_snapshot"]
                    states = {slot["id"]: slot["state"] for slot in observation["slots"]}
                    after = self.placing_grid.reconcile_observation(before, states)
                    old = {slot["id"]: slot for slot in before["locations"]}
                    observation["released_ids"] = [slot["id"] for slot in after["locations"]
                        if old[slot["id"]]["occupied"] and not slot["occupied"]]
                    observation["applied"] = True
                    free = sum(not slot["occupied"] and not slot.get("vision_blocked", False)
                               for slot in after["locations"])
                    observation["message"] = (f"Placement grid checked using three fresh frames: {free} available. "
                                              "Remove a cup and scan again to release its location.")
            self.save(inventory)
        self._check_stop()
        self.scan_usable = not self.stop_requested
        return inventory

    def _check_selection(self, capture_id):
        self._check_stop()
        if self.motion_uncertain:
            raise SelectionError("Previous movement was not confirmed. Check the robot, then use reset.")
        if not self.scan_usable:
            raise SelectionError("Run scan before selecting a cup; scans are invalidated after movement.")
        if self.inventory is None or self.inventory["capture"] != capture_id:
            raise SelectionError("Capture ID is not the active scan. Run scan again.")

    def preview_selection(self, *, action, capture_id, cup_id):
        self._check_selection(capture_id)
        cup = selected_cup(self.inventory, capture_id, cup_id)
        if action == "hover":
            hover_moves(cup, tag_offset=self.tag_offset, speed=self.speed)
            return {"cup_ids": [cup_id], "placement": None}
        if action != "goto":
            raise SelectionError("Choose goto or hover.")
        if self.placing_grid is None:
            raise SelectionError("No placement grid configured. Restart with --placing-grid.")
        placement = self.placing_grid.preview(1)
        handover_jobs(cup, place=placement["locations"][0], tag_offset=self.tag_offset, speed=self.speed,
                      handover_offset=self.handover_offset)
        return {"cup_ids": [cup_id], "placement": placement}

    def preview_batch(self, *, capture_id):
        self._check_selection(capture_id)
        if self.placing_grid is None:
            raise SelectionError("No placement grid configured. Restart with --placing-grid.")
        candidates = [cup for cup in self.inventory["cups"] if cup.get("selectable")
                      and not cup.get("blocked_reason") and not pick_position_problem(cup, self.tag_offset)]
        candidates.sort(key=lambda cup: (-(cup.get("confidence") or 0), cup["id"]))
        if not candidates:
            raise SelectionError("No cups in this scan are available for picking.")
        available = sum(not place["occupied"] and not place.get("vision_blocked", False)
                        for place in self.placing_grid.snapshot()["locations"])
        if not available:
            raise SelectionError("All placement locations are occupied, reserved, or not confirmed clear. Inspect the area and scan again.")
        chosen = candidates[:available]
        placement = self.placing_grid.preview(len(chosen))
        for cup, place in zip(chosen, placement["locations"]):
            handover_jobs(cup, place=place, tag_offset=self.tag_offset, speed=self.speed,
                          handover_offset=self.handover_offset)
        return {"cup_ids": [cup["id"] for cup in chosen], "placement": placement,
                "excluded_cup_ids": [cup["id"] for cup in candidates[len(chosen):]]}

    def execute_selection(self, *, action, capture_id, cup_id, placement=None, expected_motion=None,
                          identity_check=None):
        preview = self.preview_selection(action=action, capture_id=capture_id, cup_id=cup_id)
        if placement is not None:
            if action != "goto" or len(placement.get("locations", [])) != 1:
                raise SelectionError("Invalid placement for this selection.")
            self.placing_grid.validate_preview(placement)
            preview["placement"] = placement
        return self._execute_many(action, capture_id, [cup_id], preview["placement"], batch=False,
                                  expected_motion=expected_motion, identity_check=identity_check)

    def execute_batch(self, *, capture_id, cup_ids=None, placement=None, expected_motion=None):
        if cup_ids is not None and (not isinstance(cup_ids, list) or not cup_ids
                or any(type(cup_id) is not int or cup_id < 1 for cup_id in cup_ids)
                or len(cup_ids) != len(set(cup_ids))):
            raise SelectionError("Batch cup IDs must be distinct positive integers.")
        preview = self.preview_batch(capture_id=capture_id)
        if cup_ids is not None and cup_ids != preview["cup_ids"]:
            raise SelectionError("The batch cup selection changed. Request a new batch preview.")
        if placement is not None:
            self.placing_grid.validate_preview(placement)
            if placement != preview["placement"]:
                raise SelectionError("The batch destinations changed. Request a new batch preview.")
        return self._execute_many("goto", capture_id, preview["cup_ids"], placement or preview["placement"], batch=True,
                                  expected_motion=expected_motion)

    def _execute_many(self, action, capture_id, cup_ids, placement, *, batch, expected_motion=None,
                      identity_check=None):
        if not self._activity_lock.acquire(blocking=False):
            raise SelectionError("This session is already executing an operation.")
        try:
            self._check_stop()
            if expected_motion is not None and expected_motion != self.motion_settings():
                raise SelectionError("Movement settings changed. Request a new preview before moving.")
            return self._execute_many_active(action, capture_id, cup_ids, placement, batch=batch,
                                             identity_check=identity_check)
        finally:
            self._activity_lock.release()

    def _execute_many_active(self, action, capture_id, cup_ids, placement, *, batch, identity_check=None):
        context = {"placement": copy.deepcopy(placement), "reservation": None, "index": 0,
                   "sequence": self.inventory["frame"]["sequence"], "completed_cup_ids": [],
                   "identity_check": identity_check}
        if placement is not None:
            self.placing_grid.validate_preview(placement)
        jobs = []
        self.batch_progress = ({"state": "running", "cup_ids": list(cup_ids), "completed_cups": [],
                                "current_cup_id": cup_ids[0], "total": len(cup_ids)} if batch else None)
        if batch:
            self.inventory["batch_execution"] = self.batch_progress
        succeeded = False
        try:
            for index, cup_id in enumerate(cup_ids):
                self._check_stop()
                context["index"] = index
                if batch:
                    self.batch_progress["current_cup_id"] = cup_id
                # The batch is bound to the original IDs, and each cup must match
                # a NEW observation. This flag never exposes a usable old scan to
                # another web action: the whole batch owns the single worker.
                self.scan_usable = True
                jobs.extend(self._execute_one(action=action, capture_id=capture_id,
                                             cup_id=cup_id, context=context))
                context["completed_cup_ids"].append(cup_id)
                if batch:
                    self.batch_progress["completed_cups"].append(cup_id)
                    self.save(self.inventory)
            if batch:
                self.batch_progress["state"] = "previewed" if self.robot.dry_run else "completed"
                self.save(self.inventory)
            with self._state_lock:
                self._check_stop()
                if not self.robot.dry_run and self.recovery is not None:
                    self.recovery.clear()
                # Stop latches without waiting for disk I/O or this lock. Check
                # again so a stop during journal clearing cannot be lost.
                if self.stop_requested and self.recovery is not None and not self.robot.dry_run:
                    self.recovery.begin({"action": "software_stop", "stopped": False,
                                         "batch": copy.deepcopy(self.batch_progress)})
                self._check_stop()
                self.motion_uncertain = False
            succeeded = True
            return jobs
        except BaseException as error:
            if self.stop_requested or (not self.robot.dry_run and (jobs or context["reservation"] is not None)):
                self.motion_uncertain = True
            if batch:
                self.batch_progress.update(state="interrupted", error=str(error))
                try:
                    self.save(self.inventory)
                except OSError:
                    pass
            raise
        finally:
            self.scan_usable = (succeeded and self.robot.dry_run and not self.motion_uncertain
                                and not self.stop_requested and not batch)

    def _execute_one(self, *, action, capture_id, cup_id, context):
        """One cup's checked sequence inside a single or confirmed batch request."""
        self._check_stop()
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
        sequence = context["sequence"]
        place = context["placement"]["locations"][context["index"]] if action == "goto" else None

        def plan(target):
            if action == "goto":
                return handover_jobs(target, place=place, tag_offset=self.tag_offset, speed=self.speed,
                                     handover_offset=self.handover_offset)
            return [{"name": name, "endpoint": "/robot/rws/move", "body": body}
                    for name, body in zip(("left_ready", "approach_cup"),
                                          hover_moves(target, tag_offset=self.tag_offset, speed=self.speed))]

        # Validate the saved observation too, before taking a fresh image or
        # opening either gripper. Picking requires a cup on the calibrated table.
        plan(original)

        def verify():
            nonlocal sequence
            fresh, started = self.capture("verification")
            self._check_stop()
            if fresh["calibration_sha256"] != self.calibration_id or tuple(fresh.get("tag_offset_mm", (0, 0, 0))) != self.tag_offset:
                raise SelectionError("Calibration changed. Restart with the intended calibration and scan again.")
            if fresh["frame"]["sequence"] <= sequence:
                raise SelectionError("Camera sequence did not advance or the gateway restarted. Scan again.")
            sequence = fresh["frame"]["sequence"]
            context["sequence"] = sequence
            checked_place = None
            if action == "goto" and ("placement_observation" in self.inventory
                                      or "placement_observation" in fresh):
                # Fresh target matching alone cannot protect a destination that
                # became occupied since Scan. Check it once before any command,
                # without changing occupancy/reservations.
                observation = fresh.get("placement_observation")
                slots = observation.get("slots") if isinstance(observation, dict) else None
                matches = ([slot for slot in slots if isinstance(slot, dict)
                            and type(slot.get("id")) is int and slot["id"] == place["id"]]
                           if isinstance(slots, list) else [])
                if len(matches) != 1 or matches[0].get("state") != "clear":
                    state = matches[0].get("state", "unknown") if len(matches) == 1 else "unknown"
                    raise SelectionError(f"Placement location {place['id']} is {state} in the fresh camera check; "
                                         "inspect the destination and scan again.")
                checked_place = copy.deepcopy(matches[0])
            target = match_observation(original, fresh["cups"], self.match_mm, self.match_px,
                                       tag_offset=self.tag_offset)
            # Named cups reuse this one pre-motion image. A VLM may veto the
            # geometric target, but must never redirect the robot to another cup.
            identity = (context["identity_check"](fresh, target)
                        if context["identity_check"] is not None else None)
            self._check_stop()
            jobs = plan(target)
            original["last_verification"] = {
                "capture": fresh["capture"], "frame": fresh["frame"],
                "matched_cup_id": target["id"],
                "robot_mm": {axis: target[axis] for axis in ("x", "y", "z")},
                "action": action, "jobs": jobs, "placement": copy.deepcopy(place),
                "motion_settings": self.motion_settings(),
            }
            if checked_place is not None:
                original["last_verification"]["placement_visibility"] = checked_place
            if identity is not None:
                original["last_verification"]["named_identity"] = identity
            return jobs, started

        jobs, started = verify()

        def check_age():
            age = self.clock() - started
            if not math.isfinite(age) or age < 0 or age > self.max_result_age:
                raise SelectionError(f"Verification image is {age:.1f}s old; motion blocked "
                                     f"(limit {self.max_result_age:g}s).")

        check_age()
        self._check_stop()
        if self.robot.dry_run:
            self.robot.execute_jobs(jobs)
            self._check_stop()
            original["last_action"] = f"dry-run {action}; no robot command sent"
            self.save(self.inventory)
            self._check_stop()
            self.scan_usable = not self.stop_requested
            return jobs

        for cup in self.inventory["cups"]:
            if cup["id"] not in context["completed_cup_ids"]:
                cup["status"] = "needs_rescan"
        original["status"] = "command_pending"
        original["execution"] = {"action": action, "state": "pending", "completed_steps": []}
        execution = original["execution"]
        execution["total_steps"] = len(jobs)
        execution["placement"] = copy.deepcopy(place)

        def before_job(index, job):
            self._check_stop()
            # Use the single pre-motion verification throughout this cup's pick.
            # Do not capture again at ready; the initial image must still meet
            # the freshness deadline before approach, descent and gripping.
            # After gripping, the observed cup is intentionally moved and may
            # be hidden by the arm. Its old camera age must not interrupt the
            # transfer while either gripper is holding it. Fixed handover steps
            # still require a live lease and confirmed completion of each job.
            if index == 0 or job["name"] in ("approach_cup", "descend_to_cup", "grip_cup"):
                check_age()
            job = jobs[index]
            execution.update(state="running", pending_step=job["name"], pending_job=job)
            self.save(self.inventory)
            # Recheck after potentially slow persistence work, just
            # before dispatch. A changed destination must stop this command.
            if context["reservation"] is not None:
                remaining = [p["id"] for p in context["placement"]["locations"][context["index"]:]]
                self.placing_grid.validate_reservation(context["reservation"], remaining)
            if index == 0 or job["name"] in ("approach_cup", "descend_to_cup", "grip_cup"):
                check_age()
            print(f"Step {index + 1}/{len(jobs)}: {job['name'].replace('_', ' ')}", flush=True)
            self._check_stop()
            return job

        def after_job(index, job):
            self._check_stop()
            execution["completed_steps"].append(job)
            execution["last_completed_step"] = job["name"]
            execution.pop("pending_step", None)
            execution.pop("pending_job", None)
            self.save(self.inventory)

        try:
            self.save(self.inventory)  # If persistence fails, do not dispatch motion.
            with self._state_lock:
                self._check_stop()
                if self.recovery is not None:
                    self.recovery.begin({"capture": capture_id, "cup_id": cup_id, "action": action,
                                         "placement": copy.deepcopy(place), "batch": copy.deepcopy(self.batch_progress)})
            self.motion_uncertain = True
            if action == "goto" and context["reservation"] is None:
                context["reservation"] = self.placing_grid.reserve(context["placement"])
            # Reservation I/O also counts toward the camera freshness deadline.
            check_age()
            self._check_stop()
            self.robot.execute_jobs(jobs, before_send=before_job, after_done=after_job)
            self._check_stop()
            if action == "goto":
                self.placing_grid.complete(context["reservation"], place["id"])
            original["status"] = "placed" if action == "goto" else "at_hover"
            execution["state"] = "completed"
            self.save(self.inventory)
            with self._state_lock:
                self._check_stop()
                self.motion_uncertain = False
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

    def motion_settings(self):
        return {"speed": self.speed, "handover": handover_settings(self.handover_offset, self.speed)}

    def set_handover_offset(self, offset):
        """Change future receiving poses only; operator review precedes motion."""
        offset = normalize_handover_offset(offset)
        if not self._activity_lock.acquire(blocking=False):
            raise SelectionError("Wait for the active operation before changing handover alignment.")
        try:
            self._check_stop()
            if self.motion_uncertain:
                raise SelectionError("Inspect and acknowledge recovery before changing handover alignment.")
            self.handover_offset = offset
            self.scan_usable = False
            self.batch_progress = None
        finally:
            self._activity_lock.release()

    def set_speed(self, speed):
        """Update future plans only; never change an in-flight movement."""
        from robot_controls import valid_speed
        speed = valid_speed(speed)
        if not self._activity_lock.acquire(blocking=False):
            raise SelectionError("Wait for the active operation before changing speed.")
        try:
            self._check_stop()
            if self.motion_uncertain:
                raise SelectionError("Inspect and acknowledge recovery before changing speed.")
            self.speed = speed
        finally:
            self._activity_lock.release()

    def execute_control(self, action, arm):
        """Execute a reviewed operator action with the same persistent recovery guard.

        Home/gripper commands use the existing lease, sequential DONE checks and
        measured home verification. No camera selection, automatic home, or retry
        is inferred from a controller reset or collision acknowledgement.
        """
        from robot_controls import control_plan
        plan = control_plan(action, arm, self.speed)
        if not self._activity_lock.acquire(blocking=False):
            raise SelectionError("Wait for the active operation before using robot controls.")
        armed = False
        try:
            self._check_stop()
            if self.motion_uncertain:
                raise SelectionError("Inspect and acknowledge app recovery before using robot controls.")
            self.scan_usable = False
            self.batch_progress = None

            def before_job(_index, _job):
                self._check_stop()
                status = self.robot.read_control_status()
                if (status.get("execution_state") != "running"
                        or status.get("controller_state") != "motoron"
                        or status.get("operation_mode") != "AUTO"):
                    raise SelectionError("Robot controls require RAPID running, motors on, and AUTO mode. "
                                         "Inspect the controller status; no automatic restart is performed.")
                if status.get("collision_state") not in ("INIT", "CONFIRMED"):
                    raise SelectionError("Controller collision clearance was not confirmed. Refresh status using the updated "
                                         "Pi gateway and acknowledge any collision. No movement sent.")
                self._check_stop()

            if not self.robot.dry_run:
                with self._state_lock:
                    self._check_stop()
                    # Persist before any potentially effective command. A failed
                    # write prevents dispatch, and an unconfirmed reply stays blocked.
                    if self.recovery is not None:
                        self.recovery.begin({"action": "operator_control", "control": action, "arm": arm})
                    self.motion_uncertain = True
                    armed = True
            self._check_stop()
            if plan["jobs"]:
                self.robot.execute_jobs(plan["jobs"], before_send=before_job)
                result = {"message": ("Preview only; no robot command sent." if self.robot.dry_run else
                                      "Robot reported the control action complete. Scan again before picking.")}
            else:
                result = self.robot.control_action(action)
            self._check_stop()
            with self._state_lock:
                self._check_stop()
                if armed and self.recovery is not None:
                    self.recovery.clear()
                if self.stop_requested and armed and self.recovery is not None:
                    self.recovery.begin({"action": "software_stop", "stopped": False})
                self._check_stop()
                self.motion_uncertain = False
            return result
        except BaseException:
            if armed or self.stop_requested:
                self.motion_uncertain = True
            raise
        finally:
            self.scan_usable = False
            self._activity_lock.release()

    def acknowledge_recovery(self):
        """Operator acknowledgement only; does not stop, home, or otherwise move hardware."""
        if not self._activity_lock.acquire(blocking=False):
            raise SelectionError("Wait for the active operation to finish before acknowledging recovery.")
        try:
            if not self._stop_call_lock.acquire(blocking=False):
                raise SelectionError("Wait for the software stop request to finish before recovery.")
            try:
                if self._stop_pending.is_set():
                    raise SelectionError("Wait for the software stop request to finish before recovery.")
                with self._state_lock:
                    hook = getattr(self.robot, "clear_stop", None)
                    if callable(hook):
                        hook()
                    if self.recovery is not None and not self.robot.dry_run:
                        self.recovery.clear()
                    if self._stop_pending.is_set():
                        self.latch_stop()
                        if self.recovery is not None and not self.robot.dry_run:
                            self.recovery.begin({"action": "software_stop", "stopped": False})
                        raise SelectionError("A new software stop was requested; recovery remains blocked.")
                    self._stop_event.clear()
                    self.motion_uncertain = False
                    self.scan_usable = False
                    # latch_stop is deliberately lock-free. Restore its state
                    # if a new request arrived during the clearing operations.
                    if self._stop_pending.is_set():
                        self.latch_stop()
                        if self.recovery is not None and not self.robot.dry_run:
                            self.recovery.begin({"action": "software_stop", "stopped": False})
                        raise SelectionError("A new software stop was requested; recovery remains blocked.")
            finally:
                self._stop_call_lock.release()
        finally:
            self._activity_lock.release()
