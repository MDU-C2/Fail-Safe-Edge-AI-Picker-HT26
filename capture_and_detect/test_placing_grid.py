"""Placement store tests use temporary grids only, never the lab's occupancy file."""

import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from cup_inventory import SelectionError, atomic_json
from placing_grid import PlacingGrid, validate_place


def location(location_id, *, occupied=False, **fields):
    return {"id": location_id, "x": 400 + location_id * 20, "y": -200,
            "z": 230, "occupied": occupied, **fields}


class PlacingGridTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "placing_grid.json"
        self.data = {"description": "preserve extra metadata", "locations": [
            location(1, occupied=True), location(2), location(3),
        ]}
        self.write(self.data)
        self.grid = PlacingGrid(self.path)

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_preview_uses_file_order_and_does_not_mutate_occupancy(self):
        before = self.path.read_bytes()
        preview = self.grid.preview(2)
        self.assertEqual([item["id"] for item in preview["locations"]], [2, 3])
        self.grid.validate_preview(preview)
        self.assertEqual(self.path.read_bytes(), before)
        preview["locations"][0]["x"] = 0
        self.assertEqual(self.grid.snapshot()["locations"][1]["x"], 440)

    def test_snapshot_copies_and_fingerprints_all_document_fields(self):
        before = self.grid.snapshot()
        before["locations"][0]["occupied"] = False
        self.assertTrue(self.grid.snapshot()["locations"][0]["occupied"])
        self.data["description"] = "changed"
        self.write(self.data)
        self.assertNotEqual(before["fingerprint"], self.grid.snapshot()["fingerprint"])

    def test_release_all_clears_occupancy_reservations_and_vision_preserving_metadata(self):
        self.data["locations"][0].update(
            reserved=True, reservation_id="interrupted", vision_blocked=True,
            operator_note={"message": "keep this note"})
        self.data["locations"][1].update(reserved=False, vision_blocked=False)
        self.data["locations"][2].update(vision_blocked=True, label="third")
        self.write(self.data)
        before = self.grid.snapshot()
        with patch("placing_grid.atomic_json", wraps=atomic_json) as write:
            result = self.grid.release_all(before["fingerprint"])
        write.assert_called_once()
        expected = copy.deepcopy(self.data)
        for slot in expected["locations"]:
            slot["occupied"] = False
            for field in ("reserved", "reservation_id", "vision_blocked"):
                slot.pop(field, None)
        self.assertEqual(self.read(), expected)
        self.assertEqual(result, self.grid.snapshot())
        self.assertNotEqual(result["fingerprint"], before["fingerprint"])
        self.assertEqual(self.grid.preview(3)["locations"], expected["locations"])
        result["locations"][0]["operator_note"]["message"] = "changed copy"
        self.assertEqual(self.read(), expected)

    def test_repeated_release_uses_current_fingerprint_without_rewriting_free_grid(self):
        before = self.grid.snapshot()
        released = self.grid.release_all(before["fingerprint"])
        current = self.path.read_bytes()
        with patch("placing_grid.atomic_json") as write:
            result = self.grid.release_all(released["fingerprint"])
        write.assert_not_called()
        self.assertEqual(result, released)
        self.assertEqual(self.path.read_bytes(), current)
        with self.assertRaisesRegex(SelectionError, "changed"):
            self.grid.release_all(before["fingerprint"])
        self.assertEqual(self.path.read_bytes(), current)

    def test_stale_release_rejects_any_document_change_without_writes(self):
        for mutation in (lambda value: value.update(description="another operator edited this"),
                         lambda value: value["locations"][0].update(x=421),
                         lambda value: value["locations"][1].update(
                             occupied=True, reserved=True, reservation_id="another-controller")):
            self.write(self.data)
            snapshot = self.grid.snapshot()
            changed = copy.deepcopy(self.data)
            mutation(changed)
            self.write(changed)
            before = self.path.read_bytes()
            with self.subTest(changed=changed), patch("placing_grid.atomic_json") as write:
                with self.assertRaisesRegex(SelectionError, "changed"):
                    self.grid.release_all(snapshot["fingerprint"])
            write.assert_not_called()
            self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_release_fingerprint_cannot_change_slots(self):
        fingerprint = self.grid.snapshot()["fingerprint"]
        invalid = (None, {}, [], 1, True, b"a" * 64, "", "a" * 63,
                   "a" * 65, "g" * 64, fingerprint.upper(), fingerprint + "\n")
        before = self.path.read_bytes()
        for candidate in invalid:
            with self.subTest(fingerprint=candidate), patch("placing_grid.atomic_json") as write:
                with self.assertRaisesRegex(SelectionError, "Invalid.*fingerprint"):
                    self.grid.release_all(candidate)
            write.assert_not_called()
            self.assertEqual(self.path.read_bytes(), before)

    def test_release_refuses_invalid_grid_without_repairing_or_writing(self):
        fingerprint = self.grid.snapshot()["fingerprint"]
        invalid = ['{"locations": [], "locations": []}', '{',
                   json.dumps({"locations": [location(1), location(1)]}),
                   json.dumps({"locations": [location(1, x=900)]}),
                   json.dumps({"locations": [location(1, reserved=True)]}),
                   json.dumps({"locations": [location(1, reservation_id="unattached")]}),
                   json.dumps({"locations": [location(1, occupied="true")]})]
        for source in invalid:
            self.path.write_text(source, encoding="utf-8")
            with self.subTest(source=source), patch("placing_grid.atomic_json") as write:
                with self.assertRaises(SelectionError):
                    self.grid.release_all(fingerprint)
            write.assert_not_called()
            self.assertEqual(self.path.read_text(encoding="utf-8"), source)

    def test_failed_release_write_surfaces_error_and_preserves_current_grid(self):
        snapshot = self.grid.snapshot()
        before = self.path.read_bytes()
        with patch("placing_grid.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(SelectionError, "disk full"):
                self.grid.release_all(snapshot["fingerprint"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_release_invalidates_existing_motion_preview_and_reservation(self):
        preview = self.grid.preview()
        reservation = self.grid.reserve(preview)
        self.grid.release_all(self.grid.snapshot()["fingerprint"])
        with self.assertRaisesRegex(SelectionError, "changed"):
            self.grid.reserve(preview)
        with self.assertRaisesRegex(SelectionError, "changed"):
            self.grid.validate_reservation(reservation)

    def test_observation_releases_clear_slot_and_marks_observed_cup_occupied(self):
        self.data["locations"][0].update(vision_blocked=True, label={"name": "first"})
        self.data["locations"][1]["vision_blocked"] = True
        self.write(self.data)
        with patch("placing_grid.atomic_json", wraps=atomic_json) as write:
            result = self.grid.reconcile_observation(
                self.grid.snapshot(), {1: "clear", 2: "occupied", 3: "clear"})
        write.assert_called_once()
        expected = copy.deepcopy(self.data)
        expected["locations"][0]["occupied"] = False
        expected["locations"][0].pop("vision_blocked")
        expected["locations"][1]["occupied"] = True
        expected["locations"][1].pop("vision_blocked")
        self.assertEqual(self.read(), expected)
        self.assertEqual(result, self.grid.snapshot())
        result["locations"][0]["x"] = 0
        self.assertEqual(self.grid.preview()["locations"][0], expected["locations"][0])

    def test_unknown_preserves_occupancy_and_prevents_free_slot_selection(self):
        result = self.grid.reconcile_observation(
            self.grid.snapshot(), {1: "unknown", 2: "unknown", 3: "clear"})
        self.assertTrue(result["locations"][0]["occupied"])
        self.assertFalse(result["locations"][1]["occupied"])
        self.assertTrue(result["locations"][0]["vision_blocked"])
        self.assertTrue(result["locations"][1]["vision_blocked"])
        self.assertEqual(self.grid.preview()["locations"][0]["id"], 3)
        with self.assertRaisesRegex(SelectionError, "1 free"):
            self.grid.preview(2)
        self.grid.reconcile_observation(result, {1: "unknown", 2: "clear", 3: "clear"})
        self.assertNotIn("vision_blocked", self.read()["locations"][1])
        self.assertEqual(self.grid.preview()["locations"][0]["id"], 2)

    def test_reserved_slot_and_all_its_metadata_are_untouched_by_observation(self):
        self.data["locations"][0].update(
            reserved=True, reservation_id="unfinished", vision_blocked=True,
            operator_note={"message": "inspect this arm"})
        for state in ("clear", "occupied", "unknown"):
            self.write(self.data)
            before = self.path.read_bytes()
            with self.subTest(state=state), patch("placing_grid.atomic_json") as write:
                result = self.grid.reconcile_observation(
                    self.grid.snapshot(), {1: state, 2: "clear", 3: "clear"})
            write.assert_not_called()
            self.assertEqual(result["locations"][0], self.data["locations"][0])
            self.assertEqual(self.path.read_bytes(), before)

    def test_unchanged_observation_does_not_rewrite_file(self):
        snapshot = self.grid.snapshot()
        before = self.path.read_bytes()
        with patch("placing_grid.atomic_json") as write:
            result = self.grid.reconcile_observation(snapshot, {1: "occupied", 2: "clear", 3: "clear"})
        write.assert_not_called()
        self.assertEqual(result, snapshot)
        self.assertEqual(self.path.read_bytes(), before)

    def test_stale_observation_rejected_for_any_document_change(self):
        for mutation in (lambda value: value.update(description="another operator edited this"),
                         lambda value: value["locations"][0].update(x=421),
                         lambda value: value["locations"][1].update(
                             occupied=True, reserved=True, reservation_id="another-controller")):
            self.write(self.data)
            snapshot = self.grid.snapshot()
            changed = copy.deepcopy(self.data)
            mutation(changed)
            self.write(changed)
            before = self.path.read_bytes()
            with self.subTest(changed=changed), patch("placing_grid.atomic_json") as write:
                with self.assertRaisesRegex(SelectionError, "changed"):
                    self.grid.reconcile_observation(snapshot, {1: "clear", 2: "clear", 3: "clear"})
            write.assert_not_called()
            self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_observation_mapping_rejects_whole_update_before_write(self):
        invalid = [None, [], {}, {1: "clear", 2: "clear"},
                   {1: "clear", 2: "clear", 3: "clear", 4: "clear"},
                   {True: "clear", 2: "clear", 3: "clear"},
                   {1.0: "clear", 2: "clear", 3: "clear"},
                   {"1": "clear", 2: "clear", 3: "clear"},
                   {1: "clear", 2: "clear", 3: "free"},
                   {1: "clear", 2: "clear", 3: True},
                   {1: "clear", 2: "clear", 3: []}]
        snapshot = self.grid.snapshot()
        before = self.path.read_bytes()
        for states in invalid:
            with self.subTest(states=states), patch("placing_grid.atomic_json") as write:
                with self.assertRaises(SelectionError):
                    self.grid.reconcile_observation(snapshot, states)
            write.assert_not_called()
            self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_or_tampered_snapshot_cannot_change_slots(self):
        snapshot = self.grid.snapshot()
        invalid = [None, {}, {"fingerprint": snapshot["fingerprint"]},
                   {**snapshot, "fingerprint": None}, {**snapshot, "fingerprint": "invalid"},
                   {**snapshot, "locations": []}, {**snapshot, "locations": [location(1)]}]
        before = self.path.read_bytes()
        for candidate in invalid:
            with self.subTest(snapshot=candidate), patch("placing_grid.atomic_json") as write:
                with self.assertRaises(SelectionError):
                    self.grid.reconcile_observation(candidate, {1: "clear", 2: "clear", 3: "clear"})
            write.assert_not_called()
            self.assertEqual(self.path.read_bytes(), before)

    def test_failed_observation_write_preserves_current_grid(self):
        snapshot = self.grid.snapshot()
        before = self.path.read_bytes()
        with patch("placing_grid.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(SelectionError, "disk full"):
                self.grid.reconcile_observation(snapshot, {1: "clear", 2: "clear", 3: "clear"})
        self.assertEqual(self.path.read_bytes(), before)

    def test_cannot_forge_preview_with_vision_blocked_free_slot(self):
        self.data["locations"][1]["vision_blocked"] = True
        self.write(self.data)
        preview = {"fingerprint": self.grid.snapshot()["fingerprint"],
                   "locations": [copy.deepcopy(self.data["locations"][1])]}
        for action in (self.grid.validate_preview, self.grid.reserve):
            with self.subTest(action=action), self.assertRaisesRegex(SelectionError, "free locations"):
                action(preview)
        self.assertEqual(self.read(), self.data)

    def test_observation_change_invalidates_existing_motion_preview(self):
        preview = self.grid.preview()
        self.grid.reconcile_observation(self.grid.snapshot(), {1: "occupied", 2: "unknown", 3: "clear"})
        with self.assertRaisesRegex(SelectionError, "changed"):
            self.grid.reserve(preview)

    def test_reserve_is_persistent_and_original_picker_will_skip_slot(self):
        reserved = self.grid.reserve(self.grid.preview())
        current = self.read()
        self.assertTrue(current["locations"][1]["occupied"])
        self.assertTrue(current["locations"][1]["reserved"])
        self.assertEqual(current["locations"][1]["reservation_id"], reserved["reservation_id"])
        self.assertEqual(current["description"], self.data["description"])
        self.assertEqual(current["locations"][0], self.data["locations"][0])
        self.assertEqual(PlacingGrid(self.path).preview()["locations"][0]["id"], 3)
        self.assertEqual(reserved["fingerprint"], self.grid.snapshot()["fingerprint"])

    def test_completion_keeps_occupied_and_removes_only_selected_reservation(self):
        reservation = self.grid.reserve(self.grid.preview(2))
        result = self.grid.complete(reservation, 2)
        self.assertEqual(result, location(2, occupied=True))
        self.assertTrue(self.read()["locations"][2]["reserved"])
        self.grid.complete(reservation, 3)
        self.assertEqual(self.read()["locations"], [location(1, occupied=True),
                         location(2, occupied=True), location(3, occupied=True)])
        with self.assertRaises(SelectionError):
            self.grid.complete(reservation, 2)

    def test_completion_preserves_unrelated_changes(self):
        reservation = self.grid.reserve(self.grid.preview())
        value = self.read()
        value["locations"][2]["occupied"] = True
        value["description"] = "edited by another operator"
        self.write(value)
        self.grid.complete(reservation, 2)
        self.assertTrue(self.read()["locations"][2]["occupied"])
        self.assertEqual(self.read()["description"], value["description"])

    def test_stale_preview_cannot_reserve_or_validate(self):
        preview = self.grid.preview()
        self.data["locations"][2]["x"] += 1
        self.write(self.data)
        for action in (self.grid.validate_preview, self.grid.reserve):
            with self.subTest(action=action), self.assertRaisesRegex(SelectionError, "changed"):
                action(preview)
        self.assertFalse(self.read()["locations"][1]["occupied"])

    def test_cannot_reserve_tampered_occupied_duplicate_or_unknown_slot(self):
        for update in (lambda p: p["locations"][0].update(x=500),
                       lambda p: p.update(locations=[copy.deepcopy(self.data["locations"][0])]),
                       lambda p: p["locations"].append(copy.deepcopy(p["locations"][0])),
                       lambda p: p["locations"][0].update(id=999),
                       lambda p: p.update(locations=[])):
            preview = self.grid.preview()
            update(preview)
            with self.subTest(preview=preview), self.assertRaises(SelectionError):
                self.grid.reserve(preview)
        self.assertEqual(self.read(), self.data)

    def test_failed_reservation_write_prevents_returning_reservation(self):
        preview = self.grid.preview()
        with patch("placing_grid.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(SelectionError, "disk full"):
                self.grid.reserve(preview)
        self.assertEqual(self.read(), self.data)

    def test_failed_completion_keeps_slot_reserved(self):
        reservation = self.grid.reserve(self.grid.preview())
        before = self.path.read_bytes()
        with patch("placing_grid.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(SelectionError, "disk full"):
                self.grid.complete(reservation, 2)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(self.read()["locations"][1]["reserved"])

    def test_changed_reserved_slot_cannot_be_completed(self):
        reservation = self.grid.reserve(self.grid.preview())
        value = self.read()
        value["locations"][1]["x"] += 1
        self.write(value)
        with self.assertRaisesRegex(SelectionError, "changed"):
            self.grid.complete(reservation, 2)
        self.assertEqual(self.read(), value)

    def test_invalid_reservation_completion_is_rejected(self):
        reservation = self.grid.reserve(self.grid.preview())
        before = self.path.read_bytes()
        for candidate, slot in (({}, 2), (reservation, True), (reservation, 3),
                                ({**reservation, "reservation_id": "wrong"}, 2)):
            with self.subTest(candidate=candidate, slot=slot), self.assertRaises(SelectionError):
                self.grid.complete(candidate, slot)
        self.assertEqual(self.path.read_bytes(), before)

    def test_validate_reservation_checks_pending_subset_without_writes(self):
        reservation = self.grid.reserve(self.grid.preview(2))
        before = self.path.read_bytes()
        self.assertEqual(self.grid.validate_reservation(reservation), self.grid.snapshot())
        self.assertEqual(self.path.read_bytes(), before)
        self.grid.complete(reservation, 2)
        self.grid.validate_reservation(reservation, [3])
        with self.assertRaisesRegex(SelectionError, "changed"):
            self.grid.validate_reservation(reservation)

    def test_validate_reservation_detects_changed_freed_or_reassigned_slot(self):
        for mutation in (lambda slot: slot.update(x=500),
                         lambda slot: slot.update(occupied=False, reserved=False),
                         lambda slot: slot.update(reservation_id="someone-else")):
            self.write(self.data)
            reservation = self.grid.reserve(self.grid.preview())
            value = self.read()
            mutation(value["locations"][1])
            self.write(value)
            before = self.path.read_bytes()
            with self.subTest(value=value), self.assertRaises(SelectionError):
                self.grid.validate_reservation(reservation)
            self.assertEqual(self.path.read_bytes(), before)

    def test_validate_reservation_rejects_unknown_duplicate_or_invalid_ids(self):
        reservation = self.grid.reserve(self.grid.preview())
        for ids in ([], [2, 2], [3], [True], [2.0], "2", {2}):
            with self.subTest(ids=ids), self.assertRaises(SelectionError):
                self.grid.validate_reservation(reservation, ids)

    def test_only_one_concurrent_controller_can_reserve_same_preview(self):
        preview = self.grid.preview()

        def reserve():
            try:
                return PlacingGrid(self.path).reserve(preview)
            except SelectionError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: reserve(), range(2)))
        self.assertEqual(sum(item is not None for item in results), 1)

    def test_observation_and_reservation_cannot_both_apply_same_snapshot(self):
        snapshot = self.grid.snapshot()
        preview = self.grid.preview()

        def apply(action):
            try:
                action()
                return True
            except SelectionError:
                return False

        actions = [lambda: PlacingGrid(self.path).reserve(preview),
                   lambda: PlacingGrid(self.path).reconcile_observation(
                       snapshot, {1: "clear", 2: "unknown", 3: "clear"})]
        with ThreadPoolExecutor(max_workers=2) as pool:
            reserved, observed = list(pool.map(apply, actions))
        self.assertNotEqual(reserved, observed)
        current = self.read()["locations"]
        if reserved:
            self.assertTrue(current[0]["occupied"])
            self.assertTrue(current[1]["reserved"])
            self.assertNotIn("vision_blocked", current[1])
        else:
            self.assertFalse(current[0]["occupied"])
            self.assertTrue(current[1]["vision_blocked"])
            self.assertNotIn("reserved", current[1])

    def test_missing_full_or_insufficient_grid_never_creates_fallback_slots(self):
        with self.assertRaisesRegex(SelectionError, "2 free"):
            self.grid.preview(3)
        self.data["locations"] = [location(1, occupied=True)]
        self.write(self.data)
        with self.assertRaisesRegex(SelectionError, "0 free"):
            self.grid.preview()
        self.path.unlink()
        with self.assertRaises(SelectionError):
            self.grid.preview()
        self.assertFalse(self.path.exists())

    def test_preview_count_requires_positive_integer(self):
        for count in (0, -1, True, 1.0, None):
            with self.subTest(count=count), self.assertRaises(SelectionError):
                self.grid.preview(count)

    def test_malformed_json_is_rejected_without_rewriting(self):
        for text in ('{"locations": [], "locations": []}', '{"locations": [NaN]}',
                     '{"locations": [], "extra": 1e999}', '[]', '{}', '{',
                     '{"locations": [{"id": 1, "id": 2}]}'):
            self.path.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(SelectionError):
                self.grid.snapshot()
            self.assertEqual(self.path.read_text(encoding="utf-8"), text)

    def test_invalid_or_duplicate_locations_fail_closed_even_if_occupied(self):
        bad = [location(True), location(0), location(2, occupied=1),
               location(2, x=float("nan")), location(2, x=True), location(2, y=-401),
               location(2, z=621), location(2, z=89),
               location(2, reserved=True), location(2, reserved="false"),
               location(2, vision_blocked="false"), location(2, vision_blocked=0),
               location(2, vision_blocked=None),
               location(2, reservation_id="unattached"), location(2, occupied=True, x=900)]
        for candidate in bad:
            self.data["locations"] = [candidate]
            self.write(self.data)
            with self.subTest(candidate=candidate), self.assertRaises(SelectionError):
                self.grid.preview()
        self.data["locations"] = [location(2), location(2)]
        self.write(self.data)
        with self.assertRaisesRegex(SelectionError, "Duplicate"):
            self.grid.snapshot()

    def test_placement_can_descend_below_tray_clearance_with_clear_approach(self):
        for height in (119, 198, 199, 230, 620):
            candidate = location(1, z=height)
            with self.subTest(height=height):
                self.assertEqual(validate_place(candidate), candidate)
        for height in (49, 118, 620.1, 701):
            with self.subTest(height=height), self.assertRaises(SelectionError):
                validate_place(location(1, z=height))

    def test_validate_place_returns_copy_and_checks_raised_target(self):
        source = location(10, y=-360)
        result = validate_place(source)
        result["x"] = 0
        self.assertEqual(source["x"], 600)
        with self.assertRaisesRegex(SelectionError, "above place"):
            validate_place(location(2, z=650))

    def test_duplicate_coordinates_rejected_even_for_different_ids_or_occupied_slots(self):
        for occupied in (False, True):
            self.data["locations"] = [location(1, occupied=occupied),
                                      location(2, x=420.0, y=-200.0, z=230.0)]
            self.write(self.data)
            before = self.path.read_bytes()
            with self.subTest(occupied=occupied), self.assertRaisesRegex(SelectionError, "Duplicate placement coordinates"):
                self.grid.preview()
            self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
