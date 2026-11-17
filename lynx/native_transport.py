"""Standard-library-only native worker lifecycle and bounded pipe transport."""
import json
import math
import os
import select
import struct
import subprocess
import threading
import time


class InferenceError(RuntimeError):
    pass


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


class WorkerTransport:
    def __init__(self, executable, model, *, threads=1, confidence=0.5,
                 timeout=10.0, startup_timeout=90.0, max_detections=32, command=None, perception=False):
        if not _integer(threads, 1, 4) or not _integer(max_detections, 1, 64):
            raise ValueError("Use 1..4 inference threads and 1..64 detections.")
        if not 0 < confidence < 1 or not all(math.isfinite(v) and v > 0 for v in (timeout, startup_timeout)):
            raise ValueError("Invalid inference confidence or deadline.")
        self.model, self.timeout, self.max_detections = model, timeout, max_detections
        self.perception = perception
        self.sequence, self.lock, self.error_log = 0, threading.Lock(), bytearray()
        argv = command or [str(executable), "--param", str(model.param), "--bin", str(model.weights),
            "--input-size", str(model.input_size), "--classes-count", str(len(model.names)),
            "--threads", str(threads), "--confidence", str(confidence), "--iou", "0.7",
            "--max-detections", str(max_detections)]
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, bufsize=0, close_fds=True)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            os.set_blocking(stream.fileno(), False)
        try:
            ready, masks = self._response(0, time.monotonic() + startup_timeout, max_mask_bytes=0)
            if masks or ready.get("ready") is not True or ready.get("protocol_version") != 1 or (perception and ready.get("perception_version") != 2) or ready.get("input_size") != model.input_size or ready.get("classes_count") != len(model.names) or ready.get("max_detections") != max_detections:
                raise InferenceError("Native worker readiness disagrees with the configured model.")
        except BaseException:
            self.close()
            raise


    def _wait(self, stream, write, deadline):
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise InferenceError("Native inference timed out; restart the Lynx picker before scanning again.")
            errors = self.process.stderr
            reads = ([stream] if not write else []) + ([errors] if errors else [])
            try:
                readable, writable, _ = select.select(reads, [stream] if write else [], [], left)
            except (BlockingIOError, InterruptedError):
                continue
            if errors and errors in readable:
                try:
                    chunk = os.read(errors.fileno(), 4096)
                except (BlockingIOError, InterruptedError):
                    # Readiness can change before a nonblocking syscall. Do
                    # not discard simultaneous stdout/stdin readiness below.
                    pass
                else:
                    self.error_log.extend(chunk)
                    del self.error_log[:-8192]
                    if not chunk:
                        errors.close()
                        self.process.stderr = None
            if (write and writable) or (not write and stream in readable):
                return


    def _read(self, count, deadline):
        data = bytearray()
        while len(data) < count:
            self._wait(self.process.stdout, False, deadline)
            try:
                chunk = os.read(self.process.stdout.fileno(), min(65536, count - len(data)))
            except (BlockingIOError, InterruptedError):
                continue
            if not chunk:
                detail = self.error_log.decode("utf-8", errors="replace")[-2000:]
                raise InferenceError("Native worker exited before returning a complete result. " + detail)
            data.extend(chunk)
        return data


    def _write(self, value, deadline):
        pending = memoryview(value).cast("B")
        while pending:
            self._wait(self.process.stdin, True, deadline)
            try:
                sent = os.write(self.process.stdin.fileno(), pending[:65536])
            except (BlockingIOError, InterruptedError):
                continue
            if not sent:
                raise InferenceError("Native worker stopped accepting the request.")
            pending = pending[sent:]


    def _response(self, request_id, deadline, *, max_mask_bytes, max_json_bytes=65536):
        magic, returned_id, status, json_size, mask_size = struct.unpack("<4sIIII", self._read(20, deadline))
        if magic != b"LNXR" or returned_id != request_id or status not in (0, 1) or not 0 < json_size <= max_json_bytes or mask_size > min(64 * 1024 * 1024, max_mask_bytes):
            raise InferenceError("Invalid or oversized native response.")
        document = json.loads(self._read(json_size, deadline))
        if not isinstance(document, dict):
            raise InferenceError("Native response is not an object.")
        if status:
            raise InferenceError(str(document.get("error", "Native inference failed"))[:1000])
        return document, self._read(mask_size, deadline)


    def close(self):
        process = self.process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
