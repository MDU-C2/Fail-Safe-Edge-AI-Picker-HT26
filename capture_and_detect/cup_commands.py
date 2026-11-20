"""Structured selection interface shared by the terminal and a future language UI."""

import copy
import json
import math
import threading
import time

from cup_inventory import GRIPPER_TCP_Z, SelectionError, hover_moves, selected_cup
from handover_plan import (handover_jobs, left_only_jobs, handover_settings, motion_speeds,
                           normalize_handover_offset, pick_position_problem,
                           normalize_phase_speeds, effective_phase_speeds, normalize_right_park_pose)


class WorkspaceCheckError(SelectionError):
    """Placement finished, but the source observation does not permit another pick."""


class CupSession:
    def __init__(self, capture, save, robot, calibration_id, *, max_result_age=15.0,
                 match_mm=20.0, match_px=40.0, tag_offset=(0, 0, 0), speed=None,
                 clock=time.monotonic, recovery=None, placing_grid=None, handover_offset=(0, 0, 0),
                 left_only=False, verification_frames=1, camera_z=False,
                 strict_single_verification=False, phase_speeds=None, home_after_cup=False,
                 between_cup_check=False, right_park_pose=None, between_cup_grid_check=False):
        if type(home_after_cup) is not bool:
            raise ValueError("home_after_cup must be a boolean")
        if type(left_only) is not bool:
            raise ValueError("left_only must be a boolean")
        if type(camera_z) is not bool:
            raise ValueError("camera_z must be a boolean")
        if type(between_cup_check) is not bool:
            raise ValueError("between_cup_check must be a boolean")
        if between_cup_check and left_only:
            raise ValueError("Between-cup workspace checks require the dual-arm workflow")
        if type(between_cup_grid_check) is not bool:
            raise ValueError("between_cup_grid_check must be a boolean")
        if between_cup_grid_check and (not between_cup_check or placing_grid is None):
            raise ValueError("Between-cup grid checks require workspace checks and a placement grid")
        self._right_park_pose = normalize_right_park_pose(right_park_pose)
        if self._right_park_pose is not None and (left_only or home_after_cup):
            raise ValueError("Right-arm parking requires the dual-arm workflow without automatic Home")
        self.between_cup_check = between_cup_check
        self.between_cup_grid_check = between_cup_grid_check
        self._workspace_destinations = []
        if type(verification_frames) is not int or verification_frames not in (1, 3):
            raise ValueError("verification_frames must be 1 or 3")
        if type(strict_single_verification) is not bool or (strict_single_verification and verification_frames != 1):
            raise ValueError("strict_single_verification requires a boolean and verification_frames=1")
        for name, value in (("max_result_age", max_result_age), ("match_mm", match_mm), ("match_px", match_px)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        self.capture = capture
        self.save = save
        self.robot = robot
        self.calibration_id = calibration_id
        # Observation age remains a compatibility option. Source matching uses
        # the tolerances only when the Mac UI enables between-cup checks.
        self.max_result_age = max_result_age
        self.match_mm = match_mm
        self.match_px = match_px
        self.clock = clock
        self.tag_offset = tuple(tag_offset)
        self.speed, self.home_speed = motion_speeds(speed)
        self.phase_speeds = normalize_phase_speeds(phase_speeds)
        self.handover_offset = normalize_handover_offset(handover_offset)
        self.inventory = None
        self.scan_usable = False
        self.recovery = recovery
        self.placing_grid = placing_grid
        self.left_only = left_only
        self._home_after_cup = home_after_cup
        self._camera_z = camera_z
        self.verification_frames = verification_frames
        self.strict_single_verification = strict_single_verification
        # Older launchers used these policies to require placement-camera
        # evidence. Preserve that requirement using the original scan only.
        self.require_placement_evidence = strict_single_verification or verification_frames == 3
        self.batch_progress = None
        self.motion_uncertain = recovery.required() if recovery is not None else False
        self._stop_event = threading.Event()
        self._finish_batch_event = threading.Event()
        self._stop_pending = threading.Event()
        self._stop_call_lock = threading.Lock()
        self._activity_lock = threading.Lock()
        self._state_lock = threading.RLock()

    @property
    def right_park_pose(self):
        """Operator-taught pose fixed for this session; callers receive a copy."""
        return copy.deepcopy(self._right_park_pose)

    @property
    def home_after_cup(self):
        """Fixed at session startup; cup continuation must not change mid-batch."""
        return self._home_after_cup

    @property
    def camera_z(self):
        """Pick-height policy is fixed when this session is constructed."""
        return self._camera_z

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

    def finish_batch_when_parked(self):
        """Let server shutdown finish a grid-refresh batch without new motion.

        An active transfer still completes normally. This is not a robot Stop;
        it only prevents subsequent cups or indefinite stationary camera waits.
        """
        if self.between_cup_grid_check:
            self._finish_batch_event.set()

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
        self._workspace_destinations = []
        inventory, _ = self.capture("scan")
        self.inventory = inventory
        self._check_stop()
        observation = inventory.get("placement_observation")
        if observation is not None and self.placing_grid is not None and "grid_snapshot" not in observation:
            raise SelectionError("Placement area could not be checked. Inspect the grid configuration and scan again.")
        if observation and "grid_snapshot" in observation and self.placing_grid is not None:
            if self.robot.dry_run:
                observation["message"] = "Placement check preview only; saved locations unchanged. Dry-run mode."
            else:
                # Scan owns the activity lock, so no sequence can still own these
                # reservations. Reconcile the grid even after a failed movement;
                # the independent recovery lock still prevents robot dispatch.
                with self._state_lock:
                    self._check_stop()
                    before = observation["grid_snapshot"]
                    states = {slot["id"]: slot["state"] for slot in observation["slots"]}
                    after = self.placing_grid.reconcile_observation(before, states)
                    old = {slot["id"]: slot for slot in before["locations"]}
                    current = {slot["id"]: slot for slot in after["locations"]}
                    for slot in observation["slots"]:
                        slot["reserved"] = current.get(slot["id"], {}).get("reserved", False)
                    observation["released_ids"] = [slot["id"] for slot in after["locations"]
                        if old.get(slot["id"], {}).get("occupied", False) and not slot["occupied"]]
                    observation["applied"] = True
                    free = sum(not slot["occupied"] and not slot.get("vision_blocked", False)
                               for slot in after["locations"])
                    views = "one fresh observation" if observation.get("frame_count") == 1 else "three fresh frames"
                    observation["message"] = (f"Placement grid checked using {views}: {free} available. "
                                              "Remove a cup and scan again to release its location.")
                    if self.motion_uncertain:
                        observation["message"] += " Robot recovery still needs acknowledgement before picking."
            self.save(inventory)
        self._check_stop()
        if self.between_cup_check and self.placing_grid is not None:
            # Source-only refresh keeps this whitelist. Full grid refresh
            # replaces it with cells positively observed clear in each capture.
            self._workspace_destinations = [copy.deepcopy(place)
                for place in self.placing_grid.snapshot()["locations"]
                if not place["occupied"] and not place.get("vision_blocked", False)]
        self.scan_usable = not self.stop_requested
        return inventory

    def _check_selection(self, capture_id):
        self._check_stop()
        if self.motion_uncertain:
            raise SelectionError("Previous movement was not confirmed. Check the robot, then use reset.")
        if not self.scan_usable:
            raise SelectionError("Run scan before selecting a cup.")
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
            left_only_jobs(cup, tag_offset=self.tag_offset, speed=self.speed,
                           home_speed=self.home_speed, camera_z=self.camera_z, phase_speeds=self.phase_speeds,
                           home_after_cup=self.home_after_cup)
            return {"cup_ids": [cup_id], "placement": None}
        if self.placing_grid is None:
            raise SelectionError("No placement grid configured. Restart with --placing-grid.")
        placement = self.placing_grid.preview(1)
        handover_jobs(cup, place=placement["locations"][0], tag_offset=self.tag_offset, speed=self.speed,
                      home_speed=self.home_speed, handover_offset=self.handover_offset,
                      camera_z=self.camera_z, phase_speeds=self.phase_speeds, home_after_cup=self.home_after_cup,
                      right_park_pose=self.right_park_pose)
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
                left_only_jobs(cup, tag_offset=self.tag_offset, speed=self.speed,
                               home_speed=self.home_speed, camera_z=self.camera_z, phase_speeds=self.phase_speeds,
                               home_after_cup=self.home_after_cup)
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
                          home_speed=self.home_speed, handover_offset=self.handover_offset,
                          camera_z=self.camera_z, phase_speeds=self.phase_speeds, home_after_cup=self.home_after_cup,
                          right_park_pose=self.right_park_pose)
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
                   "identity_check": identity_check, "completed_places": [], "at_home": False}
        dynamic = batch and self.between_cup_check and not self.robot.dry_run
        continuous = dynamic and self.between_cup_grid_check
        if placement is not None:
            self.placing_grid.validate_preview(placement)
        jobs = []
        self.batch_progress = ({"state": "running", "cup_ids": list(cup_ids), "completed_cups": [],
                                "current_cup_id": cup_ids[0], "total": len(cup_ids),
                                "pending_cups": list(cup_ids), "revision": 0} if batch else None)
        if batch:
            if continuous:
                self.batch_progress.update(continuous=True, arms_home=False, home_cycles=0)
            self.inventory["batch_execution"] = self.batch_progress
        succeeded = False
        completed_motion_cleared = False
        pending = list(cup_ids)
        index = 0
        try:
            while pending or continuous:
                self._check_stop()
                if dynamic and self._finish_batch_event.is_set():
                    self.batch_progress["completion_reason"] = "session_closed"
                    break
                if not pending:
                    # Full or temporarily unpickable scenes keep their current
                    # clearance poses. Only a freshly observed EMPTY source
                    # workspace permits Home, once until another transfer moves
                    # the arms. The placement grid may still contain cups.
                    reason = self.batch_progress.get("completion_reason")
                    if reason == "workspace_empty" and not context["at_home"]:
                        home_jobs = self._home_when_workspace_empty()
                        if home_jobs is None:
                            self.batch_progress["completion_reason"] = "session_closed"
                            break
                        jobs.extend(home_jobs)
                        context["at_home"] = True
                        completed_motion_cleared = True
                    self.batch_progress.update(
                        state="waiting_for_grid" if reason == "grid_full" else "waiting_for_cups",
                        current_cup_id=None, arms_home=context["at_home"])
                    self.save(self.inventory)
                    if self._finish_batch_event.is_set():
                        self.batch_progress["completion_reason"] = "session_closed"
                        break
                    self._wait_for_grid()
                    if self._finish_batch_event.is_set():
                        self.batch_progress["completion_reason"] = "session_closed"
                        break
                    self._check_workspace_after_cup(context)
                    completed_motion_cleared = True
                    pending = list(self.batch_progress["pending_cups"])
                    continue
                cup_id = pending.pop(0)
                self._check_stop()
                context["index"] = 0 if dynamic and index else index
                if batch:
                    self.batch_progress["state"] = "running"
                    self.batch_progress["current_cup_id"] = cup_id
                    if continuous:
                        context["at_home"] = False
                        self.batch_progress["arms_home"] = False
                # A live Mac batch follows the latest source observation. Each
                # individual transfer still completes its full accepted plan.
                self.scan_usable = True
                completed_jobs = self._execute_one(action=action, capture_id=capture_id,
                                                   cup_id=cup_id, context=context)
                jobs.extend(completed_jobs)
                completed_motion_cleared = False
                if batch:
                    self.batch_progress["completed_cups"].append(cup_id)
                    self.batch_progress["pending_cups"] = list(pending)
                    if dynamic:
                        place = context["placement"]["locations"][context["index"]]
                        context["completed_places"].append(place["id"])
                    self.save(self.inventory)
                if self.between_cup_check and action == "goto" and not self.robot.dry_run:
                    self._check_workspace_after_cup(context if dynamic else None)
                    completed_motion_cleared = True
                    if dynamic:
                        pending = list(self.batch_progress["pending_cups"])
                index += 1
            if batch:
                self.batch_progress["state"] = "previewed" if self.robot.dry_run else "completed"
                self.save(self.inventory)
            if completed_motion_cleared:
                self._check_stop()
            else:
                self._finish_confirmed_motion()
            succeeded = True
            return jobs
        except BaseException as error:
            if self.stop_requested or (not isinstance(error, WorkspaceCheckError)
                    and not completed_motion_cleared
                    and not self.robot.dry_run and (jobs or context["reservation"] is not None)):
                self.motion_uncertain = True
            if batch:
                self.batch_progress.update(state="interrupted", error=str(error))
                try:
                    self.save(self.inventory)
                except OSError:
                    pass
            raise
        finally:
            self.scan_usable = succeeded and not self.motion_uncertain and not self.stop_requested

    def _finish_confirmed_motion(self):
        """Clear completed motion recovery without losing a concurrent Stop."""
        with self._state_lock:
            self._check_stop()
            if not self.robot.dry_run and self.recovery is not None:
                self.recovery.clear()
            if self.stop_requested and self.recovery is not None and not self.robot.dry_run:
                self.recovery.begin({"action": "software_stop", "stopped": False,
                                     "batch": copy.deepcopy(self.batch_progress)})
            self._check_stop()
            self.motion_uncertain = False

    def _home_when_workspace_empty(self):
        """Home once between continuous batches, using the existing Home jobs.

        The caller holds the activity lock and has finished every cup job and
        the fresh empty-workspace capture. Keep this separate from operator
        controls, which clear batch state and acquire that lock themselves.
        """
        from robot_controls import control_plan
        class FinishBeforeHome(Exception):
            pass

        self._check_stop()
        jobs = control_plan("home", "both", self.speed)["jobs"]
        for job in jobs:
            job["name"] = f"workspace_{job['body']['arm']}_home"
        execution = {"state": "pending", "completed_steps": [],
                     "pending_job": None, "total_steps": len(jobs)}
        self.batch_progress.update(state="homing_when_empty", current_cup_id=None,
                                   idle_home=execution)
        self.scan_usable = False
        dispatched = False

        def check_finish():
            self._check_stop()
            if self._finish_batch_event.is_set() and not dispatched:
                raise FinishBeforeHome()

        def persist():
            if self.recovery is not None:
                with self._state_lock:
                    self.recovery.begin({"action": "workspace_empty_home",
                                         "batch": copy.deepcopy(self.batch_progress),
                                         "execution": copy.deepcopy(execution),
                                         "stop_requested": self.stop_requested})
            self.save(self.inventory)

        def before_job(_index, job):
            nonlocal dispatched
            check_finish()
            execution.update(state="running", pending_job=copy.deepcopy(job))
            persist()
            check_finish()
            dispatched = True
            return job

        def after_job(_index, job):
            self._check_stop()
            execution["completed_steps"].append(copy.deepcopy(job))
            execution["pending_job"] = None
            persist()

        armed = False
        try:
            self.save(self.inventory)
            check_finish()
            with self._state_lock:
                check_finish()
                self.motion_uncertain = True
                armed = True
                persist()
            check_finish()
            self.robot.execute_jobs(jobs, before_send=before_job, after_done=after_job)
            self._check_stop()
            if (execution["pending_job"] is not None
                    or [job["name"] for job in execution["completed_steps"]]
                    != [job["name"] for job in jobs]):
                raise SelectionError("Home did not confirm both arms. Inspect the robot before recovery.")
            execution["state"] = "completed"
            self.batch_progress["home_cycles"] += 1
            self.batch_progress["arms_home"] = True
            self.save(self.inventory)
            self._finish_confirmed_motion()
        except FinishBeforeHome:
            execution.update(state="cancelled", pending_job=None)
            # No Home command reached dispatch. Previous cup movement was
            # already confirmed, so discard only the unused Home journal.
            self._finish_confirmed_motion()
            return None
        except BaseException as error:
            if armed or self.stop_requested:
                self.motion_uncertain = True
                execution.update(state="interrupted", error=str(error))
                try:
                    persist()
                except OSError:
                    pass
            raise
        return jobs

    def _refresh_batch(self, context):
        """Rebuild remaining work from current sources and observed clear slots."""
        candidates = [cup for cup in self.inventory["cups"] if cup.get("selectable")
                      and not cup.get("blocked_reason") and not pick_position_problem(cup, self.tag_offset)]
        candidates.sort(key=lambda cup: (-(cup.get("confidence") or 0), cup["id"]))
        self._check_stop()
        if self.between_cup_grid_check:
            # The new observation supersedes all old occupancy/reservations.
            # Completed placement history belongs to the cups, not permanently
            # to a cell: an observed-empty cell may be reused in this batch.
            snapshot = self.placing_grid.snapshot()
            observed = {place["id"]: place for place in self._workspace_destinations}
            free = [place for place in snapshot["locations"]
                    if place["id"] in observed and not place["occupied"]
                    and not place.get("reserved", False) and not place.get("vision_blocked", False)
                    and all(place[axis] == observed[place["id"]][axis] for axis in ("x", "y", "z"))]
            replanned = {"placement": {"fingerprint": snapshot["fingerprint"],
                                       "locations": free[:len(candidates)]}, "reservation": None}
            context["completed_places"] = []
        else:
            replanned = self.placing_grid.replan(context["reservation"], self._workspace_destinations,
                                                len(candidates), completed_ids=context["completed_places"])
        context.update(placement=replanned["placement"], reservation=replanned["reservation"], index=0)
        chosen = candidates[:len(context["placement"]["locations"])]
        pending_ids = [cup["id"] for cup in chosen]
        waiting_ids = ([cup["id"] for cup in candidates]
                       if self.between_cup_grid_check and candidates and not chosen else [])
        completed_ids = self.batch_progress["completed_cups"]
        self.batch_progress.update(cup_ids=list(completed_ids) + pending_ids + waiting_ids,
                                   pending_cups=pending_ids, waiting_cup_ids=waiting_ids,
                                   total=len(completed_ids) + len(pending_ids) + len(waiting_ids),
                                   excluded_cup_ids=[cup["id"] for cup in candidates[len(chosen):]],
                                   revision=self.batch_progress.get("revision", 0) + 1)
        planned = {cup["id"]: cup.get("execution", {}).get("total_steps", 0)
                   for cup in self.inventory["cups"] if cup["id"] in completed_ids}
        for cup, place in zip(chosen, context["placement"]["locations"]):
            planned[cup["id"]] = len(handover_jobs(cup, place=place, tag_offset=self.tag_offset,
                speed=self.speed, home_speed=self.home_speed, handover_offset=self.handover_offset,
                camera_z=self.camera_z, phase_speeds=self.phase_speeds, home_after_cup=self.home_after_cup,
                right_park_pose=self.right_park_pose))
        self.batch_progress["planned_steps"] = planned
        if not pending_ids:
            sources = [cup for cup in self.inventory["cups"]
                       if cup.get("status") not in ("placed", "replaced")]
            # A visible cup without a detected base is not pickable, but it
            # still means the workspace is occupied. Capture excludes grid
            # cups before reporting this independent presence count.
            source_presence = (self.inventory.get("source_presence", {}).get("cup_detections", 0)
                               if self.between_cup_grid_check else 0)
            has_sources = bool(sources) or source_presence > 0
            self.batch_progress["completion_reason"] = (
                "grid_full" if candidates else "no_pickable_cups" if has_sources else "workspace_empty")
            if self.between_cup_grid_check:
                state = ("waiting_for_grid" if candidates else
                         "homing_when_empty" if not has_sources and not context["at_home"] else
                         "waiting_for_cups")
                self.batch_progress.update(state=state, current_cup_id=None)
        else:
            self.batch_progress.pop("completion_reason", None)
        self._check_stop()

    def _wait_for_grid(self):
        """Wait between stationary camera checks, interruptibly by Stop."""
        self._check_stop()
        self._stop_event.wait(1.0)
        self._check_stop()

    def _refresh_grid_after_cup(self, fresh):
        """Apply this stationary camera view before planning another placement."""
        observation = copy.deepcopy(fresh.get("placement_observation"))
        if (not isinstance(observation, dict) or "grid_snapshot" not in observation
                or not isinstance(observation.get("slots"), list)):
            raise SelectionError("Placement area could not be observed. Run scan again.")
        slots = observation["slots"]
        if any(not isinstance(slot, dict) or "id" not in slot or "state" not in slot for slot in slots):
            raise SelectionError("Placement observation is incomplete. Run scan again.")
        states = {slot["id"]: slot["state"] for slot in slots}
        if len(states) != len(slots):
            raise SelectionError("Placement observation repeats a location. Run scan again.")
        with self._state_lock:
            self._check_stop()
            before = observation["grid_snapshot"]
            after = self.placing_grid.reconcile_observation(before, states)
            old = {place["id"]: place for place in before["locations"]}
            current = {place["id"]: place for place in after["locations"]}
            for slot in slots:
                slot["reserved"] = current.get(slot["id"], {}).get("reserved", False)
            self._workspace_destinations = [copy.deepcopy(place) for place in after["locations"]
                if states.get(place["id"]) == "clear" and not place["occupied"]
                and not place.get("vision_blocked", False)
                and all(place[axis] == old[place["id"]][axis] for axis in ("x", "y", "z"))]
            observation.update(applied=True, capture=fresh["capture"],
                released_ids=[place["id"] for place in after["locations"]
                              if old.get(place["id"], {}).get("occupied", False) and not place["occupied"]],
                message=(f"Placement grid refreshed from the latest photo: {len(self._workspace_destinations)} available. "
                         "Removed cups release their cells; occupied or unclear cells are not used."))
            self.inventory["placement_observation"] = observation

    def _check_workspace_after_cup(self, batch_context=None):
        """Observe sources after ALL existing jobs finish, including left parking.

        This sends no robot command. When grid refresh is enabled, the same
        capture also updates placement occupancy before rebuilding the batch.
        Source geometry comes from the fresh image. A batch may rebuild its
        remaining queue, while a selected/named pick remains exactly one cup.
        """
        from workspace_check import refresh_workspace
        previous = copy.deepcopy(self.inventory)
        previous["capture"] = previous.get("image_capture", previous["capture"])
        audit = {"state": "capturing", "scope": "workspace_and_grid" if self.between_cup_grid_check else "source_workspace", "grid_checked": False,
                 "message": "Placement completed. Refreshing the workspace cups…"}
        self.inventory["workspace_check"] = audit
        self.scan_usable = False
        motion_cleared = False
        try:
            # A changed scene is not an unconfirmed movement. The execution
            # barrier and completion bookkeeping have already finished.
            self._finish_confirmed_motion()
            motion_cleared = True
            self.save(self.inventory)
            fresh, _ = self.capture("between_cups_grid" if self.between_cup_grid_check else "between_cups")
            self._check_stop()
            result = refresh_workspace(previous, fresh, calibration_id=self.calibration_id,
                                       tag_offset=self.tag_offset, match_mm=self.match_mm, match_px=self.match_px)
            if self.between_cup_grid_check:
                self._refresh_grid_after_cup(fresh)
                audit["grid_checked"] = True
            self.inventory["cups"] = result["cups"]
            self.inventory["next_cup_id"] = result["next_cup_id"]
            self.inventory["source_presence"] = copy.deepcopy(fresh.get("source_presence", {}))
            self.inventory["frame"] = copy.deepcopy(fresh["frame"])
            self.inventory["images"] = copy.deepcopy(fresh.get("images", {}))
            if self.inventory["images"].get("rgb"):
                # The UI draws the stable selection IDs on the fresh raw photo.
                self.inventory["images"]["annotated"] = self.inventory["images"]["rgb"]
            self.inventory["image_capture"] = fresh["capture"]
            self.inventory["saved_at"] = fresh.get("saved_at", self.inventory.get("saved_at"))
            audit.update({key: value for key, value in result.items() if key != "cups"},
                         message=("Workspace and placement grid refreshed from the latest photo."
                                  if self.between_cup_grid_check else
                                  "Workspace refreshed from the latest photo. Placement grid not checked."))
            if batch_context is not None:
                self._refresh_batch(batch_context)
            self._check_stop()
            self.save(self.inventory)
        except BaseException as error:
            audit.update(state="failed", error=str(error),
                         message="Placement completed. Workspace check did not pass; run a new scan before continuing.")
            try:
                self.save(self.inventory)
            except OSError:
                pass
            if self.stop_requested or not isinstance(error, Exception) or not motion_cleared:
                self.motion_uncertain = True
                if self.recovery is not None:
                    try:
                        self.recovery.begin({"action": "software_stop" if self.stop_requested else "workspace_check_interrupted",
                                             "stopped": False, "stop_requested": self.stop_requested,
                                             "batch": copy.deepcopy(self.batch_progress),
                                             "workspace_check": copy.deepcopy(audit)})
                    except OSError as save_error:
                        print(f"Could not save interrupted workspace check: {save_error}", flush=True)
                raise
            raise WorkspaceCheckError(audit["message"] + " " + str(error)) from error

    def _execute_one(self, *, action, capture_id, cup_id, context):
        """One cup's checked sequence inside a single or confirmed batch request."""
        self._check_stop()
        if action not in ("goto", "hover"):
            raise SelectionError("Supported actions: goto (pick, hand over, place and clear) or hover.")
        if self.motion_uncertain:
            raise SelectionError("Previous movement was not confirmed. Check the robot, then use reset.")
        if not self.scan_usable:
            raise SelectionError("Run scan before selecting a cup.")
        original = selected_cup(self.inventory, capture_id, cup_id)
        self.scan_usable = False  # The active action owns the snapshot until completion.
        place = (context["placement"]["locations"][context["index"]]
                 if action == "goto" and not self.left_only else None)

        def plan(target):
            if action == "goto":
                if self.left_only:
                    return left_only_jobs(target, tag_offset=self.tag_offset, speed=self.speed,
                                          home_speed=self.home_speed, camera_z=self.camera_z,
                                          phase_speeds=self.phase_speeds, home_after_cup=self.home_after_cup)
                return handover_jobs(target, place=place, tag_offset=self.tag_offset, speed=self.speed,
                                     home_speed=self.home_speed, handover_offset=self.handover_offset,
                                     camera_z=self.camera_z, phase_speeds=self.phase_speeds,
                                     home_after_cup=self.home_after_cup, right_park_pose=self.right_park_pose)
            return [{"name": name, "endpoint": "/robot/rws/move", "body": body}
                    for name, body in zip(("left_ready", "approach_cup"),
                                          hover_moves(target, tag_offset=self.tag_offset, speed=self.speed))]

        def destination(observation):
            slots = observation.get("slots") if isinstance(observation, dict) else None
            matches = ([slot for slot in slots if isinstance(slot, dict)
                        and type(slot.get("id")) is int and slot["id"] == place["id"]]
                       if isinstance(slots, list) else [])
            return matches[0] if len(matches) == 1 else None

        def finite_number(value):
            return type(value) in (int, float) and math.isfinite(value)

        def destination_identity(observation, slot):
            # Saved evidence belongs to the scanned destination geometry.
            # Ignore occupancy changes from this batch, but never apply that
            # clear polygon to a newly edited location.
            snapshot = observation.get("grid_snapshot", {}) if isinstance(observation, dict) else {}
            locations = snapshot.get("locations", []) if isinstance(snapshot, dict) else []
            scanned = [item for item in locations if isinstance(item, dict) and item.get("id") == place["id"]]
            if len(scanned) != 1 or any(scanned[0].get(axis) != place[axis] for axis in ("x", "y", "z")):
                raise SelectionError("Placement geometry changed since the scan; scan again.")
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

        def validate_visual_schema(slot):
            """Separate a valid but unusable view from malformed evidence."""
            evidence = slot.get("evidence") if isinstance(slot, dict) else None
            assigned = slot.get("assigned_cups") if isinstance(slot, dict) else None
            if (slot is None or slot.get("state") not in ("clear", "occupied", "unknown")
                    or not isinstance(slot.get("reason"), str) or not slot["reason"]
                    or slot.get("reason_code") not in ("clear", "cup", "detected_object", "raised_depth", "occluded",
                                                       "invalid_depth", "table_depth", "outside_view", "insufficient_projection", "inconsistent_frames")
                    or type(slot.get("cup_count")) is not int or slot["cup_count"] < 0
                    or not isinstance(assigned, list) or any(not isinstance(cup, dict) for cup in assigned)
                    or slot["cup_count"] != len(assigned)
                    or not isinstance(evidence, list) or any(item not in ("cup", "detected_object", "raised_depth") for item in evidence)
                    or len(evidence) != len(set(evidence))
                    or (slot["cup_count"] > 0) != ("cup" in evidence)
                    or (slot["state"] == "occupied") != bool(evidence)
                    or (slot["state"] == "occupied" and slot["reason_code"] !=
                        ("cup" if "cup" in evidence else "detected_object" if "detected_object" in evidence else "raised_depth"))
                    or (slot["state"] == "unknown" and slot["reason_code"] in ("clear", "cup", "detected_object", "raised_depth"))
                    or any(not finite_number(slot.get(key)) or not 0 <= slot[key] <= 1
                           for key in ("valid_fraction", "table_fraction", "visible_fraction", "floor_visible_fraction"))
                    or (slot["state"] == "clear" and (slot["reason_code"] != "clear" or not unobstructed(slot, clear=True)))
                    ):
                raise SelectionError("Malformed placement evidence in the saved scan; scan again.")
        def blocked_destination(slot):
            state = slot.get("state", "unknown") if slot is not None else "unknown"
            reason = slot.get("reason") if slot is not None else None
            detail = reason if isinstance(reason, str) and reason else "Destination evidence is missing or ambiguous."
            return SelectionError(f"Placement location {place['id']} is {state} in the saved scan: "
                                  f"{detail} Inspect the destination and scan again.")

        # Mac grid-refresh mode uses the latest stationary observation for
        # destinations too. Other workflows retain their original scan evidence.
        audit = {"action": action, "source": "saved_scan", "state": "checking",
                 "capture": self.inventory["capture"],
                 "frame": copy.deepcopy(self.inventory.get("frame", {})),
                 "placement": copy.deepcopy(place), "placement_checks": []}
        workspace = self.inventory.get("workspace_check", {})
        if self.between_cup_check and workspace.get("state") in ("unchanged", "refreshed"):
            audit.update(source="workspace_check", capture=workspace["capture"],
                         frame=copy.deepcopy(workspace["frame"]))
        original["last_verification"] = audit  # Existing UI/audit schema.
        try:
            observation = self.inventory.get("placement_observation")
            if place is not None and (observation is not None or self.require_placement_evidence):
                slot = destination(observation)
                destination_identity(observation, slot)
                validate_visual_schema(slot)
                audit["placement_checks"].append({"capture": observation.get("capture", self.inventory["capture"]),
                    "state": slot["state"], "reason": slot["reason"],
                    "reason_code": slot["reason_code"], "source": "workspace_check" if self.between_cup_grid_check and observation.get("capture") else "saved_scan"})
                if slot["state"] != "clear" or not unobstructed(slot, clear=True):
                    raise blocked_destination(slot)
                audit["placement_visibility"] = copy.deepcopy(slot)
            jobs = plan(original)
            bodies = {job["name"]: job["body"] for job in jobs}
            pick_target = None
            if action == "goto":
                # Dual-arm descent is the final waypoint of one buffered
                # approach; the left-only workflow still uses a separate move.
                pick_target = (bodies["descend_to_cup"] if self.left_only else
                               bodies["approach_cup"]["points"][-1])
            identity = (context["identity_check"](self.inventory, original)
                        if context["identity_check"] is not None else None)
            audit.update(state="verified", matched_cup_id=original["id"],
                         robot_mm={axis: original[axis] for axis in ("x", "y", "z")},
                         jobs=copy.deepcopy(jobs), motion_settings=self.motion_settings(),
                         grip_geometry={**{key: original.get(key) for key in
                                             ("diameter", "aperture", "rz", "has_handle")},
                                        "pick_tcp_z_mm": (pick_target["z"] - GRIPPER_TCP_Z
                                                            if pick_target is not None else None)})
            if identity is not None:
                audit["named_identity"] = identity
            self._check_stop()
        except Exception as error:
            audit.update(state="failed", error=str(error))
            self.save(self.inventory)
            raise

        if self.robot.dry_run:
            self.robot.execute_jobs(jobs)
            self._check_stop()
            original["last_action"] = f"dry-run {action}; no robot command sent"
            self.save(self.inventory)
            self._check_stop()
            self.scan_usable = not self.stop_requested
            return jobs

        original["status"] = "command_pending"
        original["execution"] = {"action": action, "state": "pending", "completed_steps": [],
                                 "pending_jobs": {}, "pending_steps": [], "total_steps": len(jobs),
                                 "home_after_cup": self.home_after_cup,
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
            job = jobs[index]
            with self._state_lock:
                execution["state"] = "running"
                execution["pending_jobs"][job["name"]] = copy.deepcopy(job)
                pending_changed()
            persist_execution()
            # Execute the accepted plan. Grid bookkeeping changes do not
            # interrupt an active handover or request additional validation.
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
            self._check_stop()
            self.robot.execute_jobs(jobs, before_send=before_job, after_done=after_job)
            self._check_stop()
            if (execution["pending_jobs"] or len(execution["completed_steps"]) != len(jobs)
                    or {job["name"] for job in execution["completed_steps"]} != {job["name"] for job in jobs}):
                raise SelectionError("Not every robot step was confirmed. Inspect both arms before recovery.")
            # This means all planned jobs completed. The default smooth
            # choreography ends at clearance poses; it must never claim Home.
            if place is not None:
                self.placing_grid.complete(context["reservation"], place["id"])
            original["status"] = ("replaced" if self.left_only else "placed") if action == "goto" else "at_hover"
            if action == "goto":
                original.update(selectable=False, blocked_reason="Cup was already processed from this scan.")
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
        phases = effective_phase_speeds(self.speed, self.phase_speeds)
        return {"speed": self.speed, "home_speed": self.home_speed,
                "left_only": self.left_only, "camera_z": self.camera_z,
                "home_after_cup": self.home_after_cup,
                "handover_motion": "blended_path" if not self.left_only else "single_arm",
                "handover_plan": "smooth_dual_arm_v1" if not self.left_only else "single_arm",
                "workflow": "workspace_between_cups" if self.between_cup_check else "v4_snapshot",
                "between_cup_check": self.between_cup_check,
                "dynamic_batch": self.between_cup_check,
                "right_park_pose": self.right_park_pose,
                "between_cup_grid_check": self.between_cup_grid_check,
                "continuous_batch": self.between_cup_grid_check,
                "phase_speeds": normalize_phase_speeds(self.phase_speeds),
                "effective_phase_speeds": phases,
                "handover": handover_settings(self.handover_offset, phases["handover"])}

    def set_phase_speeds(self, settings):
        """Change only future plans under the same admission lock as execution."""
        settings = normalize_phase_speeds(settings)
        if not self._activity_lock.acquire(blocking=False):
            raise SelectionError("Wait for the active operation before changing movement speeds.")
        try:
            self._check_stop()
            if self.motion_uncertain:
                raise SelectionError("Inspect and acknowledge recovery before changing movement speeds.")
            self.phase_speeds = settings
            self.batch_progress = None
            return {"message": "Future movement speeds updated. Review a new pick preview before confirming."}
        finally:
            self._activity_lock.release()

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
        """The V4 workflow uses fixed receiving coordinates."""
        raise SelectionError("Receiving-hand adjustment is unavailable in the V4 workflow.")

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
        is inferred from an operator control action.
        """
        from robot_controls import control_plan
        plan = control_plan(action, arm, self.speed)
        supported = getattr(self.robot, "require_control_action", None)
        if callable(supported):
            supported(action)
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
            self.robot.execute_jobs(plan["jobs"], before_send=before_job)
            result = {"message": ("Preview only; no robot command sent." if self.robot.dry_run else
                                  "Robot reported the control action complete. Scan again before picking.")}
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
