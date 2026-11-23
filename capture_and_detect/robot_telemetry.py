"""Read-only arm telemetry for the 3D view, built not to compete with control.

The C++ gateway serves each HTTP connection on its own thread (64 at most) and
runs every RWS call through one lock. Requests that arrive faster than the
controller answers queue inside the gateway, each holding a connection, even
after the client gave up. When all 64 are taken, the control lease renewal
gets "503 Too many connections" and the dashboard stops. So this reader:

* uses one keep-alive connection and never has more than one request in flight;
* leaves the RWS lock free at least half the time (waits at least as long as
  the last request took, and never less than MIN_GAP);
* after any error or timeout waits longer than the gateway's RWS timeout
  (10 s) before the next request, so an abandoned request has finished;
* runs only while a dashboard asked for a pose in the last DEMAND_WINDOW
  seconds, and shares its readings between all open dashboards.

It never takes the control lease and never sends anything but GET.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Callable, Mapping, Optional

import requests

ARMS = ("left", "right")


def _finite(value) -> bool:
    try:
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        return False


class RobotTelemetry:
    DEMAND_WINDOW = 3.0       # s without a viewer before the reader stops
    MIN_GAP = 0.1             # s between requests, at least
    DUTY_FACTOR = 1.0         # wait >= this x the last request's duration
    GRIPPER_PERIOD = 2.0      # s between gripper reads (alternating arms)
    REQUEST_TIMEOUT = 3.0     # s; a slower answer counts as an error
    ERROR_BACKOFF = (12.0, 30.0)  # s; first wait > gateway rws_timeout (10 s), then doubling to the max

    def __init__(self, api: str, headers: Mapping[str, str], *,
                 session_factory: Callable[[], requests.Session] = requests.Session,
                 clock: Callable[[], float] = time.monotonic):
        self.api = api.rstrip("/")
        self.headers = dict(headers)
        self._session_factory = session_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = False
        self._thread: Optional[threading.Thread] = None
        self._demand_until = 0.0
        self._arms = {arm: None for arm in ARMS}
        self._grippers = {arm: None for arm in ARMS}
        self._error: Optional[str] = None
        self._backoff = 0.0

    # ---------------------------------------------------------- public --

    def snapshot(self) -> dict:
        """Latest readings, and keep the reader running for DEMAND_WINDOW seconds.

        Each arm is None or {"joints": [rax_1..rax_6], "arm_angle", "gripper", "t"};
        "t" and "now" are this process's monotonic clock in seconds, so a
        viewer can order readings and see how old they are.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("telemetry closed")
            self._demand_until = self._clock() + self.DEMAND_WINDOW
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="robot-telemetry", daemon=True)
                self._thread.start()
            self._wake.set()
            arms = {arm: None if reading is None else {**reading, "joints": list(reading["joints"]),
                                                       "gripper": self._grippers[arm]}
                    for arm, reading in self._arms.items()}
            return {"arms": arms, "now": self._clock(), "error": self._error}

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._wake.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.REQUEST_TIMEOUT + 1)

    # --------------------------------------------------------- private --

    def _wanted(self) -> bool:
        with self._lock:
            return not self._closed and self._clock() < self._demand_until

    def _sleep(self, seconds: float) -> None:
        # Close interrupts the wait; new demand does not shorten a backoff.
        deadline = self._clock() + seconds
        while not self._closed:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return
            self._wake.clear()
            self._wake.wait(min(remaining, 0.5))

    def _get(self, session, endpoint):
        response = session.get(f"{self.api}{endpoint}", headers=self.headers, timeout=self.REQUEST_TIMEOUT)
        try:
            response.raise_for_status()
            reply = response.json()
        finally:
            response.close()
        if not isinstance(reply, dict):
            raise ValueError(f"{endpoint} returned no object")
        return reply

    def _read_joints(self, session, arm):
        reply = self._get(session, f"/robot/arms/{arm}/joints")
        joints = reply.get("joints")
        if (reply.get("arm") != arm or not isinstance(joints, list) or len(joints) != 6
                or not all(map(_finite, joints)) or not _finite(reply.get("arm_angle"))):
            raise ValueError(f"invalid joints for the {arm} arm")
        return {"joints": [float(value) for value in joints], "arm_angle": float(reply["arm_angle"])}

    def _read_gripper(self, session, arm):
        reply = self._get(session, f"/robot/arms/{arm}/gripper")
        value = reply.get("position")
        return float(value) if reply.get("arm") == arm and _finite(value) and 0 <= value <= 25 else None

    def _run(self) -> None:
        session = None
        turn, next_gripper, gripper_turn = 0, 0.0, 0
        try:
            while self._wanted():
                if session is None:
                    session = self._session_factory()
                now = self._clock()
                if now >= next_gripper:
                    arm, kind = ARMS[gripper_turn % 2], "gripper"
                    gripper_turn += 1
                    next_gripper = now + self.GRIPPER_PERIOD
                else:
                    arm, kind = ARMS[turn % 2], "joints"
                    turn += 1
                started = self._clock()
                try:
                    if kind == "joints":
                        reading = self._read_joints(session, arm)
                    else:
                        reading = self._read_gripper(session, arm)
                except (requests.RequestException, ValueError) as exc:
                    # The gateway may still be working on an abandoned request:
                    # drop the connection and wait past its RWS timeout.
                    session.close()
                    session = None
                    self._backoff = (self.ERROR_BACKOFF[0] if not self._backoff
                                     else min(self._backoff * 2, self.ERROR_BACKOFF[1]))
                    with self._lock:
                        self._error = f"{type(exc).__name__}: {exc}"[:200]
                    self._sleep(self._backoff)
                    continue
                finished = self._clock()
                self._backoff = 0.0
                with self._lock:
                    self._error = None
                    if kind == "joints":
                        reading["t"] = (started + finished) / 2
                        self._arms[arm] = reading
                    else:
                        self._grippers[arm] = reading
                self._sleep(max(self.MIN_GAP, (finished - started) * self.DUTY_FACTOR))
        finally:
            if session is not None:
                session.close()  # frees the gateway connection while nobody watches
