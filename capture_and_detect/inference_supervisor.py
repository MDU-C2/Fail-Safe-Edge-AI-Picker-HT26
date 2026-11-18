"""Select an inference worker without transferring robot or inventory ownership.

Only normal, validated predictions count as accepted work. Recovery probes use
the last image but never become detections. Status is coordinator-authored and
the audit log survives restarts; an old result never survives as active state.
"""

from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid


class InferenceUnavailable(ValueError):
    """No validated result is available for this observation."""


class InferenceAuditError(InferenceUnavailable):
    """The required inference audit cannot be persisted."""


class _BackendBusy(InferenceUnavailable):
    pass


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _positive(value, name):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and greater than zero.")
    return number


class InferenceSupervisor:
    """Prefer Mac inference, fall back locally, and prove recovery by inference.

    ``observation_group()`` yields its starting generation. Callers collecting
    several observations must discard and repeat a group if ``generation``
    changes: fallback can happen inside a group, but promotion cannot.

    A supplied ``event_path`` is mandatory audit storage: failures inhibit
    further inference acceptance. Omitting it is useful for tests/local use.
    ``snapshot`` reads memory only. ``on_update`` is called outside locks and
    must be nonblocking; callback exceptions cannot change inference decisions.
    """

    def __init__(self, local_backend, remote_backend=None, *,
                 validate_response=None, remote_timeout_s=2.0,
                 local_timeout_s=5.0, probe_interval_s=3.0,
                 recovery_successes=3, event_path=None, on_update=None,
                 clock=time.monotonic, local_provider="pi", event_limit=200):
        if local_provider not in ("pi", "local"):
            raise ValueError("local_provider must be 'pi' or 'local'.")
        if type(recovery_successes) is not int or recovery_successes < 1:
            raise ValueError("recovery_successes must be a positive integer.")
        if type(event_limit) is not int or event_limit < 2:
            raise ValueError("event_limit must be an integer of at least two.")
        self.local_backend = local_backend
        self.remote_backend = remote_backend
        self.local_provider = local_provider
        self.validate_response = validate_response
        self.remote_timeout_s = _positive(remote_timeout_s, "remote_timeout_s")
        self.local_timeout_s = _positive(local_timeout_s, "local_timeout_s")
        self.probe_interval_s = _positive(probe_interval_s, "probe_interval_s")
        self.on_update = on_update
        self._clock = clock
        self._path = Path(event_path) if event_path is not None else None
        self._event_limit = event_limit
        self._state_lock = threading.RLock()
        self._transition_lock = threading.Lock()
        self._operation_lock = threading.Lock()
        self._backend_locks = {"mac": threading.Lock(), local_provider: threading.Lock()}
        self._closed = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._started = False
        self._group_owner = None
        self._group_depth = 0
        self._operation_owner = None
        self._last_request = None
        self._probe_in_flight = False
        self._audit_failed = None
        self._line_count = 0
        self._needs_separator = False
        self._event_sequence = 0
        self._status = {
            "state": "mac_ready" if remote_backend is not None else "local_ready",
            "enabled": remote_backend is not None,
            "local_provider": local_provider,
            "selected_provider": "mac" if remote_backend is not None else local_provider,
            "active_provider": None,
            "in_flight_provider": None,
            "reason": "Waiting for the first accepted inference result.",
            "failure": None,
            "last_result": None,
            "counters": {"mac": 0, local_provider: 0},
            "recovery": {"successes": 0, "required": recovery_successes,
                         "next_probe_at": None, "ready": False, "in_flight": False},
            "events": [],
            "session_id": str(uuid.uuid4()),
            "generation": 0,
            "revision": 0,
        }
        skipped = self._restore()
        self._transition(message=("Inference supervisor started; no previous result is active."
                                  + (f" Ignored {skipped} damaged audit entries." if skipped else "")),
                         level="warning" if skipped else "info")

    @property
    def generation(self):
        with self._state_lock:
            return self._status["generation"]

    def snapshot(self):
        with self._state_lock:
            return deepcopy(self._status)

    def _restore(self):
        if self._path is None:
            return 0
        history = deque(maxlen=self._event_limit)
        skipped = 0
        try:
            with self._path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    self._line_count += 1
                    self._needs_separator = not line.endswith("\n")
                    try:
                        entry = json.loads(line)
                        counters = entry["counters"]
                        if (not isinstance(entry, dict) or entry.get("schema_version") != 1
                                or not isinstance(entry.get("id"), str)
                                or not isinstance(entry.get("time"), str)
                                or not isinstance(entry.get("message"), str)
                                or not isinstance(counters, dict)
                                or any(type(v) is not int or v < 0 for v in counters.values())):
                            raise ValueError("Invalid audit record")
                        for provider in self._status["counters"]:
                            self._status["counters"][provider] = max(
                                self._status["counters"][provider], counters.get(provider, 0))
                        history.append(entry)
                    except (ValueError, TypeError, KeyError, AttributeError):
                        skipped += 1
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise InferenceAuditError(f"Cannot read inference audit {self._path}: {exc}") from exc
        self._status["events"] = list(history)
        return skipped

    def _persist(self, event, history):
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as stream:
            if self._needs_separator:
                stream.write("\n")
            stream.write(json.dumps(event, allow_nan=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._needs_separator = False
        self._line_count += 1
        if self._line_count > 2 * self._event_limit:
            temporary = self._path.with_name(self._path.name + f".{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("x", encoding="utf-8") as stream:
                    for item in history:
                        stream.write(json.dumps(item, allow_nan=False, separators=(",", ":")) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self._path)
                directory = os.open(self._path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                self._line_count = len(history)
            finally:
                if temporary.exists():
                    temporary.unlink()

    def _notify(self, snapshot):
        callback = self.on_update
        if callback is not None:
            try:
                callback(snapshot)
            except Exception:
                # UI/log subscribers do not have authority over accepted results.
                pass

    def _transition(self, update=None, *, message=None, level="info", provider=None, request=None):
        error = None
        with self._transition_lock:
            with self._state_lock:
                if self._audit_failed:
                    raise InferenceAuditError(self._audit_failed)
                candidate = deepcopy(self._status)
            if update is not None:
                update(candidate)
            candidate["revision"] += 1
            if message is not None:
                self._event_sequence += 1
                event = {"schema_version": 1,
                         "id": f"{candidate['session_id']}:{self._event_sequence}",
                         "session_id": candidate["session_id"], "time": _utc_now(),
                         "level": level, "message": message, "provider": provider,
                         "request_id": (request or {}).get("request_id"),
                         "observation_id": (request or {}).get("observation_id"),
                         "counters": deepcopy(candidate["counters"])}
                candidate["events"] = (candidate["events"] + [event])[-self._event_limit:]
                try:
                    self._persist(event, candidate["events"])
                except (OSError, ValueError, TypeError) as exc:
                    error = f"Inference audit unavailable; new results are blocked: {exc}"
                    with self._state_lock:
                        self._audit_failed = error
                        candidate = deepcopy(self._status)
                    candidate.update(state="audit_failed", reason=error, in_flight_provider=None)
                    candidate["recovery"]["ready"] = False
                    candidate["recovery"]["in_flight"] = False
            with self._state_lock:
                self._status = candidate
                snapshot = deepcopy(candidate)
        self._notify(snapshot)
        if error:
            raise InferenceAuditError(error)
        return snapshot

    def start(self):
        with self._state_lock:
            self._check_open()
            if self._started:
                return self
            self._started = True
            if self.remote_backend is not None:
                self._thread = threading.Thread(target=self._probe_loop,
                                                name="inference-recovery", daemon=True)
                self._thread.start()
        return self

    def _check_open(self):
        if self._closed.is_set():
            raise InferenceUnavailable("Inference supervisor is closed.")
        if self._audit_failed:
            raise InferenceAuditError(self._audit_failed)

    def _promote_at_boundary(self):
        def update(status):
            self._check_open()
            if status["selected_provider"] != "mac" and status["recovery"]["ready"]:
                status.update(selected_provider="mac", state="switching_to_mac",
                              reason="Mac passed recovery checks; awaiting an accepted normal result.")
                status["generation"] += 1
                status["recovery"]["ready"] = False
                status["recovery"]["next_probe_at"] = None
        with self._state_lock:
            ready = self._status["recovery"]["ready"]
        if ready:
            self._transition(update, message="Mac recovery verified; selecting Mac for the next observation.",
                             provider="mac")

    @contextmanager
    def observation_group(self):
        identity = threading.get_ident()
        with self._state_lock:
            self._check_open()
            if self._group_owner not in (None, identity):
                raise InferenceUnavailable("Another observation group is already in progress.")
            if self._operation_owner not in (None, identity):
                raise InferenceUnavailable("An inference operation is already running in another thread.")
            outermost = self._group_depth == 0
            self._group_owner = identity
            self._group_depth += 1
        try:
            if outermost:
                self._promote_at_boundary()
            yield self.generation
        finally:
            with self._state_lock:
                self._group_depth -= 1
                if self._group_depth == 0:
                    self._group_owner = None

    def _validated(self, response, request):
        if not isinstance(response, dict):
            raise ValueError("Inference response must be an object.")
        if self.validate_response is not None:
            validated = self.validate_response(response, request)
            if validated is not None:
                response = validated
        else:
            for key in ("request_id", "observation_id", "width", "height", "image_sha256",
                        "model_sha256", "settings"):
                if key not in request or response.get(key) != request[key]:
                    raise ValueError(f"Inference response does not match {key}.")
            if not isinstance(response.get("detections"), list):
                raise ValueError("Inference detections must be a list.")
        if not isinstance(response, dict):
            raise ValueError("Validated inference response must be an object.")
        return deepcopy(response)

    def _call_backend(self, provider, request, timeout_s):
        """One call per backend, no backlog, and no late callback can accept work.

        Production backends also terminate their process/transport on deadline.
        This outer deadline protects the coordinator from a faulty implementation;
        its occupied backend remains unavailable until that call actually ends.
        """
        self._check_open()
        backend_lock = self._backend_locks[provider]
        if not backend_lock.acquire(blocking=False):
            raise _BackendBusy(f"{provider} inference worker is still busy with an earlier request.")
        backend = self.remote_backend if provider == "mac" else self.local_backend
        done = threading.Event()
        outcome = {}
        started = self._clock()
        real_started = time.monotonic()

        def run():
            try:
                outcome["response"] = backend.predict(deepcopy(request), timeout_s)
            except Exception as exc:
                outcome["error"] = exc
            finally:
                backend_lock.release()
                done.set()

        threading.Thread(target=run, name=f"inference-{provider}", daemon=True).start()
        while not done.wait(min(0.02, timeout_s)):
            self._check_open()
            if (time.monotonic() - real_started >= timeout_s
                    or self._clock() - started >= timeout_s):
                raise TimeoutError(f"{provider} inference exceeded its {timeout_s:g}s deadline.")
        self._check_open()
        duration = self._clock() - started
        if duration > timeout_s or time.monotonic() - real_started > timeout_s:
            raise TimeoutError(f"{provider} inference exceeded its {timeout_s:g}s deadline.")
        if "error" in outcome:
            raise outcome["error"]
        validated = self._validated(outcome["response"], request)
        self._check_open()
        duration = self._clock() - started
        if duration > timeout_s or time.monotonic() - real_started > timeout_s:
            raise TimeoutError(f"{provider} inference validation exceeded its {timeout_s:g}s deadline.")
        return validated, max(0.0, duration)

    def _fall_back(self, error, request):
        reason = f"Mac inference failed: {str(error)[:300]}"

        def update(status):
            self._check_open()
            if status["selected_provider"] != self.local_provider:
                status["generation"] += 1
            status.update(selected_provider=self.local_provider, state="switching_to_pi",
                          reason=reason, in_flight_provider=None)
            status["failure"] = {"provider": "mac", "reason": reason, "time": _utc_now()}
            status["recovery"].update(successes=0, ready=False,
                                      next_probe_at=self._clock() + self.probe_interval_s)
        self._transition(update, message=reason + "; trying local inference.",
                         level="warning", provider="mac", request=request)
        self._wake.set()

    def _accept(self, response, provider, request, duration, generation):
        metadata = {"provider": provider, "request_id": request["request_id"],
                    "observation_id": request["observation_id"],
                    "duration_ms": round(duration * 1000, 2), "accepted_at": _utc_now(),
                    "session_id": self._status["session_id"], "generation": generation}

        def update(status):
            self._check_open()
            if status["generation"] != generation or status["selected_provider"] != provider:
                raise InferenceUnavailable("Inference result was superseded and cannot be accepted.")
            status["counters"][provider] += 1
            state = "mac_active" if provider == "mac" else "pi_active" if provider == "pi" else "local_active"
            if provider != "mac" and status["recovery"]["ready"]:
                state = "failback_pending"
            reason = f"Validated {provider} result accepted."
            if provider == "mac":
                status["failure"] = None
            elif status["failure"] and status["failure"]["provider"] == "mac":
                reason = f"Using {provider}: {status['failure']['reason']}"
            status.update(state=state, active_provider=provider, in_flight_provider=None,
                          last_result=deepcopy(metadata), reason=reason)
        self._transition(update, message=f"{provider.capitalize()} result accepted.",
                         provider=provider, request=request)
        self._check_open()
        response["_inference"] = metadata
        return response

    def predict(self, request):
        if not isinstance(request, dict):
            raise ValueError("Inference request must be an object.")
        request = deepcopy(request)
        if not self._operation_lock.acquire(blocking=False):
            raise InferenceUnavailable("Another inference operation is already in progress.")
        try:
            with self._state_lock:
                self._operation_owner = threading.get_ident()
            self.start()
            with self._state_lock:
                if self._group_owner not in (None, threading.get_ident()):
                    raise InferenceUnavailable("An observation group owns inference in another thread.")
                grouped = self._group_depth > 0
                self._last_request = deepcopy(request)
            if not grouped:
                self._promote_at_boundary()
            with self._state_lock:
                provider = self._status["selected_provider"]
            if provider == "mac":
                try:
                    self._transition(lambda status: status.update(in_flight_provider="mac"))
                    result, duration = self._call_backend("mac", request, self.remote_timeout_s)
                    return self._accept(result, "mac", request, duration, self.generation)
                except InferenceAuditError:
                    raise
                except Exception as exc:
                    self._check_open()
                    self._fall_back(exc, request)
            provider = self.local_provider
            generation = self.generation
            try:
                self._transition(lambda status: status.update(in_flight_provider=provider))
                result, duration = self._call_backend(provider, request, self.local_timeout_s)
                return self._accept(result, provider, request, duration, generation)
            except InferenceAuditError:
                raise
            except Exception as exc:
                self._check_open()
                reason = f"No usable inference result: local worker failed: {str(exc)[:300]}"
                self._transition(lambda status: status.update(
                    state="unavailable", reason=reason, in_flight_provider=None,
                    failure={"provider": provider, "reason": reason, "time": _utc_now()}),
                    message=reason, level="error", provider=provider, request=request)
                raise InferenceUnavailable(reason) from exc
        finally:
            with self._state_lock:
                self._operation_owner = None
            self._operation_lock.release()

    def _probe_once(self):
        """Run one due recovery check; also provides a deterministic test seam."""
        with self._state_lock:
            if (self._closed.is_set() or self._audit_failed or self.remote_backend is None
                    or self._probe_in_flight or self._last_request is None
                    or self._status["selected_provider"] == "mac"
                    or self._status["recovery"]["ready"]
                    or self._status["recovery"]["next_probe_at"] is None
                    or self._clock() < self._status["recovery"]["next_probe_at"]):
                return False
            self._probe_in_flight = True
            request = deepcopy(self._last_request)
            # A probe is a new attempt against the same immutable observation.
            # Its identity must never collide with an operational call/cache key.
            request["request_id"] = f"probe-{uuid.uuid4().hex}"
            generation = self._status["generation"]
        try:
            self._transition(lambda status: status["recovery"].update(in_flight=True))
            try:
                self._call_backend("mac", request, self.remote_timeout_s)
                error = None
            except _BackendBusy:
                # A timed-out operation may still be unwinding; never queue probes.
                self._transition(lambda status: status["recovery"].update(
                    in_flight=False, next_probe_at=self._clock() + self.probe_interval_s))
                return False
            except Exception as exc:
                error = exc
            self._check_open()
            with self._state_lock:
                if self._status["generation"] != generation or self._status["selected_provider"] == "mac":
                    return False

            def update(status):
                self._check_open()
                recovery = status["recovery"]
                recovery["successes"] = recovery["successes"] + 1 if error is None else 0
                recovery["ready"] = recovery["successes"] >= recovery["required"]
                recovery.update(in_flight=False, next_probe_at=(
                    None if recovery["ready"] else self._clock() + self.probe_interval_s))
                if recovery["ready"]:
                    status.update(state="failback_pending",
                                  reason="Mac passed recovery checks; waiting for an observation boundary.")
                elif error is None and status["state"] != "unavailable":
                    status.update(state="mac_probing", reason="Local inference remains selected while Mac recovery is checked.")
            self._transition(update, message=("Mac recovery inference passed." if error is None
                                             else f"Mac recovery inference failed: {str(error)[:300]}"),
                             level="info" if error is None else "warning", provider="mac", request=request)
            return error is None
        finally:
            with self._state_lock:
                self._probe_in_flight = False

    def _probe_loop(self):
        while not self._closed.is_set():
            try:
                self._probe_once()
            except InferenceUnavailable:
                if self._closed.is_set() or self._audit_failed:
                    break
            except Exception:
                # Operational requests still validate independently if a probe fails.
                pass
            self._wake.wait(0.1)
            self._wake.clear()

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        self._wake.set()

        def update(status):
            status.update(state="stopped", in_flight_provider=None,
                          reason="Inference supervisor stopped.")
            status["generation"] += 1
            status["recovery"].update(ready=False, in_flight=False, next_probe_at=None)
        try:
            self._transition(update, message="Inference supervisor stopped.")
        except InferenceAuditError:
            pass
        for backend in (self.remote_backend, self.local_backend):
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=0.2)
