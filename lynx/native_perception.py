"""Small Python adapter for C++ segmentation, depth and placement assessment.

Raw camera bytes cross the pipe once. No image/array/ML package is imported.
"""
import json
import math
import struct
import sys
import time

from .native_transport import InferenceError, WorkerTransport
from .resources import trim_unused_heap


class NativePerception(WorkerTransport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, perception=True, **kwargs)

    def observe(self, rgb_planar, depths, config):
        """Consume the depth list (oldest first); retain latest raw frame outside.

        The caller owns the latest frame for auditing. Earlier burst frames are
        released while streaming to avoid retaining two copies of the burst.
        """
        camera = config["intrinsics"]
        width, height = camera["width"], camera["height"]
        if (type(width) is not int or type(height) is not int
                or not 0 < width <= 1920 or not 0 < height <= 1080
                or len(rgb_planar) != width * height * 3
                or not isinstance(depths, list) or not 1 <= len(depths) <= 9
                or any(len(frame) != width * height * 2 for frame in depths)):
            raise InferenceError("Native perception requires matching, bounded raw camera frames.")
        payload = json.dumps(config, allow_nan=False, separators=(",", ":")).encode()
        if len(payload) > 65536:
            raise InferenceError("Perception configuration exceeds its size limit.")
        if not self.lock.acquire(blocking=False):
            raise InferenceError("Perception is already busy; there is no request queue.")
        try:
            if self.process.poll() is not None or self.process.stdin.closed:
                raise InferenceError("Native perception is unavailable; restart the picker.")
            self.sequence += 1
            deadline = time.monotonic() + self.timeout
            self._write(struct.pack("<4sIIIIIII", b"LNXP", self.sequence, width, height,
                                    len(rgb_planar), len(depths), len(payload), 0), deadline)
            self._write(payload, deadline)
            while depths:
                frame = depths.pop(0)
                self._write(frame, deadline)
                del frame
            # Large bytearray burst buffers can remain in glibc's heap after
            # their Python objects are freed. Return those pages before native
            # inference starts; only latest raw depth remains for auditing.
            if sys.platform == "linux":
                trim_unused_heap()
            self._write(rgb_planar, deadline)
            result, masks = self._response(self.sequence, deadline, max_mask_bytes=0,
                                           max_json_bytes=4 * 1024 * 1024)
            self._validate(result, width, height, config["grid_snapshot"]["locations"])
            return result
        except BaseException:
            self.close()
            raise
        finally:
            self.lock.release()

    def _validate(self, value, width, height, locations):
        if (value.get("perception_version") != 2 or value.get("width") != width
                or value.get("height") != height):
            raise InferenceError("Native perception returned a different observation contract.")
        # JSON NaN/Infinity must never reach inventory or motion planning.
        try:
            json.dumps(value, allow_nan=False)
        except (ValueError, TypeError) as error:
            raise InferenceError("Native perception returned nonfinite measurements.") from error
        elapsed = value.get("inference_ms")
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
            raise InferenceError("Native perception returned an invalid inference duration.")
        objects, slots = value.get("objects"), value.get("slots")
        if not isinstance(objects, list) or len(objects) > self.max_detections:
            raise InferenceError("Native perception returned an invalid object count.")
        for obj in objects:
            if (not isinstance(obj, dict) or obj.get("class") not in self.model.names
                    or type(obj.get("detection_id")) is not int):
                raise InferenceError("Native perception returned an invalid object identity.")
            for key in ("x_px", "y_px", "confidence"):
                if type(obj.get(key)) not in (int, float):
                    raise InferenceError("Native perception returned missing pixel/confidence data.")
            if not (0 <= obj["x_px"] < width and 0 <= obj["y_px"] < height and 0 <= obj["confidence"] <= 1):
                raise InferenceError("Native perception returned out-of-frame coordinates.")
            for axis in ("x", "y", "z"):
                coordinate = obj.get("robot_" + axis + "_mm")
                if coordinate is not None and type(coordinate) not in (int, float):
                    raise InferenceError("Native perception returned invalid robot coordinates.")
        if (not isinstance(slots, list) or any(not isinstance(slot, dict) for slot in slots)
                or [slot.get("id") for slot in slots] != [place["id"] for place in locations]
                or any(slot.get("state") not in ("clear", "occupied", "unknown") for slot in slots)):
            raise InferenceError("Native placement evidence does not match the requested grid.")
