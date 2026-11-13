"""Validated placement slots with conservative, persistent motion reservations.

A live caller reserves a slot before dispatching any robot motion. Interrupted or
unconfirmed sequences leave it occupied and reserved until a person inspects the
workspace and explicitly releases the locations. Manual release requires the
caller to establish that inspection and ensure no sequence is active. Camera
observations may update unreserved slots, but uncertain visibility blocks
selection and can never release a reservation.
The original picker also sees reservations as occupied slots.
"""

import copy
import fcntl
import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from pathlib import Path

from cup_inventory import SelectionError, _motion_flange_problem, atomic_json, finite_number


PLACE_ABOVE = 80


def validate_place(location):
    """Return a copy after checking a grid slot and its derived flange targets.

    These are coarse coordinate checks, not collision/reachability guarantees.
    The planner additionally checks the turning midpoint using the selected
    receiving offset; the grid itself only owns the place and raised-place.
    """
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
    place = tuple(location[axis] for axis in ("x", "y", "z"))
    above = (place[0], place[1], place[2] + PLACE_ABOVE)
    for label, target, pick in (("place", place, True), ("above place", above, False)):
        problem = _motion_flange_problem(target, pick=pick)
        if problem:
            raise SelectionError(f"Placement location {location['id']} ({label}): {problem}.")
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

    All cooperating custom controllers use a separate advisory lock file so an
    atomic replacement of the JSON does not replace the locked inode. Other
    programs that ignore this lock must not run concurrently with this controller.
    """

    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def _locked(self):
        lock_path = self.path.with_name("." + self.path.name + ".lock")
        try:
            with lock_path.open("a", encoding="utf-8") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
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
        """Atomically apply a complete camera observation of this exact snapshot.

        The caller must establish positive evidence for ``clear`` or ``occupied``.
        ``unknown`` preserves occupancy but prevents selecting the slot until a
        reliable observation arrives. Reserved slots are never changed, including
        their visibility metadata, because vision cannot confirm robot recovery.
        A concurrent grid change rejects the whole observation without writes.
        """
        with self._locked():
            value = self._read()
            if (not isinstance(snapshot, dict)
                    or not isinstance(snapshot.get("fingerprint"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", snapshot["fingerprint"])
                    or not isinstance(snapshot.get("locations"), list)):
                raise SelectionError("Invalid placement observation snapshot. Scan again.")
            if snapshot["fingerprint"] != _fingerprint(value):
                raise SelectionError("Placement grid changed during observation. Scan again.")
            if snapshot["locations"] != value["locations"]:
                raise SelectionError("Placement observation snapshot does not match the grid. Scan again.")
            if (("cell_size_mm" in snapshot) != ("cell_size_mm" in value)
                    or snapshot.get("cell_size_mm") != value.get("cell_size_mm")):
                raise SelectionError("Placement observation cell size does not match the grid. Scan again.")
            ids = {location["id"] for location in value["locations"]}
            if (not isinstance(states, dict)
                    or any(type(key) is not int or key < 1 for key in states)
                    or set(states) != ids
                    or any(type(state) is not str or state not in {"clear", "occupied", "unknown"}
                           for state in states.values())):
                raise SelectionError("Placement observation must give clear, occupied, or unknown for every location ID.")

            updated = copy.deepcopy(value)
            for location in updated["locations"]:
                if location.get("reserved") is True:
                    continue
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
        never trigger a retry or release; inspect the file and robot first.
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

    def complete(self, reservation, location_id):
        """After a confirmed complete sequence, keep occupied and clear its marker.

        Unrelated slot changes are preserved; the reserved slot itself must still
        match the reservation exactly. Calling complete twice is an error.
        """
        expected_locations = self._reservation_locations(reservation)
        if type(location_id) is not int or location_id < 1:
            raise SelectionError("Invalid placement reservation completion.")
        if location_id not in expected_locations:
            raise SelectionError("Placement location is not part of this reservation.")
        expected = expected_locations[location_id]
        with self._locked():
            value = self._read()
            location = next((item for item in value["locations"] if item["id"] == location_id), None)
            if location != expected:
                raise SelectionError("Reserved placement location changed; inspect the grid and robot.")
            location.pop("reserved", None)
            location.pop("reservation_id", None)
            atomic_json(self.path, value)
            return copy.deepcopy(location)
