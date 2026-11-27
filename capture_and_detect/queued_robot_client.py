"""Gateway-owned cup queues and a persistent, priority-bound standby lease.

One submitted queue may continue after this process or its connection disappears.
Submission is therefore never retried, and losing control never cancels a queue.
Only an explicit software Stop asks the gateway to cancel queued work. This
module uses requests and the shared motion validators; it imports no AI runtime.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid

import requests

from robot_client import RobotClient, RobotCommandError


QUEUE_ENDPOINT = "/robot/rws/queue"
MAX_QUEUE_STEPS = 64


class ControlUnavailable(RobotCommandError):
    """Ownership was lost or expired; this is not an explicit robot Stop."""


class QueueNotSubmitted(RobotCommandError):
    """This queue was definitely not accepted; no physical retry was sent."""

    def __init__(self, message, *, control_unavailable=False, http_status=None):
        super().__init__(message)
        self.control_unavailable = control_unavailable
        self.http_status = http_status


class QueueWaitCancelled(RobotCommandError):
    """Local receipt waiting ended; accepted gateway work remains untouched."""


def plan_to_queue_steps(jobs, *, name_prefix=""):
    """Preserve arm order and every cross-arm dependency with queue groups.

    Jobs in a group run in separate sequential arm lanes. A later group waits
    for both lanes in the previous group. Split a lane after a job whose result
    releases the other arm, so its own continuation can overlap that arm rather
    than delaying it to the end of the lane. Serial plans keep their serial order.
    Names may be prefixed to distinguish repeated plans at the gateway.
    """
    return _plan_to_queue_steps(jobs, name_prefix=name_prefix, grouping_version=2)


def _plan_to_queue_steps(jobs, *, name_prefix, grouping_version):
    """Version 1 is retained only to validate already-saved queue receipts."""
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= MAX_QUEUE_STEPS:
        raise RobotCommandError(f"A gateway queue requires 1..{MAX_QUEUE_STEPS} jobs.")
    jobs = [RobotClient._validate_job(job) for job in jobs]
    if len({job["name"] for job in jobs}) != len(jobs):
        raise RobotCommandError("Queued jobs require distinct step names.")
    dependencies = RobotClient._job_dependencies(jobs)
    arms = {job["name"]: job["body"]["arm"] for job in jobs}
    if dependencies is None:
        levels = {job["name"]: index for index, job in enumerate(jobs)}
    else:
        barriers = {dep for name, required in dependencies.items() for dep in required
                    if arms[dep] != arms[name]}
        levels, remaining = {}, list(jobs)
        while remaining:
            advanced = False
            for job in remaining[:]:
                name = job["name"]
                if not dependencies[name] <= levels.keys():
                    continue
                levels[name] = max((levels[dep] + (dep in barriers if grouping_version == 2
                                                 else arms[dep] != arms[name])
                                    for dep in dependencies[name]), default=0)
                remaining.remove(job)
                advanced = True
            if not advanced:  # The shared validator normally catches this first.
                raise RobotCommandError("Queued dependencies contain a cycle.")
    result = []
    for index, job in sorted(enumerate(jobs), key=lambda item: (levels[item[1]["name"]], item[0])):
        # Keep generated names short regardless of arbitrary library step names.
        name = (f"{name_prefix}{index}_{job['name'][:32]}" if name_prefix else job["name"])
        result.append({"name": name, "type": job["endpoint"].rsplit("/", 1)[-1],
                       "body": {key: copy.deepcopy(value) for key, value in job["body"].items()
                                if key != "wait"}, "group": f"g{levels[job['name']]}"})
    return result


class QueueRobotClient(RobotClient):
    QUEUE_TIMEOUT = 300.0
    QUEUE_POLL = 0.3
    supports_queue_callbacks = True

    def __init__(self, api, headers, dry_run=False):
        super().__init__(api, headers, dry_run=dry_run)
        self._persistent = False
        self._standby_stop = None
        self._standby_renewal = None
        self._expected_identity = None
        self._lease_client_id = None
        self._batch_binding = None
        self._receipt_cancel = threading.Event()
        self._completed_in_process = set()
        self._acknowledged_idle_errors = {}

    def latch_stop(self):
        super().latch_stop()
        self._acknowledged_idle_errors = {}

    def _post(self, endpoint, *, move=None, allow_conflict=False):
        # An inspected old arm result is valid only before the next dispatch.
        # Keep it through repeated idle checks and lease renewal/reacquisition.
        if endpoint == QUEUE_ENDPOINT or endpoint in {
                self.MOVE_ENDPOINT, self.PATH_ENDPOINT, self.HOME_ENDPOINT, self.GRIPPER_ENDPOINT}:
            self._acknowledged_idle_errors = {}
        return super()._post(endpoint, move=move, allow_conflict=allow_conflict)

    @property
    def lease_client_id(self):
        return self._lease_client_id

    @property
    def has_acknowledged_idle_errors(self):
        """This process inspected old arm errors that the next dispatch replaces."""
        return bool(self._acknowledged_idle_errors)

    def _record_lease(self, reply, response_headers):
        lease = reply.get("lease")
        if not isinstance(lease, dict):
            raise RobotCommandError("Gateway did not return a control lease.")
        client_id, priority = lease.get("client_id"), lease.get("priority")
        if not isinstance(client_id, str) or not client_id or type(priority) is not int:
            raise RobotCommandError("Gateway returned invalid control identity or priority.")
        if self._expected_identity is not None and (client_id, priority) != self._expected_identity:
            raise RobotCommandError("Gateway token does not match the configured standby identity and priority.")
        if self._lease_client_id is not None and client_id != self._lease_client_id:
            raise RobotCommandError("Gateway changed control identity during renewal.")
        super()._record_lease(reply, response_headers)
        self._lease_client_id = client_id

    def try_begin_standby(self, expected_client_id="tertiary", expected_priority=100):
        """Attempt one acquisition; a competing owner returns False without motion.

        A successful call renews until end_standby(), including between cups and
        during camera waits. A token with the wrong role is never used to move.
        """
        if (not isinstance(expected_client_id, str) or not expected_client_id
                or type(expected_priority) is not int or expected_priority < 0):
            raise ValueError("Standby requires a client identity and nonnegative integer priority.")
        self._check_stop()
        if not self._execution_lock.acquire(blocking=False):
            raise RobotCommandError("A robot command is already executing.")
        try:
            if self._persistent:
                if self._expected_identity != (expected_client_id, expected_priority):
                    raise RobotCommandError("Standby identity cannot change while control is held.")
                self.check_lease()
                return True
            self._expected_identity = (expected_client_id, expected_priority)
            self._lease_client_id = None
            self._lease_error = None
            if self.dry_run:
                self._persistent = True
                self._lease_client_id = expected_client_id
                return True
            # Never adopt another process's lease merely because it shares our
            # token. Rejected competing acquisitions still register contention.
            owner = self._get_object("/control")
            if (type(owner.get("active")) is not bool or
                    (owner.get("active") is True and (
                        not isinstance(owner.get("client_id"), str) or not owner["client_id"]
                        or type(owner.get("priority")) is not int))):
                raise RobotCommandError("Gateway returned invalid control ownership.")
            if owner["active"] and owner["client_id"] == expected_client_id:
                if owner["priority"] != expected_priority:
                    raise RobotCommandError("Active standby identity has a different priority than configured.")
                return False
            with self._lease_lock:
                self._check_stop()
                reply, headers = self._post("/control/acquire", allow_conflict=True)
                if reply is None:
                    return False
                lease = reply.get("lease")
                if (reply.get("success") is True and isinstance(lease, dict)
                        and lease.get("active") is True and owner["active"]
                        and lease.get("client_id") == owner["client_id"]):
                    # Some gateway variants may treat acquire as idempotent.
                    # A shared/wrong token must never adopt or release the
                    # already-active process's lease on that kind of response.
                    raise RobotCommandError("Gateway returned the already-active control owner; "
                                            "this standby did not acquire a new lease. No control was released.")
                self._release_needed = (reply.get("success") is True
                                        and isinstance(reply.get("lease"), dict)
                                        and reply["lease"].get("active") is True)
                try:
                    self._record_lease(reply, headers)
                except BaseException:
                    if self._release_needed:
                        self._release_control()
                    raise
            self._persistent = True
            self._standby_stop = threading.Event()
            self._standby_renewal = threading.Thread(
                target=self._renew_loop, args=(self._standby_stop,), daemon=True,
                name="picker-standby-lease")
            self._standby_renewal.start()
            return True
        finally:
            self._execution_lock.release()

    def end_standby(self):
        """Release this client's lease; never cancel or repeat physical work."""
        if not self._execution_lock.acquire(blocking=False):
            raise RobotCommandError("Wait for queue execution to finish before releasing standby control.")
        try:
            if self._standby_stop is not None:
                self._standby_stop.set()
            if self._standby_renewal is not None:
                self._standby_renewal.join()
            if self._release_needed:
                self._release_control()
            self._standby_stop = self._standby_renewal = None
            self._persistent = False
            self._lease_client_id = None
            self._expected_identity = None
        finally:
            self._execution_lock.release()

    def check_lease(self):
        self._check_stop()
        if self.dry_run:
            return
        try:
            self._check_lease()
        except RobotCommandError as error:
            raise ControlUnavailable(str(error)) from error
        if not self._owned_lease:
            raise ControlUnavailable("This client does not hold a confirmed control lease.")

    def bind_batch(self, coordinator, run_id, pending_path):
        """Opt into coordinated execution; do not acquire control or submit work.

        Existing local metadata is retained even when it belongs to an earlier
        run. Call pending_completion() and acknowledge_pending() after checking
        the corresponding session journal before starting a different queue.
        """
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", run_id):
            raise ValueError("Invalid coordinated batch identity.")
        if not self._execution_lock.acquire(blocking=False):
            raise RobotCommandError("Wait for queue execution before binding a batch.")
        try:
            path = Path(pending_path)
            self._read_pending(path)
            self._batch_binding = (coordinator, run_id, path)
            self._receipt_cancel.clear()
        finally:
            self._execution_lock.release()

    def unbind_batch(self):
        """Detach coordination after execution ends; preserve recovery metadata."""
        if not self._execution_lock.acquire(blocking=False):
            raise RobotCommandError("Cancel receipt waiting and wait for execution before unbinding.")
        try:
            self._batch_binding = None
        finally:
            self._execution_lock.release()

    def cancel_wait(self):
        """Interrupt only local receipt waiting, never gateway work or RAPID."""
        self._receipt_cancel.set()

    @staticmethod
    def _read_pending(path):
        if path.is_symlink():
            raise RobotCommandError("Pending queue metadata must be a regular file.")
        if not path.exists():
            return None
        try:
            with path.open("rb") as stream:
                raw = stream.read(4 * 1024 * 1024 + 1)
            value = json.loads(raw)
            if (len(raw) > 4 * 1024 * 1024 or not isinstance(value, dict)
                    or value.get("schema_version") != 1
                    or not isinstance(value.get("run_id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value["run_id"])
                    or not isinstance(value.get("submission_id"), str)
                    or not re.fullmatch(r"[0-9a-f]{16}", value["submission_id"])
                    or not isinstance(value.get("expected_owner"), str) or not value["expected_owner"]
                    or value.get("state") not in {"prepared", "pending", "unknown", "done", "failed", "cancelled", "not_submitted"}
                    or not isinstance(value.get("jobs"), list) or not value["jobs"]
                    or not isinstance(value.get("steps"), list)
                    or type(value.get("dispatch_started")) is not bool
                    or type(value.get("callbacks_applied")) is not bool
                    or type(value.get("reconciled")) is not bool
                    or type(value.get("rejected_at_coordinator")) is not bool
                    or not isinstance(value.get("original_indices"), list)
                    or len(value["original_indices"]) != len(value["jobs"])
                    or any(type(index) is not int or index < 0 for index in value["original_indices"])
                    or len(set(value["original_indices"])) != len(value["original_indices"])):
                raise ValueError("Invalid metadata")
            jobs = [RobotClient._validate_job(job) for job in value["jobs"]]
            if len(jobs) == 1 and jobs[0]["endpoint"] == RobotClient.HOME_ENDPOINT:
                jobs = [{key: item for key, item in jobs[0].items() if key != "after"}]
            grouping_version = value.get("grouping_version", 1)
            if type(grouping_version) is not int or grouping_version not in {1, 2}:
                raise ValueError("Unsupported queue grouping version")
            if _plan_to_queue_steps(jobs, name_prefix=f"q{value['submission_id']}_",
                                    grouping_version=grouping_version) != value["steps"]:
                raise ValueError("Queue metadata does not match the saved jobs")
            for key in ("callback_attempted", "callback_completed", "home_verified"):
                names = value.get(key)
                if (not isinstance(names, list) or len(names) != len(set(names))
                        or any(name not in {job["name"] for job in jobs} for name in names)):
                    raise ValueError("Invalid callback metadata")
            inspection = value.get("inspected_recovery")
            abandonment = value.get("inspected_abandonment")
            if abandonment is not None:
                if (inspection is not None or value["state"] != "unknown" or not value["reconciled"]
                        or not value["dispatch_started"] or value["rejected_at_coordinator"]):
                    raise ValueError("Abandonment must retain an inspected unknown submission")
                QueueRobotClient.validate_abandoned_record(value, {
                    "state": "unknown", "queue": value.get("abandoned_receipt_queue"),
                    "abandonment": abandonment})
            elif "abandoned_receipt_queue" in value:
                raise ValueError("Abandoned queue evidence requires its inspection")
            if inspection is not None:
                from failover_coordinator import CoordinatorError, validate_recovery
                if (value["state"] not in {"failed", "cancelled"} or not value["reconciled"]
                        or not value["dispatch_started"] or value["rejected_at_coordinator"]):
                    raise ValueError("Inspection must retain a retired failure")
                try:
                    validate_recovery(inspection, run_id=value["run_id"],
                                      submission_id=value["submission_id"], queue=value.get("queue"))
                    queue = value["queue"]
                    if (queue["state"] != value["state"] or queue.get("owner") != value["expected_owner"]
                            or queue.get("started_at") != value.get("started_at")
                            or QueueRobotClient._signature(queue["steps"]) !=
                                QueueRobotClient._signature(value["steps"], submitted=True)):
                        raise ValueError("Inspected queue does not match this saved submission")
                except CoordinatorError as error:
                    raise ValueError("Invalid inspected failure evidence") from error
            if (not set(value["callback_completed"]) <= set(value["callback_attempted"])
                    or not set(value["home_verified"]) <= {job["name"] for job in jobs if job["endpoint"] == RobotClient.HOME_ENDPOINT}
                    or value["callbacks_applied"] and (value["state"] != "done"
                        or set(value["callback_completed"]) != {job["name"] for job in jobs}
                        or set(value["home_verified"]) != {job["name"] for job in jobs if job["endpoint"] == RobotClient.HOME_ENDPOINT})
                    or value["state"] == "not_submitted" and (value.get("queue") is not None
                        or value.get("started_at") is not None or value["callback_attempted"]
                        or value["callback_completed"] or value["home_verified"])
                    or value["reconciled"] and value["state"] not in {"done", "not_submitted"}
                        and inspection is None and abandonment is None):
                raise ValueError("Inconsistent completion metadata")
            return value
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
            raise RobotCommandError("Pending queue metadata is invalid; preserve it for recovery.") from error

    @staticmethod
    def validate_abandoned_record(record, receipt):
        """Bind explicit inspection to an unknown original queue, never a completion.

        The coordinator's last observation can contain more completed steps than
        this client's earlier observation. Keep both unchanged and require exact
        submission identity with no regression of any completion already known.
        """
        from failover_coordinator import CoordinatorError, validate_abandonment
        try:
            queue = QueueRobotClient._validate_queue_status(receipt.get("queue"))
            proof = validate_abandonment(receipt.get("abandonment"), run_id=record["run_id"],
                submission_id=record["submission_id"], queue=queue)
            if (receipt.get("state") != "unknown"
                    or record["state"] not in {"pending", "unknown"}
                    or not record["dispatch_started"] or record["rejected_at_coordinator"]
                    or record["callbacks_applied"] or queue["state"] == "idle"
                    or queue.get("owner") != record["expected_owner"]
                    or QueueRobotClient._signature(queue["steps"]) !=
                        QueueRobotClient._signature(record["steps"], submitted=True)
                    or record.get("started_at") not in (None, queue["started_at"])):
                raise ValueError("Abandonment refers to another submission")
            previous = record.get("queue")
            if previous is not None:
                previous = QueueRobotClient._validate_queue_status(previous)
                if (previous["state"] == "idle" or previous.get("owner") != queue["owner"]
                        or previous.get("started_at") != queue["started_at"]
                        or QueueRobotClient._signature(previous["steps"]) !=
                            QueueRobotClient._signature(queue["steps"])
                        or not {step["name"] for step in previous["steps"] if step["state"] == "done"}
                            <= {step["name"] for step in queue["steps"] if step["state"] == "done"}):
                    raise ValueError("Abandonment contradicts the saved queue observation")
            return proof
        except (CoordinatorError, ValueError, KeyError, TypeError) as error:
            raise RobotCommandError("Inspected abandonment does not match this unknown queue; preserve recovery.") from error

    @staticmethod
    def _write_pending(path, record):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise RobotCommandError("Pending queue metadata must be a regular file.")
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(record, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _transient_receipt_error(error):
        # Keep the unbound client usable without importing the coordinator.
        try:
            from failover_coordinator import CoordinatorUnavailable
        except ImportError:
            CoordinatorUnavailable = ()
        return (isinstance(error, (requests.RequestException, OSError, TimeoutError))
                or bool(CoordinatorUnavailable and isinstance(error, CoordinatorUnavailable)))

    def _reject_prepared(self, record, *, require_confirmation=False):
        coordinator, _, path = self._batch_binding
        record.update(state="not_submitted", queue=None)
        confirmation_error = None
        # A missing HTTP response here cannot turn a known local no-POST into
        # an unknown robot action. Reconciliation retries only this metadata.
        try:
            reply = coordinator.reject_queue(record["run_id"], record["submission_id"])
        except Exception as error:
            confirmation_error = error
            record["rejected_at_coordinator"] = False
        else:
            record["rejected_at_coordinator"] = (isinstance(reply, dict)
                and reply.get("state") == "not_submitted" and reply.get("queue") is None)
        self._write_pending(path, record)
        if require_confirmation and not record["rejected_at_coordinator"]:
            # Preserve transient transport evidence for the failover runner;
            # a known local no-POST must not permanently block its shared run
            # just because confirmation is temporarily unreachable.
            raise RobotCommandError("Coordinator has not yet confirmed this non-submission.") from confirmation_error

    def _reconcile_non_submission(self, record):
        coordinator, _, path = self._batch_binding
        # Confirmation is durable. Older coordinators refuse re-rejection
        # after a newer batch starts; that unnecessary retry can erase it.
        if record["rejected_at_coordinator"]:
            return
        try:
            receipt = coordinator.receipt(record["run_id"], record["submission_id"])
        except Exception as error:
            from failover_coordinator import CoordinatorConflict
            if not (isinstance(error, CoordinatorConflict) or self._transient_receipt_error(error)):
                raise RobotCommandError("Coordinated non-submission receipt was refused or invalid; preserve recovery.") from error
        else:
            if (not isinstance(receipt, dict) or "queue" not in receipt
                    or receipt.get("state") not in {"pending", "unknown", "not_submitted"}
                    or receipt["queue"] is not None):
                raise RobotCommandError("Coordinator receipt contradicts this non-submission; preserve recovery.")
            if receipt["state"] == "not_submitted":
                # Also repairs receipts whose confirmation was lost by older
                # clients. Reading this exact historical identity changes no
                # shared batch, gateway queue, or local placement bookkeeping.
                record["rejected_at_coordinator"] = True
                self._write_pending(path, record)
                return
        # A lost preparation/rejection reply may still need registering. This
        # metadata-only retry cannot confirm an observed gateway submission.
        self._reject_prepared(record, require_confirmation=True)

    def _observe_receipt(self, record, *, wait):
        coordinator, _, path = self._batch_binding
        while True:
            previous = copy.deepcopy(record)
            self._check_stop()
            if self._receipt_cancel.is_set():
                raise QueueWaitCancelled("Stopped waiting locally; an accepted gateway queue may still finish.")
            try:
                receipt = coordinator.receipt(record["run_id"], record["submission_id"])
            except Exception as error:
                if not self._transient_receipt_error(error):
                    raise RobotCommandError("Coordinated queue receipt was refused or invalid; preserve recovery.") from error
                if not wait:
                    return copy.deepcopy(record)
                self._receipt_cancel.wait(self.QUEUE_POLL)
                continue
            if (not isinstance(receipt, dict)
                    or receipt.get("state") not in {"pending", "unknown", "done", "failed", "cancelled", "not_submitted"}):
                raise RobotCommandError("Coordinator returned an invalid queue receipt.")
            state, queue = receipt["state"], receipt.get("queue")
            if receipt.get("abandonment") is not None:
                # The runner must archive the session journal before retiring
                # this local record. Do not wait forever or replay callbacks.
                self.validate_abandoned_record(record, receipt)
                return copy.deepcopy(record)
            if queue is not None:
                queue = self._validate_queue_status(queue)
                if (queue["state"] == "idle" or queue.get("owner") != record["expected_owner"]
                        or self._signature(queue["steps"]) != self._signature(record["steps"], submitted=True)):
                    raise RobotCommandError("Coordinated receipt does not match this queue; preserve recovery.")
                previous_start = record.get("started_at")
                if previous_start is not None and queue["started_at"] != previous_start:
                    raise RobotCommandError("Coordinated queue identity changed; preserve recovery.")
                record["started_at"] = queue["started_at"]
                record["queue"] = copy.deepcopy(queue)
            if state in {"done", "failed", "cancelled"} and (queue is None or queue["state"] != state):
                raise RobotCommandError("Coordinator completion lacks its matching gateway snapshot.")
            if state == "not_submitted" and queue is not None:
                raise RobotCommandError("Coordinator contradicted its queue submission result.")
            if state == "not_submitted":
                record["rejected_at_coordinator"] = True
            record["state"] = state
            if record != previous:
                self._write_pending(path, record)
            if state in {"done", "failed", "cancelled", "not_submitted"} or not wait:
                return copy.deepcopy(record)
            self._receipt_cancel.wait(self.QUEUE_POLL)

    def pending_completion(self, *, wait=False):
        """Reconcile durable queue identity, without motion or session-journal edits.

        Returns None or a metadata dict containing state, run_id, submission_id,
        jobs, steps, queue and callbacks_applied. A DONE receipt proves gateway
        completion, not that the old session saved its placement bookkeeping.
        Call only after the execution worker has exited. No callbacks are replayed.
        """
        if self._batch_binding is None:
            raise RobotCommandError("Bind a coordinator before reconciling pending work.")
        if not self._execution_lock.acquire(blocking=False):
            raise RobotCommandError("Wait for the execution worker before reconciling pending work.")
        try:
            record = self._read_pending(self._batch_binding[2])
            if record is None:
                return None
            if record.get("inspected_abandonment") is not None:
                return copy.deepcopy(record)  # Already validated by _read_pending.
            if record["state"] == "not_submitted":
                self._reconcile_non_submission(record)
                return copy.deepcopy(record)
            if not record["dispatch_started"]:
                self._reject_prepared(record, require_confirmation=True)
                return copy.deepcopy(record)
            if record["state"] in {"done", "failed", "cancelled"}:
                # Revalidate even locally persisted terminal receipts.
                queue = self._validate_queue_status(record.get("queue"))
                if (queue["state"] != record["state"] or queue.get("owner") != record["expected_owner"]
                        or self._signature(queue["steps"]) != self._signature(record["steps"], submitted=True)
                        or queue["started_at"] != record.get("started_at")):
                    raise RobotCommandError("Saved completion does not match its pending queue.")
                return copy.deepcopy(record)
            return self._observe_receipt(record, wait=wait)
        finally:
            self._execution_lock.release()

    def acknowledge_pending(self, submission_id):
        """Allow replacement only after the caller reconciles its session journal.

        This does not clear robot recovery or Stop. Home requires independent
        measured-joint verification before its receipt can be acknowledged.
        """
        if self._batch_binding is None or not self._execution_lock.acquire(blocking=False):
            raise RobotCommandError("Bind coordination and wait for execution before acknowledging work.")
        try:
            path = self._batch_binding[2]
            record = self._read_pending(path)
            if record is None or record["submission_id"] != submission_id or record["state"] not in {"done", "not_submitted"}:
                raise RobotCommandError("Only this confirmed completion or non-submission can be acknowledged.")
            self._check_stop()
            if record["state"] == "not_submitted" and not record["rejected_at_coordinator"]:
                raise RobotCommandError("Coordinator has not confirmed this non-submission; reconcile it first.")
            if record["state"] == "done":
                queue = self._validate_queue_status(record.get("queue"))
                if (queue["state"] != "done" or queue.get("owner") != record["expected_owner"]
                        or self._signature(queue["steps"]) != self._signature(record["steps"], submitted=True)
                        or queue["started_at"] != record.get("started_at")):
                    raise RobotCommandError("Saved completion does not match its pending queue.")
            for job in record["jobs"]:
                if job["endpoint"] == self.HOME_ENDPOINT and record["state"] == "done" and job["name"] not in record["home_verified"]:
                    self.check_lease()
                    self.wait_for_idle_queue()
                    self._verify_home(job["body"]["arm"])
                    record["home_verified"].append(job["name"])
            record["reconciled"] = True
            self._write_pending(path, record)
        finally:
            self._execution_lock.release()

    def read_gripper_position(self, arm):
        # A queue can finish its grip and release between status polls. Reading
        # the current opening cannot measure that historical grip operation.
        if arm not in ("left", "right"):
            raise RobotCommandError("Gripper telemetry requires arm='left' or 'right'.")
        return None

    @staticmethod
    def _validate_queue_status(reply, *, inspection=False, completed_history=False):
        if (not isinstance(reply, dict)
                or reply.get("state") not in {"idle", "running", "done", "failed", "cancelled"}
                or not isinstance(reply.get("steps"), list)
                or len(reply["steps"]) > MAX_QUEUE_STEPS
                or type(reply.get("length")) is not int or reply["length"] != len(reply["steps"])):
            raise RobotCommandError("Gateway returned invalid queue status; robot completion is unknown.")
        if reply["state"] == "idle":
            if reply["steps"]:
                raise RobotCommandError("Idle gateway queue unexpectedly contains steps.")
            return reply
        if (not reply["steps"] or not isinstance(reply.get("owner"), str) or not reply["owner"]
                or not isinstance(reply.get("started_at"), str) or not reply["started_at"]):
            raise RobotCommandError("Gateway queue identity is missing; robot completion is unknown.")
        try:
            started = datetime.fromisoformat(reply["started_at"].replace("Z", "+00:00"))
            if started.tzinfo is None:
                raise ValueError("no timezone")
        except ValueError as error:
            raise RobotCommandError("Gateway queue start time is invalid.") from error
        # Legacy terminal pickers reused a few unprefixed step names. Such a
        # failed snapshot is usable only as an exact, whole-array inspection
        # target; it can never identify completed callbacks or new admission.
        legacy_labels = all(isinstance(step, dict) and isinstance(step.get("name"), str)
                            and not re.match(r"q[0-9a-f]{16}_", step["name"])
                            for step in reply["steps"])
        legacy_failure = inspection and reply["state"] in {"failed", "cancelled"} and legacy_labels
        # Older terminal runs can also finish successfully with reused names.
        # Accept that whole-array history only before admitting NEW work, never
        # as a receipt or a source of per-step completion callbacks. Every step
        # must still be DONE, and wait_for_idle_queue checks both arms separately.
        legacy_completed = completed_history and reply["state"] == "done" and legacy_labels
        names = set()
        for step in reply["steps"]:
            if (not isinstance(step, dict) or not isinstance(step.get("name"), str) or not step["name"]
                    or step["name"] in names and not (legacy_failure or legacy_completed)
                    or step.get("type") not in {"move", "path", "home", "gripper"}
                    or step.get("arm") not in {"left", "right"} or not isinstance(step.get("group"), str)
                    or step.get("state") not in {"pending", "running", "done", "failed", "skipped", "cancelled"}
                    or (step["state"] == "done" and step.get("message") != "DONE")):
                raise RobotCommandError("Gateway returned invalid queue step results; robot completion is unknown.")
            names.add(step["name"])
        if reply["state"] == "done" and any(step["state"] != "done" for step in reply["steps"]):
            raise RobotCommandError("Gateway queue reports done without confirming every step.")
        if inspection and reply["state"] in {"failed", "cancelled"} and any(
                step["state"] == "running" for step in reply["steps"]):
            raise RobotCommandError("A queue step is still running; wait before recovery.")
        return reply

    def queue_status(self, *, completed_history=False):
        return self._validate_queue_status(self._get_object(QUEUE_ENDPOINT),
                                           completed_history=completed_history)

    def inspection_queue_status(self):
        """Read terminal evidence for explicit inspection, never completion."""
        return self._validate_queue_status(self._get_object(QUEUE_ENDPOINT), inspection=True,
                                           completed_history=True)

    @staticmethod
    def _terminal_arm_error(result):
        return result.get("accepted") is False and result.get("message") == "ERR move failed"

    def recover_failed_queue(self, *, expected_queue, archive_dir, acknowledged):
        """Clear one inspected terminal queue, retaining its original evidence.

        The caller guards shared coordinator/pending state and holds a primary
        lease. This operation never acquires control, moves an arm, resets RAPID,
        retries DELETE, or turns a failed arm result into successful completion.
        """
        if acknowledged is not True:
            raise RobotCommandError("Inspect both stationary arms and the cup before acknowledging queue recovery.")
        expected = copy.deepcopy(self._validate_queue_status(expected_queue, inspection=True))
        if expected["state"] not in {"failed", "cancelled", "idle"}:
            raise RobotCommandError("Only an inspected failed, cancelled, or already idle queue can be recovered.")
        if any(step["state"] == "running" for step in expected["steps"]):
            raise RobotCommandError("A queue step is still running; wait before recovery.")
        if not self._execution_lock.acquire(blocking=False):
            raise RobotCommandError("Wait for the execution worker before recovering the queue.")
        try:
            if self.dry_run:
                return {"dry_run": True, "cleared": False, "queue_state": expected["state"],
                        "message": "Preview only; no queue or robot request sent."}
            self._acknowledged_idle_errors = {}
            self.check_lease()
            if self._lease_client_id != "primary":
                raise RobotCommandError("Explicit queue recovery requires the primary control lease.")
            if self.inspection_queue_status() != expected:
                raise RobotCommandError("Gateway queue changed; review it again before recovery.")
            arms = {}
            for arm in ("left", "right"):
                self.check_lease()
                result = self._get_object(f"/robot/rws/status?arm={arm}")
                if result.get("message") == "BUSY":
                    raise RobotCommandError(f"The {arm} arm is still busy; wait before recovery.")
                if not (result.get("accepted") is True and result.get("message") == "DONE"
                        or self._terminal_arm_error(result)):
                    raise RobotCommandError(f"The {arm} arm has an unconfirmed state; recovery was not performed.")
                arms[arm] = copy.deepcopy(result)
            self.check_lease()
            if self.inspection_queue_status() != expected:
                raise RobotCommandError("Gateway queue changed; review it again before recovery.")
            archive = Path(archive_dir) / f"gateway-recovery-{uuid.uuid4().hex}.json"
            evidence = {"schema_version": 1, "acknowledged": True,
                        "recorded_at": datetime.now(timezone.utc).isoformat(),
                        "state": "prepared", "queue": expected, "arms": arms,
                        "clear_requested": expected["state"] != "idle"}
            self._write_pending(archive, evidence)
            self.check_lease()
            # Recheck after durable storage: a changed queue is never cancelled
            # as a side effect of acknowledging this older terminal failure.
            if self.inspection_queue_status() != expected:
                raise RobotCommandError("Gateway queue changed; review it again before recovery.")
            self.check_lease()
            if expected["state"] != "idle":
                response = None
                try:
                    response = requests.delete(f"{self.api}{QUEUE_ENDPOINT}", headers=self.headers,
                                               timeout=self.CONTROL_TIMEOUT)
                    response.raise_for_status()
                except requests.RequestException as error:
                    raise RobotCommandError("Queue recovery outcome is unconfirmed; the clear request was not retried. "
                                            "Refresh status before acknowledging recovery again.") from error
                finally:
                    if response is not None:
                        response.close()
            self.check_lease()
            if self.queue_status()["state"] != "idle":
                raise RobotCommandError("Gateway did not confirm an idle queue after recovery; the clear request was not retried.")
            evidence.update(state="recovered", recovered_at=datetime.now(timezone.utc).isoformat())
            self._write_pending(archive, evidence)
            self.check_lease()
            self._acknowledged_idle_errors = {arm: result for arm, result in arms.items()
                                              if self._terminal_arm_error(result)}
            # Stop is intentionally lock-free; do not retain permission if it
            # arrived between the final lease check and recording the evidence.
            if self.stop_requested:
                self._acknowledged_idle_errors = {}
                self._check_stop()
            return {"dry_run": False, "cleared": expected["state"] != "idle", "queue_state": "idle",
                    "archive": str(archive), "acknowledged_arm_errors": list(self._acknowledged_idle_errors),
                    "message": ("Inspection acknowledged. Saved gateway failure cleared. No movement sent. Scan again."
                                if expected["state"] != "idle" else
                                "Inspection acknowledged. Gateway queue is idle. No movement sent. Scan again.")}
        finally:
            self._execution_lock.release()

    def wait_for_idle_queue(self, timeout=300.0, poll=0.3):
        """Wait for an inherited cup, then require confirmed idle arm states.

        Failed/cancelled/unknown queues remain untouched for operator recovery.
        This function never homes, clears a queue, or starts a movement.
        """
        self._validate_wait(timeout, poll)
        if self.dry_run:
            return {"state": "idle", "length": 0, "steps": []}
        deadline = time.monotonic() + timeout
        while True:
            if self._batch_binding is not None and self._receipt_cancel.is_set():
                raise QueueWaitCancelled("Stopped waiting locally; the previous gateway queue remains untouched.")
            self.check_lease()
            status = self.queue_status(completed_history=True)
            self.check_lease()
            if status["state"] != "idle":
                self._acknowledged_idle_errors = {}
            if status["state"] in {"failed", "cancelled"}:
                raise RobotCommandError(f"Gateway queue is {status['state']}; inspect both arms and the cup before recovery.")
            if status["state"] != "running":
                for arm in ("left", "right"):
                    self.check_lease()
                    result = self._get_object(f"/robot/rws/status?arm={arm}")
                    if (status["state"] == "idle" and self._terminal_arm_error(result)
                            and self._acknowledged_idle_errors.get(arm) == result):
                        continue  # Inspected old failure remains a failure, not DONE.
                    self._acknowledged_idle_errors.pop(arm, None)
                    if result.get("accepted") is not True or result.get("message") not in {"DONE", "BUSY"}:
                        raise RobotCommandError(f"The {arm} arm did not report a confirmed idle or busy state.")
                    if result["message"] == "BUSY":
                        self._acknowledged_idle_errors = {}
                        break
                else:
                    self.check_lease()
                    return status
            if time.monotonic() >= deadline:
                raise RobotCommandError("Timed out waiting for the previous gateway movement; no new commands were sent.")
            self._stop_event.wait(min(poll, max(0, deadline - time.monotonic())))

    @staticmethod
    def _validate_wait(timeout, poll):
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) or value <= 0 for value in (timeout, poll)):
            raise ValueError("Queue wait timeout and poll interval must be positive finite numbers.")

    @staticmethod
    def _signature(steps, *, submitted=False):
        return [(step["name"], step["type"], step["body"]["arm"] if submitted else step["arm"],
                 step["group"]) for step in steps]

    def execute_jobs(self, jobs, before_send=None, after_done=None, *,
                     before_queue=None, after_queue=None):
        if not isinstance(jobs, list):
            raise RobotCommandError("Provide a list of named RWS jobs.")
        jobs = [self._validate_job(job) for job in jobs]
        if not jobs:
            return
        # Validate topology, size and unique names before any HTTP or callbacks.
        ordered_steps = plan_to_queue_steps(jobs)
        homes = [job["endpoint"] == self.HOME_ENDPOINT for job in jobs]
        if any(homes) and not all(homes):
            raise RobotCommandError("Home jobs must be a separate plan from cup movement so measured Home verification remains a barrier.")
        for job in jobs:
            self._check_motion_height(job)
        if self.dry_run:
            # Preserve the shared dry-run contract: no callbacks, leases or HTTP.
            return super().execute_jobs(jobs, before_send=before_send, after_done=after_done)
        self._check_stop()
        if not self._execution_lock.acquire(blocking=False):
            raise RobotCommandError("This client is already executing a command sequence.")
        renewal_stop, renewal = threading.Event(), None
        ordinary = not self._persistent
        try:
            if ordinary:
                with self._lease_lock:
                    self._lease_error = None
                    self._release_needed = False
                    self._lease_client_id = None
                    self._expected_identity = None
                    self._take_control()
                renewal = threading.Thread(target=self._renew_loop, args=(renewal_stop,), daemon=True)
                renewal.start()
            if all(homes):
                # The next arm must not move until the preceding Home has both
                # reported DONE and passed its independent joint-pose check.
                # Keep one lease and execution lock across these small queues.
                by_name = {job["name"]: index for index, job in enumerate(jobs)}
                for step in ordered_steps:
                    index = by_name[step["name"]]
                    self._execute_queue([jobs[index]], [index], before_send, after_done)
            else:
                self._execute_queue(jobs, list(range(len(jobs))), before_send, after_done,
                                    before_queue=before_queue, after_queue=after_queue)
        finally:
            renewal_stop.set()
            try:
                if renewal is not None:
                    renewal.join()
                if ordinary and self._release_needed:
                    self._release_control()
            finally:
                self._execution_lock.release()

    def _execute_queue(self, jobs, original_indices, before_send, after_done, *,
                       before_queue=None, after_queue=None):
        """Submit one indivisible cup or one verified Home under the caller's lease."""
        if self._batch_binding is not None:
            return self._execute_bound_queue(jobs, original_indices, before_send, after_done,
                                             before_queue=before_queue, after_queue=after_queue)
        self.check_lease()
        # Never silently replace or clear another queue. The gateway remains
        # the final atomic arbiter if another actor changes it after this read.
        self.wait_for_idle_queue(timeout=self.QUEUE_TIMEOUT, poll=self.QUEUE_POLL)
        candidates = []
        for index, job in enumerate(jobs):
            self.check_lease()
            candidate = self._before_job(original_indices[index], job, before_send)
            if (candidate["name"] != job["name"] or candidate["body"]["arm"] != job["body"]["arm"]
                    or candidate["endpoint"] != job["endpoint"] or candidate.get("after") != job.get("after")):
                raise RobotCommandError("Pre-queue replacement changed step identity, endpoint or dependencies.")
            self._check_motion_height(candidate)
            candidates.append(candidate)
        prefix = f"q{uuid.uuid4().hex[:16]}_"
        submission = candidates
        if len(candidates) == 1 and candidates[0]["endpoint"] == self.HOME_ENDPOINT:
            # Its dependencies were validated and completed by execute_jobs.
            # They do not refer to jobs inside this one-step gateway queue.
            submission = [{key: value for key, value in candidates[0].items() if key != "after"}]
        steps = plan_to_queue_steps(submission, name_prefix=prefix)
        expected_signature = self._signature(steps, submitted=True)
        indices = {f"{prefix}{index}_{job['name'][:32]}": index for index, job in enumerate(candidates)}
        self.check_lease()
        # Exactly one POST. Any exception leaves every queued job pending in
        # the caller's durable journal; a standby can inspect gateway status.
        answer, _ = self._post(QUEUE_ENDPOINT, move={"steps": steps})
        self.check_lease()
        if answer.get("accepted") is False:
            raise RobotCommandError("Gateway refused the cup queue; no retry was sent.")
        started_at, completed = None, set()
        deadline = time.monotonic() + self.QUEUE_TIMEOUT
        while True:
            self.check_lease()
            status = self.queue_status()
            self.check_lease()
            if (status["state"] == "idle" or status.get("owner") != self._lease_client_id
                    or self._signature(status["steps"]) != expected_signature):
                raise RobotCommandError("Gateway queue no longer matches this execution; robot completion is unknown.")
            if started_at is None:
                started_at = status["started_at"]
            elif started_at != status["started_at"]:
                raise RobotCommandError("Gateway queue identity changed; robot completion is unknown.")
            by_name = {step["name"]: step for step in status["steps"]}
            if any(by_name[name]["state"] != "done" for name in completed):
                raise RobotCommandError("Gateway queue completion regressed; no new commands were sent.")
            # Report only exact DONE steps. Keep original plan indices and
            # names in journal callbacks, independent of queue group ordering.
            for step in status["steps"]:
                name = step["name"]
                if step["state"] != "done" or name in completed:
                    continue
                index = indices[name]
                job = candidates[index]
                if job["endpoint"] == self.HOME_ENDPOINT:
                    self._verify_home(job["body"]["arm"])
                completed.add(name)  # Failed callbacks are never retried.
                self._after_job(original_indices[index], job, after_done)
            self.check_lease()
            if status["state"] == "done":
                if len(completed) != len(candidates):
                    raise RobotCommandError("Not every queued robot job was confirmed.")
                return
            if status["state"] in {"failed", "cancelled"}:
                raise RobotCommandError(f"Gateway queue {status['state']}: {str(status.get('message', ''))[:500]}. No retry was sent.")
            if time.monotonic() >= deadline:
                raise RobotCommandError("Gateway queue completion timed out; it may still be running. No retry was sent.")
            self._stop_event.wait(self.QUEUE_POLL)

    @staticmethod
    def _cause_matches(error, error_type):
        seen = set()
        while error is not None and id(error) not in seen:
            seen.add(id(error))
            if isinstance(error, error_type):
                return True
            error = error.__cause__
        return False

    @staticmethod
    def _http_rejection(error):
        seen = set()
        while error is not None and id(error) not in seen:
            seen.add(id(error))
            response = getattr(error, "response", None)
            code = getattr(response, "status_code", None)
            if isinstance(code, int) and 400 <= code < 500:
                return code
            error = error.__cause__
        return None

    def _execute_bound_queue(self, jobs, original_indices, before_send, after_done, *,
                             before_queue=None, after_queue=None):
        coordinator, run_id, path = self._batch_binding
        if self._receipt_cancel.is_set():
            raise QueueWaitCancelled("Local queue waiting was cancelled before submission.")
        previous = self._read_pending(path)
        if previous is not None and not (previous["reconciled"] or
                previous["state"] == "done" and previous["callbacks_applied"]
                and previous["submission_id"] in self._completed_in_process):
            raise RobotCommandError("Reconcile the previous local queue journal before submitting another queue.")
        self.check_lease()
        if getattr(coordinator, "role", self._lease_client_id) != self._lease_client_id:
            raise RobotCommandError("Coordinator role does not match the gateway lease identity.")
        self.wait_for_idle_queue(timeout=self.QUEUE_TIMEOUT, poll=self.QUEUE_POLL)
        submission_id = uuid.uuid4().hex[:16]
        submission = jobs
        if len(jobs) == 1 and jobs[0]["endpoint"] == self.HOME_ENDPOINT:
            submission = [{key: value for key, value in jobs[0].items() if key != "after"}]
        steps = plan_to_queue_steps(submission, name_prefix=f"q{submission_id}_")
        record = {"schema_version": 1, "grouping_version": 2,
                  "run_id": run_id, "submission_id": submission_id,
                  "expected_owner": self._lease_client_id, "steps": steps, "jobs": copy.deepcopy(jobs),
                  "original_indices": list(original_indices), "state": "prepared", "dispatch_started": False,
                  "queue": None, "started_at": None, "callbacks_applied": False, "reconciled": False,
                  "callback_attempted": [], "callback_completed": [], "home_verified": [],
                  "rejected_at_coordinator": False}
        # Durable identity precedes both caller callbacks and the physical POST.
        self._write_pending(path, record)
        try:
            self.check_lease()
            prepared = coordinator.prepare_queue(run_id, submission_id, copy.deepcopy(steps))
            if not isinstance(prepared, dict) or prepared.get("state") not in {"pending", "unknown"}:
                raise RobotCommandError("Coordinator did not prepare this new queue.")
            aggregate = not any(job["endpoint"] == self.HOME_ENDPOINT for job in jobs)
            if aggregate and before_queue is not None:
                self.check_lease()
                try:
                    candidates = before_queue(list(zip(original_indices, copy.deepcopy(jobs))))
                    if not isinstance(candidates, list) or len(candidates) != len(jobs):
                        raise RobotCommandError("Pre-queue callback must return the prepared jobs in order.")
                    for candidate, job in zip(candidates, jobs):
                        self.check_lease()
                        if self._validate_job(candidate) != job:
                            raise RobotCommandError("Coordinated queue callbacks cannot change the prepared movement plan.")
                except Exception as error:
                    raise RobotCommandError(f"Pre-queue check failed: {error}") from error
            else:
                for index, job in enumerate(jobs):
                    self.check_lease()
                    candidate = self._before_job(original_indices[index], job, before_send)
                    if candidate != job:
                        raise RobotCommandError("Coordinated queue callbacks cannot change the prepared movement plan.")
            self.check_lease()
            self._check_stop()
            if self._receipt_cancel.is_set():
                raise QueueWaitCancelled("Local queue waiting was cancelled before submission.")
        except BaseException as error:
            self._reject_prepared(record)
            if not isinstance(error, Exception) or isinstance(error, QueueWaitCancelled) or self.stop_requested:
                raise
            raise QueueNotSubmitted("Coordinated queue was not submitted; no robot command was sent.",
                control_unavailable=self._cause_matches(error, ControlUnavailable)) from error

        # Once this write succeeds, a crash or lost response is ambiguous. The
        # saved nonce and coordinator receipt are the only completion evidence.
        try:
            record.update(dispatch_started=True, state="pending")
            self._write_pending(path, record)
            self.check_lease()
        except BaseException as error:
            self._reject_prepared(record)
            if not isinstance(error, Exception) or self.stop_requested:
                raise
            raise QueueNotSubmitted("Coordinated queue was not submitted; no robot command was sent.",
                control_unavailable=self._cause_matches(error, ControlUnavailable)) from error
        try:
            answer, _ = self._post(QUEUE_ENDPOINT, move={"steps": steps})
        except RobotCommandError as error:
            rejected = self._http_rejection(error)
            if rejected is not None:
                self._reject_prepared(record)
                raise QueueNotSubmitted("Gateway rejected this queue; no retry was sent.",
                    control_unavailable=isinstance(self._lease_error, RobotCommandError) or not self._owned_lease,
                    http_status=rejected) from error
            record["state"] = "unknown"
            self._write_pending(path, record)
        else:
            if answer.get("accepted") is False:
                self._reject_prepared(record)
                raise QueueNotSubmitted("Gateway refused this queue; no retry was sent.")
        # Deliberately no lease checks here: the gateway owns an accepted cup.
        # A successor may already own control and archive our DONE receipt.
        result = self._observe_receipt(record, wait=True)
        if result["state"] == "not_submitted":
            raise QueueNotSubmitted("Coordinator confirmed this queue was not submitted; no retry was sent.")
        if result["state"] not in {"done", "failed", "cancelled"}:
            raise RobotCommandError("This queue remains unknown after inspected retirement; no completion callback was replayed.")
        queue = result["queue"]
        indices = {f"q{submission_id}_{index}_{job['name'][:32]}": index for index, job in enumerate(jobs)}
        if aggregate and after_queue is not None and result["state"] == "done":
            # The whole accepted cup is now confirmed. Record one durable
            # attempt before updating the caller's journal in one transaction.
            # A failed callback is never replayed, including after a restart.
            completed = [indices[step["name"]] for step in queue["steps"] if step["state"] == "done"]
            if len(completed) != len(jobs):
                raise RobotCommandError("Not every coordinated queue job was confirmed.")
            names = [jobs[index]["name"] for index in completed]
            if any(name in result["callback_attempted"] for name in names):
                raise RobotCommandError("A completion callback was already attempted; reconcile its journal instead of replaying it.")
            self._check_stop()
            result["callback_attempted"].extend(names)
            self._write_pending(path, result)
            self._check_stop()
            try:
                after_queue([(original_indices[index], copy.deepcopy(jobs[index])) for index in completed])
            except Exception as error:
                raise RobotCommandError(
                    f"Post-queue check failed after the cup reported DONE: {error}. "
                    "No further commands will be sent."
                ) from error
            self._check_stop()
            result["callback_completed"].extend(names)
            result["callbacks_applied"] = True
            self._write_pending(path, result)
            self._completed_in_process.add(submission_id)
            return
        for step in queue["steps"]:
            if step["state"] != "done":
                continue
            index = indices[step["name"]]
            job = jobs[index]
            if job["name"] in result["callback_attempted"]:
                raise RobotCommandError("A completion callback was already attempted; reconcile its journal instead of replaying it.")
            self._check_stop()
            if job["endpoint"] == self.HOME_ENDPOINT:
                self._verify_home(job["body"]["arm"])
                result["home_verified"].append(job["name"])
            result["callback_attempted"].append(job["name"])
            self._write_pending(path, result)
            self._after_job(original_indices[index], job, after_done)
            result["callback_completed"].append(job["name"])
            self._write_pending(path, result)
        if result["state"] != "done":
            raise RobotCommandError(f"Gateway queue {result['state']}; inspect both arms and preserve recovery.")
        if len(result["callback_completed"]) != len(jobs):
            raise RobotCommandError("Not every coordinated queue job was confirmed.")
        result["callbacks_applied"] = True
        self._write_pending(path, result)
        self._completed_in_process.add(submission_id)

    def _cancel_running_queue(self):
        status = self.queue_status()
        if status["state"] != "running":
            return  # Preserve failed/cancelled history; Stop is not recovery.
        response = None
        try:
            response = requests.delete(f"{self.api}{QUEUE_ENDPOINT}", headers=self.headers,
                                       timeout=self.CONTROL_TIMEOUT)
            response.raise_for_status()
            result = self.queue_status()
            if result["state"] == "running" and result.get("cancel_requested") is not True:
                raise RobotCommandError("Gateway did not confirm cancellation of pending queued work.")
        except requests.RequestException as error:
            raise RobotCommandError("Gateway queue cancellation is unconfirmed; cancellation was not retried.") from error
        finally:
            if response is not None:
                response.close()

    def request_stop(self):
        """Cancel future queued jobs, then stop RAPID; neither request is retried."""
        self.latch_stop()
        if not self._stop_call_lock.acquire(blocking=False):
            raise RobotCommandError("A software stop request is already in progress.")
        try:
            if self.dry_run:
                return {"dry_run": True, "stopped": False,
                        "message": "Dry run: software stop latched; no robot command sent."}
            with self._lease_lock:
                try:
                    if not self._release_needed:
                        self._take_control()
                    cancel_error = None
                    try:
                        self._cancel_running_queue()
                    except RobotCommandError as error:
                        cancel_error = error
                    reply, _ = self._post("/robot/rapid/stop")
                    if reply.get("execution_state") != "stopped":
                        raise RobotCommandError("Software stop was not confirmed by RAPID. Inspect the controller; no retry was sent.")
                    if cancel_error is not None:
                        raise RobotCommandError(f"RAPID reported stopped, but {cancel_error}") from cancel_error
                    return {"dry_run": False, "stopped": True,
                            "message": "Pending queue work cancelled and RAPID reported stopped. Inspect both arms and the cup before recovery."}
                finally:
                    self._stop_finished.set()
                    if self._release_needed:
                        self._release_control()
        finally:
            self._stop_finished.set()
            self._stop_call_lock.release()
