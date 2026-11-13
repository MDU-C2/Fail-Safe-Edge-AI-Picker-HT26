"""Deadline-bounded local and remote segmentation providers.

Both heavy inference and blocking HTTP run in killable child processes. A
daemon exchange thread moves one bounded message through a pipe; the caller
never blocks in send/recv and kills the child if the total budget expires.
"""

import math
import multiprocessing
import os
import queue
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from inference_protocol import (InferenceError, InferenceTimeout, MAX_REQUEST_BYTES,
                                MAX_RESPONSE_BYTES, decode_request, json_bytes,
                                load_json, model_sha256, validate_response,
                                validate_settings)


def _deadline(timeout_s):
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 300:
        raise InferenceError("Invalid inference deadline")
    return time.monotonic() + timeout_s


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise InferenceTimeout("Inference request exceeded its total deadline")
    return remaining


def _exchange(connection, payload, deadline, limit=MAX_RESPONSE_BYTES):
    """Pipe framing can block on a partial write: perform it off the caller."""
    result = queue.Queue(maxsize=1)
    def run():
        try:
            if payload is not None:
                connection.send_bytes(payload)
            result.put((True, connection.recv_bytes(maxlength=limit)))
        except (OSError, EOFError, ValueError, TypeError):
            result.put((False, None))
    threading.Thread(target=run, daemon=True, name="inference-exchange").start()
    try:
        ok, raw = result.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise InferenceTimeout("Inference request exceeded its total deadline") from None
    _remaining(deadline)
    if not ok:
        raise InferenceError("Inference worker became unavailable")
    return raw


def _kill(process, connection):
    # Bound cleanup too. Killing closes the other end and releases any exchange
    # thread blocked in a pipe read or write. Never reuse a timed-out pipe.
    if process is not None:
        try:
            if process.is_alive():
                process.terminate()
            process.join(timeout=0.15)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.15)
            if not process.is_alive():
                process.close()
        except (OSError, ValueError, AssertionError):
            pass
    if connection is not None:
        try:
            connection.close()
        except OSError:
            pass


def _send_error(connection):
    try:
        connection.send_bytes(b'{"error":"inference_failed"}')
    except (OSError, EOFError):
        pass


def _clear_control_environment():
    """Child workers need no inherited gateway/controller or service tokens."""
    for name in list(os.environ):
        if name.startswith(("GATEWAY_", "RWS_", "ABB_", "ROBOT_")) or name == "INFERENCE_TOKEN":
            os.environ.pop(name, None)


def _local_child(connection, model_path, expected_hash, settings, threads):
    _clear_control_environment()
    try:
        from inference_worker import ModelRunner
        runner = ModelRunner(model_path, settings, threads)
        if runner.model_hash != expected_hash:
            raise InferenceError("Segmentation weights changed during startup")
        connection.send_bytes(b'{"ready":true}')
        while True:
            raw = connection.recv_bytes(maxlength=MAX_REQUEST_BYTES)
            request = load_json(raw, MAX_REQUEST_BYTES)
            try:
                response = runner.predict(request)
                connection.send_bytes(json_bytes(response, MAX_RESPONSE_BYTES))
            except Exception:
                _send_error(connection)
    except (EOFError, OSError):
        pass
    except Exception:
        _send_error(connection)
    finally:
        connection.close()


