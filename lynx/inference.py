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

from picker_workflow import Detector, KEEP_CLASSES, mark_cup_bases
from picker_depth import EDGE_PX


from .native_transport import InferenceError, WorkerTransport, _integer


class NativeWorker(WorkerTransport):
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
            # Include the erosion halo so a crop boundary cannot look like a
            # foreground mask edge. Clip only at the real image boundary, where
            # OpenCV must retain the same border behavior as the full image.
            x0, y0 = max(0, x - EDGE_PX), max(0, y - EDGE_PX)
            x1, y1 = min(depth.shape[1], x + w + EDGE_PX), min(depth.shape[0], y + h + EDGE_PX)
            padded_mask = np.zeros((y1 - y0, x1 - x0), dtype=bool)
            padded_mask[y-y0:y-y0+h, x-x0:x-x0+w] = mask
            # Depth samples remain read-only views of the retained raw frames;
            # the newest raw depth is still used for placement and saved images.
            cropped = SimpleNamespace(
                calibration=self.calibration,
                K={**self.K, "cx": self.K["cx"] - x0, "cy": self.K["cy"] - y0},
                _depth_samples=tuple(sample[y0:y1, x0:x1]
                                     for sample in getattr(self, "_depth_samples", ())))
            obj = Detector.describe(cropped, name, item["score"], padded_mask, depth[y0:y1, x0:x1])
            # The encoded mask may be cropped again around its nonzero pixels.
            # Retain that inner offset when mapping it into the full camera frame.
            origin = obj["segmentation"]["origin"]
            obj["segmentation"]["origin"] = [origin[0] + x0, origin[1] + y0]
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
