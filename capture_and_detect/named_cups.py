"""Persistent visual cup references, never saved motion coordinates.

References are cropped from raw captures and stored together in one atomic JSON
document. The separate flock file protects cooperating processes across atomic
replacements; an unreadable existing document is never treated as an empty store.
OpenCV/NumPy are imported only when an image actually needs processing.
"""

import base64
import binascii
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import stat
import struct
import uuid

from cup_inventory import SelectionError, atomic_json, finite_number


MAX_REFERENCES = 32
MAX_CANDIDATES = 12
MAX_IMAGE_BYTES = 512 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_PIXELS = 20_000_000
MAX_SIDE = 512
MAX_COMPARISON_SIDE = 896
MAX_COMPARISON_BYTES = 1024 * 1024
MAX_BASE64_BYTES = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
MAX_STORE_BYTES = MAX_REFERENCES * (MAX_BASE64_BYTES + 2048) + 1024
PUBLIC_FIELDS = ("id", "name", "saved_at", "capture_id", "cup_id", "reference_sha256")
STORED_FIELDS = set(PUBLIC_FIELDS) | {"image_jpeg_base64"}
_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _name(value):
    if not isinstance(value, str) or not value.isascii() or re.search(r"[^A-Za-z0-9 _-]", value):
        raise SelectionError("Cup names may contain only ASCII letters, digits, spaces, underscores and hyphens.")
    value = " ".join(value.split())
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 _-]{0,47}", value):
        raise SelectionError("Cup names must start with a letter and contain 1 to 48 characters.")
    return value


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise SelectionError("Invalid saved cup ID.")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite JSON value")


