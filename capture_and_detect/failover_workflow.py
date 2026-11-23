"""Explicitly started picking sessions shared by Mac, Pi and Lynx.

The gateway owns each complete cup queue. This client owns only the decision to
submit the next cup. Coordination receipts survive a successor replacing the
gateway's single latest queue; reconnecting clients always take a fresh scan.
"""

import copy
import hashlib
import json
from pathlib import Path
import threading
import uuid

import requests

from camera_client import TransientCameraError
from cup_inventory import SelectionError, atomic_json
from failover_coordinator import (CoordinatorConflict, CoordinatorError, CoordinatorUnavailable,
                                  RECOVERY_CAPABILITY, DEVICE_SPEED_CAPABILITY,
                                  validate_device_speeds, validate_recovery)
from queued_robot_client import ControlUnavailable, QueueNotSubmitted, QueueWaitCancelled
from robot_client import GatewayControlUnavailable, GatewayReadUnavailable, RobotCommandError


PRIORITIES = {"primary": 300, "secondary": 200, "tertiary": 100}
ACTIVE = {"active", "finalizing", "watching"}


def transient(error):
    """Transport/ownership loss may yield; robot failures must remain blocked."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (CoordinatorUnavailable, ControlUnavailable, TransientCameraError,
                              GatewayReadUnavailable, GatewayControlUnavailable,
                              requests.ConnectionError, requests.Timeout)):
            return True
        if isinstance(error, QueueNotSubmitted) and error.control_unavailable:
            return True
        error = error.__cause__
    return False


class FailoverRunner:
    def __init__(self, session, coordinator, *, role, state_dir, output=print):
        if role not in PRIORITIES:
            raise ValueError("Unknown failover role.")
        if (session.robot.dry_run or session.left_only or session.home_after_cup
                or not session.between_cup_grid_check):
            raise ValueError("Automatic failover requires the dual-arm live grid-refresh workflow.")
        self.session, self.robot, self.coordinator = session, session.robot, coordinator
        self.role, self.output = role, output
        self._configured_continuous = session._continuous_batch
        self.pending_path = Path(state_dir) / "failover-pending.json"
        self.supports_device_speeds = role == "primary"
        self.device_speeds_path = Path(state_dir) / "device-speeds.json"
        self._device_speeds = None
        self._restore_speeds = None
        if self.device_speeds_path.exists() or self.device_speeds_path.is_symlink():
            try:
                if self.device_speeds_path.is_symlink() or not self.device_speeds_path.is_file():
                    raise ValueError("Invalid settings file.")
                with self.device_speeds_path.open("rb") as stream:
                    raw = stream.read(4097)
                if len(raw) > 4096:
                    raise ValueError("Settings file too large.")
                saved = json.loads(raw)
                if (not isinstance(saved, dict) or set(saved) != {"schema_version", "device_speeds"}
                        or type(saved["schema_version"]) is not int or saved["schema_version"] != 1):
                    raise ValueError("Invalid settings file.")
                self._device_speeds = validate_device_speeds(saved["device_speeds"])
            except (OSError, ValueError, TypeError, CoordinatorError) as error:
                raise SelectionError("Saved device speeds are invalid or unavailable; review device-speeds.json.") from error
            session.speed = self._device_speeds[role]
            session.home_speed = min(session.speed, 200.0)
        self.executing = False
        self.run_id = None
        self._closed = threading.Event()
        self._monitor = None
        self._execution = threading.Lock()
        session.failover = self
        original_capture = session.capture

        def capture(purpose):
            if self.executing:
                self.boundary()
            result = original_capture(purpose)
            if self.executing:
                self.boundary()
            return result
        session.capture = capture

    def device_speed_settings(self):
        """Configured next-batch preferences, independent of transferred runs."""
        if self._device_speeds is not None:
            return dict(self._device_speeds)
        speed = self._restore_speeds[0] if self._restore_speeds else self.session.speed
        return {role: float(speed) for role in PRIORITIES}

    def set_device_speeds(self, mapping):
        """Persist future per-device limits only, never alter admitted work."""
        if self.role != "primary":
            raise SelectionError("Only the primary UI can change device speeds.")
        try:
            speeds = validate_device_speeds(mapping)
        except CoordinatorError as error:
            raise SelectionError(str(error)) from error
        if not self._execution.acquire(blocking=False):
            raise SelectionError("Wait for the shared batch before changing device speeds.")
        activity_acquired = False
        try:
            activity_acquired = self.session._activity_lock.acquire(blocking=False)
            if not activity_acquired or self.executing or self._closed.is_set():
                raise SelectionError("Wait for the active operation before changing device speeds.")
            self.session._check_stop()
            if (self.session.motion_uncertain
                    or self.session.recovery is not None and self.session.recovery.required()):
                raise SelectionError("Inspect and acknowledge recovery before changing device speeds.")
            pending = self.robot._read_pending(self.pending_path)
            if pending is not None and not pending["reconciled"]:
                # Normal completion saves every callback but retains this
                # receipt until the next batch reconciles it. Future speed
                # preferences need no receipt or placement bookkeeping edits.
                completed = pending["state"] == "done" and pending["callbacks_applied"]
                if completed:
                    queue = self.robot._validate_queue_status(pending.get("queue"))
                    completed = (queue["state"] == "done"
                        and queue.get("owner") == pending["expected_owner"]
                        and queue.get("started_at") == pending.get("started_at")
                        and self.robot._signature(queue["steps"]) ==
                            self.robot._signature(pending["steps"], submitted=True))
                if not completed:
                    raise SelectionError("Resolve the previous queue receipt before changing device speeds.")
            state = self.coordinator.state()
            if not isinstance(state, dict) or "run" not in state or "head" not in state:
                raise SelectionError("Shared batch status is unavailable; device speeds are unchanged.")
            run, head = state["run"], state["head"]
            if run is not None and (not isinstance(run, dict) or run.get("phase") not in {"complete", "stopped", "blocked"}):
                raise SelectionError("Stop or finish the shared batch before changing device speeds.")
            if head is not None and (not isinstance(head, dict) or head.get("state") not in {"done", "not_submitted"}):
                raise SelectionError("Shared queue recovery is required before changing device speeds.")
            if DEVICE_SPEED_CAPABILITY not in state.get("capabilities", []):
                raise SelectionError("The shared controller needs the device-speed update before applying speeds.")
            self.session._check_stop()
            try:
                atomic_json(self.device_speeds_path, {"schema_version": 1, "device_speeds": speeds})
            except OSError as error:
                raise SelectionError("Could not save device speeds; previous speeds are unchanged.") from error
            self._device_speeds = speeds
            self.session.speed, self.session.home_speed = speeds["primary"], min(speeds["primary"], 200.0)
            self.session.batch_progress = None
            return {"message": "Device speeds saved for the next batch. Review a new preview before confirming.",
                    "device_speeds": dict(speeds)}
        finally:
            if activity_acquired:
                self.session._activity_lock.release()
            self._execution.release()

    def set_pick_height(self, camera_z):
        """Change the next batch's height policy without altering admitted work."""
        if type(camera_z) is not bool:
            raise SelectionError("camera_z must be a boolean")
        if self.role != "primary":
            raise SelectionError("Only the primary UI can change pickup height.")
        if not self._execution.acquire(blocking=False):
            raise SelectionError("Wait for the shared batch before changing pickup height.")
        activity_acquired = False
        try:
            activity_acquired = self.session._activity_lock.acquire(blocking=False)
            if not activity_acquired or self.executing or self._closed.is_set():
                raise SelectionError("Wait for the active operation before changing pickup height.")
            self.session._check_stop()
            if (self.session.motion_uncertain
                    or self.session.recovery is not None and self.session.recovery.required()):
                raise SelectionError("Inspect and acknowledge recovery before changing pickup height.")
            pending = self.robot._read_pending(self.pending_path)
            if pending is not None and not pending["reconciled"]:
                # Match speed preferences: a proved completed local receipt is
                # historical evidence, and changing future settings must leave
                # both that receipt and its placement callbacks untouched.
                completed = pending["state"] == "done" and pending["callbacks_applied"]
                if completed:
                    queue = self.robot._validate_queue_status(pending.get("queue"))
                    completed = (queue["state"] == "done"
                        and queue.get("owner") == pending["expected_owner"]
                        and queue.get("started_at") == pending.get("started_at")
                        and self.robot._signature(queue["steps"]) ==
                            self.robot._signature(pending["steps"], submitted=True))
                if not completed:
                    raise SelectionError("Resolve the previous queue receipt before changing pickup height.")
            state = self.coordinator.state()
            if not isinstance(state, dict) or "run" not in state or "head" not in state:
                raise SelectionError("Shared batch status is unavailable; pickup height is unchanged.")
            run, head = state["run"], state["head"]
            if run is not None and (not isinstance(run, dict) or run.get("phase") not in {"complete", "stopped", "blocked"}):
                raise SelectionError("Stop or finish the shared batch before changing pickup height.")
            if head is not None and (not isinstance(head, dict) or head.get("state") not in {"done", "not_submitted"}):
                raise SelectionError("Shared queue recovery is required before changing pickup height.")
            # Camera height is already part of the original motion contract, so
            # existing standbys require no new queue or coordinator capability.
            return self.session._set_pick_height_locked(camera_z)
        finally:
            if activity_acquired:
                self.session._activity_lock.release()
            self._execution.release()

    def settings(self):
        session = self.session
        grid = session.placing_grid._read()
        geometry = {"locations": [{k: p[k] for k in ("id", "x", "y", "z")}
                                   for p in grid["locations"]],
                    "cell_size_mm": grid.get("cell_size_mm")}
        identity = {"calibration": session.calibration_id, "tag_offset": list(session.tag_offset),
                    "grid": hashlib.sha256(json.dumps(geometry, sort_keys=True,
                        separators=(",", ":"), allow_nan=False).encode()).hexdigest()}
        settings = {"identity": identity, "motion": {"speed": session.speed,
            "home_speed": session.home_speed, "camera_z": session.camera_z,
            "phase_speeds": copy.deepcopy(session.phase_speeds),
            "handover_offset": list(session.handover_offset)}}
        if session.continuous_batch:
            settings["continuous_batch"] = True
        if self._device_speeds is not None:
            settings["device_speeds"] = dict(self._device_speeds)
        return settings

    def _apply_settings(self, settings):
        from device_agent import validate_motion
        from handover_plan import normalize_phase_speeds, normalize_handover_offset
        if (not isinstance(settings, dict)
                or not {"identity", "motion"}.issubset(settings)
                or set(settings) - {"identity", "motion", "continuous_batch", "device_speeds"}
                or type(settings.get("continuous_batch", False)) is not bool
                or settings["identity"] != self.settings()["identity"]):
            raise SelectionError("Failover worker calibration or placement grid differs from the starting device.")
        motion = settings["motion"]
        validate_motion(motion)
        speed, home_speed = motion["speed"], motion["home_speed"]
        if "device_speeds" in settings:
            try:
                speed = validate_device_speeds(settings["device_speeds"])[self.role]
            except CoordinatorError as error:
                raise SelectionError(str(error)) from error
            home_speed = min(speed, 200.0)
        if self._restore_speeds is None:
            self._restore_speeds = (self.session.speed, self.session.home_speed)
        self.session.speed, self.session.home_speed = speed, home_speed
        self.session._camera_z = motion["camera_z"]
        self.session.phase_speeds = normalize_phase_speeds(motion.get("phase_speeds"))
        self.session.handover_offset = normalize_handover_offset(motion["handover_offset"])
        # A standby follows the admitted session's behavior, including old
        # finite runs whose settings predate the continuous option.
        self.session._continuous_batch = settings.get("continuous_batch", False)

    def boundary(self):
        self.session._check_stop()
        if self._closed.is_set():
            raise QueueWaitCancelled("This worker is shutting down; the shared batch remains available.")
        self.robot.check_lease()
        run = self.coordinator.state().get("run")
        if not run or run["id"] != self.run_id or run["phase"] not in ACTIVE:
            raise QueueWaitCancelled("The shared batch is no longer active.")

    def stamp_context(self, context):
        """Bind recovery to the exact durable queue prepared before callbacks."""
        if self.executing:
            execution = context.get("execution", {})
            known = list(execution.get("completed_steps", []))
            known.extend(execution.get("pending_jobs", {}).values())
            if execution.get("pending_job") is not None:
                known.append(execution["pending_job"])
            if not known:
                # CupSession saves its initial journal before the adapter has
                # prepared this queue. Never stamp the previous cup's identity.
                context["failover"] = {"run_id": self.run_id, "submission_id": None,
                                       "not_dispatched": True}
                return context
            record = self.robot._read_pending(self.pending_path)
            if (record and record["run_id"] == self.run_id
                    and any(job in known for job in record["jobs"])):
                context["failover"] = {"run_id": self.run_id, "submission_id": record["submission_id"]}
        return context

    def before_home(self):
        if self.executing:
            self.boundary()
            self.coordinator.set_phase(self.run_id, "finalizing")

    def after_home(self):
        if self.executing and self.session.continuous_batch:
            self.boundary()
            self.coordinator.watch(self.run_id)
            self.output("Workspace empty. Arms are home; watching for new cups until Stop.")

    def home_done(self, job):
        """Persist the adapter's measured Home barrier before its next queue."""
        if not self.executing:
            return
        record = self.robot._read_pending(self.pending_path)
        if (not record or record["run_id"] != self.run_id or record["state"] != "done"
                or record["jobs"] != [job] or job["endpoint"] != self.robot.HOME_ENDPOINT
                or job["name"] not in record["home_verified"]):
            raise SelectionError("Home confirmation does not match its measured queue receipt.")
        self.coordinator.confirm_home(self.run_id, record["submission_id"], job["body"]["arm"])

    def _confirm_recovered_homes(self, record):
        if record["state"] != "done":
            return
        if any(job["endpoint"] == self.robot.HOME_ENDPOINT for job in record["jobs"]):
            state = self.coordinator.state()
            run = state.get("run")
            receipt = self.coordinator.receipt(record["run_id"], record["submission_id"])
            if (not run or run["id"] != record["run_id"]
                    or receipt.get("home_cycle", 0) < run.get("home_cycle", 0)):
                return  # Historical completion cannot confirm a later cycle's Home.
        for job in record["jobs"]:
            if job["endpoint"] == self.robot.HOME_ENDPOINT:
                self.coordinator.confirm_home(record["run_id"], record["submission_id"], job["body"]["arm"])

    def _resume_homes(self, state):
        """Adopt a crashed worker's last Home only after measuring it ourselves."""
        run = state["run"]
        confirmed = run.get("confirmed_homes", [])
        if confirmed not in ([], ["right"], ["right", "left"]):
            raise SelectionError("Shared Home progress is invalid.")
        head = state.get("head")
        if not head or head.get("run_id") != self.run_id:
            return confirmed
        receipt = self.coordinator.receipt(self.run_id, head["submission_id"])
        queue = receipt.get("queue")
        if not queue or len(queue.get("steps", [])) != 1 or queue["steps"][0].get("type") != "home":
            return confirmed
        queue = self.robot._validate_queue_status(queue)
        step = queue["steps"][0]
        arm = step["arm"]
        if arm in confirmed:
            return confirmed
        if (receipt["state"] != "done" or queue["state"] != "done"
                or queue.get("owner") != head["owner"]
                or step["name"] != f"q{head['submission_id']}_0_workspace_{arm}_home"):
            raise SelectionError("Shared Home receipt does not confirm the expected finalization.")
        self.boundary()
        self.robot._verify_home(arm)
        result = self.coordinator.confirm_home(self.run_id, head["submission_id"], arm)
        return result["run"]["confirmed_homes"]

    @staticmethod
    def _validate_recovery_context(record, context, run_id):
        """Check journal ownership and job evidence without changing state."""
        execution = context.get("execution", {})
        known = list(execution.get("completed_steps", []))
        pending = execution.get("pending_jobs", {})
        known.extend(pending.values())
        if execution.get("pending_job") is not None:
            known.append(execution["pending_job"])
        unused = (context.get("failover") == {"run_id": run_id, "submission_id": None,
                                              "not_dispatched": True} and not known
                  and not context.get("stop_requested"))
        if record is None:
            return unused
        identity = {"run_id": record["run_id"], "submission_id": record["submission_id"]}
        if context.get("stop_requested") or (context.get("failover") != identity and not unused):
            raise SelectionError("Recovery does not belong to this confirmed queue; inspect before resuming.")
        # Every original job must be represented exactly in this journal.
        # Home queues contain one job, while their journal spans both arms.
        if record["state"] == "done" and not unused and any(job not in known for job in record["jobs"]):
            raise SelectionError("Recovery steps do not match the confirmed queue.")
        if record["state"] == "not_submitted" and not unused:
            pending_jobs = list(pending.values())
            if execution.get("pending_job") is not None:
                pending_jobs.append(execution["pending_job"])
            if (any(job not in record["jobs"] for job in pending_jobs)
                    or any(job in record["jobs"] for job in execution.get("completed_steps", []))):
                raise SelectionError("Unsubmitted queue contradicts its local execution journal.")
        return unused

    def _shared_home_verified(self, record, receipt):
        """Bind durable measured Home proof to this exact dispatched queue."""
        if (record.get("dispatch_started") is not True or record.get("rejected_at_coordinator") is not False
                or record.get("state") not in {"prepared", "pending", "unknown", "done"}
                or len(record.get("jobs", [])) != 1
                or record["jobs"][0].get("endpoint") != self.robot.HOME_ENDPOINT
                or not isinstance(receipt, dict) or receipt.get("state") != "done"
                or receipt.get("home_verified") is not True):
            return False
        try:
            queue = self.robot._validate_queue_status(receipt.get("queue"))
            return (queue["state"] == "done" and queue.get("owner") == record["expected_owner"]
                    and self.robot._signature(queue["steps"]) == self.robot._signature(record["steps"], submitted=True)
                    and record.get("started_at") in (None, queue["started_at"])
                    and (record["state"] != "done" or record.get("queue") == queue))
        except (RobotCommandError, KeyError, TypeError, ValueError):
            return False

    def _reconcile(self, *, wait=True, allow_home_verification=True):
        """Only a receipt for this journal may resolve interrupted local work."""
        record = self.robot.pending_completion(wait=wait)
        session = self.session
        journal = (json.loads(session.recovery.path.read_text())
                   if session.recovery.required() else None)
        context = journal.get("context", {}) if journal else {}
        if record is not None and record["state"] in {"pending", "unknown", "prepared"} and not wait:
            return False
        if (record is not None and record["state"] == "done"
                and any(job["endpoint"] == self.robot.HOME_ENDPOINT and job["name"] not in record["home_verified"]
                        for job in record["jobs"])):
            receipt = self.coordinator.receipt(record["run_id"], record["submission_id"])
            if self._shared_home_verified(record, receipt):
                # A successor already measured this exact Home. The robot may
                # now be in a later cycle, so measuring its current pose would
                # not verify or disprove that historical completion.
                session._check_stop()
                record = copy.deepcopy(record)
                record["home_verified"] = [job["name"] for job in record["jobs"]
                                            if job["endpoint"] == self.robot.HOME_ENDPOINT]
                self.robot._write_pending(self.pending_path, record)
        if (record is not None and record["state"] == "done" and not allow_home_verification
                and any(job["endpoint"] == self.robot.HOME_ENDPOINT and job["name"] not in record["home_verified"]
                        for job in record["jobs"])):
            return False
        if record is None:
            if self._validate_recovery_context(None, context, self.run_id):
                session._finish_confirmed_motion()
                session.scan_usable = False
                return True
            if session.motion_uncertain or session.recovery.required():
                raise SelectionError("Unrelated robot recovery requires inspection before failover.")
            return True
        if record["state"] in {"failed", "cancelled"}:
            receipt = self.coordinator.receipt(record["run_id"], record["submission_id"])
            if receipt.get("recovery") is not None:
                self._retire_inspected_record(record, receipt)
                return True
        if record["state"] not in {"done", "not_submitted"}:
            raise SelectionError("The previous cup did not finish successfully; inspect the robot.")
        session._check_stop()
        if journal is not None:
            self._validate_recovery_context(record, context, self.run_id)
            self.robot.acknowledge_pending(record["submission_id"])
            self._confirm_recovered_homes(record)
            session._finish_confirmed_motion()
        else:
            if session.motion_uncertain:
                raise SelectionError("Unrelated in-memory recovery requires inspection before failover.")
            self.robot.acknowledge_pending(record["submission_id"])
            self._confirm_recovered_homes(record)
        session.scan_usable = False
        return True

    def _release(self):
        try:
            try:
                self.robot.end_standby()
            except Exception as error:
                if not transient(error):
                    raise
                self.output("Control release could not be confirmed; its lease will expire.")
        finally:
            self.robot.unbind_batch()
            self.session._continuous_batch = self._configured_continuous
            if self._restore_speeds is not None:
                self.session.speed, self.session.home_speed = self._restore_speeds
                self._restore_speeds = None

    def _yieldable(self, error):
        if transient(error):
            return True
        if not isinstance(error, (QueueNotSubmitted, CoordinatorConflict)):
            return False
        # A coordinator can detect preemption before the renew thread does.
        # Only a confirmed no-submit plus actual ownership loss may yield;
        # validation/configuration refusals must still be reported.
        if isinstance(error, QueueNotSubmitted):
            record = self.robot.pending_completion(wait=False)
            if not record or record["state"] != "not_submitted":
                return False
        else:
            try:
                run = self.coordinator.state().get("run")
            except CoordinatorUnavailable:
                return True
            if not run or run["id"] != self.run_id or run["phase"] not in ACTIVE:
                return True
        try:
            owner = self.robot._get_object("/control")
        except Exception as read_error:
            if transient(read_error):
                return True
            raise
        return (owner.get("active") is False or owner.get("active") is True
                and isinstance(owner.get("client_id"), str) and owner["client_id"] != self.role)

    def _cleanup_inactive(self):
        """Resolve this worker's old receipt without acquiring or moving arms."""
        record = self.robot._read_pending(self.pending_path)
        if record is None:
            return
        if record["reconciled"] and not self.session.recovery.required() and not self.session.motion_uncertain:
            return
        self.run_id = record["run_id"]
        self.robot.bind_batch(self.coordinator, record["run_id"], self.pending_path)
        self._reconcile(wait=False, allow_home_verification=False)

    def _retain_read_retry(self, error, *, stop_event=None):
        """Keep a confirmed owner through a temporary read failure, without acquiring.

        Releasing a healthy primary during every camera retry lets a standby
        start a scan that the primary immediately preempts on its next retry.
        Network/ownership loss still yields; only read-only service/frame
        failures can keep the lease, after a fresh ownership confirmation.
        """
        seen, readonly = set(), False
        while error is not None and id(error) not in seen:
            seen.add(id(error))
            if isinstance(error, (ControlUnavailable, GatewayControlUnavailable, CoordinatorUnavailable,
                                  QueueNotSubmitted, requests.ConnectionError, requests.Timeout)):
                return False
            readonly = readonly or isinstance(error, (TransientCameraError, GatewayReadUnavailable))
            error = error.__cause__
        if (not readonly or self._closed.is_set() or self.session.stop_requested
                or stop_event and stop_event.is_set()):
            return False
        try:
            self.robot.check_lease()
            if self.robot.lease_client_id != self.role:
                return False
            owner = self.robot._get_object("/control")
            self.robot.check_lease()
        except RobotCommandError:
            return False
        return (not self._closed.is_set() and not self.session.stop_requested
                and not (stop_event and stop_event.is_set()) and owner.get("active") is True
                and owner.get("client_id") == self.role and type(owner.get("priority")) is int
                and owner["priority"] == PRIORITIES[self.role])

    @staticmethod
    def _retry_reason(error):
        """Report the bounded cause chain, omitting private credentials."""
        names, seen, last = [], set(), error
        while error is not None and id(error) not in seen and len(names) < 6:
            seen.add(id(error))
            names.append(type(error).__name__)
            last, error = error, error.__cause__
        detail = " ".join(str(last).split())
        # Exceptions should not contain credentials. Still redact a caller's
        # accidental bearer header before writing it to operator-visible logs.
        import re
        detail = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", detail)
        return (" -> ".join(names) + ": " + detail[:300])[:500]

    def _run(self, run_id, *, stop_event=None):
        try:
            return self._run_active(run_id, stop_event=stop_event)
        finally:
            # A read retry may retain the lease during its interruptible wait.
            # Stop/close/shared completion must release that lease on every exit.
            self.executing = False
            self._release()

    def _run_active(self, run_id, *, stop_event=None):
        session, jobs = self.session, []
        self.run_id = run_id
        retry_delay = 1.0
        while not self._closed.is_set() and not (stop_event and stop_event.is_set()):
            retain_lease = False
            try:
                state = self.coordinator.state()
                run = state.get("run")
                if not run or run["id"] != run_id or run["phase"] not in ACTIVE:
                    self._cleanup_inactive()
                    return jobs
                self._apply_settings(run["settings"])
                self.coordinator.heartbeat(run_id, "waiting")
                if not self.robot.try_begin_standby(expected_client_id=self.role,
                                                   expected_priority=PRIORITIES[self.role]):
                    self._closed.wait(1)
                    continue
                self.robot.bind_batch(self.coordinator, run_id, self.pending_path)
                self.executing = True
                self.coordinator.heartbeat(run_id, "active")
                self.output("Control acquired; waiting for the accepted cup to finish.")
                self.robot.wait_for_idle_queue()
                self._reconcile()
                self.boundary()
                # Refresh phase after draining: the old owner may have observed
                # an empty workspace and entered Home while we were waiting.
                state = self.coordinator.state()
                run = state["run"]
                at_home = run["phase"] == "watching"
                if run["phase"] == "finalizing":
                    completed_homes = self._resume_homes(state)
                    if session.inventory is None:
                        session.inventory = {"capture": "failover-" + run_id, "cups": []}
                    session.batch_progress = {"completed_cups": [], "pending_cups": [],
                        "completion_reason": "workspace_empty", "home_cycles": 0, "arms_home": False}
                    # Home remains a separate, measured completion barrier.
                    home_jobs = session._home_when_workspace_empty(skip_arms=completed_homes)
                    if home_jobs is None:
                        return jobs
                    jobs.extend(home_jobs)
                    at_home = True
                if run["phase"] != "finalizing" or session.continuous_batch:
                    # Admission precedes acquisition. A standby may finish a
                    # cup in that gap, so even the primary's original preview
                    # must be replaced after draining inherited work.
                    inventory = session._scan_active()
                    self.boundary()
                    jobs.extend(session._execute_many_active("goto", inventory["capture"], [], None,
                                                             batch=True, at_home=at_home))
                reason = (session.batch_progress or {}).get("completion_reason")
                if reason == "session_closed" or self._closed.is_set() or (stop_event and stop_event.is_set()):
                    return jobs
                if reason not in {"workspace_empty", "no_pickable_cups"}:
                    raise SelectionError("Batch did not report a finite completion condition.")
                self.coordinator.finish(run_id, reason)
                self.output("Batch finished. Devices remain armed for the next batch you start.")
                return jobs
            except QueueWaitCancelled:
                return jobs
            except Exception as error:
                if session.stop_requested:
                    raise
                if not self._yieldable(error):
                    try:
                        self.coordinator.block(run_id, str(error))
                    except (CoordinatorUnavailable, CoordinatorConflict):
                        pass
                    raise
                retain_lease = self._retain_read_retry(error, stop_event=stop_event)
                self.output("Camera, connection or control is temporarily unavailable. "
                            "Waiting to continue this batch between cups. " + self._retry_reason(error))
            finally:
                self.executing = retain_lease
                if not retain_lease:
                    self._release()
            self._closed.wait(retry_delay)
            retry_delay = min(5.0, retry_delay * 2)
        return jobs

    def execute_primary(self, action, capture_id, cup_ids, placement):
        """Called only after UI batch confirmation, under the session lock."""
        if self.role != "primary":
            raise SelectionError("Only the MacBook can start a shared batch.")
        with self._execution:
            capabilities = (self.coordinator.state().get("capabilities", [])
                            if self.session.continuous_batch or self._device_speeds is not None else [])
            if self.session.continuous_batch and "continuous_batches" not in capabilities:
                raise SelectionError("The shared controller needs the continuous-picking update before starting.")
            if self._device_speeds is not None and DEVICE_SPEED_CAPABILITY not in capabilities:
                raise SelectionError("The shared controller needs the device-speed update before starting.")
            recovery_lease = False
            if self.robot.has_acknowledged_idle_errors:
                # The inspection belongs to this process. Hold the first lease
                # before admission so a standby cannot win that gap and reject
                # the old arm error before our first new queue replaces it.
                recovery_lease = self.robot.try_begin_standby(expected_client_id=self.role,
                                                             expected_priority=PRIORITIES[self.role])
                if not recovery_lease:
                    raise SelectionError("Could not acquire control for the first batch after recovery. No batch was started.")
            run_id = uuid.uuid4().hex
            self.run_id = run_id
            # Persisting authorization can be retried with this same identity;
            # it never submits robot work or starts a second batch.
            try:
                while not self._closed.is_set():
                    self.session._check_stop()
                    try:
                        self.coordinator.start(run_id, self.settings())
                    except CoordinatorUnavailable:
                        self.session._check_stop()
                        self._closed.wait(1)
                        continue
                    self.session._check_stop()
                    break
            except BaseException:
                if self.session.stop_requested:
                    # Start may have been committed before its reply was lost.
                    # Cancel this exact admission, never a different batch.
                    try:
                        self.coordinator.stop(run_id, "Operator requested Stop.")
                    except (CoordinatorUnavailable, CoordinatorConflict):
                        pass
                if recovery_lease:
                    self._release()
                raise
            try:
                return self._run(run_id)
            finally:
                if recovery_lease:
                    self._release()

    def resume(self, run_id):
        with self._execution, self.session._activity_lock:
            self._run(run_id)
        return {"message": "Shared batch finished or handed over; devices remain armed."}

    def serve(self, stop_event=None):
        while not self._closed.is_set() and not (stop_event and stop_event.is_set()):
            try:
                self.coordinator.heartbeat(self.run_id if self.executing else None, "armed")
                run = self.coordinator.state().get("run")
                if run and run["phase"] in ACTIVE:
                    with self._execution, self.session._activity_lock:
                        self._run(run["id"], stop_event=stop_event)
                elif self.pending_path.exists():
                    with self._execution, self.session._activity_lock:
                        try:
                            self._cleanup_inactive()
                        finally:
                            self._release()
            except CoordinatorUnavailable:
                pass
            except Exception as error:
                self.output("Failover waiting for inspection: " + str(error))
            self._closed.wait(1)

    def start_monitor(self, resume_callback):
        """Primary status-only monitor schedules recovery on the UI worker."""
        if self._monitor is not None:
            return
        def monitor():
            while not self._closed.is_set():
                try:
                    self.coordinator.heartbeat(self.run_id if self.executing else None,
                                               "active" if self.executing else "armed")
                    run = self.coordinator.state().get("run")
                    if run and run["phase"] in ACTIVE and not self._execution.locked():
                        resume_callback(run["id"])
                    elif not self._execution.locked():
                        recovery_run = self._inactive_recovery_run()
                        if recovery_run is not None:
                            resume_callback(recovery_run)
                except Exception:
                    pass
                self._closed.wait(1)
        self._monitor = threading.Thread(target=monitor, name="picker-failover-status", daemon=True)
        self._monitor.start()

    def _inactive_recovery_run(self):
        """Schedule only recovery that this inactive queue can resolve.

        An old receipt is retained after a batch finishes. It cannot resolve a
        later operator action or Stop. Scheduling those every poll repeatedly
        takes the UI worker away from the operator's recovery controls.
        This is a read-only scheduling filter; _reconcile still validates the
        full receipt and journal under the session lock before clearing either.
        """
        session = self.session
        if session.stop_requested or not session.recovery.required():
            return None
        record = self.robot._read_pending(self.pending_path)
        if record is None:
            return None
        context = json.loads(session.recovery.path.read_text()).get("context", {})
        try:
            self._validate_recovery_context(record, context, record["run_id"])
        except SelectionError:
            return None
        if record["state"] in {"failed", "cancelled"}:
            receipt = self.coordinator.receipt(record["run_id"], record["submission_id"])
            if receipt.get("recovery") is None:
                return None
            self._checked_inspected_receipt(record, receipt)
            return record["run_id"]
        state = record["state"] if record["dispatch_started"] else "not_submitted"
        if record["state"] in {"prepared", "pending", "unknown"} and record["dispatch_started"]:
            receipt = self.coordinator.receipt(record["run_id"], record["submission_id"])
            state = receipt.get("state")
            if state in {"failed", "cancelled"} and receipt.get("recovery") is not None:
                self._checked_inspected_receipt(record, receipt)
                return record["run_id"]
            if state not in {"done", "not_submitted"}:
                return None  # Observe completion without toggling the UI busy.
        if state != record["state"]:
            try:
                self._validate_recovery_context({**record, "state": state}, context, record["run_id"])
            except SelectionError:
                return None
        if (state == "done" and any(job["endpoint"] == self.robot.HOME_ENDPOINT
                and job["name"] not in record["home_verified"] for job in record["jobs"])):
            receipt = self.coordinator.receipt(record["run_id"], record["submission_id"])
            if not self._shared_home_verified(record, receipt):
                return None  # Inactive cleanup must not measure or retry Home.
        return record["run_id"]

    def _checked_inspected_receipt(self, record, receipt):
        """Bind the shared inspection to this worker's exact historical queue."""
        queue = self.robot._validate_queue_status(receipt.get("queue"))
        try:
            recovery = validate_recovery(receipt.get("recovery"), run_id=record["run_id"],
                submission_id=record["submission_id"], queue=queue)
        except CoordinatorError as error:
            raise SelectionError("The saved inspection does not match this failed cup.") from error
        if (receipt.get("state") != queue["state"]
                or record["state"] not in {"pending", "unknown", "failed", "cancelled"}
                or not record["dispatch_started"] or record["rejected_at_coordinator"]
                or queue.get("owner") != record["expected_owner"]
                or self.robot._signature(queue["steps"]) != self.robot._signature(record["steps"], submitted=True)
                or record.get("started_at") not in (None, queue["started_at"])
                or record.get("queue") is not None and record["queue"] != queue
                and record["state"] in {"failed", "cancelled"}):
            raise SelectionError("The saved inspection refers to a different queue; recovery remains blocked.")
        return recovery

    def _retire_inspected_record(self, record, receipt, *, explicit=False):
        """Archive a failed cup without replaying placement or Home callbacks."""
        recovery = self._checked_inspected_receipt(record, receipt)
        session = self.session
        journal = (json.loads(session.recovery.path.read_text())
                   if session.recovery.required() else None)
        if not explicit:
            session._check_stop()
            if journal is not None:
                self._validate_recovery_context(record, journal.get("context", {}), record["run_id"])
            elif session.motion_uncertain and not record.get("inspected_recovery"):
                raise SelectionError("Unrelated local recovery still requires inspection.")
        if record.get("inspected_recovery") != recovery or not record["reconciled"]:
            archive = self.pending_path.parent / "recovery-history" / ("inspected-cup-" + uuid.uuid4().hex + ".json")
            self.robot._write_pending(archive, {"pending": record, "journal": journal, "receipt": receipt})
            retired = {**record, "state": receipt["state"], "queue": copy.deepcopy(receipt["queue"]),
                       "started_at": receipt["queue"]["started_at"], "reconciled": True,
                       "inspected_recovery": recovery}
            self.robot._write_pending(self.pending_path, retired)
        if not explicit:
            session._finish_confirmed_motion()
        session.scan_usable = False

    def recover_after_inspection(self):
        """Retire inspected terminal failures, preserving all original evidence."""
        if self.role != "primary":
            raise SelectionError("Acknowledge gateway recovery from the MacBook controller.")
        if not self._execution.acquire(blocking=False):
            raise SelectionError("Wait for shared execution to finish before recovery.")
        acquired = False
        try:
            def check_shared():
                state = self.coordinator.state()
                run, head = state.get("run"), state.get("head")
                pending = self.robot._read_pending(self.pending_path)
                if run and run["phase"] in ACTIVE:
                    raise SelectionError("The shared batch is still active; stop it before acknowledging recovery.")
                if head and head["state"] not in {"done", "not_submitted", "failed", "cancelled"}:
                    raise SelectionError("The shared batch has an unresolved queue receipt; preserve it for coordinated recovery.")
                target = None
                if head and head["state"] in {"failed", "cancelled"}:
                    if not run or run["id"] != head["run_id"] or run["phase"] not in {"blocked", "stopped"}:
                        raise SelectionError("The failed receipt does not match the stopped batch.")
                    target = (head["run_id"], head["submission_id"])
                elif (run and run.get("recovery") and pending and not pending["reconciled"]
                      and (pending["run_id"], pending["submission_id"]) ==
                          (run["id"], run["recovery"]["submission_id"])):
                    target = (run["id"], run["recovery"]["submission_id"])
                if target and RECOVERY_CAPABILITY not in state.get("capabilities", []):
                    raise SelectionError("The coordinator needs the recovery update before this batch can be unblocked.")
                if (pending and pending["state"] not in {"done", "not_submitted"}
                        and not (pending["reconciled"] and pending.get("inspected_recovery"))):
                    if (pending["run_id"], pending["submission_id"]) != target:
                        raise SelectionError("This device has an unrelated unresolved cup receipt; preserve it for recovery.")
                return state, pending, target

            before_shared, pending, target = check_shared()
            before = self.robot.queue_status()
            if before["state"] == "running":
                raise SelectionError("A gateway queue is running; wait for it to finish before recovery.")
            if before["state"] == "done" and not target:
                return None  # Successful history does not need deleting.
            if target:
                saved = self.coordinator.receipt(*target)
                saved_queue = self.robot._validate_queue_status(saved.get("queue"))
                if saved["state"] not in {"failed", "cancelled"} or saved_queue["state"] != saved["state"]:
                    raise SelectionError("The failed batch has no confirmed terminal queue record.")
                if before["state"] != "idle" and before != saved_queue:
                    raise SelectionError("The gateway queue differs from the failed batch. No queue was cleared.")
            owner = self.robot._get_object("/control")
            if owner.get("active") is not False:
                raise SelectionError("Another controller holds robot control; wait before acknowledging recovery.")
            acquired = self.robot.try_begin_standby(expected_client_id=self.role,
                                                    expected_priority=PRIORITIES[self.role])
            if not acquired:
                raise SelectionError("Could not acquire control for recovery; the failed queue was preserved.")
            after_shared, after_pending, after_target = check_shared()
            # Heartbeat timestamps may change. The admitted run and last
            # submission must not change while acquiring the recovery lease.
            if (any(before_shared.get(key) != after_shared.get(key) for key in ("run", "head"))
                    or pending != after_pending or target != after_target):
                raise SelectionError("The shared batch changed; inspect its current state before recovery.")
            if target:
                # Archive shared evidence before touching the gateway's only
                # current queue, even when this Mac did not own the failed cup.
                archive = self.pending_path.parent / "recovery-history" / ("shared-failure-" + uuid.uuid4().hex + ".json")
                self.robot._write_pending(archive, {"shared": before_shared, "receipt": saved, "pending": pending})
            result = self.robot.recover_failed_queue(expected_queue=before,
                archive_dir=self.pending_path.parent / "recovery-history", acknowledged=True)
            if target:
                self.robot.check_lease()
                receipt = self.coordinator.recover(*target, acknowledged=True)
                validate_recovery(receipt.get("recovery"), run_id=target[0],
                                  submission_id=target[1], queue=receipt.get("queue"))
                if pending and (pending["run_id"], pending["submission_id"]) == target:
                    self._retire_inspected_record(pending, receipt, explicit=True)
                result["message"] = "Failed batch unblocked. No movement sent. Scan again and confirm a new batch."
            return result
        finally:
            try:
                if acquired:
                    self.robot.end_standby()
            finally:
                self._execution.release()

    def stop_batch(self):
        # The UI sends physical Stop independently. Keep this asynchronous
        # metadata request tied to its original epoch even if recovery and a
        # new confirmation happen before a delayed HTTP response returns.
        target = self.run_id
        admitting = self._execution.locked()
        run = self.coordinator.state().get("run")
        if target:
            if (run and run["id"] == target and run["phase"] in ACTIVE) or admitting:
                try:
                    self.coordinator.stop(target, "Operator requested Stop.")
                except CoordinatorConflict:
                    pass  # An in-flight admission has not become visible yet.
        elif run and run["phase"] in ACTIVE and self.run_id is None:
            self.coordinator.stop(run["id"], "Operator requested Stop.")

    def close(self):
        self._closed.set()
        self.robot.cancel_wait()
        if self._monitor is not None:
            self._monitor.join(timeout=4)
