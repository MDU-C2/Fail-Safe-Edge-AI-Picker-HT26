"""
Depth helpers for picker_workflow.py and the browser dashboard:

    capture_averaged(gateway, n)  RGB of the first frame + the depth of n frames averaged
                                  pixel by pixel (mean, 0 = no depth is ignored)
    mask_depth(depth, mask)       depth of an object: median inside its mask, shrunk by
                                  EDGE_PX first, because the edge pixels mix the object
                                  with what is behind it

The legacy capture_averaged helper assumes a static scene. The unified application
uses capture_fresh_samples and preserves newest raw aligned depth for grid checks.
Needs: numpy, opencv-python, requests (through the gateway object).
"""

import time

import cv2
import numpy as np

DEFAULT_DEPTH_FRAMES = 5
EDGE_PX = 5            # pixels taken off the mask edge before reading its depth
NEW_FRAME_WAIT = 2.0   # s, longest wait for the camera to deliver the next frame


def _frame(r, dtype, planes=1):
    """Raw frame bytes -> (planes, H, W) array, sized from the X-Frame-* headers."""
    w, h = int(r.headers["X-Frame-Width"]), int(r.headers["X-Frame-Height"])
    return np.frombuffer(r.content, dtype).reshape(planes, h, w)


def _pair(gateway, tries=5):
    """One matching RGB + depth pair from the gateway: (rgb BGR HxWx3, depth uint16 HxW, sequence)."""
    for _ in range(tries):
        r_rgb, r_depth = gateway.get("/camera/rgb"), gateway.get("/camera/depth")
        seq = r_depth.headers["X-Frame-Sequence"]
        if r_rgb.headers["X-Frame-Sequence"] == seq:
            rgb = _frame(r_rgb, np.uint8, 3).transpose(1, 2, 0).copy()   # planar BGR
            return rgb, _frame(r_depth, np.uint16)[0], seq
    raise RuntimeError("Could not get a matching RGB + depth pair from the gateway")


def average_depth(depths):
    """Pixel-by-pixel mean of several depth images (uint16 mm); 0 (no depth) is ignored,
    a pixel without depth in every image stays 0. (Saved as the capture's depth image;
    each object's Z is the mean of its Z in the single frames, see picker_workflow.Detector.describe.)"""
    stack = np.stack(depths).astype(np.float32)
    valid = np.isfinite(stack) & (stack > 0)
    count = valid.sum(axis=0)
    mean = np.divide(np.where(valid, stack, 0).sum(axis=0), count,
                     out=np.zeros(stack.shape[1:], dtype=np.float32), where=count > 0)
    return mean.round().astype(np.uint16)


def capture_averaged(gateway, n=3):
    """RGB of the first frame and the depth of n different frames averaged.
    Returns (rgb, averaged depth, list of the single depth frames); fewer frames if the
    camera stops delivering new ones."""
    rgb, depth, seq = _pair(gateway)
    depths = [depth]
    while len(depths) < n:
        deadline = time.monotonic() + NEW_FRAME_WAIT
        while True:                             # wait for the next camera frame
            _, depth, new_seq = _pair(gateway)
            if new_seq != seq or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        if new_seq == seq:
            break                               # no new frame: use what we have
        depths.append(depth)
        seq = new_seq
    return rgb, (depths[0] if len(depths) == 1 else average_depth(depths)), depths


def mask_depth(depth, mask, edge_px=EDGE_PX):
    """Median depth (mm) inside the mask, edge_px taken off its edge first; None if no depth.
    A mask too small to shrink is used as it is."""
    inner = mask
    if edge_px > 0:
        kernel = np.ones((2 * edge_px + 1, 2 * edge_px + 1), np.uint8)
        shrunk = cv2.erode(mask.astype(np.uint8), kernel).astype(bool)
        if shrunk.any():
            inner = shrunk
    vals = depth[inner]
    vals = vals[np.isfinite(vals) & (vals > 0)]  # 0 = no depth there
    return float(np.median(vals)) if vals.size else None


def capture_fresh_samples(camera, count=DEFAULT_DEPTH_FRAMES, timeout_s=10, *, require_fresh=False):
    """Collect available distinct aligned pairs for V4 coordinate averaging.

    Return newest RGB and its RAW depth, metadata, and the individual depths.
    The samples are for object-coordinate averaging only. Placement evidence
    must remain the last raw aligned image; a mean could hide holes/obstacles.
    As in V4, a stopped frame stream ends the burst with the samples collected.
    Transport and malformed-frame failures still propagate. Placement vision
    keeps the latest raw aligned pair, independently of coordinate averaging.
    A between-cup workspace check requires its first pair to be newer than the
    camera status read at the start of this call, after movement has finished.
    """
    from camera_client import CameraError, NoNewCameraFrame
    if type(count) is not int or not 1 <= count <= 9:
        raise ValueError("Depth frame count must be an integer from 1 to 9")
    initial_capture = (camera.capture if require_fresh else
                       getattr(camera, "capture_latest", camera.capture))
    if count == 1:
        rgb, depth, frame = (initial_capture(timeout_s=timeout_s) if require_fresh
                             else initial_capture())
        return rgb, depth, frame, (depth,)
    deadline = time.monotonic() + timeout_s
    samples, metadata = [], []
    shape = None
    for _ in range(count):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if samples:
                break
            raise CameraError("Depth sample collection timed out")
        try:
            capture = camera.capture if samples else initial_capture
            pair = capture(timeout_s=min(remaining, NEW_FRAME_WAIT) if samples else remaining)
        except NoNewCameraFrame:
            if samples:
                break
            raise
        next_rgb, next_depth, frame = pair
        sequence = frame.get("sequence")
        if type(sequence) is not int:
            raise CameraError("Depth sample has no valid frame sequence")
        if metadata and sequence == metadata[-1]["sequence"]:
            break
        rgb, depth = next_rgb, next_depth
        if (depth.ndim != 2 or depth.dtype != np.uint16 or rgb.ndim != 3
                or rgb.shape[2] != 3 or depth.shape != rgb.shape[:2]
                or depth.size > 8_400_000 or (shape is not None and depth.shape != shape)):
            raise CameraError("Depth sample dimensions or format changed")
        shape = depth.shape
        samples.append(depth)
        metadata.append(dict(frame))
    result = dict(metadata[-1])
    result["observation_started_monotonic"] = metadata[0]["observation_started_monotonic"]
    result["depth_sampling"] = {"count": len(samples), "method": "mean_of_available_eroded_mask_depths",
                                "sequences": [item["sequence"] for item in metadata],
                                "timestamps": [item.get("timestamp") for item in metadata],
                                "grid_evidence": "latest_raw_aligned_frame"}
    return rgb, depth, result, tuple(samples)
