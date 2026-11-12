"""Bounded, lossless wire format for stateless cup segmentation.

The camera calls its image ``rgb`` but delivers OpenCV BGR channel order.  The
protocol preserves those exact uint8 bytes. Requests never contain depth,
calibration, placement state, or robot-control credentials.
"""

import base64
import hashlib
import json
import math
import re
import uuid
from pathlib import Path

PROTOCOL_VERSION = 1
KEEP_CLASSES = ("CUP", "CUP BASE", "CUP HANDLE")
MAX_PIXELS = 4096 * 2160
MAX_DIMENSION = 4096
MAX_DETECTIONS = 64
MAX_REQUEST_BYTES = 36 * 1024 * 1024
MAX_RESPONSE_BYTES = 36 * 1024 * 1024
_HASH = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_ECHO_FIELDS = ("protocol_version", "request_id", "observation_id", "width", "height",
                "image_sha256", "model_sha256", "settings")


class InferenceError(ValueError):
    """An inference provider failed; never contains credentials or raw responses."""


class InferenceTimeout(InferenceError, TimeoutError):
    """The complete request exceeded its deadline."""


class ProtocolError(InferenceError):
    """An untrusted request/result did not match the segmentation contract."""


def make_settings(conf=0.5, imgsz=640):
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not math.isfinite(conf) or not 0 < conf <= 1:
        raise ProtocolError("Invalid inference confidence")
    if type(imgsz) is not int or not 32 <= imgsz <= 2048 or imgsz % 32:
        raise ProtocolError("Inference image size must be a multiple of 32 between 32 and 2048")
    return {"conf": float(conf), "imgsz": imgsz, "iou": 0.7,
            "classes": list(KEEP_CLASSES), "device": "cpu", "half": False,
            "rect": False, "retina_masks": True, "augment": False,
            "max_det": MAX_DETECTIONS, "input_format": "bgr8"}


def validate_settings(settings):
    if not isinstance(settings, dict):
        raise ProtocolError("Invalid inference settings")
    expected = make_settings(settings.get("conf"), settings.get("imgsz"))
    # bool equals integer in Python, so a JSON comparison also enforces types.
    if json_bytes(settings, 4096) != json_bytes(expected, 4096):
        raise ProtocolError("Unsupported inference settings")
    return settings


def model_sha256(path):
    path = Path(path)
    if not path.is_file():
        raise InferenceError("Segmentation weights must be an existing local file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(value, limit):
    try:
        raw = json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ProtocolError("Invalid inference JSON") from None
    if len(raw) > limit:
        raise ProtocolError("Inference payload exceeds size limit")
    return raw


def load_json(raw, limit):
    if not isinstance(raw, bytes) or len(raw) > limit:
        raise ProtocolError("Inference payload exceeds size limit")
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProtocolError("Duplicate inference JSON field")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError("Nonfinite inference value")))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProtocolError("Invalid inference JSON") from None
    if not isinstance(value, dict):
        raise ProtocolError("Inference payload must be an object")
    return value


def _metadata(value):
    if not isinstance(value, dict) or type(value.get("protocol_version")) is not int or value["protocol_version"] != PROTOCOL_VERSION:
        raise ProtocolError("Unsupported inference protocol")
    for field in ("request_id", "observation_id"):
        if not isinstance(value.get(field), str) or not _ID.fullmatch(value[field]):
            raise ProtocolError("Invalid inference identity")
    for field in ("image_sha256", "model_sha256"):
        if not isinstance(value.get(field), str) or not _HASH.fullmatch(value[field]):
            raise ProtocolError("Invalid inference digest")
    width, height = value.get("width"), value.get("height")
    if (type(width) is not int or type(height) is not int or
            not 1 <= width <= MAX_DIMENSION or not 1 <= height <= MAX_DIMENSION or
            width * height > MAX_PIXELS):
        raise ProtocolError("Invalid inference image dimensions")
    validate_settings(value.get("settings"))


def _decode_base64(value, expected_bytes):
    if not isinstance(value, str) or len(value) != 4 * ((expected_bytes + 2) // 3):
        raise ProtocolError("Invalid encoded inference data length")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, UnicodeError):
        raise ProtocolError("Invalid encoded inference data") from None
    if len(raw) != expected_bytes or base64.b64encode(raw).decode("ascii") != value:
        raise ProtocolError("Invalid encoded inference data")
    return raw


def make_request(rgb, *, model_sha256, settings, observation_id, request_id=None):
    import numpy as np
    if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ProtocolError("Inference image must be a uint8 BGR image")
    height, width = rgb.shape[:2]
    request = {"protocol_version": PROTOCOL_VERSION,
               "request_id": request_id or uuid.uuid4().hex,
               "observation_id": str(observation_id), "width": int(width), "height": int(height),
               "image_sha256": "0" * 64, "model_sha256": model_sha256,
               "settings": dict(settings)}
    _metadata(request)
    raw = np.ascontiguousarray(rgb).tobytes()
    request["image_sha256"] = hashlib.sha256(raw).hexdigest()
    request["image"] = {"encoding": "base64-bgr8", "data": base64.b64encode(raw).decode("ascii")}
    json_bytes(request, MAX_REQUEST_BYTES)
    return request


