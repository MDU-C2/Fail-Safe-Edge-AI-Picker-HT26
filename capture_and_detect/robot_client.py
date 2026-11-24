"""Guarded RWS arm and gripper jobs with shared control-lease renewal.

Importing this module never contacts the gateway. A failed or timed-out command
is never retried: it may already have reached the physical controller. Closing
this client does not stop physical motion. request_stop sends a separate RAPID
software stop; it is not a hardware emergency stop.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import copy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import threading
import time
from typing import Callable, Mapping, Optional
from urllib.parse import urlsplit

import requests


class RobotCommandError(RuntimeError):
    """A command could not be validated, dispatched, or confirmed complete."""


class GatewayReadUnavailable(RobotCommandError):
    """A status read temporarily failed; no robot command was issued."""


class GatewayControlUnavailable(RobotCommandError):
    """A lease request is unconfirmed; yield and establish ownership afresh."""


class RobotClient:
    CONTROL_TIMEOUT = 5
    # The RWS gateway waits up to 60 seconds for a job to complete.
    COMMAND_TIMEOUT = 90
    MOVE_ENDPOINT = "/robot/rws/move"
    PATH_ENDPOINT = "/robot/rws/path"
    HOME_ENDPOINT = "/robot/rws/home"
    GRIPPER_ENDPOINT = "/robot/rws/gripper"
    MOTION_OPTIONS = {"speed": (1, 1500), "acceleration": (20, 100), "ramp": (10, 100)}
    HOME_JOINTS = (0, -130, 30, 0, 40, 0)
    HOME_ARM_ANGLES = {"left": 135, "right": -135}
    HOME_TOLERANCE_DEGREES = 1.0

    def __init__(self, api: str, headers: Mapping[str, str], dry_run: bool = False):
        self.api = api.rstrip("/")
        self.headers = {key: value for key, value in headers.items() if key.lower() != "connection"}
        self.headers["Connection"] = "close"
        self.dry_run = dry_run
        self._owned_lease = False
        self._release_needed = False
        self._lease_error: Optional[RobotCommandError] = None
        self._lease_deadline: Optional[float] = None
        self._lease_request_started: Optional[float] = None
        self._lease_clock_anchor = None
        self._renew_after = 1.0
        self._execution_lock = threading.Lock()
        self._lease_lock = threading.RLock()
        self._stop_call_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._stop_finished = threading.Event()
        self._stop_finished.set()
        self._operator_gateway = None
        self._capability_error = None

    def configure_operator_controls(self, controller_profile=None):
        """Discover read-only support once; no control lease or motion is sent."""
        # Retain the optional argument for saved launcher configurations. Robot
        # controls use only the original gateway; no direct controller reads.
        return self.refresh_operator_capabilities()

    def refresh_operator_capabilities(self):
        from gateway_capabilities import parse_gateway_capabilities
        if self.dry_run:
            return self.operator_capabilities
        parts = urlsplit(self.api)
        endpoint = f"{parts.scheme}://{parts.netloc}/openapi.json"
        response = None
        self._capability_error = None
        # Invalidate prior support before I/O; a failed refresh cannot preserve
        # a stale confirmation for an endpoint no longer offered by the gateway.
        self._operator_gateway = parse_gateway_capabilities(None, api_prefix=parts.path)
        try:
            response = requests.get(endpoint, headers=self.headers, timeout=self.CONTROL_TIMEOUT,
                                    allow_redirects=False)
            response.raise_for_status()
            if response.status_code != 200 or len(response.content) > 2_000_000:
                raise ValueError("Invalid gateway API specification response")
            self._operator_gateway = parse_gateway_capabilities(response.json(), api_prefix=parts.path)
            if not self._operator_gateway.get("schema_valid"):
                raise ValueError("Gateway API specification is invalid")
        except (requests.RequestException, ValueError):
            self._capability_error = "Gateway controls could not be discovered; refresh robot status."
        finally:
            if response is not None:
                response.close()
        return self.operator_capabilities

    @property
    def operator_capabilities(self):
        from gateway_capabilities import ACTIONS
        if self.dry_run:
            return {name: {"supported": True, "reason": None, "protocol": "preview"} for name in ACTIONS}
        if self._operator_gateway is None:
            return None  # Library clients retain their existing explicit API.
        result = copy.deepcopy(self._operator_gateway["actions"])
        if not self._operator_gateway.get("schema_valid"):
            # Status can rediscover capabilities after a disconnect.
            result["robot_status"].update(supported=None, reason=self._capability_error)
        return result

    def require_control_action(self, action):
        """Reject actions outside the original gateway control contract."""
        from gateway_capabilities import ACTIONS
        if action not in ACTIONS:
            raise RobotCommandError("This robot control is not supported by the original gateway.")
        capabilities = self.operator_capabilities
        if capabilities is not None and capabilities[action]["supported"] is not True:
            raise RobotCommandError(capabilities[action].get("reason") or
                                    "This gateway does not support the requested robot control.")

    @property
    def stop_requested(self):
        return self._stop_event.is_set()

    def latch_stop(self):
        """Inhibit further jobs immediately; no HTTP or motion lock is needed."""
        if not self._stop_event.is_set():
            self._stop_finished.clear()
        self._stop_event.set()

    def _check_stop(self):
        if self.stop_requested:
            raise RobotCommandError("Software stop requested. Inspect the robot before recovery; no further jobs will be sent.")

    def clear_stop(self):
        """Only called after an operator acknowledges recovery while idle."""
        if not self._execution_lock.acquire(blocking=False):
            raise RobotCommandError("Wait for the active command to finish unwinding before recovery.")
        try:
            if not self._stop_call_lock.acquire(blocking=False):
                raise RobotCommandError("Wait for the software stop request to finish before recovery.")
            try:
                if self.stop_requested and not self._stop_finished.is_set():
                    raise RobotCommandError("Wait for the pending software stop request before recovery.")
                self._stop_event.clear()
                self._stop_finished.set()
            finally:
                self._stop_call_lock.release()
        finally:
            self._execution_lock.release()

    def request_stop(self):
        """Send a software RAPID stop independently of any waiting move.

        A failed response is unconfirmed, never retried. An active execution's
        lease is retained until this request finishes; an idle request acquires
        its own lease. Dry runs perform no HTTP.
        """
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
                    reply, _ = self._post("/robot/rapid/stop")
                    if reply.get("execution_state") != "stopped":
                        raise RobotCommandError("Software stop was not confirmed by RAPID. Inspect the controller; no retry was sent.")
                    return {"dry_run": False, "stopped": True,
                            "message": "RAPID reported stopped. Inspect both arms and the cup before acknowledging recovery."}
                finally:
                    self._stop_finished.set()
                    if self._release_needed:
                        self._release_control()
        finally:
            self._stop_finished.set()
            self._stop_call_lock.release()

    def _get_object(self, endpoint):
        response = None
        try:
            response = requests.get(f"{self.api}{endpoint}", headers=self.headers,
                                    timeout=self.CONTROL_TIMEOUT)
            response.raise_for_status()
            reply = response.json()
            if not isinstance(reply, dict):
                raise RobotCommandError(f"Gateway {endpoint} returned invalid status.")
            return reply
        except (requests.RequestException, ValueError) as exc:
            error_type = (GatewayReadUnavailable if self._temporary_transport_failure(exc, response)
                          else RobotCommandError)
            raise error_type(f"Gateway {endpoint} status failed: {exc}{self._response_detail(response)}") from exc
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def _control_status_fields(reply):
        fields = ("execution_state", "controller_state", "operation_mode")
        if any(not isinstance(reply.get(name), str) or not reply[name] for name in fields):
            raise RobotCommandError("Gateway returned incomplete controller status.")
        return {name: reply[name] for name in fields}

    def read_control_status(self):
        """Read current controller state; dry-run/demo never contacts hardware."""
        if self.dry_run:
            return {"execution_state": "simulated", "controller_state": "simulated",
                    "operation_mode": "simulated",
                    "read_at": datetime.now(timezone.utc).isoformat(), "dry_run": True}
        result = self._control_status_fields(self._get_object("/robot/rapid"))
        result["read_at"] = datetime.now(timezone.utc).isoformat()
        return result

    def _post(self, endpoint: str, *, move: Optional[dict] = None,
              allow_conflict: bool = False):
        headers = dict(self.headers)
        options = {"headers": headers, "timeout": self.CONTROL_TIMEOUT}
        if move is not None:
            options.update(json=move, timeout=self.COMMAND_TIMEOUT)
        response = None
        try:
            if endpoint in ("/control/acquire", "/control/renew"):
                self._lease_request_started = time.monotonic()
            response = requests.post(f"{self.api}{endpoint}", **options)
            if allow_conflict and response.status_code == 409:
                return None, None
            response.raise_for_status()
            reply = response.json()
            if not isinstance(reply, dict):
                raise RobotCommandError(f"Gateway {endpoint} returned a non-object JSON reply.")
            return reply, dict(response.headers)
        except (requests.RequestException, ValueError) as exc:
            detail = (" Robot completion is unknown; the command was not retried."
                      if move is not None else " No further commands will be sent.")
            # Lease requests cannot move the robot. Their lost replies require
            # fresh ownership checks; physical POSTs remain ambiguous and must
            # never be labelled as a read/control operation that can be retried.
            error_type = (GatewayControlUnavailable
                          if endpoint in {"/control/acquire", "/control/renew", "/control/release"}
                          and self._temporary_transport_failure(exc, response) else RobotCommandError)
            raise error_type(f"Gateway {endpoint} failed: {exc}{self._response_detail(response)}.{detail}") from exc
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def _temporary_transport_failure(error, response):
        code = getattr(response, "status_code", None)
        if isinstance(code, int) and (code in {408, 429} or 500 <= code < 600):
            return True
        return isinstance(error, (requests.ConnectionError, requests.Timeout,
                                  requests.exceptions.ChunkedEncodingError,
                                  requests.exceptions.ContentDecodingError))

    @staticmethod
    def _response_detail(response):
        """Preserve the gateway's bounded refusal reason, e.g. Local Client privileges."""
        if response is None:
            return ""
        try:
            value = response.json()
        except (ValueError, requests.RequestException):
            return ""
        detail = value.get("detail") if isinstance(value, dict) else None
        return f" ({detail[:500]})" if isinstance(detail, str) else ""

    def _record_lease(self, reply: dict, response_headers: Mapping[str, str]) -> None:
        lease = reply.get("lease")
        if (reply.get("success") is not True or not isinstance(lease, dict)
                or lease.get("active") is not True):
            raise RobotCommandError(f"Gateway did not confirm an active control lease: {reply}")

        renew_after, deadline = 1.0, None
        anchor = self._lease_clock_anchor
        expires_at = lease.get("expires_at")
        if expires_at is None and anchor is not None:
            raise RobotCommandError("Gateway omitted lease expiry during renewal.")
        if expires_at is not None:
            try:
                expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    raise ValueError("lease expiry has no timezone")
                date_header = next((value for key, value in response_headers.items()
                                    if key.lower() == "date"), None)
                now = time.monotonic()
                if anchor is not None or (not date_header and lease.get("acquired_at") is not None):
                    # Lynx may have an unset UTC clock and omit HTTP Date. Bind
                    # its acquisition timestamp to our request START, so all
                    # request/response latency consumes the lease budget.
                    acquired = datetime.fromisoformat(str(lease.get("acquired_at")).replace("Z", "+00:00"))
                    if acquired.tzinfo is None or self._lease_request_started is None:
                        raise ValueError("lease acquisition has no clock reference")
                    if anchor is None:
                        if self._owned_lease:
                            raise ValueError("cannot establish acquisition clock during renewal")
                        duration = (expires - acquired).total_seconds()
                        if duration <= 0:
                            raise RobotCommandError("Gateway returned an expired control lease.")
                        anchor = (acquired, self._lease_request_started, duration)
                    if acquired != anchor[0]:
                        raise ValueError("lease acquisition changed during renewal")
                    # Renewals preserve acquired_at. Never treat the growing
                    # acquired-to-expiry span as a new duration. Cap a forward
                    # server-clock jump at the original lease length as well.
                    deadline = min(anchor[1] + (expires - anchor[0]).total_seconds(),
                                   self._lease_request_started + anchor[2])
                    remaining = deadline - now
                else:
                    # Gateways with HTTP Date retain their server-clock path.
                    server_now = (parsedate_to_datetime(date_header) if date_header
                                  else datetime.now(timezone.utc))
                    remaining = (expires - server_now).total_seconds()
                    deadline = now + remaining
            except (TypeError, ValueError, OverflowError) as exc:
                raise RobotCommandError("Gateway returned an invalid lease expiry.") from exc
            if remaining <= 0:
                raise RobotCommandError("Gateway returned an expired control lease.")
            renew_after = max(0.01, min(2.0, remaining / 3.0))
        self._renew_after = renew_after
        self._lease_deadline = deadline
        self._lease_clock_anchor = anchor
        self._owned_lease = True

    def _take_control(self) -> None:
        with self._lease_lock:
            self._take_control_locked()

    def _take_control_locked(self) -> None:
        # Only reuse a lease this instance previously acquired. A fresh client
        # never guesses ownership from a shared token or from a 409 response.
        if self._owned_lease:
            reply, response = self._post("/control/renew", allow_conflict=True)
            if reply is not None:
                self._release_needed = True
                self._record_lease(reply, response)
                return
            self._owned_lease = False
            self._lease_clock_anchor = None
        reply, response = self._post("/control/acquire")
        # Even if expiry metadata is invalid, release a confirmed acquisition.
        self._release_needed = (reply.get("success") is True
                                and isinstance(reply.get("lease"), dict)
                                and reply["lease"].get("active") is True)
        self._record_lease(reply, response)

    def _release_control(self) -> None:
        with self._lease_lock:
            # Stop may have been latched by the UI before its separate worker
            # reaches request_stop. Transfer cleanup responsibility to it.
            if self.stop_requested and not self._stop_finished.is_set():
                return
            if not self._release_needed:
                return
            self._release_control_locked()

    def _release_control_locked(self) -> None:
        try:
            reply, _ = self._post("/control/release")
            if reply.get("success") is not True:
                raise RobotCommandError(f"Gateway did not confirm release: {reply}")
        except RobotCommandError as exc:
            # Releasing ownership must never replace a motion error or an
            # interrupt. The owner-only endpoint cannot release another owner.
            print(f"Warning: control release failed: {exc} The lease may remain until expiry.")
        finally:
            self._owned_lease = False
            self._release_needed = False
            self._lease_deadline = None
            self._lease_request_started = None
            self._lease_clock_anchor = None

    def _renew_loop(self, stop: threading.Event) -> None:
        while not stop.wait(self._renew_after):
            try:
                with self._lease_lock:
                    if self.stop_requested:
                        return
                    reply, response = self._post("/control/renew")
                    self._record_lease(reply, response)
            except RobotCommandError as exc:
                self._owned_lease = False
                self._lease_error = RobotCommandError(
                    f"Control renewal failed: {exc} No further commands will be sent; "
                    "an already dispatched movement may still be running."
                )
                return

    def _check_lease(self) -> None:
        if self._lease_error is not None:
            raise self._lease_error
        if self._lease_deadline is not None and time.monotonic() >= self._lease_deadline:
            self._owned_lease = False
            raise RobotCommandError(
                "The last confirmed control lease expired. No further commands will be sent; "
                "an already dispatched movement may still be running."
            )

    @classmethod
    def _validate_motion(cls, body, *, home=False, left_only=False):
        required = {"arm", "wait"}
        coordinates = () if home else ("x", "y", "z", "rx", "ry", "rz")
        required.update(coordinates)
        if (not isinstance(body, dict) or not required <= body.keys()
                or body.keys() - required - cls.MOTION_OPTIONS.keys()):
            raise RobotCommandError(
                f"Each {'home job' if home else 'move'} must contain only arm, wait, "
                + ("" if home else "x/y/z, rx/ry/rz, ") +
                "and optional speed, acceleration, ramp. Raw robot commands are not accepted."
            )
        arms = ("left",) if left_only else ("left", "right")
        if body["arm"] not in arms or body["wait"] is not True:
            raise RobotCommandError(
                "Only left-arm moves with wait=True are supported." if left_only else
                "Motion jobs require arm='left' or 'right' and wait=True."
            )
        for name in (*coordinates, *cls.MOTION_OPTIONS):
            if name not in body:
                continue
            value = body[name]
            try:
                finite = (not isinstance(value, bool) and isinstance(value, (int, float))
                          and math.isfinite(value))
            except OverflowError:
                finite = False
            if not finite:
                raise RobotCommandError(f"Move {name} must be a finite number.")
            if name in cls.MOTION_OPTIONS:
                low, high = cls.MOTION_OPTIONS[name]
                if not low <= value <= high:
                    raise RobotCommandError(f"Move {name} must be between {low} and {high}.")
        return dict(body)

    @classmethod
    def _validate_move(cls, move):
        """Retain the original hover-only interface's left-arm restriction."""
        return cls._validate_motion(move, left_only=True)

    @classmethod
    def _validate_path(cls, body):
        """Accept only bounded structured paths; never raw RAPID commands.

        A path remains one blocking job: the controller must stop at its final
        point and report DONE before any dependent arm or gripper can proceed.
        Validate and copy every point before acquiring the shared lease.
        """
        required = {"arm", "wait", "points"}
        options = {**cls.MOTION_OPTIONS, "zone": (0, 100), "max_acceleration": (0.1, 20)}
        if (not isinstance(body, dict) or not required <= body.keys()
                or body.keys() - required - options.keys()):
            raise RobotCommandError(
                "Path jobs require only arm, wait, points and optional speed, acceleration, "
                "ramp, zone, max_acceleration. Raw robot commands are not accepted."
            )
        if body["arm"] not in ("left", "right") or body["wait"] is not True:
            raise RobotCommandError("Path jobs require arm='left' or 'right' and wait=True.")
        points = body["points"]
        if not isinstance(points, list) or not 1 <= len(points) <= 8:
            raise RobotCommandError("Path points must be a list containing between 1 and 8 points.")

        def number(value, name, limits=None):
            try:
                finite = (not isinstance(value, bool) and isinstance(value, (int, float))
                          and math.isfinite(value))
            except OverflowError:
                finite = False
            if not finite:
                raise RobotCommandError(f"Path {name} must be a finite number.")
            if limits is not None and not limits[0] <= value <= limits[1]:
                raise RobotCommandError(f"Path {name} must be between {limits[0]} and {limits[1]}.")

        for name, limits in options.items():
            if name in body:
                number(body[name], name, limits)
        coordinates = {"x", "y", "z", "rx", "ry", "rz"}
        point_options = {"speed": cls.MOTION_OPTIONS["speed"], "zone": (0, 100)}
        validated = []
        for index, point in enumerate(points):
            if (not isinstance(point, dict) or not coordinates <= point.keys()
                    or point.keys() - coordinates - point_options.keys() - {"linear"}):
                raise RobotCommandError(
                    f"Path point {index + 1} requires only x/y/z, rx/ry/rz "
                    "and optional linear, speed, zone."
                )
            if "linear" in point and type(point["linear"]) is not bool:
                raise RobotCommandError(f"Path point {index + 1} linear must be a boolean.")
            for name in coordinates:
                number(point[name], f"point {index + 1} {name}")
            for name, limits in point_options.items():
                if name in point:
                    number(point[name], f"point {index + 1} {name}", limits)
            validated.append(dict(point))
        return {**body, "points": validated}

    @classmethod
    def _validate_job(cls, job):
        if (not isinstance(job, dict) or not {"name", "endpoint", "body"} <= set(job)
                or set(job) - {"name", "endpoint", "body", "after", "group"}):
            raise RobotCommandError("Each job must contain name, endpoint and body, and optional after dependencies or group.")
        if not isinstance(job["name"], str) or not job["name"].strip():
            raise RobotCommandError("Each job requires a nonempty step name.")
        group = job.get("group")
        if group is not None and (not isinstance(group, str) or not group.strip()):
            raise RobotCommandError("A job group must be a nonempty string.")
        if group is not None and (not isinstance(job["body"], dict)
                                  or job["body"].get("arm") not in ("left", "right")):
            raise RobotCommandError(f"Grouped job '{job['name']}' must name its arm, 'left' or 'right'.")
        if group is not None and "after" in job:
            raise RobotCommandError("Plans cannot mix group stages with after dependencies.")
        if "after" in job:
            after = job["after"]
            if (not isinstance(after, list) or any(not isinstance(name, str) or not name.strip() for name in after)
                    or len(after) != len(set(after)) or job["name"] in after):
                raise RobotCommandError("Step dependencies must be distinct other step names.")
        endpoint = job["endpoint"]
        if endpoint == cls.MOVE_ENDPOINT:
            body = cls._validate_motion(job["body"])
        elif endpoint == cls.PATH_ENDPOINT:
            body = cls._validate_path(job["body"])
        elif endpoint == cls.HOME_ENDPOINT:
            body = cls._validate_motion(job["body"], home=True)
        elif endpoint == cls.GRIPPER_ENDPOINT:
            body = job["body"]
            if (not isinstance(body, dict) or not {"arm", "wait", "action"} <= body.keys()
                    or body.keys() - {"arm", "wait", "action", "position"}
                    or body["arm"] not in ("left", "right") or body["wait"] is not True
                    or body["action"] not in ("open", "grip", "move")):
                raise RobotCommandError(
                    "Gripper jobs require arm='left' or 'right', wait=True, "
                    "and action='open', 'grip' or 'move'; only move accepts position."
                )
            if (body["action"] == "move") != ("position" in body):
                raise RobotCommandError("Gripper position is required for move and forbidden for open/grip.")
            if "position" in body:
                value = body["position"]
                try:
                    valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                             and math.isfinite(value) and 0 <= value <= 25)
                except OverflowError:
                    valid = False
                if not valid:
                    raise RobotCommandError("Gripper position must be a finite number from 0 to 25 mm.")
            body = dict(body)
        else:
            raise RobotCommandError("Only RWS move, path, home and gripper endpoints are supported.")
        result = {"name": job["name"], "endpoint": endpoint, "body": body}
        if "after" in job:
            result["after"] = list(job["after"])
        if group is not None:
            result["group"] = group
        return result

    @staticmethod
    def _copy_job(job):
        # Path points are nested; callback mutations must never change a
        # validated plan, an in-flight request, or the completion record.
        return copy.deepcopy(job)

    @staticmethod
    def _job_dependencies(jobs):
        """Validate the whole graph before acquiring control or dispatching.

        Legacy plans remain serial. Explicit plans keep each arm's list order
        in addition to their handover barriers, with one active job per arm.
        """
        if any("group" in job for job in jobs) and any("after" in job for job in jobs):
            raise RobotCommandError("Plans cannot mix group stages with after dependencies.")
        if not any("after" in job for job in jobs):
            return None
        if not all("after" in job for job in jobs):
            raise RobotCommandError("Parallel plans must specify dependencies for every step.")
        names = {job["name"] for job in jobs}
        if len(names) != len(jobs):
            raise RobotCommandError("Parallel plans require unique step names.")
        dependencies, previous = {}, {}
        for job in jobs:
            required = set(job["after"])
            if not required <= names:
                raise RobotCommandError(f"Step '{job['name']}' has an unknown dependency.")
            arm = job["body"]["arm"]
            if arm in previous:
                required.add(previous[arm])
            dependencies[job["name"]] = required
            previous[arm] = job["name"]
        remaining, confirmed = dict(dependencies), set()
        while remaining:
            ready = {name for name, required in remaining.items() if required <= confirmed}
            if not ready:
                raise RobotCommandError("Step dependencies contain a cycle or conflict with arm order.")
            confirmed.update(ready)
            remaining = {name: required for name, required in remaining.items() if name not in ready}
        return dependencies

    def _check_motion_height(self, job):
        """Apply V4's height rule to the move about to be dispatched."""
        if job["endpoint"] not in (self.MOVE_ENDPOINT, self.PATH_ENDPOINT):
            return
        pick = job["endpoint"] == self.MOVE_ENDPOINT and job["name"] in {
            "descend_to_cup", "replace_cup", "right_place"}
        points = job["body"].get("points", [job["body"]])
        for index, point in enumerate(points):
            # A smooth approach includes travel and descent in one job. Only
            # its final linear picking/placing target gets the existing low-Z
            # exception; intermediate transit targets keep the travel limit.
            path_pick = (job["endpoint"] == self.PATH_ENDPOINT
                         and (job["name"], job["body"]["arm"]) in {
                             ("approach_cup", "left"), ("right_place", "right")}
                         and index == len(points) - 1 and point.get("linear") is True)
            if point["z"] > 700 or (point["z"] < 199 and not (pick or path_pick)):
                raise RobotCommandError(
                    f"Step '{job['name']}' flange Z {point['z']} refused: "
                    "ordinary motion requires 199..700 mm; only picking/placing may go lower.")

    def _perform_job(self, job):
        """One blocking request; confirmation never infers or retries motion."""
        self._check_stop()
        self._check_lease()
        self._check_motion_height(job)
        reply, _ = self._post(job["endpoint"], move=job["body"])
        self._check_stop()
        print(f"Sent: POST {job['endpoint']} {json.dumps(job['body'], sort_keys=True)}"
              f" -> {reply} ({job['name']})")
        if reply.get("accepted") is not True or reply.get("message") != "DONE":
            raise RobotCommandError(
                f"Step '{job['name']}' did not report successful completion: {reply}. "
                "No further commands will be sent."
            )
        if job["endpoint"] == self.HOME_ENDPOINT:
            self._verify_home(job["body"]["arm"])
        self._check_stop()

    def _before_job(self, index, job, callback):
        if callback is None:
            return job
        try:
            replacement = callback(index, self._copy_job(job))
            candidate = job if replacement is None else self._validate_job(replacement)
            if (candidate["endpoint"] != job["endpoint"]
                    and self.PATH_ENDPOINT in (candidate["endpoint"], job["endpoint"])):
                raise RobotCommandError("Pre-command replacement cannot change a blended path endpoint.")
            return candidate
        except Exception as exc:
            raise RobotCommandError(f"Pre-command check failed: {exc}") from exc

    def _after_job(self, index, job, callback):
        if callback is not None:
            try:
                callback(index, self._copy_job(job))
            except Exception as exc:
                raise RobotCommandError(
                    f"Post-command check failed after '{job['name']}' reported DONE: {exc}. "
                    "No further commands will be sent."
                ) from exc

    def _execute_parallel_jobs(self, jobs, dependencies, before_send, after_done):
        """Overlap independent arms while keeping callbacks on this thread.

        Only requests already submitted may finish after a peer fails. Drain
        those requests and record confirmed completions before releasing the
        shared lease; no dependent or later job is dispatched after failure.
        """
        pending = dict(enumerate(jobs))
        active, completed, reported = {}, set(), set()
        aborted = threading.Event()
        failure_lock, failures = threading.Lock(), []

        def check():
            with failure_lock:
                if failures:
                    raise failures[0]
            self._check_stop()
            self._check_lease()

        def run(job):
            try:
                if aborted.is_set():
                    raise RobotCommandError("Another step failed; no further commands will be sent.")
                self._perform_job(job)
            except BaseException as error:
                with failure_lock:
                    if not failures:
                        failures.append(error)
                    aborted.set()
                raise

        pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="robot-arm")
        try:
            while pending or active:
                check()
                for future in list(active):
                    if not future.done():
                        continue
                    index, job = active[future]
                    future.result()
                    reported.add(index)  # A failed persistence callback is never retried.
                    self._after_job(index, job, after_done)
                    completed.add(job["name"])
                    del active[future]
                check()
                busy_arms = {job["body"]["arm"] for _, job in active.values()}
                for index, job in list(pending.items()):
                    if job["body"]["arm"] in busy_arms or not dependencies[job["name"]] <= completed:
                        continue
                    check()
                    candidate = self._before_job(index, job, before_send)
                    if (candidate["name"] != job["name"] or candidate["body"]["arm"] != job["body"]["arm"]
                            or candidate.get("after") != job["after"]):
                        raise RobotCommandError("Pre-command replacement changed the parallel step identity or dependencies.")
                    check()
                    future = pool.submit(run, candidate)
                    active[future] = (index, candidate)
                    busy_arms.add(candidate["body"]["arm"])
                    del pending[index]
                if active:
                    wait(active, timeout=0.05, return_when=FIRST_COMPLETED)
                elif pending:
                    raise RobotCommandError("No executable step remains in the parallel plan.")
        except BaseException:
            aborted.set()
            pool.shutdown(wait=True, cancel_futures=True)
            # A peer may have completed while the failure was being handled.
            # Record that fact, but never mask the original failure or schedule
            # another job. Failed/unconfirmed steps remain pending for recovery.
            for future, (index, job) in active.items():
                if index in reported or future.cancelled():
                    continue
                try:
                    future.result()
                    reported.add(index)
                    self._after_job(index, job, after_done)
                except BaseException:
                    pass
            raise
        finally:
            aborted.set()
            pool.shutdown(wait=True, cancel_futures=True)

    @staticmethod
    def _stages(jobs):
        """Consecutive equal groups are a stage; ungrouped jobs stand alone."""
        stages = []
        for index, job in enumerate(jobs):
            group = job.get("group")
            if group is not None and stages and stages[-1][0][1].get("group") == group:
                stages[-1].append((index, job))
            else:
                stages.append([(index, job)])
        return stages

    def _prepare_grouped_job(self, index, job, before_send, hooks):
        self._check_stop()
        self._check_lease()
        with hooks:
            candidate = self._before_job(index, job, before_send)
        if (candidate.get("group") != job.get("group")
                or candidate["body"].get("arm") != job["body"].get("arm")):
            raise RobotCommandError("A replacement job may not change its arm or group.")
        if "after" in candidate:
            raise RobotCommandError("Plans cannot mix group stages with after dependencies.")
        self._check_stop()
        self._check_lease()
        return candidate

    def _send_grouped_job(self, index, job, after_done, hooks):
        self._perform_job(job)
        with hooks:
            self._after_job(index, job, after_done)
        self._check_stop()
        self._check_lease()

    def _execute_grouped_stage(self, lanes, before_send, after_done, hooks):
        """Preserve the RobotStudio group protocol, with one worker per arm.

        All first hooks finish in plan order before either arm is dispatched.
        Later hooks are serialized between lanes. Failures and interrupts stop
        pending work and drain requests already sent before control is released.
        """
        first = [self._prepare_grouped_job(lane[0][0], lane[0][1], before_send, hooks)
                 for lane in lanes]
        failed, errors, error_lock = threading.Event(), [], threading.Lock()
        launch_ready = threading.Event()

        def run(lane, prepared, finished):
            try:
                # A start can raise after spawning its worker. No arm may send
                # until every successful start has its completion registered.
                launch_ready.wait()
                for position, (index, job) in enumerate(lane):
                    if failed.is_set():
                        return
                    candidate = (prepared if position == 0 else
                                 self._prepare_grouped_job(index, job, before_send, hooks))
                    if failed.is_set():
                        return
                    self._send_grouped_job(index, candidate, after_done, hooks)
            except BaseException as error:
                with error_lock:
                    if not errors:
                        errors.append(error)
                    failed.set()
            finally:
                finished.set()

        finished = []
        try:
            for lane, prepared in zip(lanes, first):
                done = threading.Event()
                thread = threading.Thread(target=run, args=(lane, prepared, done),
                                          name="robot-group-arm", daemon=True)
                thread.start()
                finished.append(done)
            launch_ready.set()
            self._wait_grouped_workers(finished)
        except BaseException:
            failed.set()
            raise
        finally:
            # Completion events reflect actual worker exit even if Ctrl+C has
            # interrupted Python's Thread.join/is_alive bookkeeping. Repeated
            # interrupts must not release control while a request is in flight.
            failed.set()
            # Release even an unregistered worker whose start was interrupted;
            # the closed launch gate kept it from sending, and it now exits.
            launch_ready.set()
            while not all(done.is_set() for done in finished):
                try:
                    for done in finished:
                        done.wait(0.05)
                except KeyboardInterrupt:
                    failed.set()
        if errors:
            raise errors[0]

    def _wait_grouped_workers(self, finished):
        for done in finished:
            while not done.wait(0.05):
                self._check_stop()
                self._check_lease()

    def _execute_grouped_jobs(self, jobs, before_send, after_done):
        hooks = threading.Lock()
        for stage in self._stages(jobs):
            self._check_stop()
            self._check_lease()
            lanes = {}
            for index, job in stage:
                lanes.setdefault(job["body"].get("arm", "left"), []).append((index, job))
            if len(lanes) == 1:
                for index, job in stage:
                    candidate = self._prepare_grouped_job(index, job, before_send, hooks)
                    self._send_grouped_job(index, candidate, after_done, hooks)
            else:
                self._execute_grouped_stage(list(lanes.values()), before_send, after_done, hooks)

    def read_gripper_position(self, arm):
        """Optional read-only telemetry; unknown readings never trigger a retry.

        The reported finger position does not prove that a cup is held. No HTTP
        is sent in dry-run mode, and a software stop remains an execution error.
        """
        if arm not in ("left", "right"):
            raise RobotCommandError("Gripper telemetry requires arm='left' or 'right'.")
        self._check_stop()
        if self.dry_run:
            return None
        response = None
        position = None
        try:
            response = requests.get(f"{self.api}/robot/arms/{arm}/gripper", headers=self.headers,
                                    timeout=self.CONTROL_TIMEOUT)
            response.raise_for_status()
            reply = response.json()
            value = reply.get("position") if isinstance(reply, dict) else None
            if (isinstance(reply, dict) and reply.get("arm") == arm
                    and not isinstance(value, bool) and isinstance(value, (int, float))
                    and math.isfinite(value) and 0 <= value <= 25):
                position = float(value)
        except (requests.RequestException, ValueError, OverflowError):
            pass
        finally:
            if response is not None:
                response.close()
        self._check_stop()
        return position

    def _verify_home(self, arm):
        """Independently confirm the deployed controller reached the expected home pose."""
        endpoint = f"/robot/arms/{arm}/joints"
        response = None
        try:
            response = requests.get(f"{self.api}{endpoint}", headers=self.headers,
                                    timeout=self.CONTROL_TIMEOUT)
            response.raise_for_status()
            reply = response.json()
            if (not isinstance(reply, dict) or reply.get("arm") != arm
                    or not isinstance(reply.get("joints"), list) or len(reply["joints"]) != 6):
                raise RobotCommandError(f"Home verification returned invalid joints for the {arm} arm.")
            actual = reply["joints"] + [reply.get("arm_angle")]
            expected = [*self.HOME_JOINTS, self.HOME_ARM_ANGLES[arm]]
            for index, (value, target) in enumerate(zip(actual, expected)):
                try:
                    finite = (not isinstance(value, bool) and isinstance(value, (int, float))
                              and math.isfinite(value))
                except OverflowError:
                    finite = False
                if not finite or abs(value - target) > self.HOME_TOLERANCE_DEGREES:
                    axis = f"joint {index + 1}" if index < 6 else "arm angle"
                    raise RobotCommandError(
                        f"The {arm} arm reported DONE but its home pose was not confirmed: "
                        f"{axis} is {value!r}, expected {target} +/- "
                        f"{self.HOME_TOLERANCE_DEGREES} degrees. No further commands will be sent."
                    )
            print(f"Verified {arm} home joint pose.")
        except (requests.RequestException, ValueError) as exc:
            error_type = (GatewayReadUnavailable if self._temporary_transport_failure(exc, response)
                          else RobotCommandError)
            raise error_type(
                f"Home verification for the {arm} arm failed: {exc}. "
                "The arm reported DONE but its home pose is unconfirmed; "
                "no further commands will be sent."
            ) from exc
        finally:
            if response is not None:
                response.close()

    def execute(self, moves: list[dict],
                before_send: Optional[Callable[[], Optional[dict]]] = None) -> None:
        """Run movement-only requests in order; require accepted=True and DONE.

        ``before_send`` takes no arguments and may raise to prevent a move, or
        return a replacement move dictionary. None keeps the planned move.
        Replacements receive the same validation as initial moves. The callback
        runs after control acquisition, immediately before each live move.
        Dry-run prints endpoint/JSON without acquiring control or running callbacks.
        """
        if not isinstance(moves, list):
            raise RobotCommandError("Provide a list of movement dictionaries.")
        moves = [self._validate_move(move) for move in moves]
        jobs = [{"name": f"move {index + 1}", "endpoint": self.MOVE_ENDPOINT, "body": move}
                for index, move in enumerate(moves)]

        def check(_index, job):
            replacement = before_send()
            if replacement is not None:
                return {**job, "body": self._validate_move(replacement)}
            return None

        self.execute_jobs(jobs, before_send=check if before_send is not None else None)

    def execute_jobs(self, jobs: list[dict],
                     before_send: Optional[Callable[[int, dict], Optional[dict]]] = None,
                     after_done: Optional[Callable[[int, dict], None]] = None) -> None:
        """Run named jobs under one renewed control lease.

        Validate the complete plan before acquiring control. Every job requires
        wait=True and an exact accepted=True / DONE response. No failed command
        is retried and no recovery moves are added. Paths require
        blocking completion just like individual moves. Hooks receive a zero-based
        index and a copy of the job; before_send may return a replacement job,
        which is validated before dispatch. after_done runs only after confirmed
        completion (and measured home joints for home jobs); raising from either
        hook prevents all later jobs. Dry-run validates and prints the plan
        without HTTP or either hook. Plans with explicit ``after`` dependencies
        may overlap different arms. Per-arm order remains sequential, and hooks
        always run on the coordinator thread with original plan indices.

        The separate RobotStudio plans can instead use consecutive ``group``
        stages, with a serial lane per arm and both lanes completed before the
        next stage. All first hooks finish before any stage request; later
        group hooks are serialized on the worker threads. Group and dependency
        plans cannot be combined.
        """
        if not isinstance(jobs, list):
            raise RobotCommandError("Provide a list of named RWS jobs.")
        jobs = [self._validate_job(job) for job in jobs]
        dependencies = self._job_dependencies(jobs)
        if not jobs:
            return
        self._check_stop()
        if not self._execution_lock.acquire(blocking=False):
            raise RobotCommandError("This client is already executing a command sequence.")
        stop = threading.Event()
        renewal = None
        try:
            if self.dry_run:
                for job in jobs:
                    self._check_stop()
                    together = f" [together: {job['group']}]" if job.get("group") else ""
                    print(f"DRY RUN: POST {job['endpoint']} "
                          f"{json.dumps(job['body'], sort_keys=True)} ({job['name']}){together}")
                    if dependencies is not None:
                        print(f"  after: {', '.join(sorted(dependencies[job['name']])) or 'start'}")
                self._check_stop()
                return
            with self._lease_lock:
                self._check_stop()
                self._lease_error = None
                self._release_needed = False
                self._take_control()
            self._check_stop()
            renewal = threading.Thread(target=self._renew_loop, args=(stop,), daemon=True)
            renewal.start()
            if dependencies is not None:
                self._execute_parallel_jobs(jobs, dependencies, before_send, after_done)
                self._check_stop()
                self._check_lease()
                return
            if any("group" in job for job in jobs):
                self._execute_grouped_jobs(jobs, before_send, after_done)
                self._check_stop()
                self._check_lease()
                return
            for index, job in enumerate(jobs):
                self._check_stop()
                self._check_lease()
                job = self._before_job(index, job, before_send)
                self._check_lease()
                self._check_stop()
                self._perform_job(job)
                self._after_job(index, job, after_done)
                self._check_lease()
                self._check_stop()
        finally:
            stop.set()
            try:
                if renewal is not None:
                    renewal.join()
                if self._release_needed:
                    self._release_control()
            finally:
                self._execution_lock.release()
