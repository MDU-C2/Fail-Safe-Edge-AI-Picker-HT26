"""Structured selection interface shared by the terminal and a future language UI."""

import copy
import json
import math
import threading
import time

from cup_inventory import SelectionError, hover_moves, match_observation, selected_cup
from handover_plan import (handover_jobs, left_only_jobs, handover_settings, motion_speeds,
                           normalize_handover_offset, pick_position_problem)


class CupSession:
    def __init__(self, capture, save, robot, calibration_id, *, max_result_age=15.0,
                 match_mm=20.0, match_px=40.0, tag_offset=(0, 0, 0), speed=None,
                 clock=time.monotonic, recovery=None, placing_grid=None, handover_offset=(0, 0, 0),
                 left_only=False):
        if type(left_only) is not bool:
            raise ValueError("left_only must be a boolean")
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
        self.speed, self.home_speed = motion_speeds(speed)
        self.handover_offset = normalize_handover_offset(handover_offset)
        self.inventory = None
        self.scan_usable = False
        self.recovery = recovery
        self.placing_grid = placing_grid
        self.left_only = left_only
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
                                             "batch": copy.deepcopy(self.batch_progress),
                                             "capture": (self.inventory or {}).get("capture"),
                                             "executions": {str(cup["id"]): copy.deepcopy(cup["execution"])
                                                            for cup in (self.inventory or {}).get("cups", [])
                                                            if "execution" in cup}})
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
        if self.left_only:
            left_only_jobs(cup, tag_offset=self.tag_offset, speed=self.speed, home_speed=self.home_speed)
            return {"cup_ids": [cup_id], "placement": None}
        if self.placing_grid is None:
            raise SelectionError("No placement grid configured. Restart with --placing-grid.")
        placement = self.placing_grid.preview(1)
        handover_jobs(cup, place=placement["locations"][0], tag_offset=self.tag_offset, speed=self.speed,
                      home_speed=self.home_speed, handover_offset=self.handover_offset)
        return {"cup_ids": [cup_id], "placement": placement}

    def preview_batch(self, *, capture_id):
        self._check_selection(capture_id)
        if self.placing_grid is None and not self.left_only:
            raise SelectionError("No placement grid configured. Restart with --placing-grid.")
        candidates = [cup for cup in self.inventory["cups"] if cup.get("selectable")
                      and not cup.get("blocked_reason") and not pick_position_problem(cup, self.tag_offset)]
        candidates.sort(key=lambda cup: (-(cup.get("confidence") or 0), cup["id"]))
        if not candidates:
            raise SelectionError("No cups in this scan are available for picking.")
        if self.left_only:
            for cup in candidates:
                left_only_jobs(cup, tag_offset=self.tag_offset, speed=self.speed, home_speed=self.home_speed)
            return {"cup_ids": [cup["id"] for cup in candidates], "placement": None,
                    "excluded_cup_ids": []}
        available = sum(not place["occupied"] and not place.get("vision_blocked", False)
                        for place in self.placing_grid.snapshot()["locations"])
        if not available:
            raise SelectionError("All placement locations are occupied, reserved, or not confirmed clear. Inspect the area and scan again.")
        chosen = candidates[:available]
        placement = self.placing_grid.preview(len(chosen))
        for cup, place in zip(chosen, placement["locations"]):
            handover_jobs(cup, place=place, tag_offset=self.tag_offset, speed=self.speed,
                          home_speed=self.home_speed, handover_offset=self.handover_offset)
        return {"cup_ids": [cup["id"] for cup in chosen], "placement": placement,
                "excluded_cup_ids": [cup["id"] for cup in candidates[len(chosen):]]}

    def execute_selection(self, *, action, capture_id, cup_id, placement=None, expected_motion=None,
                          identity_check=None):
        preview = self.preview_selection(action=action, capture_id=capture_id, cup_id=cup_id)
        if placement is not None:
            if self.left_only:
                raise SelectionError("Left-only picks do not use placement destinations.")
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
            if self.left_only:
                raise SelectionError("Left-only picks do not use placement destinations.")
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
        place = (context["placement"]["locations"][context["index"]]
                 if action == "goto" and not self.left_only else None)

        def plan(target):
            if action == "goto":
                if self.left_only:
                    return left_only_jobs(target, tag_offset=self.tag_offset, speed=self.speed,
                                          home_speed=self.home_speed)
                return handover_jobs(target, place=place, tag_offset=self.tag_offset, speed=self.speed,
                                     home_speed=self.home_speed, handover_offset=self.handover_offset)
            return [{"name": name, "endpoint": "/robot/rws/move", "body": body}
                    for name, body in zip(("left_ready", "approach_cup"),
                                          hover_moves(target, tag_offset=self.tag_offset, speed=self.speed))]

        # Validate the saved observation too, before taking a fresh image or
        # opening either gripper. Picking requires a cup on the calibrated table.
        original_jobs = plan(original)

        def verification_age(started):
            if isinstance(started, bool) or not isinstance(started, (int, float)):
                raise SelectionError("Verification image has no valid capture time; motion blocked.")
            age = self.clock() - started
            if not math.isfinite(age) or age < 0 or age > self.max_result_age:
                raise SelectionError(f"Verification image is {age:.1f}s old; motion blocked "
                                     f"(limit {self.max_result_age:g}s).")

        def destination(observation):
            slots = observation.get("slots") if isinstance(observation, dict) else None
            matches = ([slot for slot in slots if isinstance(slot, dict)
                        and type(slot.get("id")) is int and slot["id"] == place["id"]]
                       if isinstance(slots, list) else [])
            return matches[0] if len(matches) == 1 else None

        def finite_number(value):
            return type(value) in (int, float) and math.isfinite(value)

        def destination_identity(observation, slot):
            # Only the bounded recheck uses this stricter schema. Older clear
            # observations retain their existing single-frame path.
            polygon = slot.get("polygon_px") if slot is not None else None
            if (not isinstance(polygon, list) or not 3 <= len(polygon) <= 16
                    or any(not isinstance(point, list) or len(point) != 2
                           or not all(finite_number(v) for v in point) for point in polygon)):
                raise SelectionError("Placement destination polygon is missing or invalid; scan again.")
            area = sum(a[0] * b[1] - b[0] * a[1]
                       for a, b in zip(polygon, polygon[1:] + polygon[:1]))
            if len({tuple(point) for point in polygon}) != len(polygon) or abs(area) < 1:
                raise SelectionError("Placement destination polygon is invalid; scan again.")
            if ("table_reference" in observation
                    and not isinstance(observation["table_reference"], dict)):
                raise SelectionError("Placement table reference is invalid; scan again.")
            identity = {key: observation[key] for key in ("cell_size_mm", "table_reference")
                        if key in observation}
            identity["polygon_px"] = polygon
            try:
                return json.dumps(identity, sort_keys=True, allow_nan=False)
            except (TypeError, ValueError) as error:
                raise SelectionError("Placement geometry or table reference is invalid; scan again.") from error

        def unobstructed(slot, *, clear=False):
            return (slot is not None and type(slot.get("cup_count")) is int
                    and slot["cup_count"] == 0 and slot.get("evidence") == []
                    and slot.get("assigned_cups") == []
                    and all(finite_number(slot.get(key)) and (0.995 if clear else 0) <= slot[key] <= 1
                            for key in ("valid_fraction", "table_fraction"))
                    and all(finite_number(slot.get(key)) and 0.5 <= slot[key] <= 1
                            for key in ("visible_fraction", "floor_visible_fraction")))

        def blocked_destination(slot):
            state = slot.get("state", "unknown") if slot is not None else "unknown"
            reason = slot.get("reason") if slot is not None else None
            detail = reason if isinstance(reason, str) and reason else "Destination evidence is missing or ambiguous."
            return SelectionError(f"Placement location {place['id']} is {state} in the fresh camera check: "
                                  f"{detail} Inspect the destination and scan again.")

        def verify():
            nonlocal sequence
            checks = []
            rechecking = False
            expected_identity = None
            audit = {"action": action, "placement": copy.deepcopy(place),
                     "placement_checks": checks, "state": "checking"}
            original["last_verification"] = audit
            try:
                # At most three additional observations, all taken while the
                # robot is stationary. A failed recheck ends this attempt.
                for attempt in range(4):
                    self._check_stop()
                    fresh, started = self.capture("verification")
                    if not isinstance(fresh, dict):
                        raise SelectionError("Malformed verification observation; scan again.")
                    observation = fresh.get("placement_observation")
                    checking_place = place is not None and (rechecking
                        or "placement_observation" in self.inventory or "placement_observation" in fresh)
                    slot = destination(observation) if checking_place else None
                    entry = {"capture": fresh.get("capture"), "frame": copy.deepcopy(fresh.get("frame")),
                             "state": slot.get("state", "unknown") if slot is not None else "unknown",
                             "reason": slot.get("reason") if slot is not None else None,
                             "reason_code": slot.get("reason_code") if slot is not None else None}
                    checks.append(entry)
                    audit.update(capture=fresh.get("capture"), frame=copy.deepcopy(fresh.get("frame")))
                    self._check_stop()
                    verification_age(started)
                    offset = fresh.get("tag_offset_mm", (0, 0, 0))
                    if (fresh.get("calibration_sha256") != self.calibration_id
                            or not isinstance(offset, (list, tuple)) or tuple(offset) != self.tag_offset):
                        raise SelectionError("Calibration changed. Restart with the intended calibration and scan again.")
                    frame = fresh.get("frame")
                    new_sequence = frame.get("sequence") if isinstance(frame, dict) else None
                    if type(new_sequence) is not int or new_sequence <= sequence:
                        raise SelectionError("Camera sequence did not advance or the gateway restarted. Scan again.")
                    sequence = new_sequence
                    context["sequence"] = sequence
                    if checking_place:
                        if rechecking:
                            if (slot is None or slot.get("state") != "clear"
                                    or slot.get("reason_code") != "clear" or not unobstructed(slot, clear=True)):
                                raise blocked_destination(slot)
                            if destination_identity(observation, slot) != expected_identity:
                                raise SelectionError("Placement geometry or table reference changed during recheck; scan again.")
                        elif slot is None or slot.get("state") != "clear":
                            if (slot is None or slot.get("state") != "unknown"
                                    or slot.get("reason_code") not in ("table_depth", "invalid_depth")
                                    or not unobstructed(slot)):
                                raise blocked_destination(slot)
                            scan_observation = self.inventory.get("placement_observation")
                            expected_identity = destination_identity(scan_observation, destination(scan_observation))
                            if destination_identity(observation, slot) != expected_identity:
                                raise SelectionError("Placement geometry or table reference changed since the scan; scan again.")
                            rechecking = True
                            audit["stationary_recheck"] = {"required_clear": 3, "clear_count": 0}
                    if not isinstance(fresh.get("cups"), list):
                        raise SelectionError("Malformed cup observations in fresh camera check; scan again.")
                    target = match_observation(original, fresh["cups"], self.match_mm, self.match_px,
                                               tag_offset=self.tag_offset)
                    jobs = plan(target)
                    if action == "goto":
                        before = {job["name"]: job["body"] for job in original_jobs}
                        after = {job["name"]: job["body"] for job in jobs}
                        yaw_change = abs(before["approach_cup"]["rz"] - after["approach_cup"]["rz"])
                        opening_change = abs(before["left_open"].get("position", 25)
                                             - after["left_open"].get("position", 25))
                        if yaw_change > 5 or opening_change > 2.5:
                            raise SelectionError("Cup grip geometry changed since the scan. Scan and review the grip again.")
                    audit.update(matched_cup_id=target["id"],
                                 robot_mm={axis: target[axis] for axis in ("x", "y", "z")},
                                 jobs=jobs, motion_settings=self.motion_settings(),
                                 grip_geometry={key: target.get(key) for key in
                                                ("diameter", "aperture", "rz", "has_handle")})
                    if slot is not None:
                        audit["placement_visibility"] = copy.deepcopy(slot)
                    if rechecking:
                        audit["stationary_recheck"]["clear_count"] = attempt
                        if attempt < 3:
                            self.save(self.inventory)
                            self._check_stop()
                            verification_age(started)
                            continue
                    # Named identity is checked only on the final accepted image.
                    identity = (context["identity_check"](fresh, target)
                                if context["identity_check"] is not None else None)
                    self._check_stop()
                    verification_age(started)
                    if identity is not None:
                        audit["named_identity"] = identity
                    audit["state"] = "verified"
                    return jobs, started
            except Exception as error:
                audit.update(state="failed", error=str(error))
                if rechecking:
                    self.save(self.inventory)
                raise

        jobs, started = verify()

        def check_age():
            verification_age(started)

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
        original["execution"] = {"action": action, "state": "pending", "completed_steps": [],
                                 "pending_jobs": {}, "pending_steps": [], "total_steps": len(jobs),
                                 "placement": copy.deepcopy(place)}
        execution = original["execution"]
        original["grip_left_mm"] = None
        original["grip_right_mm"] = None

        def pending_changed():
            execution["pending_steps"] = list(execution["pending_jobs"])
            if execution["pending_steps"]:
                # Retain the old single-step fields for saved-record readers.
                name = execution["pending_steps"][-1]
                execution.update(pending_step=name, pending_job=execution["pending_jobs"][name])
            else:
                execution.pop("pending_step", None)
                execution.pop("pending_job", None)

        def journal_execution():
            if self.recovery is not None:
                with self._state_lock:
                    self.recovery.begin({"capture": capture_id, "cup_id": cup_id, "action": action,
                                         "placement": copy.deepcopy(place),
                                         "batch": copy.deepcopy(self.batch_progress),
                                         "stop_requested": self.stop_requested,
                                         "execution": copy.deepcopy(execution)})

        def persist_execution():
            journal_execution()
            self.save(self.inventory)

        def before_job(index, job):
            self._check_stop()
            # Use the single pre-motion verification throughout this cup's pick.
            # Do not capture again at ready; the initial image must still meet
            # the freshness deadline before approach, descent and gripping.
            # After gripping, the observed cup is intentionally moved and may
            # be hidden by the arm. Its old camera age must not interrupt the
            # transfer while either gripper is holding it. Fixed handover steps
            # still require a live lease and confirmed completion of each job.
            if index == 0 or job["name"] in ("approach_cup", "left_open", "descend_to_cup", "grip_cup"):
                check_age()
            job = jobs[index]
            with self._state_lock:
                execution["state"] = "running"
                execution["pending_jobs"][job["name"]] = copy.deepcopy(job)
                pending_changed()
            persist_execution()
            # Recheck after potentially slow persistence work, just
            # before dispatch. A changed destination must stop this command.
            if context["reservation"] is not None:
                remaining = [p["id"] for p in context["placement"]["locations"][context["index"]:]]
                self.placing_grid.validate_reservation(context["reservation"], remaining)
            if index == 0 or job["name"] in ("approach_cup", "left_open", "descend_to_cup", "grip_cup"):
                check_age()
            print(f"Step {index + 1}/{len(jobs)}: {job['name'].replace('_', ' ')}", flush=True)
            self._check_stop()
            return job

        def after_job(index, job):
            self._check_stop()
            arm = {"grip_cup": "left", "right_grip": "right"}.get(job["name"])
            read_position = getattr(self.robot, "read_gripper_position", None)
            if arm is not None and callable(read_position):
                # Optional read-only telemetry never retries or changes a motion.
                try:
                    original[f"grip_{arm}_mm"] = read_position(arm)
                except Exception as error:
                    with self._state_lock:
                        execution.setdefault("telemetry_errors", []).append(f"{arm} gripper: {error}")
                self._check_stop()
            with self._state_lock:
                execution["completed_steps"].append(job)
                execution["last_completed_step"] = job["name"]
                execution["pending_jobs"].pop(job["name"], None)
                pending_changed()
            persist_execution()

        try:
            self.save(self.inventory)  # If persistence fails, do not dispatch motion.
            with self._state_lock:
                self._check_stop()
                journal_execution()
            self.motion_uncertain = True
            if place is not None and context["reservation"] is None:
                context["reservation"] = self.placing_grid.reserve(context["placement"])
            # Reservation I/O also counts toward the camera freshness deadline.
            check_age()
            self._check_stop()
            self.robot.execute_jobs(jobs, before_send=before_job, after_done=after_job)
            self._check_stop()
            if (execution["pending_jobs"] or len(execution["completed_steps"]) != len(jobs)
                    or {job["name"] for job in execution["completed_steps"]} != {job["name"] for job in jobs}):
                raise SelectionError("Not every robot step was confirmed. Inspect both arms before recovery.")
            if place is not None:
                self.placing_grid.complete(context["reservation"], place["id"])
            original["status"] = ("replaced" if self.left_only else "placed") if action == "goto" else "at_hover"
            execution["state"] = "completed"
            self.save(self.inventory)
            with self._state_lock:
                self._check_stop()
                self.motion_uncertain = False
        except BaseException as error:
            # A timeout/interrupt does not tell us whether the robot actually moved.
            self.motion_uncertain = True
            original["status"] = "movement_unconfirmed"
            with self._state_lock:
                execution.update(state="interrupted", error=str(error))
            try:
                journal_execution()
            except OSError as save_error:
                print(f"Could not save interrupted recovery state: {save_error}", flush=True)
            try:
                self.save(self.inventory)
            except OSError as save_error:
                # Keep the original failure, and keep the in-memory lockout.
                print(f"Could not save interrupted sequence: {save_error}", flush=True)
            raise
        return jobs

    def motion_settings(self):
        return {"speed": self.speed, "home_speed": self.home_speed,
                "left_only": self.left_only,
                "handover": handover_settings(self.handover_offset, self.speed)}

    def release_locations(self, *, fingerprint, acknowledged):
        """Release inspected placement records only, without robot or recovery actions."""
        if acknowledged is not True:
            raise SelectionError("Confirm all placement locations are empty and both arms are stopped first.")
        if not self._activity_lock.acquire(blocking=False):
            raise SelectionError("Wait for the active operation before releasing placement locations.")
        try:
            if self._stop_pending.is_set():
                raise SelectionError("Wait for the stop request to finish before releasing placement locations.")
            if self.placing_grid is None:
                raise SelectionError("No placement grid configured.")
            # Invalidate selection even if the write fails or the reviewed grid
            # has changed. A release never acknowledges robot recovery or Stop.
            self.scan_usable = False
            self.batch_progress = None
            snapshot = self.placing_grid.release_all(fingerprint)
            count = len(snapshot["locations"])
            return {"count": count,
                    "message": f"All {count} placement locations marked available. No robot command sent. "
                               "Scan again before picking; camera checks may mark locations occupied or blocked again. "
                               "Existing robot recovery and stop blocks are unchanged."}
        finally:
            self._activity_lock.release()

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
            self.speed, self.home_speed = motion_speeds(speed)
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
