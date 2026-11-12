"""Read the gateway's immutable latest pair in the coordinator's worker thread."""

import asyncio
from concurrent.futures import CancelledError as FutureCancelled, TimeoutError as FutureTimeout
from datetime import datetime, timezone
import math
import time

import numpy as np

from camera_client import CameraError


class LocalCameraClient:
    def __init__(self, service, loop, *, api, max_age=5.0):
        if isinstance(max_age, bool) or not isinstance(max_age, (int, float)) or not math.isfinite(max_age) or max_age <= 0:
            raise ValueError("Camera maximum age must be positive and finite.")
        self.service, self.loop, self.api = service, loop, api
        self.max_age = max_age

    def intrinsics(self):
        value = self.service.get_intrinsics()
        if value is None or not self.service.is_connected():
            raise CameraError("The Lynx camera is not ready.")
        data = value.model_dump()
        for key in ("width", "height"):
            if type(data.get(key)) is not int or data[key] <= 0:
                raise CameraError("Invalid camera dimensions.")
        for key in ("fx", "fy", "cx", "cy"):
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or key in ("fx", "fy") and value <= 0:
                raise CameraError("Invalid camera intrinsics.")
        return data

    def capture(self, timeout_s=10):
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise CameraError("Capture timeout must be positive and finite.")
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is self.loop:
            raise CameraError("Capture must run in the coordinator thread.")
        if self.loop.is_closed() or not self.loop.is_running():
            raise CameraError("The camera event loop is not running.")
        started = time.monotonic()
        observed = datetime.now(timezone.utc)
        status = self.service.get_status()
        if not status.connected or type(status.sequence) is not int or status.sequence < 1:
            raise CameraError("The Lynx camera has not published a frame.")
        intrinsics = self.intrinsics()
        wait = self.service.wait_for_frame_set(status.sequence, timeout_s)
        try:
            pending = asyncio.run_coroutine_threadsafe(wait, self.loop)
        except RuntimeError as error:
            wait.close()
            raise CameraError("The camera event loop stopped before capture.") from error
        try:
            pair = pending.result(timeout=timeout_s + 0.1)
        except FutureTimeout as error:
            pending.cancel()
            raise CameraError("Timed out waiting for a new camera frame.") from error
        except FutureCancelled as error:
            raise CameraError("Camera capture was cancelled during shutdown.") from error
        if pair is None or not self.service.is_connected():
            raise CameraError("No fresh camera frame; camera disconnected or timed out.")
        rgb, depth = pair.rgb, pair.depth
        if rgb is None or depth is None:
            raise CameraError("The camera pair is incomplete.")
        if any(type(value) is not int for value in (pair.sequence, rgb.sequence, depth.sequence)) or pair.sequence <= status.sequence or rgb.sequence != pair.sequence or depth.sequence != pair.sequence:
            raise CameraError("RGB and depth sequences do not match a new observation.")
        if rgb.timestamp != pair.timestamp or depth.timestamp != pair.timestamp:
            raise CameraError("RGB and depth timestamps do not match.")
        if not isinstance(pair.timestamp, datetime) or pair.timestamp.tzinfo is None or pair.timestamp.utcoffset() is None:
            raise CameraError("Camera timestamp has no timezone.")
        age = (datetime.now(timezone.utc) - pair.timestamp).total_seconds()
        if not 0 <= age <= self.max_age:
            raise CameraError("The camera frame is stale or has a future timestamp.")
        if (rgb.width, rgb.height) != (depth.width, depth.height):
            raise CameraError("RGB and aligned depth dimensions do not match.")
        width, height = rgb.width, rgb.height
        if type(width) is not int or type(height) is not int or not 0 < width <= 1920 or not 0 < height <= 1080:
            raise CameraError("Unsupported camera dimensions.")
        if self.intrinsics() != intrinsics:
            raise CameraError("Camera intrinsics changed during capture. Restart and scan again.")
        if (width, height) != (intrinsics["width"], intrinsics["height"]):
            raise CameraError("Image dimensions do not match the camera intrinsics.")
        if rgb.pixel_format != "BGR888p" or depth.pixel_format != "RAW16":
            raise CameraError("Unsupported RGB/depth format.")
        if not isinstance(rgb.data, bytes) or not isinstance(depth.data, bytes):
            raise CameraError("Camera payloads must be immutable bytes.")
        if len(rgb.data) != width * height * 3 or len(depth.data) != width * height * 2:
            raise CameraError("RGB/depth payload size does not match its dimensions.")
        # Only RGB needs reordering. Depth safely retains the immutable bytes.
        image = np.frombuffer(rgb.data, np.uint8).reshape(3, height, width).transpose(1, 2, 0).copy()
        depth_image = np.frombuffer(depth.data, "<u2").reshape(height, width)
        if time.monotonic() - started > timeout_s:
            raise CameraError("Camera capture deadline expired.")
        return image, depth_image, {
            "sequence": pair.sequence, "timestamp": pair.timestamp.isoformat(),
            "observed_at": observed.isoformat(), "received_monotonic": time.monotonic(),
            "observation_started_monotonic": started,
        }
