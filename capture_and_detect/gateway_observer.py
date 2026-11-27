"""Read-only browser counterpart of diagnostics/gateway_diag.py.

Uses the same status endpoints and prefers the diagnostics identity. The main
dashboard may reuse its existing read credential; the standalone viewer never
loads a picker credential. Unlike the terminal
collector, requests are sequential and demand-driven: one shared reader per
server, with pacing and transport backoff to leave the gateway free for control.
No picker, model, ownership record, command route or control lease is created.
"""

import copy
from collections import deque
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

import requests

HERE = Path(__file__).resolve().parent
MAX_RESPONSE = 128 * 1024
ENDPOINTS = {
    "health": "/health", "control": "/control", "queue": "/robot/rws/queue",
    "camera": "/camera/status", "robot": "/robot/status", "calibration": "/calibration",
    "intrinsics": "/camera/intrinsics", "arms": "/robot/arms", "rapid": "/robot/rapid",
    "gripper_left": "/robot/arms/left/gripper", "gripper_right": "/robot/arms/right/gripper",
    "job_left": "/robot/rws/status?arm=left", "job_right": "/robot/rws/status?arm=right",
}
RWS_KEYS = ("arms", "rapid", "gripper_left", "gripper_right", "job_left", "job_right")
FIELDS = {
    "health": ("status", "version", "uptime_seconds", "security_mode", "robot_provider"),
    "control": ("active", "client_id", "priority", "acquired_at", "expires_at"),
    "queue": ("state", "owner", "length", "step", "started_at", "finished_at"),
    "camera": ("connected", "sequence", "rgb_available", "rgb_width", "rgb_height", "rgb_format",
               "depth_available", "depth_width", "depth_height", "depth_format"),
    "robot": ("connected", "state"), "calibration": ("updated_at", "updated_by"),
    "intrinsics": ("fx", "fy", "cx", "cy", "width", "height"),
    "gripper_left": ("position",), "gripper_right": ("position",),
    "job_left": ("accepted", "message"), "job_right": ("accepted", "message"),
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path.rstrip("/") not in ("", "/api/v1")):
        raise ValueError("Use a gateway HTTP(S) address, optionally ending in /api/v1.")
    # Force validation of the port too.
    parsed.port
    return f"{parsed.scheme}://{parsed.netloc}/api/v1"


def diagnostics_token(token_file=None):
    if token_file:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    else:
        token = os.environ.get("GATEWAY_DIAG_TOKEN")
        if not token:
            try:
                registry = json.loads((HERE.parent / "client_tokens.json").read_text(encoding="utf-8"))
                entry = registry.get("diagnostics") if isinstance(registry, dict) else None
                token = entry.get("token") if isinstance(entry, dict) else entry
            except (OSError, ValueError):
                token = None
    if token is not None and (not isinstance(token, str) or not 1 <= len(token) <= 4096
                             or any(not 33 <= ord(c) <= 126 for c in token)):
        raise ValueError("The diagnostics token is invalid.")
    return token


