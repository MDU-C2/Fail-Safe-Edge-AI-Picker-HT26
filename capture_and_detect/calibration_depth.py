"""Optical-depth calibration shared by Python and lightweight Lynx loaders.

The hand-eye producer fits corrected_z = depth_scale * raw_z + depth_offset,
in millimetres. Apply this before pinhole backprojection, never to just robot Z.
An invalid raw measurement remains invalid even when the offset is positive.
"""

import math
from collections.abc import Mapping


def depth_parameters(calibration):
    """Validate an optional affine depth mapping; old calibrations are identity."""
    get = calibration.get if isinstance(calibration, Mapping) else lambda key, default: getattr(calibration, key, default)
    scale, offset = get("depth_scale", 1.0), get("depth_offset", 0.0)
    if (isinstance(scale, bool) or isinstance(offset, bool)
            or not isinstance(scale, (int, float)) or not isinstance(offset, (int, float))
            or not math.isfinite(scale) or scale <= 0 or not math.isfinite(offset)):
        raise ValueError("Calibration depth_scale must be positive and finite; depth_offset must be finite.")
    return float(scale), float(offset)


def corrected_depth(raw_depth, scale=1.0, offset=0.0):
    """Correct one valid optical distance; missing or nonpositive values stay missing."""
    if raw_depth is None or not math.isfinite(raw_depth) or raw_depth <= 0:
        return None
    value = scale * raw_depth + offset
    return value if math.isfinite(value) and value > 0 else None


def corrected_depth_image(raw_depth, calibration):
    """Return corrected optical millimetres without modifying the saved raw frame.

    NumPy is imported only for the image path; lightweight Lynx configuration
    and table-reference identity checks do not need this dependency.
    """
    scale, offset = depth_parameters(calibration)
    if (scale, offset) == (1.0, 0.0):
        return raw_depth
    import numpy as np
    values = np.asarray(raw_depth, dtype=float)
    with np.errstate(over="ignore", invalid="ignore"):
        result = values * scale + offset
    valid = np.isfinite(values) & (values > 0) & np.isfinite(result) & (result > 0)
    return np.where(valid, result, 0.0)
