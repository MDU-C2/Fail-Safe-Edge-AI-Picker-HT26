"""Durable batch admission and queue receipts for the three trusted workers.

This is a separate, requests-only service, not a robot gateway. It never acquires
control or sends a robot command. The gateway remains the lease authority. A
worker prepares a unique submission here before its single physical queue POST;
the next worker cannot replace that queue until its result is durably recorded.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

ROLES = {"primary", "secondary", "tertiary"}
PHASES = {"active", "finalizing", "watching", "complete", "blocked", "stopped"}
RECEIPTS = {"pending", "unknown", "done", "failed", "cancelled", "not_submitted"}
TERMINAL_RECEIPTS = {"done", "failed", "cancelled", "not_submitted"}
MAX_BODY = 512 * 1024
MAX_STATE = 32 * 1024 * 1024
MAX_RECORDS = 8192
MAX_STEPS = 64
RECOVERY_CAPABILITY = "inspected_queue_recovery"
CONTINUOUS_CAPABILITY = "continuous_batches"
DEVICE_SPEED_CAPABILITY = "device_speed_profiles"


class CoordinatorError(RuntimeError):
    """Invalid credentials, configuration, request or coordinator data."""


class CoordinatorUnavailable(CoordinatorError):
    """Transport or storage failure; an attempted mutation may have committed."""


class CoordinatorConflict(CoordinatorError):
    """The requested operation conflicts with durable batch/queue state."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 80:
        raise CoordinatorError("Invalid queue timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed
    except (ValueError, OverflowError) as error:
        raise CoordinatorError("Invalid queue timestamp.") from error


def _role(value):
    if not isinstance(value, str) or value not in ROLES:
        raise CoordinatorError("Invalid worker role.")
    return value


def validate_device_speeds(value):
    """Return independent, strict per-role overall limits without any I/O."""
    if (not isinstance(value, dict) or set(value) != ROLES
            or any(type(speed) not in (int, float) or not 1 <= speed <= 1500
                   or not math.isfinite(speed) for speed in value.values())):
        raise CoordinatorError("Device speeds must specify primary, secondary and tertiary from 1 to 1500 mm/s.")
    return {role: float(value[role]) for role in ("primary", "secondary", "tertiary")}


def _identifier(value, *, submission=False):
    pattern = r"[0-9a-f]{16,64}" if submission else r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise CoordinatorError("Invalid execution identity.")
    return value


def _reason(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise CoordinatorError("Invalid batch reason.")
    return value


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field.")
        value[key] = item
    return value


def _invalid_number(_value):
    raise ValueError("Non-finite JSON number.")


def _decode(raw):
    try:
        return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_number)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise CoordinatorError("Invalid JSON data.") from error


def _json_bytes(value, *, limit=MAX_BODY):
    try:
        raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > limit:
            raise ValueError
        return raw
    except (ValueError, TypeError, RecursionError, UnicodeError) as error:
        raise CoordinatorError("Invalid or oversized JSON data.") from error


def _json_copy(value, *, limit=MAX_BODY):
    return _decode(_json_bytes(value, limit=limit))


def _write_state(path, raw):
    """Atomically replace a fully encoded state, retaining the file barrier.

    A single binary write avoids Python's per-token pretty-printing overhead
    on the embedded coordinator. The caller validates these exact bytes and
    persists the directory rename before acknowledging the transaction.
    """
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                         prefix="." + path.name, suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _address(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/", "/api/v1", "/api/v1/"}):
            raise ValueError
        parsed.port
        return value.rstrip("/")
    except (ValueError, TypeError, AttributeError) as error:
        raise CoordinatorError("Invalid service address.") from error


def _credential(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 4096
            or any(not 33 <= ord(c) <= 126 for c in value)):
        raise CoordinatorError("Service credential is unavailable.")
    return value


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise CoordinatorError("Invalid request timeout.")
    return value


def _steps(value, submission_id):
    value = _json_copy(value)
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_STEPS:
        raise CoordinatorError("Invalid prepared queue.")
    names = set()
    for step in value:
        if (not isinstance(step, dict) or set(step) != {"name", "type", "body", "group"}
                or not isinstance(step["name"], str) or not step["name"].startswith("q" + submission_id + "_")
                or len(step["name"]) > 160 or step["name"] in names
                or re.fullmatch(r"[A-Za-z0-9_.:-]+", step["name"]) is None
                or not isinstance(step["type"], str) or step["type"] not in {"move", "path", "home", "gripper"}
                or not isinstance(step["body"], dict) or not isinstance(step["body"].get("arm"), str)
                or step["body"]["arm"] not in {"left", "right"}
                or not isinstance(step["group"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", step["group"])):
            raise CoordinatorError("Invalid prepared queue step.")
        names.add(step["name"])
    return value


def _signature(steps, *, submitted=False):
    return [(step["name"], step["type"], step["body"]["arm"] if submitted else step["arm"], step["group"])
            for step in steps]


def _queue(value):
    value = _json_copy(value)
    if (not isinstance(value, dict) or not isinstance(value.get("state"), str)
            or value["state"] not in {"idle", "running", "done", "failed", "cancelled"}
            or not isinstance(value.get("steps"), list) or len(value["steps"]) > MAX_STEPS
            or type(value.get("length")) is not int or value["length"] != len(value["steps"])):
        raise CoordinatorError("Gateway queue status is invalid.")
    if value["state"] == "idle":
        if value["steps"]:
            raise CoordinatorError("Idle gateway queue contains work.")
        return value
    if not value["steps"] or not isinstance(value.get("owner"), str) or not value["owner"]:
        raise CoordinatorError("Gateway queue identity is invalid.")
    _timestamp(value.get("started_at"))
    names = set()
    for step in value["steps"]:
        if (not isinstance(step, dict) or not isinstance(step.get("name"), str) or not step["name"]
                or len(step["name"]) > 160 or step["name"] in names
                or not isinstance(step.get("type"), str) or step["type"] not in {"move", "path", "home", "gripper"}
                or not isinstance(step.get("arm"), str) or step["arm"] not in {"left", "right"}
                or not isinstance(step.get("group"), str) or len(step["group"]) > 80
                or not isinstance(step.get("state"), str)
                or step["state"] not in {"pending", "running", "done", "failed", "skipped", "cancelled"}
                or (step["state"] == "done" and step.get("message") != "DONE")):
            raise CoordinatorError("Gateway queue step result is invalid.")
        names.add(step["name"])
    if value["state"] == "done" and any(step["state"] != "done" for step in value["steps"]):
        raise CoordinatorError("Gateway queue completion is unconfirmed.")
    return value


def _queue_digest(queue):
    return hashlib.sha256(json.dumps(queue, allow_nan=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _inspected_arm_status(value):
    """Last move failure is idle evidence only after explicit inspection."""
    return (isinstance(value, dict) and
            ((value.get("accepted") is True and value.get("message") == "DONE") or
             (value.get("accepted") is False and value.get("message") == "ERR move failed")))


def validate_recovery(value, *, run_id, submission_id, queue):
    """Validate an acknowledgement against its preserved failed queue, no I/O."""
    _identifier(run_id)
    _identifier(submission_id, submission=True)
    queue = _queue(queue)
    if (not isinstance(value, dict)
            or set(value) != {"run_id", "submission_id", "acknowledged", "acknowledged_by",
                              "recovered_at", "queue_sha256", "arm_status"}
            or value["run_id"] != run_id or value["submission_id"] != submission_id
            or value["acknowledged"] is not True or value["acknowledged_by"] != "primary"
            or queue["state"] not in {"failed", "cancelled"}
            or value["queue_sha256"] != _queue_digest(queue)
            or any(not step["name"].startswith("q" + submission_id + "_") for step in queue["steps"])
            or any(step["state"] == "running" for step in queue["steps"])
            or not isinstance(value["arm_status"], dict) or set(value["arm_status"]) != {"left", "right"}
            or not all(_inspected_arm_status(status) for status in value["arm_status"].values())):
        raise CoordinatorError("Coordinator recovery evidence is invalid.")
    _timestamp(value["recovered_at"])
    return copy.deepcopy(value)


def _recovery(value, *, run_id, submission_id, item):
    if item["state"] not in {"failed", "cancelled"} or item.get("identity_conflict") is True:
        raise CoordinatorError("Coordinator recovery evidence is invalid.")
    return validate_recovery(value, run_id=run_id, submission_id=submission_id, queue=item.get("queue"))


def _http_json(method, url, headers, *, timeout, payload=None, limit=MAX_BODY):
    """One bounded HTTP attempt. Never retry a possibly committed operation."""
    response = None
    try:
        options = {"headers": headers, "timeout": (min(1.0, timeout), timeout),
                   "allow_redirects": False, "stream": True}
        if payload is not None:
            options["json"] = payload
        response = requests.request(method, url, **options)
        if response.status_code == 409:
            raise CoordinatorConflict("Coordinator operation conflicts with current state.")
        if response.status_code >= 500:
            raise CoordinatorUnavailable("Service is temporarily unavailable.")
        if response.status_code != 200:
            raise CoordinatorError("Service rejected the request.")
        deadline, raw = time.monotonic() + timeout, bytearray()
        for chunk in response.iter_content(8192):
            raw.extend(chunk)
            if len(raw) > limit or time.monotonic() > deadline:
                raise CoordinatorUnavailable("Service response is unavailable.")
        result = _decode(raw)
        if not isinstance(result, dict):
            raise CoordinatorError("Service response is invalid.")
        return result
    except requests.RequestException as error:
        raise CoordinatorUnavailable("Service connection failed; the operation was not retried.") from error
    finally:
        if response is not None:
            response.close()


class CoordinatorClient:
    def __init__(self, url, token, role, timeout=15, *, durable_timeout=15):
        self.url = _address(url)
        if urlsplit(self.url).path:
            raise CoordinatorError("Coordinator address must not include a path.")
        self.role, self.timeout = _role(role), _positive(timeout)
        self.durable_timeout = max(self.timeout, _positive(durable_timeout))
        self._headers = {"Authorization": "Bearer " + _credential(token), "X-Failover-Role": self.role,
                         "Connection": "close"}

    def _call(self, action, values=None, *, get=False):
        values = {} if values is None else values
        url = self.url + "/v1/" + action
        if get and values:
            url += "?" + urlencode(values)
        # A receipt read can also durably archive gateway completion. Status
        # and heartbeat requests may wait behind the same storage lock, so
        # their default budget also covers persistence. TCP connect stays 1s.
        # Never retry this HTTP operation here.
        timeout = self.timeout if action in {"state", "heartbeat"} else self.durable_timeout
        try:
            return _http_json("GET" if get else "POST", url, self._headers, timeout=timeout,
                              payload=None if get else {"role": self.role, **values})
        except CoordinatorUnavailable as error:
            raise CoordinatorUnavailable(f"Coordinator {action} unavailable (request budget {timeout:g}s): {error}") from error

    def state(self):
        return self._call("state", get=True)

    def start(self, run_id, settings):
        return self._call("start", {"run_id": run_id, "settings": settings})

    def heartbeat(self, run_id=None, status="armed"):
        return self._call("heartbeat", {"run_id": run_id, "status": status})

    def prepare_queue(self, run_id, submission_id, steps):
        return self._call("prepare_queue", {"run_id": run_id, "submission_id": submission_id, "steps": steps})

    def receipt(self, run_id, submission_id):
        return self._call("receipt", {"run_id": run_id, "submission_id": submission_id}, get=True)

    def reject_queue(self, run_id, submission_id):
        return self._call("reject_queue", {"run_id": run_id, "submission_id": submission_id})

    def recover(self, run_id, submission_id, *, acknowledged):
        return self._call("recover", {"run_id": run_id, "submission_id": submission_id,
                                      "acknowledged": acknowledged})

    def finish(self, run_id, reason):
        return self._call("finish", {"run_id": run_id, "reason": reason})

    def stop(self, run_id, reason):
        return self._call("stop", {"run_id": run_id, "reason": reason})

    def block(self, run_id, reason):
        return self._call("block", {"run_id": run_id, "reason": reason})

    def set_phase(self, run_id, phase):
        return self._call("set_phase", {"run_id": run_id, "phase": phase})

    def watch(self, run_id):
        return self._call("watch", {"run_id": run_id})

    def confirm_home(self, run_id, submission_id, arm):
        return self._call("confirm_home", {"run_id": run_id, "submission_id": submission_id, "arm": arm})


class CoordinatorStore:
    def __init__(self, state_file, gateway, gateway_token, *, timeout=3):
        self.path = Path(state_file).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Liveness is advisory and must not wear persistent storage at 3 Hz.
        # Restarting this service deliberately forgets every worker heartbeat.
        self._workers = {}
        # Cache only a validated fingerprint and small public snapshot, not the
        # complete receipt history. Polling must not repeatedly parse/validate
        # every past movement on the embedded coordinator.
        self._validated_digest = None
        self._cached_public = None
        self.timeout = _positive(timeout)
        base = _address(gateway)
        self._api = base if base.endswith("/api/v1") else base + "/api/v1"
        # Each evidence read releases its gateway connection immediately.
        # The gateway has a small connection budget shared by all devices.
        self._headers = {"Authorization": "Bearer " + _credential(gateway_token),
                         "Connection": "close"}
        with self._transaction(create=True):
            pass

    @contextmanager
    def _transaction(self, *, create=False, public_only=False):
        """Serialize threads and separate coordinator processes against one file."""
        import fcntl
        with self._lock:
            descriptor = None
            try:
                lock_path = self.path.with_name(self.path.name + ".lock")
                descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise CoordinatorError("Coordinator lock is invalid.")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                if not self.path.exists():
                    if not create:
                        raise CoordinatorUnavailable("Coordinator state is missing.")
                    value = {"schema_version": 1, "run": None, "workers": {}, "runs": {}, "submissions": {}, "head": None}
                    self._commit(value)
                else:
                    if self.path.is_symlink() or not self.path.is_file():
                        raise CoordinatorError("Coordinator state is invalid.")
                    with self.path.open("rb") as stream:
                        raw = stream.read(MAX_STATE + 1)
                    if len(raw) > MAX_STATE:
                        raise CoordinatorError("Coordinator state is too large.")
                    digest = hashlib.sha256(raw).digest()
                    if digest != self._validated_digest:
                        value = _decode(raw)
                        self._validate_state(value)
                        public = self._public(value)
                        public.pop("workers")
                        self._cached_public = public
                        self._validated_digest = digest
                    elif not public_only:
                        # Mutations always get their own document. A failed
                        # transaction must not modify a cached durable result.
                        value = _decode(raw)
                if public_only:
                    result = copy.deepcopy(self._cached_public)
                    result["workers"] = copy.deepcopy(self._workers)
                    yield result
                else:
                    yield value
            except OSError as error:
                raise CoordinatorUnavailable("Coordinator state storage is unavailable.") from error
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    @staticmethod
    def _validate_state(value):
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or not all(isinstance(value.get(k), dict) for k in ("workers", "runs", "submissions"))
                or len(value["submissions"]) > MAX_RECORDS or len(value["runs"]) > MAX_RECORDS
                or (value.get("head") is not None and (not isinstance(value["head"], str)
                    or value["head"] not in value["submissions"]))):
            raise CoordinatorError("Coordinator state is invalid.")
        run = value.get("run")
        if run is not None and (not isinstance(run, dict) or not isinstance(run.get("phase"), str)
                                or run["phase"] not in PHASES
                                or not isinstance(run.get("settings"), dict)
                                or not isinstance(run.get("id"), str)
                                or run["id"] not in value["runs"]
                                or run != value["runs"][run["id"]]):
            raise CoordinatorError("Coordinator batch state is invalid.")
        for identity, item in value["submissions"].items():
            _identifier(identity, submission=True)
            if (not isinstance(item, dict) or not isinstance(item.get("state"), str) or item["state"] not in RECEIPTS
                    or not isinstance(item.get("owner"), str) or item["owner"] not in ROLES
                    or not isinstance(item.get("run_id"), str) or item["run_id"] not in value["runs"]):
                raise CoordinatorError("Coordinator receipt state is invalid.")
            if type(item.get("home_cycle", 0)) is not int or item.get("home_cycle", 0) < 0:
                raise CoordinatorError("Coordinator receipt Home cycle is invalid.")
            if item.get("steps"):
                _steps(item["steps"], identity)
            elif item["state"] != "not_submitted":
                raise CoordinatorError("Coordinator receipt identity is missing.")
            if item.get("queue") is not None:
                queue = _queue(item["queue"])
                if (queue["state"] == "idle" or queue.get("owner") != item["owner"]
                        or _signature(queue["steps"]) != _signature(item["steps"], submitted=True)
                        or queue["started_at"] != item.get("started_at")):
                    raise CoordinatorError("Coordinator receipt identity is invalid.")
                if item["state"] in {"done", "failed", "cancelled"} and item["state"] != queue["state"]:
                    raise CoordinatorError("Coordinator receipt result is invalid.")
            elif item["state"] in {"done", "failed", "cancelled"}:
                raise CoordinatorError("Coordinator receipt evidence is missing.")
            if "home_verified" in item and (item["home_verified"] is not True or item["state"] != "done"
                    or len(item["steps"]) != 1 or item["steps"][0]["type"] != "home"):
                raise CoordinatorError("Coordinator measured Home evidence is invalid.")
            if "recovery" in item:
                recovery = _recovery(item["recovery"], run_id=item["run_id"], submission_id=identity, item=item)
                saved_run = value["runs"][item["run_id"]]
                if (not isinstance(saved_run, dict) or saved_run.get("recovery") != recovery
                        or value["head"] == identity):
                    raise CoordinatorError("Coordinator recovery history is inconsistent.")
        for run_id, saved in value["runs"].items():
            _identifier(run_id)
            if (not isinstance(saved, dict) or saved.get("id") != run_id
                    or not isinstance(saved.get("phase"), str) or saved["phase"] not in PHASES
                    or not isinstance(saved.get("settings"), dict)
                    or type(saved.get("home_cycle", 0)) is not int or saved.get("home_cycle", 0) < 0
                    or ("continuous_batch" in saved["settings"]
                        and type(saved["settings"]["continuous_batch"]) is not bool)):
                raise CoordinatorError("Coordinator batch history is invalid.")
            if "device_speeds" in saved["settings"]:
                validate_device_speeds(saved["settings"]["device_speeds"])
            if "recovery" in saved:
                recovery = saved["recovery"]
                if not isinstance(recovery, dict):
                    raise CoordinatorError("Coordinator recovery history is invalid.")
                submission_id = _identifier(recovery.get("submission_id"), submission=True)
                item = value["submissions"].get(submission_id)
                if (saved.get("phase") != "stopped" or item is None or item.get("run_id") != run_id
                        or item.get("recovery") != recovery):
                    raise CoordinatorError("Coordinator recovery history is inconsistent.")
                _recovery(recovery, run_id=run_id, submission_id=submission_id, item=item)
            homes, receipts = saved.get("confirmed_homes", []), saved.get("home_receipts", {})
            if homes not in ([], ["right"], ["right", "left"]) or not isinstance(receipts, dict) or set(receipts) != set(homes):
                raise CoordinatorError("Coordinator Home confirmations are invalid.")
            if saved["phase"] == "watching" and (saved["settings"].get("continuous_batch") is not True
                                                 or homes != ["right", "left"]):
                raise CoordinatorError("Continuous watching requires both verified Home receipts.")
            for arm, submission_id in receipts.items():
                _identifier(submission_id, submission=True)
                item = value["submissions"].get(submission_id)
                if (item is None or item["run_id"] != run_id or item["state"] != "done"
                        or item.get("home_cycle", 0) != saved.get("home_cycle", 0)
                        or len(item["steps"]) != 1 or item["steps"][0]["type"] != "home"
                        or item["steps"][0]["body"]["arm"] != arm):
                    raise CoordinatorError("Coordinator Home receipt evidence is invalid.")

    def _commit(self, value):
        raw = _json_bytes(value, limit=MAX_STATE)
        _decode(raw)  # Preserve strict JSON/duplicate-key validation before writing.
        _write_state(self.path, raw)
        # Persist the rename as well as the file contents before ACK.
        descriptor = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _get(self, path):
        return _http_json("GET", self._api + path, self._headers, timeout=self.timeout)

    def _lease(self, role):
        value = self._get("/control")
        if type(value.get("active")) is not bool:
            raise CoordinatorError("Gateway control status is invalid.")
        if not value["active"] or value.get("client_id") != role:
            raise CoordinatorConflict("Worker does not currently hold gateway control.")
        return value

    @staticmethod
    def _run(value, run_id, *, active=False):
        _identifier(run_id)
        run = value["run"]
        if run is None or run["id"] != run_id:
            raise CoordinatorConflict("Batch is not the current execution.")
        if active and run["phase"] not in {"active", "finalizing", "watching"}:
            raise CoordinatorConflict("Batch does not admit further work.")
        return run

    def _public(self, value):
        submission_id = value["head"]
        item = value["submissions"].get(submission_id)
        head = ({"submission_id": submission_id, **{key: item.get(key)
                for key in ("run_id", "owner", "state", "queue")}} if item is not None else None)
        return copy.deepcopy({"run": value["run"], "workers": self._workers, "head": head,
                              "capabilities": [RECOVERY_CAPABILITY, CONTINUOUS_CAPABILITY, DEVICE_SPEED_CAPABILITY]})

    @staticmethod
    def _result(item):
        result = {"state": item["state"], "queue": item.get("queue")}
        for key in ("home_cycle", "home_verified"):
            if key in item:
                result[key] = item[key]
        if "recovery" in item:
            result["recovery"] = item["recovery"]
        return copy.deepcopy(result)

    def state(self, role):
        _role(role)
        with self._transaction(public_only=True) as value:
            return value

    def start(self, role, run_id, settings):
        if _role(role) != "primary":
            raise CoordinatorConflict("Only the primary UI can admit a batch.")
        _identifier(run_id)
        settings = _json_copy(settings, limit=16384)
        if not isinstance(settings, dict):
            raise CoordinatorError("Batch settings must be an object.")
        if "continuous_batch" in settings and type(settings["continuous_batch"]) is not bool:
            raise CoordinatorError("Continuous batch setting must be boolean.")
        if "device_speeds" in settings:
            settings["device_speeds"] = validate_device_speeds(settings["device_speeds"])
        with self._transaction() as value:
            current = value["run"]
            if current is not None and current["id"] == run_id:
                if current["settings"] != settings:
                    raise CoordinatorConflict("An execution identity cannot change settings.")
                return self._public(value)
            if run_id in value["runs"]:
                raise CoordinatorConflict("A retired batch identity cannot be reused.")
            if current is not None and current["phase"] in {"active", "finalizing", "watching"}:
                raise CoordinatorConflict("Another batch is already active.")
            head = value["submissions"].get(value["head"])
            if head is not None and head["state"] not in {"done", "not_submitted"}:
                raise CoordinatorConflict("Previous queue completion needs recovery.")
            if len(value["runs"]) >= MAX_RECORDS:
                raise CoordinatorConflict("Coordinator history requires maintenance.")
            run = {"id": run_id, "phase": "active", "settings": settings, "reason": "", "started_at": _now(),
                   "confirmed_homes": [], "home_receipts": {}, "home_cycle": 0}
            value["run"] = run
            value["runs"][run_id] = copy.deepcopy(run)
            self._commit(value)
            return self._public(value)

    def heartbeat(self, role, run_id=None, status="armed"):
        _role(role)
        if not isinstance(status, str) or not re.fullmatch(r"[A-Za-z0-9_ -]{1,64}", status):
            raise CoordinatorError("Invalid worker status.")
        with self._transaction(public_only=True) as value:
            if run_id is not None:
                self._run(value, run_id)
            self._workers[role] = {"run_id": run_id, "status": status, "last_seen": _now()}
            value["workers"] = copy.deepcopy(self._workers)
            return value

    def _observe(self, value, item, queue=None):
        if item["state"] in TERMINAL_RECEIPTS or item.get("identity_conflict") is True:
            return
        before = copy.deepcopy(item)
        queue = _queue(self._get("/robot/rws/queue")) if queue is None else queue
        matches = (queue["state"] != "idle" and queue.get("owner") == item["owner"]
                   and _signature(queue["steps"]) == _signature(item["steps"], submitted=True))
        if matches and item.get("started_at") not in (None, queue["started_at"]):
            matches = False
        previous = item.get("queue")
        if matches and previous is not None:
            completed = {step["name"] for step in previous["steps"] if step["state"] == "done"}
            current_done = {step["name"] for step in queue["steps"] if step["state"] == "done"}
            matches = completed <= current_done
        if not matches:
            item["state"] = "unknown"
            if previous is not None:
                # Once a bound queue disappears, changes start time or regresses,
                # a later matching response cannot undo the observed conflict.
                item["identity_conflict"] = True
        else:
            item["started_at"], item["queue"] = queue["started_at"], queue
            item["state"] = "pending" if queue["state"] == "running" else queue["state"]
        # Unknown may later resolve when the original one-shot POST becomes
        # visible. It can never be treated as a definitive rejection.
        if item != before:
            self._commit(value)

    def prepare_queue(self, role, run_id, submission_id, steps):
        _role(role)
        _identifier(submission_id, submission=True)
        steps = _steps(steps, submission_id)
        with self._transaction() as value:
            run = self._run(value, run_id, active=True)
            self._lease(role)
            existing = value["submissions"].get(submission_id)
            if existing is not None:
                if existing["run_id"] != run_id or existing["owner"] != role or existing["steps"] != steps:
                    raise CoordinatorConflict("Submission identity has already been used.")
                return self._result(existing)
            if run["phase"] == "watching" and any(step["type"] == "home" for step in steps):
                raise CoordinatorConflict("Watching arms are already home; only a new cup may start another cycle.")
            if run["phase"] == "finalizing":
                if len(steps) != 1 or steps[0]["type"] != "home":
                    raise CoordinatorConflict("Finalizing batches admit one verified Home at a time.")
                arm = steps[0]["body"]["arm"]
                confirmed = run.get("confirmed_homes", [])
                if arm in confirmed or arm == "left" and "right" not in confirmed:
                    raise CoordinatorConflict("Home must follow its durable right-then-left verification barrier.")
            queue = _queue(self._get("/robot/rws/queue"))
            head = value["submissions"].get(value["head"])
            if head is not None:
                self._observe(value, head, queue)
                if head["state"] not in {"done", "not_submitted"}:
                    raise CoordinatorConflict("Previous queue result is not safely resolved.")
            if queue["state"] not in {"idle", "done"}:
                raise CoordinatorConflict("Existing gateway queue requires completion or recovery.")
            if any(step["name"].startswith("q" + submission_id + "_") for step in queue["steps"]):
                raise CoordinatorConflict("Submission identity exists in gateway history.")
            if len(value["submissions"]) >= MAX_RECORDS:
                raise CoordinatorConflict("Coordinator receipt history requires maintenance.")
            # Receipt collection can take time; recheck immediately before the
            # durable permit. The gateway still atomically authorizes the POST.
            self._lease(role)
            item = {"run_id": run_id, "owner": role, "steps": steps, "state": "pending",
                    "prepared_at": _now(), "started_at": None, "queue": None}
            if run["phase"] == "watching":
                # Reset the per-cycle Home barriers only when a new cup has a
                # durable permit. Scanning or preemption while empty preserves
                # the previous Home proof and never sends Home a second time.
                run.update(phase="active", confirmed_homes=[], home_receipts={},
                           home_cycle=run.get("home_cycle", 0) + 1)
                value["runs"][run_id] = copy.deepcopy(run)
            item["home_cycle"] = run.get("home_cycle", 0)
            value["submissions"][submission_id] = item
            value["head"] = submission_id
            self._commit(value)
            return self._result(item)

    def receipt(self, role, run_id, submission_id):
        _role(role)
        _identifier(run_id)
        _identifier(submission_id, submission=True)
        with self._transaction() as value:
            item = value["submissions"].get(submission_id)
            if item is None or item["run_id"] != run_id:
                raise CoordinatorConflict("Submission has no prepared identity.")
            self._observe(value, item)
            return self._result(item)

    def reject_queue(self, role, run_id, submission_id):
        _role(role)
        _identifier(run_id)
        _identifier(submission_id, submission=True)
        with self._transaction() as value:
            item = value["submissions"].get(submission_id)
            if item is not None:
                if item["run_id"] != run_id or item["owner"] != role:
                    raise CoordinatorConflict("Submission belongs to another worker or batch.")
                if item.get("started_at") is not None or item.get("queue") is not None:
                    raise CoordinatorConflict("An observed queue cannot be marked unsubmitted.")
                if item["state"] == "not_submitted":
                    # Repeat a durable result even after a later batch starts.
                    # This cannot create or change historical queue evidence.
                    return self._result(item)
            self._run(value, run_id)
            queue = _queue(self._get("/robot/rws/queue"))
            if any(step["name"].startswith("q" + submission_id + "_") for step in queue["steps"]):
                if item is not None:
                    self._observe(value, item, queue)
                raise CoordinatorConflict("Gateway has already observed this submission.")
            if item is None:
                if len(value["submissions"]) >= MAX_RECORDS:
                    raise CoordinatorConflict("Coordinator receipt history requires maintenance.")
                item = {"run_id": run_id, "owner": role, "steps": [], "prepared_at": _now(),
                        "started_at": None, "queue": None}
                value["submissions"][submission_id] = item
            item["state"] = "not_submitted"
            self._commit(value)
            return self._result(item)

    def recover(self, role, run_id, submission_id, *, acknowledged):
        """Retire an inspected failure after the gateway queue was cleared.

        Never change a failure to success, cancel work, or clear the gateway.
        An acknowledgement is bound to this receipt forever. A timed-out caller
        may retry only while that retired run remains current and admits no work.
        """
        if _role(role) != "primary":
            raise CoordinatorConflict("Only the primary UI can acknowledge inspected recovery.")
        if acknowledged is not True:
            raise CoordinatorError("Recovery requires explicit operator inspection.")
        _identifier(submission_id, submission=True)
        with self._transaction() as value:
            run = self._run(value, run_id)
            item = value["submissions"].get(submission_id)
            if (run["phase"] not in {"blocked", "stopped"} or item is None or item["run_id"] != run_id
                    or item["state"] not in {"failed", "cancelled"}
                    or item.get("identity_conflict") is True or item.get("queue") is None
                    or any(step["state"] == "running" for step in item["queue"]["steps"])):
                raise CoordinatorConflict("Recovery requires the exact terminal failed receipt of an inactive batch.")
            prior = item.get("recovery")
            if prior is None:
                if value["head"] != submission_id or run.get("recovery") is not None:
                    raise CoordinatorConflict("Recovery receipt is no longer the current queue.")
            elif value["head"] is not None or run.get("recovery") != prior:
                raise CoordinatorConflict("Newer execution prevents retrying historical recovery.")
            self._lease("primary")
            if _queue(self._get("/robot/rws/queue"))["state"] != "idle":
                raise CoordinatorConflict("Gateway queue must be cleared and idle before retiring a receipt.")
            arms = {}
            for arm in ("left", "right"):
                status = self._get(f"/robot/rws/status?arm={arm}")
                if not _inspected_arm_status(status):
                    raise CoordinatorConflict("Both arms must report an idle terminal move result.")
                arms[arm] = status
            if _queue(self._get("/robot/rws/queue"))["state"] != "idle":
                raise CoordinatorConflict("Gateway queue changed during inspected recovery.")
            self._lease("primary")
            if prior is not None:
                return self._result(item)
            recovery = {"run_id": run_id, "submission_id": submission_id, "acknowledged": True,
                        "acknowledged_by": "primary", "recovered_at": _now(),
                        "queue_sha256": _queue_digest(item["queue"]), "arm_status": arms}
            item["recovery"] = recovery
            run.update(phase="stopped", recovery=copy.deepcopy(recovery))
            value["runs"][run_id] = copy.deepcopy(run)
            value["head"] = None
            self._commit(value)
            return self._result(item)

    def set_phase(self, role, run_id, phase):
        _role(role)
        if phase != "finalizing":
            raise CoordinatorError("Only explicit finalization is supported.")
        with self._transaction() as value:
            run = self._run(value, run_id, active=True)
            if run["phase"] == "watching":
                raise CoordinatorConflict("Watching arms cannot Home again before another cup is admitted.")
            self._lease(role)
            head = value["submissions"].get(value["head"])
            if head is not None:
                self._observe(value, head)
                if head["state"] not in {"done", "not_submitted"}:
                    raise CoordinatorConflict("Resolve the accepted cup before finalizing.")
            run["phase"] = "finalizing"
            value["runs"][run_id] = copy.deepcopy(run)
            self._commit(value)
            return self._public(value)

    def watch(self, role, run_id):
        """Keep an authorized continuous batch parked after measured Home."""
        _role(role)
        with self._transaction() as value:
            run = self._run(value, run_id)
            if (run["settings"].get("continuous_batch") is not True
                    or run["phase"] not in {"finalizing", "watching"}
                    or run.get("confirmed_homes") != ["right", "left"]):
                raise CoordinatorConflict("Watching requires a continuous batch with both Home completions verified.")
            self._lease(role)
            head = value["submissions"].get(value["head"])
            if (head is None or head["run_id"] != run_id
                    or value["head"] != run.get("home_receipts", {}).get("left")):
                raise CoordinatorConflict("Watching requires the current confirmed left Home receipt.")
            self._observe(value, head)
            if head["state"] != "done":
                raise CoordinatorConflict("The last Home queue has not completed.")
            self._lease(role)
            if run["phase"] != "watching":
                # Preserve legacy current-cycle measured Home proof before a
                # later cup replaces the run's current Home receipt mapping.
                for identity in run["home_receipts"].values():
                    value["submissions"][identity]["home_verified"] = True
                run["phase"] = "watching"
                value["runs"][run_id] = copy.deepcopy(run)
                self._commit(value)
            return self._public(value)

    def finish(self, role, run_id, reason):
        _role(role)
        _reason(reason)
        with self._transaction() as value:
            run = self._run(value, run_id)
            if run["phase"] == "complete":
                if run["reason"] != reason:
                    raise CoordinatorConflict("Completed batch reason cannot change.")
                return self._public(value)
            if run["settings"].get("continuous_batch") is True:
                raise CoordinatorConflict("Continuous batches keep watching until explicitly stopped.")
            if run["phase"] not in {"active", "finalizing"}:
                raise CoordinatorConflict("Batch is not active.")
            if reason == "workspace_empty" and (run["phase"] != "finalizing"
                    or run.get("confirmed_homes") != ["right", "left"]):
                raise CoordinatorConflict("Both Home completions require measured confirmation.")
            self._lease(role)
            head = value["submissions"].get(value["head"])
            if head is not None:
                self._observe(value, head)
                if head["state"] not in {"done", "not_submitted"}:
                    raise CoordinatorConflict("Resolve the accepted queue before completing the batch.")
            run.update(phase="complete", reason=reason, finished_at=_now())
            value["runs"][run_id] = copy.deepcopy(run)
            self._commit(value)
            return self._public(value)

    def confirm_home(self, role, run_id, submission_id, arm):
        """A trusted worker calls this only AFTER its measured Home check.

        Original owners may finish historical verification after preemption.
        A successor must own the lease and still observe exactly this DONE head
        before adopting it; merely finding an old Home receipt is insufficient.
        """
        _role(role)
        _identifier(submission_id, submission=True)
        if not isinstance(arm, str) or arm not in {"right", "left"}:
            raise CoordinatorError("Invalid Home arm.")
        with self._transaction() as value:
            run = self._run(value, run_id)
            known = run.get("home_receipts", {})
            if known.get(arm) == submission_id:
                item = value["submissions"][submission_id]
                if item.get("home_verified") is not True:
                    item["home_verified"] = True
                    self._commit(value)
                return self._public(value)
            if run["phase"] != "finalizing" or arm in known:
                raise CoordinatorConflict("Batch does not admit this Home confirmation.")
            item = value["submissions"].get(submission_id)
            if (item is None or item["run_id"] != run_id or len(item["steps"]) != 1
                    or item.get("home_cycle", 0) != run.get("home_cycle", 0)
                    or item["steps"][0]["type"] != "home" or item["steps"][0]["body"]["arm"] != arm):
                raise CoordinatorConflict("Receipt is not this arm's final Home queue.")
            self._observe(value, item)
            if item["state"] != "done":
                raise CoordinatorConflict("Home queue has not completed successfully.")
            if item["owner"] != role:
                self._lease(role)
                queue = _queue(self._get("/robot/rws/queue"))
                if (value["head"] != submission_id or queue["state"] != "done"
                        or queue.get("owner") != item["owner"] or queue.get("started_at") != item["started_at"]
                        or _signature(queue["steps"]) != _signature(item["steps"], submitted=True)):
                    raise CoordinatorConflict("Takeover Home receipt is no longer the exact current queue.")
                self._lease(role)
            confirmed = run.setdefault("confirmed_homes", [])
            if arm == "left" and "right" not in confirmed:
                raise CoordinatorConflict("Right Home must be measured before left Home.")
            confirmed.append(arm)
            item["home_verified"] = True
            run.setdefault("home_receipts", {})[arm] = submission_id
            value["runs"][run_id] = copy.deepcopy(run)
            self._commit(value)
            return self._public(value)

    def _end_admission(self, role, run_id, reason, phase):
        _role(role)
        _reason(reason)
        with self._transaction() as value:
            run = self._run(value, run_id)
            if run["phase"] not in {"complete", "stopped", "blocked"}:
                run.update(phase=phase, reason=reason, finished_at=_now())
                value["runs"][run_id] = copy.deepcopy(run)
                self._commit(value)
            # Admission only: an accepted robot queue is never cancelled here.
            return self._public(value)

    def stop(self, role, run_id, reason):
        return self._end_admission(role, run_id, reason, "stopped")

    def block(self, role, run_id, reason):
        return self._end_admission(role, run_id, reason, "blocked")


class CoordinatorHandler(BaseHTTPRequestHandler):
    server_version = "PickerCoordinator"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_args):
        pass

    def _reply(self, status, value):
        raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(raw)

    def _authorized(self):
        supplied = self.headers.get_all("Authorization", [])
        return (len(supplied) == 1 and hmac.compare_digest(
            supplied[0].encode("utf-8"), ("Bearer " + self.server.token).encode("utf-8")))

    def _dispatch(self, *, post):
        try:
            if not self._authorized():
                self._reply(401, {"error": "Coordinator authentication required."})
                return
            parsed = urlsplit(self.path)
            if len(self.path) > 1024 or parsed.scheme or parsed.netloc or parsed.fragment:
                raise CoordinatorError("Invalid route.")
            if post:
                sizes = self.headers.get_all("Content-Length", [])
                if (self.headers.get("Transfer-Encoding") is not None or len(sizes) != 1
                        or not re.fullmatch(r"[0-9]{1,8}", sizes[0]) or not 1 <= int(sizes[0]) <= MAX_BODY
                        or parsed.query):
                    raise CoordinatorError("Invalid request body size.")
                raw = self.rfile.read(int(sizes[0]))
                if len(raw) != int(sizes[0]):
                    raise CoordinatorError("Incomplete request body.")
                body = _decode(raw)
                if not isinstance(body, dict):
                    raise CoordinatorError("Invalid request body.")
                role = _role(body.pop("role", None))
                routes = {"start": {"run_id", "settings"}, "heartbeat": {"run_id", "status"},
                          "prepare_queue": {"run_id", "submission_id", "steps"},
                          "reject_queue": {"run_id", "submission_id"}, "finish": {"run_id", "reason"},
                          "recover": {"run_id", "submission_id", "acknowledged"},
                          "stop": {"run_id", "reason"}, "block": {"run_id", "reason"},
                          "set_phase": {"run_id", "phase"}, "confirm_home": {"run_id", "submission_id", "arm"}}
                routes["watch"] = {"run_id"}
                action = parsed.path.removeprefix("/v1/")
                if parsed.path != "/v1/" + action or action not in routes:
                    self._reply(404, {"error": "Unknown coordinator route."})
                    return
                if set(body) != routes[action]:
                    raise CoordinatorError("Invalid request fields.")
                result = getattr(self.server.store, action)(role, **body)
            else:
                roles = self.headers.get_all("X-Failover-Role", [])
                role = _role(roles[0] if len(roles) == 1 else None)
                query = parse_qs(parsed.query, strict_parsing=True, max_num_fields=2)
                if parsed.path == "/v1/state" and not query:
                    result = self.server.store.state(role)
                elif (parsed.path == "/v1/receipt" and set(query) == {"run_id", "submission_id"}
                      and all(len(item) == 1 for item in query.values())):
                    result = self.server.store.receipt(role, query["run_id"][0], query["submission_id"][0])
                else:
                    self._reply(404, {"error": "Unknown coordinator route."})
                    return
            self._reply(200, result)
        except CoordinatorConflict:
            self._reply(409, {"error": "Operation conflicts with the current batch or queue."})
        except (CoordinatorUnavailable, OSError, TimeoutError):
            self._reply(503, {"error": "Coordinator temporarily unavailable; operation may have committed."})
        except (CoordinatorError, ValueError, TypeError, KeyError, RecursionError):
            self._reply(400, {"error": "Invalid coordinator request or gateway data."})

    def do_GET(self):
        self._dispatch(post=False)

    def do_POST(self):
        self._dispatch(post=True)

    def _unsupported(self):
        if not self._authorized():
            self._reply(401, {"error": "Coordinator authentication required."})
        else:
            self._reply(405, {"error": "Coordinator method is not supported."})

    do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _unsupported


class CoordinatorServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address, store, token):
        self.store, self.token = store, _credential(token)
        self._slots = threading.BoundedSemaphore(16)
        super().__init__(address, CoordinatorHandler)

    def process_request(self, request, address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self._slots.release()

    def handle_error(self, request, address):
        pass  # No credential-bearing tracebacks or request data in logs.


def _read_token(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise CoordinatorError("Credential must be a regular file.")
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise CoordinatorError("Credential file is too large.")
    try:
        return _credential(raw.decode("ascii").strip())
    except UnicodeError as error:
        raise CoordinatorError("Credential file is invalid.") from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("serve", choices=["serve"])
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--gateway-token-file", type=Path, required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8092)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    try:
        store = CoordinatorStore(args.state_file, args.gateway, _read_token(args.gateway_token_file))
        with CoordinatorServer((args.host, args.port), store, _read_token(args.token_file)) as server:
            print("Failover coordinator is listening. No robot commands are issued by this service.", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (CoordinatorError, OSError):
        print("Failover coordinator could not start or access its configured state.", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
