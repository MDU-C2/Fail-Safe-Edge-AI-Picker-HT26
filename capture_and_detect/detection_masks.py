"""Exact cropped segmentation for placement evidence and saved detection records."""

import base64
import binascii

import numpy as np


ENCODING = "base64-packbits-big"
MAX_DIMENSION = 4096
MAX_PIXELS = 4096 * 2160
_FIELDS = {"encoding", "origin", "size", "data"}


def _dimensions(shape):
    return (isinstance(shape, (list, tuple)) and len(shape) == 2
            and all(type(value) is int and 0 < value <= MAX_DIMENSION for value in shape)
            and shape[0] * shape[1] <= MAX_PIXELS)


def encode_mask(mask):
    """Return a JSON-safe exact crop, or None for an empty boolean mask.

    Pixel origin is XY, size is height/width, and bits follow C-order rows.
    Cropping removes only empty outer rows/columns; holes are never filled.
    """
    if not isinstance(mask, np.ndarray) or mask.dtype != np.bool_ or not _dimensions(mask.shape):
        raise ValueError("Segmentation must be a bounded two-dimensional boolean image")
    rows = np.flatnonzero(mask.any(axis=1))
    if not rows.size:
        return None
    columns = np.flatnonzero(mask.any(axis=0))
    y0, y1 = int(rows[0]), int(rows[-1]) + 1
    x0, x1 = int(columns[0]), int(columns[-1]) + 1
    packed = np.packbits(mask[y0:y1, x0:x1].reshape(-1), bitorder="big").tobytes()
    return {"encoding": ENCODING, "origin": [x0, y0], "size": [y1 - y0, x1 - x0],
            "data": base64.b64encode(packed).decode("ascii")}


def decode_mask(value, image_shape):
    """Return (x0, y0, exact boolean crop), or None for unusable evidence.

    The supplied image shape binds the crop to its aligned RGB/depth frame.
    Missing legacy masks, bad lengths, nonzero padding and invalid bounds never
    become a rectangular substitute for segmentation.
    """
    if (not _dimensions(image_shape) or not isinstance(value, dict) or set(value) != _FIELDS
            or not isinstance(value["encoding"], str) or value["encoding"] != ENCODING):
        return None
    origin, size, data = value["origin"], value["size"], value["data"]
    if (not isinstance(origin, list) or len(origin) != 2
            or not all(type(part) is int and part >= 0 for part in origin)
            or not isinstance(size, list) or not _dimensions(size) or not isinstance(data, str)):
        return None
    x0, y0 = origin
    height, width = size
    if y0 + height > image_shape[0] or x0 + width > image_shape[1]:
        return None
    count = height * width
    byte_count = (count + 7) // 8
    if len(data) != ((byte_count + 2) // 3) * 4:
        return None
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        return None
    if (len(raw) != byte_count or base64.b64encode(raw).decode("ascii") != data
            or (count % 8 and raw[-1] & ((1 << (8 - count % 8)) - 1))):
        return None
    crop = np.unpackbits(np.frombuffer(raw, dtype=np.uint8), count=count,
                         bitorder="big").reshape(height, width).astype(bool)
    if not crop.any():
        return None
    return x0, y0, crop
