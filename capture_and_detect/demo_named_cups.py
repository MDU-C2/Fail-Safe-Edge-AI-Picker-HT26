"""Saved sample cups for the offline dashboard; never a real visual matcher.

The three small JPEG illustrations are bundled assets. Runtime uses the Python
standard library only and follows the normal preview/verification/confirmation
flow, matching immutable synthetic identities rather than old scan numbers.
"""

import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import stat
import uuid

from cup_inventory import SelectionError, atomic_json
from language_commands import LanguageController, inventory_fingerprint
from named_cups import (MAX_REFERENCES, PUBLIC_FIELDS, NamedCupStore, _full_cups,
                        _identifier, _invalid_constant, _name, _unique_object, name_key)


IDENTITIES = frozenset(("teal", "white", "orange"))
_FIELDS = set(PUBLIC_FIELDS) | {"demo_identity", "image_jpeg_base64"}
_MAX_STORE = 256 * 1024


def _demo_inventory(inventory):
    if (not isinstance(inventory, dict) or inventory.get("gateway") != "demo"
            or not isinstance(inventory.get("model"), dict) or inventory["model"].get("demo") is not True
            or not isinstance(inventory.get("capture"), str)
            or not re.fullmatch(r"demo_[A-Za-z0-9_-]{1,145}", inventory["capture"])):
        raise SelectionError("Sample-cup matching is available only in the offline demo.")
    return _full_cups(inventory)


def annotate_demo_inventory(inventory, identity_by_detection):
    """Attach stable sample identities before publishing a synthetic capture."""
    cups = _demo_inventory(inventory)
    if not isinstance(identity_by_detection, dict):
        raise SelectionError("The demo needs an explicit sample identity mapping.")
    identities = []
    for cup in cups:
        identity = identity_by_detection.get(cup["cup_detection_id"])
        if identity not in IDENTITIES:
            raise SelectionError("A demo cup is missing its sample identity.")
        identities.append(identity)
    for cup, identity in zip(cups, identities):
        cup["demo_identity"] = identity
    return inventory


def _thumbnail(identity):
    if identity not in IDENTITIES:
        raise SelectionError("Unknown sample cup identity.")
    path = Path(__file__).with_name("demo_assets") / f"cup-{identity}.jpg"
    try:
        with path.open("rb") as handle:
            data = handle.read(10001)
    except OSError as error:
        raise SelectionError("The sample cup illustration is unavailable.") from error
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9") or len(data) > 10000:
        raise SelectionError("The sample cup illustration is invalid.")
    return data


