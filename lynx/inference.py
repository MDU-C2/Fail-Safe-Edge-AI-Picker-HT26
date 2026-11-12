"""Bounded, synchronous IPC to one persistent native NCNN worker."""
import json
import math
import os
import select
import struct
import subprocess
import threading
import time
from types import SimpleNamespace

import cv2
import numpy as np

from capture_and_detect import Detector, KEEP_CLASSES, mark_cup_bases


class InferenceError(RuntimeError):
    pass


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


class NativeWorker:
    def __init__(self, executable, model, *, threads=1, confidence=0.5,
                 timeout=10.0, startup_timeout=90.0, max_detections=32, command=None):
        if not _integer(threads, 1, 4) or not _integer(max_detections, 1, 64):
            raise ValueError("Use 1..4 inference threads and 1..64 detections.")
        if not 0 < confidence < 1 or not all(math.isfinite(v) and v > 0 for v in (timeout, startup_timeout)):
            raise ValueError("Invalid inference confidence or deadline.")
        self.model, self.timeout, self.max_detections = model, timeout, max_detections
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
            if masks or ready.get("ready") is not True or ready.get("protocol_version") != 1 or ready.get("input_size") != model.input_size or ready.get("classes_count") != len(model.names) or ready.get("max_detections") != max_detections:
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
            readable, writable, _ = select.select(reads, [stream] if write else [], [], left)
            if errors and errors in readable:
                chunk = os.read(errors.fileno(), 4096)
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
            chunk = os.read(self.process.stdout.fileno(), min(65536, count - len(data)))
            if not chunk:
                detail = self.error_log.decode("utf-8", errors="replace")[-2000:]
                raise InferenceError("Native worker exited before returning a complete result. " + detail)
            data.extend(chunk)
        return data

    def _write(self, value, deadline):
        pending = memoryview(value).cast("B")
        while pending:
            self._wait(self.process.stdin, True, deadline)
            sent = os.write(self.process.stdin.fileno(), pending[:65536])
            pending = pending[sent:]

    def _response(self, request_id, deadline, *, max_mask_bytes):
        magic, returned_id, status, json_size, mask_size = struct.unpack("<4sIIII", self._read(20, deadline))
        if magic != b"LNXR" or returned_id != request_id or status not in (0, 1) or not 0 < json_size <= 65536 or mask_size > min(64 * 1024 * 1024, max_mask_bytes):
            raise InferenceError("Invalid or oversized native response.")
        document = json.loads(self._read(json_size, deadline))
        if not isinstance(document, dict):
            raise InferenceError("Native response is not an object.")
        if status:
            raise InferenceError(str(document.get("error", "Native inference failed"))[:1000])
        return document, self._read(mask_size, deadline)

    def infer(self, image):
        if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3 or not 0 < image.shape[1] <= 1920 or not 0 < image.shape[0] <= 1080:
            raise InferenceError("Expected BGR uint8 image, at most 1920x1080.")
        if not self.lock.acquire(blocking=False):
            raise InferenceError("Inference is already busy; there is no request queue.")
        try:
            if self.process.poll() is not None or self.process.stdin.closed:
                raise InferenceError("Native worker is unavailable; restart the Lynx picker.")
            self.sequence += 1
            deadline = time.monotonic() + self.timeout
            height, width = image.shape[:2]
            self._write(struct.pack("<4sIIIII", b"LNXQ", self.sequence, width, height, image.nbytes, 0), deadline)
            self._write(np.ascontiguousarray(image), deadline)
            document, masks = self._response(self.sequence, deadline, max_mask_bytes=width * height * self.max_detections)
            self._validate(document, masks, width, height)
            return document, masks
        except BaseException:
            # Partial framing, timeout or corrupt geometry must not be reused.
            self.close()
            raise
        finally:
            self.lock.release()

    def _validate(self, doc, masks, width, height):
        if doc.get("width") != width or doc.get("height") != height:
            raise InferenceError("Native response dimensions do not match this observation.")
        elapsed = doc.get("inference_ms")
        if not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0:
            raise InferenceError("Invalid inference duration.")
        detections = doc.get("detections")
        if not isinstance(detections, list) or len(detections) > self.max_detections:
            raise InferenceError("Invalid detection count.")
        offset = 0
        for item in detections:
            if not isinstance(item, dict) or not _integer(item.get("class_id"), 0, len(self.model.names) - 1):
                raise InferenceError("Invalid detection class.")
            score, box, roi = item.get("score"), item.get("box_xyxy"), item.get("mask_bbox")
            if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
                raise InferenceError("Invalid detection confidence.")
            if not isinstance(box, list) or len(box) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in box) or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height):
                raise InferenceError("Invalid detection box.")
            if not isinstance(roi, list) or len(roi) != 4 or any(type(v) is not int for v in roi):
                raise InferenceError("Invalid mask rectangle.")
            x, y, w, h = roi
            if not (0 <= x < width and 0 <= y < height and 0 < w <= width - x and 0 < h <= height - y):
                raise InferenceError("Mask exceeds observation dimensions.")
            if not (x >= math.ceil(box[0]) and y >= math.ceil(box[1]) and x+w <= math.ceil(box[2]) and y+h <= math.ceil(box[3])):
                raise InferenceError("Mask exceeds its detection box.")
            if type(item.get("mask_offset")) is not int or type(item.get("mask_length")) is not int or item["mask_offset"] != offset or item["mask_length"] != w * h:
                raise InferenceError("Invalid mask payload offsets.")
            offset += w * h
            if offset > len(masks):
                raise InferenceError("Truncated mask payload.")
        if offset != len(masks) or np.frombuffer(masks, dtype=np.uint8).max(initial=0) > 1:
            raise InferenceError("Invalid mask bytes.")

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