def public_data(key, value, token=None):
    """Only display fields; never expose credentials, raw jobs or error bodies."""
    if not isinstance(value, dict):
        raise ValueError("Expected a status object.")

    def scalar(item):
        if isinstance(item, str):
            return (item.replace(token, "[redacted]") if token else item)[:240]
        if item is None or type(item) is bool or (type(item) in (int, float) and math.isfinite(item)):
            return item
        return None

    def pick(item, names):
        return {name: scalar(item[name]) for name in names if name in item} if isinstance(item, dict) else {}

    result = pick(value, FIELDS.get(key, ()))
    if key == "health":
        if value.get("status") not in ("ok", "degraded", "error"):
            raise ValueError("Invalid health status.")
        result["camera"] = pick(value.get("camera"), ("ok", "connected", "provider", "frame_age_seconds", "max_frame_age_seconds"))
        result["robot_controller"] = pick(value.get("robot_controller"), (
            "ok", "reachable", "controller_state", "operation_mode", "rapid_execution_state", "move_in_progress"))
    elif key == "control" and type(value.get("active")) is not bool:
        raise ValueError("Invalid control status.")
    elif key == "queue":
        if value.get("state") not in ("idle", "running", "done", "failed", "cancelled"):
            raise ValueError("Invalid queue status.")
        steps = value.get("steps")
        if isinstance(steps, list):
            result["steps"] = [pick(item, ("name", "arm", "state")) for item in steps[:64] if isinstance(item, dict)]
    elif key == "arms":
        for arm in ("left", "right"):
            pose = pick(value.get(arm), ("x", "y", "z", "q1", "q2", "q3", "q4", "cf1", "cf4", "cf6", "cfx", "tool", "wobj"))
            q = [pose.get(name) for name in ("q1", "q2", "q3", "q4")]
            if all(type(n) in (int, float) and math.isfinite(n) for n in q):
                length = math.sqrt(sum(n * n for n in q))
                if length > 1e-12:
                    w, x, y, z = [n / length for n in q]
                    pose["rotation"] = [math.degrees(n) for n in (
                        math.atan2(2 * (y * z + w * x), 1 - 2 * (x * x + y * y)),
                        math.asin(max(-1, min(1, 2 * (w * y - x * z)))),
                        math.atan2(2 * (x * y + w * z), 1 - 2 * (y * y + z * z)))]
            result[arm] = pose
    elif key == "rapid":
        tasks = value.get("tasks")
        result["tasks"] = []
        if isinstance(tasks, list):
            for task in tasks[:32]:
                if not isinstance(task, dict):
                    continue
                item = pick(task, ("name", "active", "state", "execution_state"))
                modules = task.get("modules")
                item["modules"] = [scalar(m) for m in modules[:32] if isinstance(m, str)] if isinstance(modules, list) else []
                result["tasks"].append(item)
    return result


def unavailable_snapshot(message="Gateway monitoring is not configured."):
    return {"status": "unconfigured", "message": message, "gateway": None, "demo": False,
            "read_only": True, "sections": {}, "events": [], "stale_after": 20, "sampled_at": utc_now()}