def _dimensions(data):
    """Read JPEG/PNG dimensions before allowing a decoder to allocate pixels."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 33 or data[8:16] != b"\x00\x00\x00\rIHDR":
            raise SelectionError("Invalid PNG image header.")
        return struct.unpack(">II", data[16:24])
    if not data.startswith(b"\xff\xd8"):
        raise SelectionError("Cup images must contain JPEG or PNG image data.")
    offset = 2
    while offset < len(data):
        if data[offset] != 0xFF:
            break
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in (0xD9, 0xDA):
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            break
        size = int.from_bytes(data[offset:offset + 2], "big")
        if size < 2 or offset + size > len(data):
            break
        if marker in _SOF_MARKERS:
            if size < 8:
                break
            height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
            return width, height
        offset += size
    raise SelectionError("Invalid JPEG image header.")


def _decode(data, *, reference=False):
    if not data or len(data) > (MAX_IMAGE_BYTES if reference else MAX_SOURCE_BYTES):
        raise SelectionError("Cup image is empty or exceeds its size limit.")
    if reference and (not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9")):
        raise SelectionError("Saved cup reference must be JPEG.")
    width, height = _dimensions(data)
    if width < 1 or height < 1 or width * height > MAX_SOURCE_PIXELS:
        raise SelectionError("Cup image exceeds the 20-million-pixel limit or has invalid dimensions.")
    if reference and max(width, height) > MAX_SIDE:
        raise SelectionError("Saved cup image exceeds 512 pixels per side.")
    try:
        import cv2
        import numpy as np
    except ImportError as error:
        raise SelectionError("OpenCV and NumPy are required to save or compare cup images.") from error
    try:
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8),
                            cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    except cv2.error as error:
        raise SelectionError("Cup image could not be decoded.") from error
    if image is None or image.shape != (height, width, 3):
        raise SelectionError("Cup image could not be decoded or its dimensions do not match.")
    return image


def _raw_image(inventory, image_root):
    images = inventory.get("images") if isinstance(inventory, dict) else None
    source = images.get("rgb") if isinstance(images, dict) else None
    if not isinstance(source, str) or not source:
        raise SelectionError("The capture has no raw RGB image. Run scan again.")
    try:
        root = Path(image_root).resolve(strict=True)
        source_path = Path(source)
        path = (source_path if source_path.is_absolute() else root / source_path).resolve(strict=True)
        if not root.is_dir() or not path.is_relative_to(root) or path.suffix.lower() not in (".jpg", ".png"):
            raise SelectionError("Raw RGB image must be a JPG or PNG inside the detections folder.")
        if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
            raise SelectionError("Raw RGB image is not a regular image file or exceeds 64 MiB.")
        with path.open("rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
    except (OSError, ValueError, RuntimeError) as error:
        if isinstance(error, SelectionError):
            raise
        raise SelectionError("Raw RGB image is missing or inaccessible. Run scan again.") from error
    return _decode(data)


def _full_cups(inventory):
    cups = inventory.get("cups") if isinstance(inventory, dict) else None
    if not isinstance(cups, list):
        raise SelectionError("A current cup inventory is required.")
    full, ids, detection_ids = [], set(), set()
    for cup in cups:
        if not isinstance(cup, dict) or type(cup.get("id")) is not int or cup["id"] < 1 or cup["id"] in ids:
            raise SelectionError("Cup inventory has missing, invalid or duplicate cup IDs.")
        ids.add(cup["id"])
        detection_id = cup.get("cup_detection_id")
        if detection_id is None:
            continue  # An orphan base is not a visual reference to a full cup.
        if not ((type(detection_id) is int and detection_id >= 0)
                or (isinstance(detection_id, str) and 1 <= len(detection_id) <= 150)):
            raise SelectionError("A full CUP has an invalid detection identity.")
        if detection_id in detection_ids:
            raise SelectionError("Full CUP detections are duplicated or ambiguous. Run scan again.")
        detection_ids.add(detection_id)
        full.append(cup)
    return full


def _crop(image, cup):
    box = cup.get("bbox_xyxy")
    height, width = image.shape[:2]
    if (not isinstance(box, (list, tuple)) or len(box) != 4
            or not all(finite_number(value) for value in box)
            or not 0 <= box[0] < box[2] <= width or not 0 <= box[1] < box[3] <= height):
        raise SelectionError(f"Cup {cup['id']} has a missing or invalid full-CUP image box. Run scan again.")
    x1, y1, x2, y2 = box
    px, py = (x2 - x1) * .05, (y2 - y1) * .05
    crop = image[max(0, math.floor(y1 - py)):min(height, math.ceil(y2 + py)),
                 max(0, math.floor(x1 - px)):min(width, math.ceil(x2 + px))]
    import cv2
    try:
        crop_height, crop_width = crop.shape[:2]
        scale = min(1, MAX_SIDE / max(crop_width, crop_height))
        if scale < 1:
            crop = cv2.resize(crop, (max(1, round(crop_width * scale)), max(1, round(crop_height * scale))),
                              interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
    except cv2.error as error:
        raise SelectionError("Cup reference image could not be encoded.") from error
    if not ok or encoded is None or encoded.nbytes > MAX_IMAGE_BYTES:
        raise SelectionError("Cup reference image could not be encoded within 512 KiB.")
    return encoded.tobytes()


def candidate_images(inventory, image_root):
    """Crop every full-CUP candidate, including blocked cups; never select a subset."""
    cups = _full_cups(inventory)
    if not 1 <= len(cups) <= MAX_CANDIDATES:
        raise SelectionError("Visual matching requires between 1 and 12 full-CUP candidates. Run scan again.")
    image = _raw_image(inventory, image_root)
    return [{"cup_id": cup["id"], "image_bytes": _crop(image, cup)} for cup in cups]


def _validate_comparison_candidates(candidates):
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_CANDIDATES:
        raise SelectionError("Visual comparison needs between 1 and 12 cup candidates.")
    ids = set()
    for candidate in candidates:
        if (not isinstance(candidate, dict) or set(candidate) != {"cup_id", "image_bytes"}
                or type(candidate["cup_id"]) is not int or candidate["cup_id"] < 1
                or candidate["cup_id"] in ids or not isinstance(candidate["image_bytes"], bytes)):
            raise SelectionError("Comparison candidates need unique positive cup IDs and JPEG bytes.")
        ids.add(candidate["cup_id"])
    return ids


def comparison_sheet(reference_bytes, candidates):
    """Return one JPEG with REFERENCE first, then CUP <id> tiles in input order.

    Every input must be a bounded reference-format JPEG. Images retain their
    aspect ratio inside white padded tiles; labels occupy separate header bands.
    Three candidates produce a 512x512 sheet. Up to twelve candidates fit within
    896x896. This changes presentation only, not reference bytes, IDs or ordering.
    """
    if not isinstance(reference_bytes, bytes):
        raise SelectionError("The comparison reference must contain JPEG bytes.")
    _validate_comparison_candidates(candidates)
    images = [_decode(reference_bytes, reference=True)]
    images.extend(_decode(candidate["image_bytes"], reference=True) for candidate in candidates)
    try:
        labels = ["REFERENCE"] + [f"CUP {candidate['cup_id']}" for candidate in candidates]
    except ValueError as error:
        raise SelectionError("A candidate cup ID is too long to label clearly.") from error
    import cv2
    import numpy as np

    columns = math.ceil(math.sqrt(len(images)))
    rows = math.ceil(len(images) / columns)
    side = min(256, MAX_COMPARISON_SIDE // max(columns, rows))
    header, padding = 36, 8
    sheet = np.full((rows * side, columns * side, 3), 255, dtype=np.uint8)
    try:
        for index, (image, label) in enumerate(zip(images, labels)):
            x, y = (index % columns) * side, (index // columns) * side
            # Blue reference header and dark candidate headers are distinct;
            # no generated text or frame is drawn over the source cup image.
            color = (130, 70, 25) if index == 0 else (45, 45, 45)
            cv2.rectangle(sheet, (x + 2, y + 2), (x + side - 3, y + header - 1), color, -1)
            font_scale = .6
            text_width = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, 1)[0][0]
            font_scale *= min(1, (side - 24) / max(1, text_width))
            if font_scale < .35:
                raise SelectionError("A candidate cup ID is too long to label clearly.")
            cv2.putText(sheet, label, (x + 12, y + 25), cv2.FONT_HERSHEY_SIMPLEX,
                        font_scale, (255, 255, 255), 1, cv2.LINE_AA)
            height, width = image.shape[:2]
            available_width, available_height = side - 2 * padding, side - header - 2 * padding
            scale = min(available_width / width, available_height / height)
            width_out, height_out = max(1, round(width * scale)), max(1, round(height * scale))
            resized = cv2.resize(image, (width_out, height_out),
                                 interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
            left = x + (side - width_out) // 2
            top = y + header + padding + (available_height - height_out) // 2
            sheet[top:top + height_out, left:left + width_out] = resized
        ok, encoded = cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 90])
    except cv2.error as error:
        raise SelectionError("The labelled comparison image could not be generated.") from error
    if not ok or encoded is None or encoded.nbytes > MAX_COMPARISON_BYTES:
        raise SelectionError("The labelled comparison image exceeds its size limit or could not be encoded.")
    return encoded.tobytes()


def visually_duplicate_ids(candidates, matched_id):
    """Return other near-identical candidate IDs, in input order, as a veto only.

    This deliberately narrow ambiguity check cannot establish object identity.
    It catches duplicate pixels and near-duplicates with at most 5% crop-aspect
    difference and mean absolute 32px thumbnail error <=3 on the 0..255 scale.
    Artificial letterbox padding is excluded from the score. Similar-looking
    real cups may be vetoed; other ambiguities still require the VLM/operator.
    """
    ids = _validate_comparison_candidates(candidates)
    if type(matched_id) is not int or matched_id not in ids:
        raise SelectionError("The matched cup ID must belong to the current candidates.")
    images = {candidate["cup_id"]: _decode(candidate["image_bytes"], reference=True)
              for candidate in candidates}
    import cv2
    import numpy as np

    def normalized(image):
        height, width = image.shape[:2]
        scale = 32 / max(width, height)
        w, h = max(1, round(width * scale)), max(1, round(height * scale))
        resized = cv2.resize(image, (w, h), interpolation=cv2.INTER_AREA)
        pixels = np.full((32, 32, 3), 255, dtype=np.float32)
        content = np.zeros((32, 32), dtype=bool)
        x, y = (32 - w) // 2, (32 - h) // 2
        pixels[y:y + h, x:x + w] = resized
        content[y:y + h, x:x + w] = True
        return pixels, content, width / height

    try:
        selected = images[matched_id]
        selected_pixels, selected_content, selected_aspect = normalized(selected)
        duplicates = []
        for candidate in candidates:
            cup_id = candidate["cup_id"]
            if cup_id == matched_id:
                continue
            other = images[cup_id]
            if selected.shape == other.shape and np.array_equal(selected, other):
                duplicates.append(cup_id)
                continue
            pixels, content, aspect = normalized(other)
            if max(aspect, selected_aspect) / min(aspect, selected_aspect) > 1.05:
                continue
            # Use the union so a different occupied image edge contributes;
            # large white letterbox bands cannot swamp the candidate pixels.
            error = np.abs(pixels - selected_pixels)[content | selected_content].mean()
            if float(error) <= 3:
                duplicates.append(cup_id)
        return duplicates
    except cv2.error as error:
        raise SelectionError("Cup candidates could not be checked for visual ambiguity.") from error


class NamedCupStore:
    def __init__(self, path, image_root):
        self.path = Path(path)
        self.image_root = Path(image_root)

    @contextmanager
    def _locked(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_name("." + self.path.name + ".lock").open("a", encoding="utf-8") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise SelectionError("Cannot access the named cup store.") from error

    def _read(self):
        if self.path.is_symlink():
            raise SelectionError("Named cup store must be a regular JSON file, not a symbolic link.")
        try:
            if not stat.S_ISREG(self.path.stat().st_mode):
                raise SelectionError("Named cup store is not a regular JSON file.")
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_STORE_BYTES + 1)
        except FileNotFoundError:
            return {"schema_version": 1, "cups": []}
        if len(raw) > MAX_STORE_BYTES:
            raise SelectionError("Named cup store exceeds its size limit; it was not changed.")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                               parse_constant=_invalid_constant)
            if (not isinstance(value, dict) or set(value) != {"schema_version", "cups"}
                    or type(value["schema_version"]) is not int or value["schema_version"] != 1
                    or not isinstance(value["cups"], list) or len(value["cups"]) > MAX_REFERENCES):
                raise ValueError("Invalid named cup document schema")
            ids, names = set(), set()
            for record in value["cups"]:
                if not isinstance(record, dict) or set(record) != STORED_FIELDS:
                    raise ValueError("Invalid saved cup fields")
                _identifier(record["id"])
                if record["name"] != _name(record["name"]):
                    raise ValueError("Saved cup name is not normalized")
                if record["id"] in ids or record["name"].casefold() in names:
                    raise ValueError("Duplicate saved cup ID or name")
                ids.add(record["id"])
                names.add(record["name"].casefold())
                if (not isinstance(record["capture_id"], str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", record["capture_id"])
                        or type(record["cup_id"]) is not int or record["cup_id"] < 1):
                    raise ValueError("Invalid source capture or cup ID")
                saved_at = record["saved_at"]
                if not isinstance(saved_at, str) or len(saved_at) > 40:
                    raise ValueError("Invalid reference timestamp")
                timestamp = datetime.fromisoformat(saved_at.replace("Z", "+00:00"))
                if timestamp.tzinfo is None or timestamp.utcoffset().total_seconds() != 0:
                    raise ValueError("Reference timestamp must be UTC")
                encoded = record["image_jpeg_base64"]
                if not isinstance(encoded, str) or not 1 <= len(encoded) <= MAX_BASE64_BYTES:
                    raise ValueError("Invalid reference image length")
                data = base64.b64decode(encoded, validate=True)
                digest = record["reference_sha256"]
                if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                        or hashlib.sha256(data).hexdigest() != digest):
                    raise ValueError("Saved reference image digest does not match")
                _decode(data, reference=True)
            return value
        except (ValueError, TypeError, UnicodeError, RecursionError, binascii.Error) as error:
            raise SelectionError(f"Invalid named cup store; it was not changed: {error}") from error

    @staticmethod
    def _public(record):
        return {key: record[key] for key in PUBLIC_FIELDS}

    @classmethod
    def _with_image(cls, record):
        return {**cls._public(record), "image_bytes": base64.b64decode(record["image_jpeg_base64"], validate=True)}

    def list(self):
        with self._locked():
            return [self._public(record) for record in self._read()["cups"]]

    def save(self, name, inventory, cup_id):
        name = _name(name)
        if type(cup_id) is not int or cup_id < 1:
            raise SelectionError("Choose a positive integer cup ID from the current scan.")
        capture = inventory.get("capture") if isinstance(inventory, dict) else None
        if not isinstance(capture, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", capture):
            raise SelectionError("The cup inventory has an invalid capture ID.")
        cups = [cup for cup in _full_cups(inventory) if cup["id"] == cup_id]
        if len(cups) != 1:
            raise SelectionError("Choose one full CUP detection; an orphan CUP BASE cannot be saved.")
        with self._locked():
            value = self._read()
            if any(record["name"].casefold() == name.casefold() for record in value["cups"]):
                raise SelectionError("That cup name already exists. Choose another name or delete its saved reference first.")
            if len(value["cups"]) >= MAX_REFERENCES:
                raise SelectionError("The named cup store is full (32 references). Delete a reference first.")
            data = _crop(_raw_image(inventory, self.image_root), cups[0])
            record = {"id": uuid.uuid4().hex, "name": name, "saved_at": datetime.now(timezone.utc).isoformat(),
                      "capture_id": capture, "cup_id": cup_id, "reference_sha256": hashlib.sha256(data).hexdigest(),
                      "image_jpeg_base64": base64.b64encode(data).decode("ascii")}
            value["cups"].append(record)
            atomic_json(self.path, value)
            return self._public(record)

    def get(self, name):
        name = _name(name).casefold()
        with self._locked():
            records = self._read()["cups"]
            for record in records:
                if record["name"].casefold() == name:
                    return self._with_image(record)
        raise SelectionError("No cup is saved with that name.")

    def get_id(self, identifier):
        _identifier(identifier)
        with self._locked():
            for record in self._read()["cups"]:
                if record["id"] == identifier:
                    return self._with_image(record)
        raise SelectionError("That saved cup reference no longer exists.")

    def delete(self, identifier):
        _identifier(identifier)
        with self._locked():
            value = self._read()
            for index, record in enumerate(value["cups"]):
                if record["id"] == identifier:
                    value["cups"].pop(index)
                    atomic_json(self.path, value)
                    return self._public(record)
        raise SelectionError("That saved cup reference no longer exists.")
