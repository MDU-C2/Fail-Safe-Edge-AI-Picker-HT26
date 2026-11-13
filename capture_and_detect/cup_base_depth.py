"""Conservative depth at a CUP BASE's reported mask centre; no device access."""

import math

import cv2
import numpy as np


MIN_CORE_PIXELS = 9
MIN_VALID_FRACTION = 0.6


def cup_base_depth(mask, depth):
    """Return (camera Z, quality), refusing a depth hole surrounded by background.

    Build the core from mask geometry, never from which pixels have depth. It
    must contain the reported centroid and a 3x3 neighbourhood inside the mask.
    For larger masks, use the central half of the bounding box and the inner
    half of the mask's distance transform. Both that core and the centroid's
    3x3 neighbourhood need a majority of finite, positive depth samples.

    Accepted CUP BASE coordinates use the core median, not the whole-mask
    median: valid background at a segmentation edge must not dominate the
    reported depth. This remains a depth-quality check, not a collision check.
    """
    mask = np.asarray(mask, dtype=bool)
    depth = np.asarray(depth)
    if mask.ndim != 2 or depth.shape != mask.shape:
        raise ValueError("CUP BASE mask and depth must be matching 2D arrays.")
    valid = np.isfinite(depth) & (depth > 0)
    ys, xs = np.nonzero(mask)
    quality = {"method": "interior_median", "valid": False,
               "mask_pixels": int(xs.size), "valid_mask_pixels": int((mask & valid).sum()),
               "core_pixels": 0, "valid_core_pixels": 0, "core_valid_fraction": 0.0,
               "center_pixels": 0, "valid_center_pixels": 0}

    def rejected(reason):
        quality["reason"] = reason
        return None, quality

    if xs.size < MIN_CORE_PIXELS:
        return rejected("CUP BASE mask is too small for reliable interior depth.")
    u, v = float(xs.mean()), float(ys.mean())
    cx, cy = int(math.floor(u + 0.5)), int(math.floor(v + 0.5))
    # Padding makes image-border masks behave like masks surrounded by zeros.
    distances = cv2.distanceTransform(np.pad(mask.astype(np.uint8), 1), cv2.DIST_L2, 5)[1:-1, 1:-1]
    interior_distance = max(2.0, float(distances.max()) * 0.5)
    if not mask[cy, cx] or distances[cy, cx] < interior_distance:
        return rejected("CUP BASE centre is not inside a sufficiently thick mask interior.")

    half_width = max(1.5, (int(xs.max()) - int(xs.min()) + 1) / 4)
    half_height = max(1.5, (int(ys.max()) - int(ys.min()) + 1) / 4)
    core = np.zeros_like(mask)
    inside = ((abs(xs - u) <= half_width) & (abs(ys - v) <= half_height)
              & (distances[ys, xs] >= interior_distance))
    core[ys[inside], xs[inside]] = True
    core_count = int(core.sum())
    good_core = core & valid
    valid_core = int(good_core.sum())
    center = mask[max(0, cy - 1):cy + 2, max(0, cx - 1):cx + 2]
    center_valid = valid[max(0, cy - 1):cy + 2, max(0, cx - 1):cx + 2] & center
    quality.update(core_pixels=core_count, valid_core_pixels=valid_core,
                   core_valid_fraction=round(valid_core / core_count, 4) if core_count else 0.0,
                   center_pixels=int(center.sum()), valid_center_pixels=int(center_valid.sum()))
    if core_count < MIN_CORE_PIXELS or center.size != 9 or not center.all():
        return rejected("CUP BASE interior is too small for reliable depth.")
    if center_valid.sum() < math.ceil(9 * MIN_VALID_FRACTION):
        return rejected("CUP BASE has insufficient valid depth at its centre.")
    if valid_core < math.ceil(core_count * MIN_VALID_FRACTION):
        return rejected("CUP BASE has insufficient valid depth across its interior.")
    quality.update(valid=True, reason="Finite positive depth covers the centre and interior.")
    return float(np.median(depth[good_core])), quality