def decode_request(request):
    import numpy as np
    _metadata(request)
    json_bytes(request, MAX_REQUEST_BYTES)
    encoded = request.get("image")
    if not isinstance(encoded, dict) or set(encoded) != {"encoding", "data"} or encoded.get("encoding") != "base64-bgr8":
        raise ProtocolError("Unsupported inference image encoding")
    raw = _decode_base64(encoded.get("data"), request["width"] * request["height"] * 3)
    if hashlib.sha256(raw).hexdigest() != request["image_sha256"]:
        raise ProtocolError("Inference image digest mismatch")
    return np.frombuffer(raw, dtype=np.uint8).reshape(request["height"], request["width"], 3).copy()


def _decode_mask(mask, width, height):
    import numpy as np
    if not isinstance(mask, dict) or set(mask) != {"encoding", "data"} or mask.get("encoding") != "base64-packbits-big":
        raise ProtocolError("Unsupported inference mask encoding")
    count = width * height
    raw = _decode_base64(mask.get("data"), (count + 7) // 8)
    if count % 8 and raw[-1] & ((1 << (8 - count % 8)) - 1):
        raise ProtocolError("Invalid inference mask padding")
    return np.unpackbits(np.frombuffer(raw, dtype=np.uint8), count=count, bitorder="big").reshape(height, width).astype(bool)


def _validate_detections(response):
    detections = response.get("detections")
    if not isinstance(detections, list) or len(detections) > MAX_DETECTIONS:
        raise ProtocolError("Invalid inference detection count")
    width, height = response["width"], response["height"]
    for detection in detections:
        if not isinstance(detection, dict) or set(detection) != {"class_name", "confidence", "bbox_xyxy", "mask"}:
            raise ProtocolError("Invalid inference detection")
        if detection["class_name"] not in KEEP_CLASSES:
            raise ProtocolError("Unexpected inference class")
        confidence = detection["confidence"]
        if type(confidence) not in (int, float) or not math.isfinite(confidence) or not response["settings"]["conf"] <= confidence <= 1:
            raise ProtocolError("Invalid inference confidence")
        box = detection["bbox_xyxy"]
        if (not isinstance(box, list) or len(box) != 4 or
                any(type(x) not in (int, float) or not math.isfinite(x) for x in box) or
                not 0 <= box[0] < box[2] <= width or not 0 <= box[1] < box[3] <= height):
            raise ProtocolError("Invalid inference bounding box")
        mask = _decode_mask(detection["mask"], width, height)
        if not mask.any():
            raise ProtocolError("Empty inference mask")
        x1, y1, x2, y2 = math.floor(box[0]), math.floor(box[1]), math.ceil(box[2]), math.ceil(box[3])
        if mask[:, :x1].any() or mask[:, x2:].any() or mask[:y1].any() or mask[y2:].any():
            raise ProtocolError("Inference mask extends outside its bounding box")


def validate_response(response, request):
    _metadata(request)
    _metadata(response)
    json_bytes(response, MAX_RESPONSE_BYTES)
    for field in _ECHO_FIELDS:
        if json_bytes(response[field], MAX_RESPONSE_BYTES) != json_bytes(request[field], MAX_RESPONSE_BYTES):
            raise ProtocolError("Inference response does not match the requested observation/configuration")
    elapsed = response.get("inference_ms")
    if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
        raise ProtocolError("Invalid inference duration")
    _validate_detections(response)
    return response


def make_response(request, detections, inference_ms=0.0):
    """Encode model output. Masks must already have the original image shape."""
    import numpy as np
    response = {field: request[field] for field in _ECHO_FIELDS}
    response.update(inference_ms=float(inference_ms), detections=[])
    if len(detections) > MAX_DETECTIONS:
        raise ProtocolError("Invalid inference detection count")
    for detection in detections:
        mask = np.asarray(detection["mask"])
        if mask.dtype != np.bool_ or mask.shape != (request["height"], request["width"]):
            raise ProtocolError("Inference mask must match the original image")
        packed = np.packbits(mask.reshape(-1), bitorder="big").tobytes()
        response["detections"].append({"class_name": detection["class_name"],
            "confidence": float(detection["confidence"]),
            "bbox_xyxy": [float(x) for x in detection["bbox_xyxy"]],
            "mask": {"encoding": "base64-packbits-big", "data": base64.b64encode(packed).decode("ascii")}})
    return validate_response(response, request)


def decode_detections(response):
    _metadata(response)
    json_bytes(response, MAX_RESPONSE_BYTES)
    _validate_detections(response)
    return [{**{key: value for key, value in detection.items() if key != "mask"},
             "mask": _decode_mask(detection["mask"], response["width"], response["height"])}
            for detection in response["detections"]]
