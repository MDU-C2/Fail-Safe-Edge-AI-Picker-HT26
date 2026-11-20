"""Validated placement slots with motion reservations reconciled by fresh scans.

A live caller reserves a slot before dispatching any robot motion. Interrupted or
unconfirmed sequences leave it reserved until the next scan or manual release.
With no sequence active, a fresh camera observation replaces stale reservations
with observed occupancy. Uncertain visibility blocks selection until a reliable
observation arrives. Grid reconciliation does not acknowledge robot recovery.
The original picker also sees reservations as occupied slots.
"""

import copy
import hashlib
import json
import re
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from cup_inventory import SelectionError, atomic_json, finite_number


PLACE_ABOVE = 80
# Multiple store instances can share a grid inside the single dashboard process.
_GRID_UPDATES = threading.RLock()


def validate_place(location):
    """Validate placement records; the gateway/controller enforce motion bounds."""
    if not isinstance(location, dict):
        raise SelectionError("Each placement location must be an object.")
    if type(location.get("id")) is not int or location["id"] < 1:
        raise SelectionError("Placement location ID must be a positive integer.")
    if type(location.get("occupied")) is not bool:
        raise SelectionError(f"Placement location {location['id']}: occupied must be true or false.")
    if "reserved" in location and type(location["reserved"]) is not bool:
        raise SelectionError(f"Placement location {location['id']}: reserved must be true or false.")
    if "vision_blocked" in location and type(location["vision_blocked"]) is not bool:
        raise SelectionError(f"Placement location {location['id']}: vision_blocked must be true or false.")
    if location.get("reserved") is True and not location["occupied"]:
        raise SelectionError(f"Placement location {location['id']}: a reserved slot must be occupied.")
    if "reservation_id" in location:
        if (location.get("reserved") is not True
                or not isinstance(location["reservation_id"], str)
                or not location["reservation_id"]):
            raise SelectionError(f"Placement location {location['id']}: invalid reservation metadata.")
    if not all(finite_number(location.get(axis)) for axis in ("x", "y", "z")):
        raise SelectionError(f"Placement location {location['id']}: coordinates must be finite numbers.")
    return copy.deepcopy(location)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"Non-finite JSON value: {value}")