class DemoNamedCupStore(NamedCupStore):
    """An isolated demo schema, incompatible with real reference libraries."""

    def __init__(self, directory):
        directory = Path(directory)
        super().__init__(directory / "demo-saved-cups.json", directory)

    def _read(self):
        if self.path.is_symlink():
            raise SelectionError("Sample cup store must not be a symbolic link.")
        try:
            if not stat.S_ISREG(self.path.stat().st_mode):
                raise SelectionError("Sample cup store must be a regular file.")
            with self.path.open("rb") as handle:
                raw = handle.read(_MAX_STORE + 1)
        except FileNotFoundError:
            return {"schema_version": 1, "demo_only": True, "cups": []}
        if len(raw) > _MAX_STORE:
            raise SelectionError("The sample cup store exceeds its size limit.")
        try:
            value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            if (not isinstance(value, dict) or set(value) != {"schema_version", "demo_only", "cups"}
                    or type(value["schema_version"]) is not int or value["schema_version"] != 1
                    or value["demo_only"] is not True or not isinstance(value["cups"], list)
                    or len(value["cups"]) > MAX_REFERENCES):
                raise ValueError("Invalid demo store schema")
            ids, names = set(), set()
            for record in value["cups"]:
                if not isinstance(record, dict) or set(record) != _FIELDS:
                    raise ValueError("Invalid demo reference fields")
                _identifier(record["id"])
                if record["name"] != _name(record["name"]):
                    raise ValueError("Noncanonical name")
                if record["id"] in ids or name_key(record["name"]) in names:
                    raise ValueError("Duplicate reference")
                ids.add(record["id"])
                names.add(name_key(record["name"]))
                if (type(record["cup_id"]) is not int or record["cup_id"] < 1
                        or not isinstance(record["capture_id"], str)
                        or not re.fullmatch(r"demo_[A-Za-z0-9_-]{1,145}", record["capture_id"])
                        or not isinstance(record["saved_at"], str) or len(record["saved_at"]) > 40):
                    raise ValueError("Invalid capture identity")
                timestamp = datetime.fromisoformat(record["saved_at"].replace("Z", "+00:00"))
                if timestamp.tzinfo is None or timestamp.utcoffset().total_seconds() != 0:
                    raise ValueError("Invalid timestamp")
                expected = _thumbnail(record["demo_identity"])
                if (record["reference_sha256"] != hashlib.sha256(expected).hexdigest()
                        or record["image_jpeg_base64"] != base64.b64encode(expected).decode("ascii")):
                    raise ValueError("The synthetic reference changed")
            return value
        except (ValueError, TypeError, UnicodeError, KeyError, RecursionError) as error:
            raise SelectionError("Invalid sample cup store; it was not changed.") from error

    @classmethod
    def _with_image(cls, record):
        return {**super()._with_image(record), "demo_identity": record["demo_identity"]}

    def save(self, name, inventory, cup_id):
        name = _name(name)
        if type(cup_id) is not int or cup_id < 1:
            raise SelectionError("Choose a positive integer cup ID from the sample scan.")
        matches = [cup for cup in _demo_inventory(inventory) if cup["id"] == cup_id]
        if len(matches) != 1 or matches[0].get("demo_identity") not in IDENTITIES:
            raise SelectionError("Choose one full sample cup with a known synthetic identity.")
        identity = matches[0]["demo_identity"]
        data = _thumbnail(identity)
        with self._locked():
            value = self._read()
            if any(name_key(record["name"]) == name_key(name) for record in value["cups"]):
                raise SelectionError("That cup name already exists. Choose another name or delete its saved reference first.")
            if len(value["cups"]) >= MAX_REFERENCES:
                raise SelectionError("The sample cup store is full. Delete a reference first.")
            record = {"id": uuid.uuid4().hex, "name": name, "saved_at": datetime.now(timezone.utc).isoformat(),
                      "capture_id": inventory["capture"], "cup_id": cup_id,
                      "reference_sha256": hashlib.sha256(data).hexdigest(), "demo_identity": identity,
                      "image_jpeg_base64": base64.b64encode(data).decode("ascii")}
            value["cups"].append(record)
            atomic_json(self.path, value)
            return self._public(record)


class DemoIdentityMatcher:
    def match(self, reference, candidates):
        identity = reference.get("demo_identity")
        matches = [cup["id"] for cup in candidates if cup.get("demo_identity") == identity]
        if identity not in IDENTITIES or not matches:
            return {"status": "not_found", "cup_id": None, "reason": "Scripted demo: sample identity is absent."}
        if len(matches) != 1:
            return {"status": "ambiguous", "cup_id": None, "reason": "Scripted demo: multiple cups share the sample identity."}
        return {"status": "match", "cup_id": matches[0], "reason": f"Scripted demo: same {identity} sample identity."}


class DemoNamedLanguageController(LanguageController):
    def __init__(self, session, interpreter, *, directory, **options):
        if getattr(session.robot, "dry_run", None) is not True:
            raise SelectionError("Sample-cup matching requires an offline dry-run robot.")
        if "named_cups" in options or "cup_matcher" in options:
            raise ValueError("The demo supplies its own isolated sample store and matcher.")
        super().__init__(session, interpreter, named_cups=DemoNamedCupStore(directory),
                         cup_matcher=DemoIdentityMatcher(), **options)

    def save_cup(self, **arguments):
        result = super().save_cup(**arguments)
        result["message"] = (f"Saved {result['cup']['name']} as a synthetic sample reference. "
                             f"Scripted demo only; no robot command sent. Later use: pick saved {result['cup']['name']}")
        return result

    def _visual_match(self, reference, inventory, *, audit=None, stage="proposal"):
        if getattr(self.session.robot, "dry_run", None) is not True:
            raise SelectionError("Sample-cup matching requires an offline dry-run robot.")
        before = inventory_fingerprint(inventory)
        candidates = _demo_inventory(inventory)
        if any(cup.get("demo_identity") not in IDENTITIES for cup in candidates):
            raise SelectionError("The sample scan is missing an immutable demo identity.")
        result = self.cup_matcher.match(reference, candidates)
        if audit is not None:
            audit.setdefault("visual_matches", []).append({"stage": stage, "capture_id": inventory["capture"],
                "candidate_ids": [cup["id"] for cup in candidates], **result})
        self.session._check_stop()
        if before != inventory_fingerprint(inventory):
            raise SelectionError("The sample scan changed during matching. Request a new preview.")
        if result["status"] != "match":
            raise SelectionError(f"Cannot uniquely identify saved sample {reference['name']}: {result['reason']} No movement requested.")
        return result
