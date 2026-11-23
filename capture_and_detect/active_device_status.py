"""Cached, read-only gateway ownership for the dashboard's failover indicator.

This monitor never selects a picker or acquires robot control. A gateway queue
can continue under its original owner after another client acquires the lease,
so the two ownership observations deliberately remain separate.
"""

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import platform
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit

import requests


ROLES = {
    "primary": ("cloud", "Cloud"),
    "secondary": ("pi", "Raspberry Pi"),
    "tertiary": ("lynx", "Lynx"),
    "admin": (None, "Maintenance"),
    "operator": (None, "Operator"),
}
QUEUE_STATES = {"idle", "running", "done", "failed", "cancelled"}
MAX_RESPONSE = 128 * 1024


def empty_status(status="unconfigured", message="Gateway monitoring is not configured."):
    return {"status": status, "device_id": None, "label": None, "client_id": None,
            "queue": None, "updated_at": None, "message": message}


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value):
        raise ValueError("Invalid gateway owner.")
    device, label = ROLES.get(value, (None, "Other controller"))
    return device, label


class ActiveDeviceMonitor:
    def __init__(self, gateway, token, *, primary_label="Cloud", poll_interval=1.0, timeout=1.0, stale_after=5.0):
        parsed = urlsplit(gateway)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment):
            raise ValueError("Invalid gateway monitoring address.")
        if (not isinstance(token, str) or not 1 <= len(token) <= 4096
                or any(not 33 <= ord(char) <= 126 for char in token)):
            raise ValueError("Gateway monitoring credential is unavailable.")
        if any(not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
               for value in (poll_interval, timeout, stale_after)):
            raise ValueError("Invalid gateway monitoring interval.")
        if (not isinstance(primary_label, str) or not 1 <= len(primary_label) <= 64
                or primary_label != primary_label.strip() or not primary_label.isprintable()):
            raise ValueError("Invalid primary device label.")
        self._names = {"cloud": primary_label, "pi": "Raspberry Pi", "lynx": "Lynx"}
        self._api = gateway.rstrip("/") + "/api/v1"
        # Monitoring must not retain one of the gateway's limited connection
        # slots between polls, especially when a device loses its network.
        self._headers = {"Authorization": "Bearer " + token, "Connection": "close"}
        self.poll_interval, self.timeout, self.stale_after = poll_interval, timeout, stale_after
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._observed = time.monotonic()
        self._status = empty_status("connecting", "Connecting to the gateway…")

    def start(self):
        with self._lock:
            if self._thread is not None or self._stop.is_set():
                return
            self._thread = threading.Thread(target=self._run, name="active-device-status", daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set()
        with self._lock:
            thread = self._thread
            self._status = empty_status("unavailable", "Gateway monitoring is stopped.")
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2 * self.timeout + 1)

    def snapshot(self):
        """Return cached data only; browser reads never cause gateway requests."""
        with self._lock:
            result = copy.deepcopy(self._status)
            if (result["status"] in ("active", "idle", "connecting")
                    and time.monotonic() - self._observed > self.stale_after):
                result = empty_status("unavailable", "Gateway status is stale.")
                result["updated_at"] = self._status["updated_at"]
            if self._names["cloud"] != "Cloud":
                # Include the route even while idle or unavailable; the UI
                # must not infer the primary machine from current ownership.
                result["names"] = dict(self._names)
            return result

    def _run(self):
        while not self._stop.is_set():
            self._poll_once()
            self._stop.wait(self.poll_interval)

    def _owner(self, value):
        identity = _identity(value)
        if "Bearer " + value == self._headers["Authorization"]:
            raise ValueError("Invalid gateway owner.")
        if value == "primary":
            return identity[0], self._names["cloud"]
        return identity

    def _get(self, path, *, optional=False):
        deadline = time.monotonic() + 2 * self.timeout
        # Do not redirect a private gateway credential to another origin.
        with requests.get(self._api + path, headers=self._headers,
                          timeout=(min(0.5, self.timeout), self.timeout),
                          allow_redirects=False, stream=True) as response:
            if optional and response.status_code == 404:
                return None
            if response.status_code != 200:
                raise ValueError("Gateway monitoring request failed.")
            raw = bytearray()
            for chunk in response.iter_content(8192):
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE or time.monotonic() > deadline:
                    raise ValueError("Gateway monitoring response is unavailable.")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("Invalid gateway monitoring response.")
            return value

    def _poll_once(self):
        observed = time.monotonic()
        try:
            control = self._get("/control")
            if type(control.get("active")) is not bool:
                raise ValueError("Invalid gateway control status.")
            result = empty_status("idle", "No device currently holds robot control.")
            if control["active"]:
                owner = control.get("client_id")
                device, label = self._owner(owner)
                result.update(status="active", device_id=device, label=label, client_id=owner,
                              message=f"{label} currently holds robot control.")
            queue = self._get("/robot/rws/queue", optional=True)
            if queue is not None:
                state, owner = queue.get("state"), queue.get("owner")
                if not isinstance(state, str) or state not in QUEUE_STATES:
                    raise ValueError("Invalid gateway queue status.")
                if state == "idle":
                    owner, label = None, None
                else:
                    _, label = self._owner(owner)
                # Bind inspection to this exact queue, not merely its owner
                # and terminal state. Do not expose raw jobs or messages.
                fingerprint = hashlib.sha256(json.dumps(
                    queue, allow_nan=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")).hexdigest()
                result["queue"] = {"state": state, "owner": owner, "label": label,
                                   "fingerprint": fingerprint}
            result["updated_at"] = datetime.now(timezone.utc).isoformat()
        except (requests.RequestException, ValueError, TypeError, RecursionError, UnicodeError):
            # Never expose exception text, gateway messages or credentials.
            result = empty_status("unavailable", "Gateway status is unavailable.")
        with self._lock:
            if not self._stop.is_set():
                self._status, self._observed = result, observed


def _environment_token():
    """Same environment/setx sources as picker_workflow.load_token, without AI imports."""
    token = os.environ.get("GATEWAY_TOKEN")
    if not token and os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                token = winreg.QueryValueEx(key, "GATEWAY_TOKEN")[0]
        except OSError:
            pass
    return token


def monitor_from_options(args, *, mode):
    """Read configuration metadata only; never initialize/activate a device."""
    if mode == "demo":
        return None
    try:
        gateway = getattr(args, "gateway", None)
        config_path = getattr(args, "device_config", None)
        token = None
        primary_label = "Cloud"
        if config_path:
            path = Path(config_path).resolve()
            with path.open("rb") as stream:
                raw = stream.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                return None
            config = json.loads(raw)
            local = config.get("devices", {}).get("mac", {})
            if local.get("backend") != "local":
                # Remote token_file authenticates a picker service, not the
                # robot gateway. Never use it for gateway ownership requests.
                return None
            gateway = local["gateway"]
            failover = local.get("failover")
            if (mode == "live" and isinstance(failover, dict)
                    and set(failover) == {"coordinator_url", "token_file"}
                    and all(isinstance(value, str) and value.strip() for value in failover.values())):
                primary_label = {"Darwin": "MacBook", "Windows": "Windows PC"}.get(platform.system(), "Local computer")
            token = os.environ.get("GATEWAY_TOKEN")
            if not token:
                from device_service import read_gateway_token
                token = read_gateway_token(path.parent / local["state_dir"] / "gateway-token")
        elif gateway:
            token = _environment_token()
        if not gateway or not token:
            return None
        return ActiveDeviceMonitor(gateway, token, primary_label=primary_label)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        # A missing monitoring credential must not prevent the existing UI
        # from starting. No credential-bearing exception enters the browser.
        return None