def _fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PlacingGrid:
    """Read/update the existing JSON grid without creating or resetting slots.

    Updates are synchronized across threads in this process. The single-user
    application does not create lock files or claim cross-process exclusivity.
    """

    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def _locked(self):
        try:
            with _GRID_UPDATES:
                yield
        except OSError as error:
            raise SelectionError(f"Cannot access placement grid {self.path}: {error}") from error

    def _read(self):
        try:
            with self.path.open(encoding="utf-8") as stream:
                value = json.load(stream, object_pairs_hook=_unique_object,
                                  parse_constant=_invalid_constant)
            if not isinstance(value, dict) or not isinstance(value.get("locations"), list):
                raise ValueError("Expected an object containing a locations list")
            if "cell_size_mm" in value:
                size = value["cell_size_mm"]
                if not finite_number(size) or not 20 <= size <= 200:
                    raise ValueError("cell_size_mm must be a finite number from 20 to 200 mm")
            ids = set()
            positions = set()
            for location in value["locations"]:
                validate_place(location)
                if location["id"] in ids:
                    raise ValueError(f"Duplicate placement location ID: {location['id']}")
                ids.add(location["id"])
                position = tuple(location[axis] for axis in ("x", "y", "z"))
                if position in positions:
                    raise ValueError(f"Duplicate placement coordinates at location {location['id']}")
                positions.add(position)
            # Reject overflow-to-infinity values even in extra metadata fields.
            _fingerprint(value)
            return value
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as error:
            raise SelectionError(f"Invalid or unreadable placement grid {self.path}: {error}") from error

    @staticmethod
    def _snapshot(value):
        snapshot = {"fingerprint": _fingerprint(value), "locations": copy.deepcopy(value["locations"])}
        if "cell_size_mm" in value:
            snapshot["cell_size_mm"] = value["cell_size_mm"]
        return snapshot

    def snapshot(self):
        with self._locked():
            return self._snapshot(self._read())

    def release_all(self, expected_fingerprint):
        """Release all locations after the caller confirms physical inspection.

        The caller must prevent concurrent motion and invalidate pending motion
        proposals. The fingerprint binds this explicit release to the exact grid
        that was inspected; any intervening change rejects the whole update.
        Coordinates and other metadata are preserved, including operator notes.
        """
        with self._locked():
            value = self._read()
            if (not isinstance(expected_fingerprint, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint)):
                raise SelectionError("Invalid placement release fingerprint. Review the grid again.")
            if expected_fingerprint != _fingerprint(value):
                raise SelectionError("Placement grid changed before release. Review the grid again.")

            updated = copy.deepcopy(value)
            for location in updated["locations"]:
                location["occupied"] = False
                for field in ("reserved", "reservation_id", "vision_blocked"):
                    location.pop(field, None)
            if updated != value:
                atomic_json(self.path, updated)
            return self._snapshot(updated)

    def reconcile_observation(self, snapshot, states):
        """Apply camera occupancy by location ID without rejecting grid changes.

        The caller must establish positive evidence for ``clear`` or ``occupied``.
        The caller must prevent concurrent motion. Stale reservations are removed
        for every observed slot; ``unknown`` preserves occupancy but prevents
        selection until a reliable observation arrives. Updating this grid does
        not confirm robot recovery, which the caller tracks separately.
        The snapshot identifies the scanned cells, not a required file revision.
        Preserve current coordinates/metadata and any newly added cells.
        """
        with self._locked():
            value = self._read()
            if (not isinstance(snapshot, dict)
                    or not isinstance(snapshot.get("fingerprint"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", snapshot["fingerprint"])
                    or not isinstance(snapshot.get("locations"), list)):
                raise SelectionError("Invalid placement observation snapshot. Scan again.")
            ids = {validate_place(location)["id"] for location in snapshot["locations"]}
            if (not isinstance(states, dict)
                    or any(type(key) is not int or key < 1 for key in states)
                    or set(states) != ids
                    or any(type(state) is not str or state not in {"clear", "occupied", "unknown"}
                           for state in states.values())):
                raise SelectionError("Placement observation must give clear, occupied, or unknown for every location ID.")

            updated = copy.deepcopy(value)
            for location in updated["locations"]:
                if location["id"] not in states:
                    continue
                location.pop("reserved", None)
                location.pop("reservation_id", None)
                state = states[location["id"]]
                if state == "unknown":
                    location["vision_blocked"] = True
                else:
                    location["occupied"] = state == "occupied"
                    location.pop("vision_blocked", None)
            if updated != value:
                atomic_json(self.path, updated)
            return self._snapshot(updated)

    def preview(self, count=1):
        """Select free, visible slots in file order without modifying the grid."""
        if type(count) is not int or count < 1:
            raise SelectionError("Placement preview count must be a positive integer.")
        with self._locked():
            value = self._read()
            free = [location for location in value["locations"]
                    if not location["occupied"] and not location.get("vision_blocked", False)]
            if len(free) < count:
                raise SelectionError(
                    f"Placement grid has {len(free)} free location(s); {count} required. "
                    "Inspect the placement area and grid before trying again.")
            return {"fingerprint": _fingerprint(value), "locations": copy.deepcopy(free[:count])}

    @staticmethod
    def _validate_preview(value, preview):
        if (not isinstance(preview, dict)
                or not isinstance(preview.get("fingerprint"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", preview["fingerprint"])
                or not isinstance(preview.get("locations"), list)
                or not preview["locations"]):
            raise SelectionError("Invalid placement preview. Select the cup again.")
        if preview["fingerprint"] != _fingerprint(value):
            raise SelectionError("Placement grid changed after selection. Select the cup again.")
        current = {location["id"]: location for location in value["locations"]}
        selected_ids = set()
        for proposed in preview["locations"]:
            validate_place(proposed)
            location_id = proposed["id"]
            if (location_id in selected_ids or proposed["occupied"] or proposed.get("vision_blocked", False)
                    or proposed != current.get(location_id)):
                raise SelectionError("Placement preview no longer matches free locations. Select the cup again.")
            selected_ids.add(location_id)

    def validate_preview(self, preview):
        with self._locked():
            self._validate_preview(self._read(), preview)

    def reserve(self, preview):
        """Persist occupied/reserved flags before the caller sends robot commands.

        Failure to write raises and must prevent motion. An unknown outcome must
        not trigger a retry or assume the slot is free; a fresh scan reconciles
        the grid once the sequence has ended.
        """
        with self._locked():
            value = self._read()
            self._validate_preview(value, preview)
            reservation_id = uuid.uuid4().hex
            selected_ids = {location["id"] for location in preview["locations"]}
            selected = []
            for location in value["locations"]:
                if location["id"] in selected_ids:
                    location.update(occupied=True, reserved=True, reservation_id=reservation_id)
                    selected.append(copy.deepcopy(location))
            atomic_json(self.path, value)
            return {"reservation_id": reservation_id, "fingerprint": _fingerprint(value),
                    "locations": selected}

    @staticmethod
    def _reservation_locations(reservation):
        if (not isinstance(reservation, dict)
                or not isinstance(reservation.get("reservation_id"), str)
                or not reservation["reservation_id"]
                or not isinstance(reservation.get("locations"), list)
                or not reservation["locations"]):
            raise SelectionError("Invalid placement reservation.")
        expected = {}
        for location in reservation["locations"]:
            checked = validate_place(location)
            if (checked["id"] in expected or checked.get("reserved") is not True
                    or not checked["occupied"]
                    or checked.get("reservation_id") != reservation["reservation_id"]):
                raise SelectionError("Placement reservation metadata does not match.")
            expected[checked["id"]] = checked
        return expected

    def validate_reservation(self, reservation, location_ids=None):
        """Check pending reserved slots without writes; return the current snapshot.

        A batch caller supplies the IDs still awaiting completion. Slots already
        completed are occupied but no longer reserved, so checking those again
        intentionally fails. Unrelated grid metadata changes are preserved.
        """
        expected = self._reservation_locations(reservation)
        selected_ids = list(expected) if location_ids is None else location_ids
        if (not isinstance(selected_ids, (list, tuple)) or not selected_ids
                or any(type(item) is not int or item < 1 for item in selected_ids)
                or len(set(selected_ids)) != len(selected_ids)
                or any(item not in expected for item in selected_ids)):
            raise SelectionError("Invalid pending placement reservation IDs.")
        with self._locked():
            value = self._read()
            current = {item["id"]: item for item in value["locations"]}
            for location_id in selected_ids:
                if current.get(location_id) != expected[location_id]:
                    raise SelectionError("Reserved placement location changed; inspect the grid and robot.")
            return self._snapshot(value)

    def replan(self, reservation, candidate_locations, count, completed_ids=()):
        """Replace pending reservations using only originally reviewed targets.

        Call only between fully confirmed cup movements. ``candidate_locations``
        is the immutable, ordered list of clear destinations from the original
        scan; this operation neither captures nor reinterprets camera evidence.
        Keep completed cells and other owners' reservations unchanged. Changed
        coordinates, newly added cells, and unavailable targets cannot enter the
        new queue. Current metadata is preserved, without requiring a matching
        whole-grid fingerprint.

        Return free-shaped pending targets in ``placement`` and a reservation
        usable by ``complete`` and subsequent replans. Historical completed
        records remain in the reservation; when no records remain it is None.
        A zero count releases this caller's unused reservations only.
        """
        expected = self._reservation_locations(reservation)
        if type(count) is not int or count < 0:
            raise SelectionError("Placement replan count must be a nonnegative integer.")
        if (not isinstance(completed_ids, (list, tuple, set, frozenset))
                or any(type(item) is not int or item < 1 for item in completed_ids)
                or len(set(completed_ids)) != len(completed_ids)
                or any(item not in expected for item in completed_ids)):
            raise SelectionError("Invalid completed placement reservation IDs.")
        completed = set(completed_ids)
        if not isinstance(candidate_locations, (list, tuple)):
            raise SelectionError("Placement replan requires the original clear locations.")
        candidates = []
        candidate_ids = set()
        for candidate in candidate_locations:
            checked = validate_place(candidate)
            if (checked["id"] in candidate_ids or checked["occupied"]
                    or checked.get("reserved", False) or checked.get("vision_blocked", False)):
                raise SelectionError("Placement replan requires distinct originally clear locations.")
            candidate_ids.add(checked["id"])
            candidates.append(checked)

        with self._locked():
            value = self._read()
            updated = copy.deepcopy(value)
            current = {location["id"]: location for location in updated["locations"]}
            reservation_id = reservation["reservation_id"]
            for location_id in expected.keys() - completed:
                location = current.get(location_id)
                if (location is not None and location.get("reserved") is True
                        and location.get("reservation_id") == reservation_id):
                    location["occupied"] = False
                    location.pop("reserved", None)
                    location.pop("reservation_id", None)

            pending = []
            reserved = [copy.deepcopy(location) for location_id, location in expected.items()
                        if location_id in completed]
            for candidate in candidates:
                if len(pending) == count:
                    break
                location = current.get(candidate["id"])
                if (location is None or location["id"] in completed
                        or location["occupied"] or location.get("reserved", False)
                        or location.get("vision_blocked", False)
                        or any(location[axis] != candidate[axis] for axis in ("x", "y", "z"))):
                    continue
                pending.append(copy.deepcopy(location))
                location.update(occupied=True, reserved=True, reservation_id=reservation_id)
                reserved.append(copy.deepcopy(location))

            if updated != value:
                atomic_json(self.path, updated)
            fingerprint = _fingerprint(updated)
            return {
                "placement": {"fingerprint": fingerprint, "locations": pending},
                "reservation": ({"reservation_id": reservation_id, "fingerprint": fingerprint,
                                 "locations": reserved} if reserved else None),
            }

    def complete(self, reservation, location_id):
        """After a confirmed complete sequence, keep occupied and clear its marker.

        Grid updates must not turn a successful movement into a recovery error.
        If a scan or another update has replaced this reservation, preserve that
        newer state. Otherwise complete the slot without comparing its metadata.
        """
        expected_locations = self._reservation_locations(reservation)
        if type(location_id) is not int or location_id < 1:
            raise SelectionError("Invalid placement reservation completion.")
        if location_id not in expected_locations:
            raise SelectionError("Placement location is not part of this reservation.")
        with self._locked():
            value = self._read()
            location = next((item for item in value["locations"] if item["id"] == location_id), None)
            if location is None or location.get("reservation_id") != reservation["reservation_id"]:
                return copy.deepcopy(location)
            location["occupied"] = True
            location.pop("reserved", None)
            location.pop("reservation_id", None)
            atomic_json(self.path, value)
            return copy.deepcopy(location)
