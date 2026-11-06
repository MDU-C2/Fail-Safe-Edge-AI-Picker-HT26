"""Read a newly published, matching RGB/depth pair without moving the robot.

The gateway's clock is recorded for auditing, never compared with this host's
clock. A capture requires a sequence newer than the initial camera status.
"""

from datetime import datetime, timezone
import math
import re
import time

import numpy as np
import requests


class CameraError(RuntimeError):
    """The camera could not supply a usable observation."""


class _TransientCameraError(CameraError):
    pass


def _integer(value, name, *, positive=False):
    if isinstance(value, bool) or not (
        isinstance(value, int)
        or isinstance(value, str) and re.fullmatch(r"[0-9]+", value)
    ):
        raise CameraError(f"Invalid {name}: expected a non-negative integer")
    number = int(value)
    if number < 0 or positive and number == 0:
        raise CameraError(f"Invalid {name}: expected {'a positive' if positive else 'a non-negative'} integer")
    return number


class CameraClient:
    def __init__(self, api, headers, session=None):
        self.api = api.rstrip("/")
        self.headers = dict(headers)
        # Injection keeps tests offline; construction never makes a request.
        self.session = requests if session is None else session

    def get(self, path, stream=False, timeout_s=15):
        """Return a response owned by the caller; errors are always CameraError."""
        response = None
        try:
            response = self.session.get(
                self.api + path,
                headers=self.headers,
                stream=stream,
                timeout=timeout_s,
            )
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            status = response.status_code if response is not None else None
            if response is not None:
                response.close()
            detail = f"HTTP {status}" if status is not None else type(error).__name__
            message = f"Camera gateway request {path} failed ({detail})"
            if status is None or status in (408, 429) or status >= 500:
                raise _TransientCameraError(message) from error
            raise CameraError(message) from error

    def intrinsics(self):
        response = self.get("/camera/intrinsics")
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("expected an object")
            for name in ("fx", "fy", "cx", "cy"):
                value = data.get(name)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"invalid {name}")
                if name in ("fx", "fy") and value <= 0:
                    raise ValueError(f"invalid {name}")
            return data
        except (ValueError, TypeError) as error:
            raise CameraError(f"Invalid camera intrinsics: {error}") from error
        finally:
            response.close()

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _TransientCameraError("Camera capture deadline expired")
        return remaining

    def _get_before(self, path, deadline, *, stream=False):
        # A short per-read timeout also bounds a stalled streamed payload. The
        # deadline is checked between chunks; a blocked socket can overrun it by
        # at most its current read timeout (up to two seconds).
        return self.get(path, stream=stream, timeout_s=min(2.0, self._remaining(deadline)))

    @staticmethod
    def _frame_headers(response, expected_format):
        headers = response.headers
        sequence = _integer(headers.get("X-Frame-Sequence"), "frame sequence")
        width = _integer(headers.get("X-Frame-Width"), "frame width", positive=True)
        height = _integer(headers.get("X-Frame-Height"), "frame height", positive=True)
        if headers.get("X-Pixel-Format") != expected_format:
            raise CameraError(f"Unsupported camera format; expected {expected_format}")
        timestamp = headers.get("X-Frame-Timestamp")
        if not isinstance(timestamp, str) or not timestamp.strip():
            raise CameraError("Missing camera frame timestamp")
        return sequence, width, height, timestamp

    def _payload(self, response, expected_bytes, deadline):
        payload = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=65536):
                self._remaining(deadline)
                if chunk:
                    payload.extend(chunk)
                    if len(payload) > expected_bytes:
                        raise CameraError("Camera payload is larger than its declared dimensions")
        except requests.RequestException as error:
            raise _TransientCameraError("Camera payload download failed") from error
        self._remaining(deadline)
        if len(payload) != expected_bytes:
            raise CameraError(
                f"Camera payload has {len(payload)} bytes; expected {expected_bytes}"
            )
        return payload

    def capture(self, timeout_s=10):
        """Return ``(bgr_image, uint16_depth_mm, metadata)`` for one new frame.

        Both streamed GETs are opened before either body is downloaded. This
        avoids downloading a large RGB image before requesting its depth peer.
        ``observation_started_monotonic`` includes network and inference age;
        ``received_monotonic`` marks completion of reception and decoding.
        ``observed_at`` is this host's UTC time at the start of the capture.
        The age starts before the baseline read, so retries never make an old
        observation appear younger.
        """
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise CameraError("Capture timeout must be a positive, finite number")
        observation_started = time.monotonic()
        observed_at = datetime.now(timezone.utc).isoformat()
        deadline = observation_started + timeout_s
        baseline = None
        last_error = "No newer matching RGB + depth pair was available"

        while time.monotonic() < deadline:
            rgb_response = depth_response = status_response = None
            try:
                if baseline is None:
                    status_response = self._get_before("/camera/status", deadline)
                    try:
                        status = status_response.json()
                    except (ValueError, TypeError) as error:
                        raise CameraError("Camera status is not valid JSON") from error
                    if not isinstance(status, dict) or status.get("connected") is not True:
                        raise CameraError("Gateway camera is not connected")
                    baseline = _integer(status.get("sequence"), "camera status sequence")
                    status_response.close()
                    status_response = None

                rgb_response = self._get_before("/camera/rgb", deadline, stream=True)
                depth_response = self._get_before("/camera/depth", deadline, stream=True)
                rgb_sequence, width, height, timestamp = self._frame_headers(rgb_response, "BGR888p")
                depth_sequence, depth_width, depth_height, depth_timestamp = self._frame_headers(depth_response, "RAW16")

                if rgb_sequence != depth_sequence:
                    raise _TransientCameraError("RGB and depth frame sequences did not match")
                if rgb_sequence <= baseline:
                    raise _TransientCameraError("Camera has not published a newer frame (possibly frozen)")
                if (width, height) != (depth_width, depth_height):
                    raise CameraError("RGB and aligned depth dimensions do not match")
                if timestamp != depth_timestamp:
                    raise CameraError("Matching RGB and depth have different timestamps")

                rgb_bytes = self._payload(rgb_response, width * height * 3, deadline)
                depth_bytes = self._payload(depth_response, width * height * 2, deadline)
                rgb = np.frombuffer(rgb_bytes, dtype=np.uint8).reshape(3, height, width).transpose(1, 2, 0).copy()
                depth = np.frombuffer(depth_bytes, dtype="<u2").reshape(height, width).astype(np.uint16, copy=True)
                self._remaining(deadline)
                return rgb, depth, {
                    "sequence": rgb_sequence,
                    "timestamp": timestamp,
                    "observed_at": observed_at,
                    "received_monotonic": time.monotonic(),
                    "observation_started_monotonic": observation_started,
                }
            except _TransientCameraError as error:
                last_error = str(error)
            finally:
                for response in (rgb_response, depth_response, status_response):
                    if response is not None:
                        response.close()

            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.05, remaining))

        raise CameraError(f"Could not capture a fresh matching RGB + depth pair: {last_error}")
