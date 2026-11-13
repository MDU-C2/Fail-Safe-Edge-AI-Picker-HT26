"""Explicit, read-only ABB collision status for a matching gateway profile.

Every read verifies both arms' seven joint values against the gateway before
using native controller status. This is a consistency check, not a controller
identity certificate or a replacement for the controller's motion interlocks.
Importing or loading a profile never performs network I/O. No mutation endpoint
is exposed; errors intentionally omit credentials, URLs and response bodies.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

import requests
from requests.auth import HTTPDigestAuth


TIMEOUT_SECONDS = 5
JOINT_TOLERANCE_DEGREES = 0.1
MAX_PROFILE_BYTES = 16_384
MAX_RESPONSE_BYTES = 65_536
COLLISION_STATES = frozenset({"INIT", "CONFIRMED", "TRIGGERED", "TRIGGERED_ACK"})


def _unique_fields(pairs):
    result, names = {}, set()
    for key, value in pairs:
        name = key.strip()
        if name in names:
            raise ValueError("Duplicate controller status field.")
        names.add(name)
        result[key] = value
    return result


def _validate_url(value, *, origin_only):
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(character.isspace() or ord(character) < 32 for character in value)
            or any(character in value for character in "\\?#")):
        raise ValueError("Controller status profile has an invalid URL.")
    try:
        parts = urlsplit(value)
        valid = (parts.scheme in {"http", "https"} and parts.hostname
                 and parts.username is None and parts.password is None
                 and not parts.query and not parts.fragment
                 and (not origin_only or not parts.path))
        # Accessing port also validates malformed/out-of-range values.
        if not valid or (parts.port is not None and parts.port < 1):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Controller status profile has an invalid URL.") from None
    return value


def load_controller_status_profile(path, gateway_api):
    """Load an opt-in exact-gateway profile; absent/unrelated profiles do nothing."""
    try:
        with Path(path).open("rb") as source:
            data = source.read(MAX_PROFILE_BYTES + 1)
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError):
        raise ValueError("Controller status profile could not be read.") from None
    try:
        if len(data) > MAX_PROFILE_BYTES:
            raise ValueError
        profile = json.loads(data, object_pairs_hook=_unique_fields)
    except (ValueError, UnicodeError):
        raise ValueError("Controller status profile is invalid.") from None
    if not isinstance(profile, dict):
        raise ValueError("Controller status profile is invalid.")
    if profile.get("gateway_api") != gateway_api:
        return None
    if (type(profile.get("schema_version")) is not int
            or profile["schema_version"] != 1
            or set(profile) != {"schema_version", "gateway_api", "controller_url"}):
        raise ValueError("Matching controller status profile is invalid.")
    _validate_url(profile["gateway_api"], origin_only=False)
    return NativeControllerStatus(profile["controller_url"])


def _number(value, *, native=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, str) if native else (int, float)):
        raise ValueError("Controller joint status is invalid.")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ValueError("Controller joint status is invalid.") from None
    if not math.isfinite(number):
        raise ValueError("Controller joint status is invalid.")
    return number


class NativeControllerStatus:
    """GET-only native status reader; credentials are never part of its repr."""

    def __init__(self, controller_url):
        self.controller_url = _validate_url(controller_url, origin_only=True)
        self._auth = HTTPDigestAuth(os.environ.get("GATEWAY_RWS_USER", "Default User"),
                                    os.environ.get("GATEWAY_RWS_PASSWORD", "robotics"))

    def _read_state(self, endpoint):
        response = None
        deadline = time.monotonic() + TIMEOUT_SECONDS
        try:
            response = requests.get(f"{self.controller_url}{endpoint}?json=1",
                                    auth=self._auth, timeout=TIMEOUT_SECONDS,
                                    allow_redirects=False, stream=True)
            response.raise_for_status()
            if response.status_code != 200 or time.monotonic() > deadline:
                raise ValueError
            data = bytearray()
            # Bound the final decoded body before parsing. requests' Digest
            # exchange/read timeout remains an inactivity limit; the elapsed
            # checks reject slow final responses, not a hard thread deadline.
            for chunk in response.iter_content(chunk_size=4096):
                if len(data) + len(chunk) > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
                    raise ValueError
                data.extend(chunk)
            payload = json.loads(data, object_pairs_hook=_unique_fields)
            entries = payload.get("_embedded", {}).get("_state")
            if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
                raise ValueError
            # ABB returns a trailing space in some field names. Reject duplicate
            # normalized keys rather than silently choosing one conflicting value.
            entry = {}
            for key, value in entries[0].items():
                if not isinstance(key, str) or key.strip() in entry:
                    raise ValueError
                entry[key.strip()] = value
            return entry
        except (requests.RequestException, ValueError, TypeError, AttributeError, OverflowError):
            raise ValueError("Native controller status could not be verified.") from None
        finally:
            if response is not None:
                response.close()

    def read_collision(self, gateway_get):
        """Return exact ABB collision state only after fresh two-arm agreement."""
        for arm, unit in (("right", "ROB_R"), ("left", "ROB_L")):
            try:
                gateway = gateway_get(f"/robot/arms/{arm}/joints")
            except Exception:
                raise ValueError("Gateway arm status could not be verified.") from None
            if (not isinstance(gateway, dict) or gateway.get("arm") != arm
                    or ("mech_unit" in gateway and gateway["mech_unit"] != unit)
                    or not isinstance(gateway.get("joints"), list)
                    or len(gateway["joints"]) != 6):
                raise ValueError("Gateway arm status is invalid.")
            gateway_values = [_number(value) for value in gateway["joints"]]
            gateway_values.append(_number(gateway.get("arm_angle")))
            native = self._read_state(f"/rw/motionsystem/mechunits/{unit}/jointtarget")
            native_values = [_number(native.get(f"rax_{axis}"), native=True) for axis in range(1, 7)]
            native_values.append(_number(native.get("eax_a"), native=True))
            if any(abs(expected - actual) > JOINT_TOLERANCE_DEGREES + 1e-9
                   for expected, actual in zip(gateway_values, native_values)):
                raise ValueError("Gateway and native controller joint positions do not match.")
        state = self._read_state("/rw/panel/coldetstate").get("coldetstate")
        if not isinstance(state, str) or state not in COLLISION_STATES:
            raise ValueError("Native controller returned an unknown collision state.")
        return state