class GatewayObserver:
    STALE_AFTER = 20.0
    DEMAND_WINDOW = 12.0
    BACKOFF = 12.0  # Longer than a timed-out RWS request can remain in the gateway.

    def __init__(self, url, token=None, *, interval=2.0, timeout=2.5, clock=time.monotonic):
        self.url = normalize_url(url)
        self._token = token
        self.interval, self.timeout = interval, timeout
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._demand_until = 0
        self._detail_until = 0
        self._retry_at = 0
        self._sections = {}
        self._events = deque(maxlen=40)
        self._previous = {}
        self._session = None

    def start(self):
        with self._lock:
            if self._thread is None and not self._stop.is_set():
                self._thread = threading.Thread(target=self._run, name="gateway-observer", daemon=True)
                self._thread.start()

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2 * self.timeout + 1)

    def snapshot(self, *, overview=False):
        now = self._clock()
        with self._lock:
            self._demand_until = now + self.DEMAND_WINDOW
            if not overview:
                self._detail_until = now + 3
            self._wake.set()
            sections = copy.deepcopy(self._sections)
            events = list(self._events)
        for section in sections.values():
            observed = section.pop("_observed", None)
            section["age_seconds"] = max(0, now - observed) if observed is not None else None
            section["stale"] = (section["state"] != "ok" or observed is None or now - observed > self.STALE_AFTER)
        health = sections.get("health", {})
        control = sections.get("control", {})
        if not health:
            status, message = "connecting", "Waiting for the first gateway reading."
        elif health.get("stale"):
            status, message = "offline", "Gateway connection lost. Displayed readings are last known values."
        elif control.get("state") == "auth_error":
            status, message = "auth_error", "Diagnostics access rejected. Check the diagnostics token on this computer."
        elif health.get("data", {}).get("status") != "ok" or any(s["stale"] and s["state"] != "unsupported" for s in sections.values()):
            status, message = "degraded", "Gateway connected. Some readings need attention or are unavailable."
        else:
            status, message = "live", "Reading directly from the gateway. No robot commands are sent."
        if self._stop.is_set():
            status, message = "offline", "Observer stopped. Displayed readings are last known values."
        return {"status": status, "message": message, "gateway": self.url, "demo": False,
                "read_only": True, "sections": sections, "events": events,
                "stale_after": self.STALE_AFTER, "sampled_at": utc_now()}

    def _wanted(self):
        with self._lock:
            return not self._stop.is_set() and self._clock() < self._demand_until

    def _run(self):
        with requests.Session() as session:
            self._session = session
            while not self._stop.is_set():
                if not self._wanted():
                    self._wake.wait(1)
                    self._wake.clear()
                    continue
                if self._clock() < self._retry_at:
                    self._stop.wait(min(1, self._retry_at - self._clock()))
                    continue
                self._poll_once()
                self._stop.wait(self.interval)

    def _get(self, key):
        # Only fixed read endpoints. No proxy accepts an arbitrary browser URL.
        headers = {"X-Dev-Client": "diagnostics", "Connection": "close"}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        started = self._clock()
        try:
            with self._session.get(self.url + ENDPOINTS[key], headers=headers,
                                   timeout=(min(1, self.timeout), self.timeout),
                                   allow_redirects=False, stream=True) as response:
                code = response.status_code
                if code != 200:
                    return {"state": "auth_error" if code in (401, 403) else "unsupported" if code == 404 else "unavailable",
                            "http_status": code}
                raw = bytearray()
                for chunk in response.iter_content(8192):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE or self._clock() - started > 2 * self.timeout:
                        raise ValueError("Response limit exceeded.")
                data = public_data(key, json.loads(raw), self._token)
                return {"state": "ok", "http_status": code, "data": data,
                        "updated_at": utc_now(), "_observed": self._clock(),
                        "elapsed_ms": round((self._clock() - started) * 1000)}
        except (requests.RequestException, ValueError, TypeError, RecursionError, OverflowError):
            # Never forward exception strings or gateway error bodies to browsers.
            return {"state": "unavailable", "http_status": None}

    def _record(self, key, result):
        with self._lock:
            old = self._sections.get(key, {})
            if result["state"] != "ok":
                result = {**old, **result}  # Retain measurements with their original timestamps.
            self._sections[key] = result
            data = result.get("data", {})
            if result["state"] != "ok":
                signature, detail = result["state"], "Reading unavailable"
            elif key == "control":
                signature = data.get("client_id") if data.get("active") else "free"
                detail = f"Control held by {signature}" if data.get("active") else "Control lease is free"
            elif key == "queue":
                signature = (data.get("state"), data.get("owner"), data.get("step"))
                detail = f"Queue {data.get('state', 'unknown')} · {data.get('owner') or 'no owner'}"
            elif key == "health":
                signature = (data.get("status"), data.get("robot_controller", {}).get("reachable"))
                detail = f"Gateway {data.get('status', 'unknown')}"
            else:
                return
            if self._previous.get(key) != signature:
                self._previous[key] = signature
                self._events.appendleft({"at": utc_now(), "source": key, "message": detail})

    def _poll_once(self):
        keys = ["health", "control", "queue", "camera"]
        # The full dashboard needs only these cheap reads outside Observer.
        # Do not keep RWS detail polling active alongside the 3D joint reader.
        if self._clock() < self._detail_until:
            keys += ["robot", "calibration", "intrinsics"] + list(RWS_KEYS)
        for key in keys:
            if not self._wanted():
                return
            if key in RWS_KEYS:
                with self._lock:
                    health = self._sections.get("health", {}).get("data", {})
                if health.get("robot_controller", {}).get("reachable") is not True:
                    self._record(key, {"state": "skipped", "http_status": None})
                    continue
            started = self._clock()
            result = self._get(key)
            self._record(key, result)
            if result["state"] in ("unavailable", "auth_error") or (key == "health" and result["state"] != "ok"):
                self._retry_at = self._clock() + self.BACKOFF
                return
            # Sequential and at most 50% duty cycle, like robot_telemetry.py.
            self._stop.wait(max(0.15, self._clock() - started))


