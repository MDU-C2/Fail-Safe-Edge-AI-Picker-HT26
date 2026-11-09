"""Sequential RWS arm and gripper jobs with control-lease renewal.

Importing this module never contacts the gateway. A failed or timed-out command
is never retried: it may already have reached the physical controller. Stopping
this client only prevents further commands; it is not a robot emergency stop.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import threading
import time
from typing import Callable, Mapping, Optional

import requests


class RobotCommandError(RuntimeError):
    """A command could not be validated, dispatched, or confirmed complete."""


class RobotClient:
    CONTROL_TIMEOUT = 5
    # The RWS gateway waits up to 60 seconds for a job to complete.
    COMMAND_TIMEOUT = 90
    MOVE_ENDPOINT = "/robot/rws/move"
    HOME_ENDPOINT = "/robot/rws/home"
    GRIPPER_ENDPOINT = "/robot/rws/gripper"
    MOTION_OPTIONS = {"speed": (1, 1500), "acceleration": (20, 100), "ramp": (10, 100)}
    HOME_JOINTS = (0, -130, 30, 0, 40, 0)
    HOME_ARM_ANGLES = {"left": 135, "right": -135}
    HOME_TOLERANCE_DEGREES = 1.0

    def __init__(self, api: str, headers: Mapping[str, str], dry_run: bool = False):
        self.api = api.rstrip("/")
        self.headers = dict(headers)
        self.dry_run = dry_run
        self._owned_lease = False
        self._release_needed = False
        self._lease_error: Optional[RobotCommandError] = None
        self._lease_deadline: Optional[float] = None
        self._renew_after = 1.0
        self._execution_lock = threading.Lock()

    def _post(self, endpoint: str, *, move: Optional[dict] = None,
              allow_conflict: bool = False):
        options = {"headers": self.headers, "timeout": self.CONTROL_TIMEOUT}
        if move is not None:
            options.update(json=move, timeout=self.COMMAND_TIMEOUT)
        response = None
        try:
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
            raise RobotCommandError(f"Gateway {endpoint} failed: {exc}.{detail}") from exc
        finally:
            if response is not None:
                response.close()

    def _record_lease(self, reply: dict, response_headers: Mapping[str, str]) -> None:
        lease = reply.get("lease")
        if (reply.get("success") is not True or not isinstance(lease, dict)
                or lease.get("active") is not True):
            raise RobotCommandError(f"Gateway did not confirm an active control lease: {reply}")

        self._renew_after = 1.0
        self._lease_deadline = None
        expires_at = lease.get("expires_at")
        if expires_at is not None:
            try:
                expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    raise ValueError("lease expiry has no timezone")
                # Prefer the gateway clock to tolerate a Pi/Mac clock difference.
                date_header = next((value for key, value in response_headers.items()
                                    if key.lower() == "date"), None)
                server_now = (parsedate_to_datetime(date_header) if date_header
                              else datetime.now(timezone.utc))
                remaining = (expires - server_now).total_seconds()
            except (TypeError, ValueError, OverflowError) as exc:
                raise RobotCommandError("Gateway returned an invalid lease expiry.") from exc
            if remaining <= 0:
                raise RobotCommandError("Gateway returned an expired control lease.")
            self._renew_after = max(0.01, min(2.0, remaining / 3.0))
            self._lease_deadline = time.monotonic() + remaining
        self._owned_lease = True

    def _take_control(self) -> None:
        # Only reuse a lease this instance previously acquired. A fresh client
        # never guesses ownership from a shared token or from a 409 response.
        if self._owned_lease:
            reply, response = self._post("/control/renew", allow_conflict=True)
            if reply is not None:
                self._release_needed = True
                self._record_lease(reply, response)
                return
            self._owned_lease = False
        reply, response = self._post("/control/acquire")
        # Even if expiry metadata is invalid, release a confirmed acquisition.
        self._release_needed = (reply.get("success") is True
                                and isinstance(reply.get("lease"), dict)
                                and reply["lease"].get("active") is True)
        self._record_lease(reply, response)

    def _release_control(self) -> None:
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

    def _renew_loop(self, stop: threading.Event) -> None:
        while not stop.wait(self._renew_after):
            try:
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
    def _validate_job(cls, job):
        if not isinstance(job, dict) or set(job) != {"name", "endpoint", "body"}:
            raise RobotCommandError("Each job must contain exactly name, endpoint and body.")
        if not isinstance(job["name"], str) or not job["name"].strip():
            raise RobotCommandError("Each job requires a nonempty step name.")
        endpoint = job["endpoint"]
        if endpoint == cls.MOVE_ENDPOINT:
            body = cls._validate_motion(job["body"])
        elif endpoint == cls.HOME_ENDPOINT:
            body = cls._validate_motion(job["body"], home=True)
        elif endpoint == cls.GRIPPER_ENDPOINT:
            body = job["body"]
            if (not isinstance(body, dict) or set(body) != {"arm", "wait", "action"}
                    or body["arm"] not in ("left", "right") or body["wait"] is not True
                    or body["action"] not in ("open", "grip")):
                raise RobotCommandError(
                    "Gripper jobs require only arm='left' or 'right', wait=True, "
                    "and action='open' or 'grip'."
                )
            body = dict(body)
        else:
            raise RobotCommandError("Only RWS move, home and gripper endpoints are supported.")
        return {"name": job["name"], "endpoint": endpoint, "body": body}

    @staticmethod
    def _copy_job(job):
        return {**job, "body": dict(job["body"])}

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
            raise RobotCommandError(
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
        """Run named jobs sequentially under one renewed control lease.

        Validate the complete plan before acquiring control. Every job requires
        wait=True and an exact accepted=True / DONE response. No failed command
        is retried and no recovery moves are added. Hooks receive a zero-based
        index and a copy of the job; before_send may return a replacement job,
        which is validated before dispatch. after_done runs only after confirmed
        completion (and measured home joints for home jobs); raising from either
        hook prevents all later jobs. Dry-run validates and prints the plan
        without HTTP or either hook.
        """
        if not isinstance(jobs, list):
            raise RobotCommandError("Provide a list of named RWS jobs.")
        jobs = [self._validate_job(job) for job in jobs]
        if not jobs:
            return
        if not self._execution_lock.acquire(blocking=False):
            raise RobotCommandError("This client is already executing a command sequence.")
        stop = threading.Event()
        renewal = None
        try:
            if self.dry_run:
                for job in jobs:
                    print(f"DRY RUN: POST {job['endpoint']} "
                          f"{json.dumps(job['body'], sort_keys=True)} ({job['name']})")
                return
            self._lease_error = None
            self._release_needed = False
            self._take_control()
            renewal = threading.Thread(target=self._renew_loop, args=(stop,), daemon=True)
            renewal.start()
            for index, job in enumerate(jobs):
                self._check_lease()
                if before_send is not None:
                    try:
                        replacement = before_send(index, self._copy_job(job))
                        if replacement is not None:
                            job = self._validate_job(replacement)
                    except Exception as exc:
                        raise RobotCommandError(f"Pre-command check failed: {exc}") from exc
                self._check_lease()
                reply, _ = self._post(job["endpoint"], move=job["body"])
                print(f"Sent: POST {job['endpoint']} {json.dumps(job['body'], sort_keys=True)}"
                      f" -> {reply} ({job['name']})")
                if reply.get("accepted") is not True or reply.get("message") != "DONE":
                    raise RobotCommandError(
                        f"Step '{job['name']}' did not report successful completion: {reply}. "
                        "No further commands will be sent."
                    )
                if job["endpoint"] == self.HOME_ENDPOINT:
                    self._verify_home(job["body"]["arm"])
                if after_done is not None:
                    try:
                        after_done(index, self._copy_job(job))
                    except Exception as exc:
                        raise RobotCommandError(
                            f"Post-command check failed after '{job['name']}' reported DONE: {exc}. "
                            "No further commands will be sent."
                        ) from exc
                self._check_lease()
        finally:
            stop.set()
            try:
                if renewal is not None:
                    renewal.join()
                if self._release_needed:
                    self._release_control()
            finally:
                self._execution_lock.release()