class LynxDetector:
    def __init__(self, worker, calibration, intrinsics, confidence=0.5):
        self.worker, self.calibration, self.K, self.conf = worker, calibration, intrinsics, confidence
        self.inference_metadata = None

    def analyse(self, rgb, depth):
        result, masks = self.worker.infer(rgb)
        objects, boxes = [], []
        annotated = rgb.copy()
        for index, item in enumerate(result["detections"]):
            name = self.worker.model.names[item["class_id"]]
            if name not in KEEP_CLASSES:
                continue
            x, y, w, h = item["mask_bbox"]
            mask = np.frombuffer(masks, dtype=np.uint8, count=w*h, offset=item["mask_offset"]).reshape(h, w).astype(bool)
            if not mask.any():
                continue
            # Translating intrinsics preserves the existing depth/robot geometry
            # while allocating depth-quality scratch space only for this ROI.
            cropped = SimpleNamespace(calibration=self.calibration, K={**self.K, "cx": self.K["cx"] - x, "cy": self.K["cy"] - y})
            obj = Detector.describe(cropped, name, item["score"], mask, depth[y:y+h, x:x+w])
            # The encoded mask may be cropped again around its nonzero pixels.
            # Retain that inner offset when mapping it into the full camera frame.
            origin = obj["segmentation"]["origin"]
            obj["segmentation"]["origin"] = [origin[0] + x, origin[1] + y]
            ys, xs = np.nonzero(mask)
            obj.update(x_px=round(float(xs.mean()) + x, 1), y_px=round(float(ys.mean()) + y, 1),
                       detection_id=index, bbox_xyxy=[round(v, 1) for v in item["box_xyxy"]])
            objects.append(obj)
            boxes.append(item["box_xyxy"])
            x1, y1, x2, y2 = (int(v) for v in item["box_xyxy"])
            cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 180, 255), 1)
            cv2.putText(annotated, name, (x1, max(12, y1-4)), cv2.FONT_HERSHEY_SIMPLEX, .4, (0, 180, 255), 1)
            cv2.drawMarker(annotated, (int(obj["x_px"]), int(obj["y_px"])), (0, 0, 255), cv2.MARKER_CROSS, 12, 1)
        mark_cup_bases(objects, boxes)
        self.inference_metadata = {"provider": "lynx-native-ncnn", "input_size": self.worker.model.input_size,
            "precision": self.worker.model.precision, "sha256": self.worker.model.sha256,
            "inference_ms": result["inference_ms"]}
        return objects, annotated