def _remote_child(connection, url, token, payload, socket_timeout):
    _clear_control_environment()
    try:
        import urllib.error
        import urllib.request
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, file_pointer, code, message, headers, new_url):
                return None
        # Explicitly bypass environment proxies: LAN requests and bearer tokens
        # must go only to the configured endpoint, never a redirected host.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(url, data=payload, method="POST",
                    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                             "Accept": "application/json", "Accept-Encoding": "identity"})
        with opener.open(request, timeout=socket_timeout) as response:
            if response.status != 200 or response.headers.get_content_type() != "application/json":
                raise InferenceError("Remote inference rejected the request")
            length = response.headers.get("Content-Length")
            if length is not None and (len(length) > 10 or not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES):
                raise InferenceError("Remote inference payload exceeds size limit")
            chunks, size = [], 0
            while True:
                chunk = response.read(min(64 * 1024, MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise InferenceError("Remote inference payload exceeds size limit")
                chunks.append(chunk)
        connection.send_bytes(b"".join(chunks))
    except Exception:
        _send_error(connection)
    finally:
        connection.close()


class ProcessBackend:
    """One preloaded CPU model process, with no queue of stale observations."""

    def __init__(self, model_path, settings, threads=3, startup_timeout_s=60):
        validate_settings(settings)
        if type(threads) is not int or not 1 <= threads <= 32:
            raise InferenceError("Inference CPU threads must be between 1 and 32")
        _deadline(startup_timeout_s)
        self.model_path = str(Path(model_path).resolve())
        self.model_hash = model_sha256(self.model_path)
        self.settings = dict(settings)
        self.threads = threads
        self.startup_timeout_s = startup_timeout_s
        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._process = self._connection = None
        self._ready = self._closed = False
        self._context = multiprocessing.get_context("spawn")
        self._target = _local_child

    @property
    def ready(self):
        with self._state_lock:
            return bool(self._ready and self._process is not None and self._process.is_alive())

    def _stop(self, expected_process=None):
        with self._state_lock:
            if expected_process is not None and self._process is not expected_process:
                return
            process, connection = self._process, self._connection
            self._process = self._connection = None
            self._ready = False
        _kill(process, connection)

    def _start(self, deadline):
        if self.ready:
            return
        self._stop()
        with self._state_lock:
            if self._closed:
                raise InferenceError("Inference worker has been closed")
            connection, child_connection = self._context.Pipe(duplex=True)
            process = self._context.Process(target=self._target,
                args=(child_connection, self.model_path, self.model_hash, self.settings, self.threads),
                daemon=True, name="picker-segmentation")
            self._process, self._connection = process, connection
            try:
                process.start()
            except Exception:
                self._process = self._connection = None
                connection.close()
                raise InferenceError("Inference worker could not start") from None
            finally:
                child_connection.close()
        try:
            response = load_json(_exchange(connection, None, deadline, 1024), 1024)
            if response != {"ready": True}:
                raise InferenceError("Inference worker could not load its local model")
            with self._state_lock:
                if self._process is not process or self._closed:
                    raise InferenceError("Inference worker has been closed")
                self._ready = True
        except Exception:
            self._stop(process)
            raise

    def start(self):
        if not self._request_lock.acquire(blocking=False):
            raise InferenceError("Inference worker is busy")
        try:
            self._start(_deadline(self.startup_timeout_s))
        finally:
            self._request_lock.release()
        return self

    def predict(self, request, timeout_s):
        deadline = _deadline(timeout_s)
        if not self._request_lock.acquire(blocking=False):
            raise InferenceError("Inference worker is busy")
        process = None
        try:
            if request.get("model_sha256") != self.model_hash or request.get("settings") != self.settings:
                raise InferenceError("Inference request does not match the local worker configuration")
            # Validate and bound before transferring anything to the child.
            decode_request(request)
            payload = json_bytes(request, MAX_REQUEST_BYTES)
            self._start(deadline)
            with self._state_lock:
                process, connection = self._process, self._connection
                if process is None or self._closed:
                    raise InferenceError("Inference worker has been closed")
            response = load_json(_exchange(connection, payload, deadline), MAX_RESPONSE_BYTES)
            if "error" in response:
                raise InferenceError("Local inference failed")
            response = validate_response(response, request)
            _remaining(deadline)
            return response
        except Exception:
            if process is not None:
                self._stop(process)
            raise
        finally:
            self._request_lock.release()

    def close(self):
        with self._state_lock:
            self._closed = True
        self._stop()


class RemoteBackend:
    """Authenticated HTTP provider with a total deadline, including slow reads."""

    def __init__(self, url, token):
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname and
                     parsed.username is None and parsed.password is None and
                     not parsed.query and not parsed.fragment and parsed.port != 0)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise InferenceError("Invalid remote inference address")
        if (not isinstance(token, str) or not token or len(token) > 4096 or
                any(ord(char) < 33 or ord(char) > 126 for char in token)):
            raise InferenceError("An inference authentication token is required")
        self._url = url.rstrip("/")
        if not self._url.endswith("/infer"):
            self._url += "/infer"
        self._token = token
        self._context = multiprocessing.get_context("spawn")
        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._process = self._connection = None
        self._closed = False

    def _stop(self):
        with self._state_lock:
            process, connection = self._process, self._connection
            self._process = self._connection = None
        _kill(process, connection)

    def predict(self, request, timeout_s):
        deadline = _deadline(timeout_s)
        if not self._request_lock.acquire(blocking=False):
            raise InferenceError("Remote inference worker is busy")
        try:
            decode_request(request)
            payload = json_bytes(request, MAX_REQUEST_BYTES)
            with self._state_lock:
                if self._closed:
                    raise InferenceError("Remote inference worker has been closed")
                connection, child_connection = self._context.Pipe(duplex=True)
                process = self._context.Process(target=_remote_child,
                    args=(child_connection, self._url, self._token, payload, _remaining(deadline)),
                    daemon=True, name="picker-inference-http")
                self._process, self._connection = process, connection
                try:
                    process.start()
                except Exception:
                    raise InferenceError("Remote inference transport could not start") from None
                finally:
                    child_connection.close()
            response = load_json(_exchange(connection, None, deadline), MAX_RESPONSE_BYTES)
            if "error" in response:
                raise InferenceError("Remote inference request failed")
            response = validate_response(response, request)
            _remaining(deadline)
            return response
        finally:
            self._stop()
            self._request_lock.release()

    def close(self):
        with self._state_lock:
            self._closed = True
        self._stop()