def observer_from_options(args, *, mode):
    if mode == "demo":
        return DemoObserver()
    try:
        gateway = getattr(args, "gateway", None)
        local = None
        if getattr(args, "device_config", None):
            path = Path(args.device_config).resolve()
            config = json.loads(path.read_text(encoding="utf-8"))
            local = config.get("devices", {}).get("mac", {})
            gateway = local.get("gateway") if local.get("backend") == "local" else None
        if gateway:
            token = diagnostics_token(getattr(args, "observer_token_file", None))
            if not token:
                # The existing UI already reads robot status using this credential.
                # Reuse it only here; standalone diagnostics has no picker config.
                from active_device_status import _environment_token
                token = _environment_token()
                if not token and local:
                    token = (path.parent / local["state_dir"] / "gateway-token").read_text(encoding="utf-8").strip()
            return GatewayObserver(gateway, token)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        pass
    return None


class DemoObserver:
    """Explicit offline sample, including a simulated primary → Pi handover."""
    def __init__(self):
        self.started = time.monotonic()

    def start(self):
        pass

    def close(self):
        pass

    def snapshot(self):
        elapsed = time.monotonic() - self.started
        role = "primary" if int(elapsed / 15) % 2 == 0 else "secondary"
        pose = {"x": 356.2, "y": 182.4, "z": 204.8, "q1": 0, "q2": 1, "q3": 0, "q4": 0,
                "cf1": 0, "cf4": 0, "cf6": 0, "cfx": 0, "tool": "gripper", "wobj": "wobj0"}
        values = {
            "health": {"status": "ok", "version": "0.6.0", "uptime_seconds": 86400 + elapsed,
                       "security_mode": "strict", "robot_provider": "rws",
                       "robot_controller": {"reachable": True, "controller_state": "motoron", "operation_mode": "AUTO", "rapid_execution_state": "running"},
                       "camera": {"connected": True, "ok": True, "provider": "realsense", "frame_age_seconds": 0.04, "max_frame_age_seconds": 2}},
            "control": {"active": True, "client_id": role, "priority": 300 if role == "primary" else 200},
            "queue": {"state": "running", "owner": "primary", "length": 19, "step": 8,
                      "steps": [{"name": f"step_{i + 1}", "state": "done" if i < 8 else "running" if i == 8 else "pending", "arm": "left" if i < 7 else "right"} for i in range(19)]},
            "camera": {"connected": True, "sequence": 14600 + int(elapsed * 30), "rgb_available": True, "rgb_width": 1280, "rgb_height": 720,
                       "depth_available": True, "depth_width": 1280, "depth_height": 720, "rgb_format": "RGB8", "depth_format": "Z16"},
            "arms": {"left": pose, "right": {**pose, "x": 404.6, "y": -156.7, "z": 220.3}},
            "gripper_left": {"position": 14.2}, "gripper_right": {"position": 9.8},
            "job_left": {"message": "DONE"}, "job_right": {"message": "BUSY"},
            "rapid": {"tasks": [{"name": name, "active": True, "state": "normal", "execution_state": "started", "modules": ["RunPath", "MainModule"]} for name in ("T_ROB_L", "T_ROB_R")]},
            "robot": {"connected": False, "state": "disconnected"},
            "calibration": {"updated_at": "2026-11-23T09:00:00+00:00", "updated_by": "operator"},
            "intrinsics": {"fx": 909.1, "fy": 909.4, "cx": 643.2, "cy": 361.8},
        }
        return {"status": "demo", "demo": True, "read_only": True, "gateway": "Offline sample · no gateway connection",
                "message": "Simulated readings. The sample switches control between the primary computer and Pi every 15 seconds.",
                "sampled_at": utc_now(), "stale_after": 20,
                "sections": {key: {"state": "ok", "data": public_data(key, value), "age_seconds": 0, "stale": False,
                                   "updated_at": utc_now(), "elapsed_ms": 42, "http_status": 200} for key, value in values.items()},
                "events": [{"at": utc_now(), "source": "control", "message": f"Simulated control holder: {role}"},
                           {"at": utc_now(), "source": "queue", "message": "Sample queue continues under its original owner"}]}
