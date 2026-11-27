"""Read an optional camera parking target without moving the robot."""

import json
import math
from pathlib import Path
from urllib.parse import urlsplit

from cup_inventory import SelectionError
from handover_plan import normalize_right_park_pose, right_via_pose


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite JSON number")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def _gateway_aliases(record):
    """Validate explicit alternate origins; never infer aliases from a subnet."""
    aliases = record.get("gateway_aliases", [])
    if (not isinstance(aliases, list) or len(aliases) > 8
            or any(not isinstance(alias, str) for alias in aliases)):
        raise SelectionError("Right-arm parking gateway aliases must be an explicit list of gateway origins.")
    for alias in aliases:
        try:
            origin = urlsplit(alias)
            valid = (origin.scheme in {"http", "https"} and origin.hostname
                     and not any(c.isspace() or c in "*\\" for c in alias)
                     and origin.username is None and origin.password is None
                     and not origin.path and not origin.query and not origin.fragment
                     and origin.port != 0 and alias == origin.scheme + "://" + origin.netloc)
        except ValueError:
            valid = False
        if not valid:
            raise SelectionError("Right-arm parking gateway aliases must be exact HTTP(S) origins without credentials or paths.")
    if len(set(aliases)) != len(aliases) or record.get("gateway") in aliases:
        raise SelectionError("Right-arm parking gateway aliases must be distinct from each other and the configured gateway.")
    return aliases


def load_right_park_pose(path, gateway):
    """Return a copied flange pose, or None when no sidecar has been saved.

    A saved pose belongs to one gateway (including explicitly listed alternate
    gateway origins) and to the controller's right-arm
    tool0/wobj0 Wobj frame. The right_via preset uses the normal workflow's
    existing upright waiting target in that frame. Reading either record never
    verifies reachability or dispatches a command; ordinary endpoint limits apply.
    """
    try:
        content = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, TypeError, ValueError):
        raise SelectionError("Cannot read the right-arm parking file.") from None
    try:
        record = json.loads(content, object_pairs_hook=_unique_object,
                            parse_constant=_invalid_constant, parse_float=_finite_float)
    except (ValueError, RecursionError):
        raise SelectionError("Right-arm parking file must contain valid JSON without duplicate fields or non-finite numbers.") from None
    if not isinstance(record, dict):
        raise SelectionError("Right-arm parking file must contain an object.")
    if type(record.get("schema_version")) is not int or record["schema_version"] != 1:
        raise SelectionError("Right-arm parking file requires schema_version 1.")
    aliases = _gateway_aliases(record)
    if (not isinstance(record.get("gateway"), str) or not record["gateway"]
            or not isinstance(gateway, str) or not gateway.rstrip("/")
            or gateway.rstrip("/") not in [record["gateway"], *aliases]):
        raise SelectionError("Right-arm parking file does not match the configured gateway.")
    if "preset" in record:
        if record["preset"] != "right_via":
            raise SelectionError("Right-arm parking preset must be right_via.")
        if "pose" in record or "controller_pose" in record:
            raise SelectionError("Right-arm parking preset cannot be combined with a taught pose.")
        return normalize_right_park_pose(right_via_pose())
    controller_pose = record.get("controller_pose")
    expected = {"arm": "right", "tool": "tool0", "wobj": "wobj0", "coordinate": "Wobj"}
    if (not isinstance(controller_pose, dict)
            or any(controller_pose.get(key) != value for key, value in expected.items())):
        raise SelectionError("Right-arm parking file requires controller_pose for right arm, tool0, wobj0 and Wobj coordinates.")
    if not isinstance(record.get("pose"), dict):
        raise SelectionError("Right-arm parking file requires a six-axis pose object.")
    return normalize_right_park_pose(record["pose"])
