"""Batch reservation changes use temporary grids and no camera or robot calls."""

import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from cup_inventory import SelectionError, atomic_json
from placing_grid import PlacingGrid
from test_placing_grid import location


class GridReplanTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "grid.json"
        self.data = {"description": "original scan", "cell_size_mm": 80,
                     "locations": [location(1, occupied=True)] +
                                  [location(i, label=f"cell {i}") for i in range(2, 7)]}
        self.write(self.data)
        self.grid = PlacingGrid(self.path)
        self.candidates = self.grid.preview(5)["locations"]

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def reserve(self, count):
        return self.grid.reserve(self.grid.preview(count))

    def test_shrinking_releases_only_unused_cells_and_keeps_completed_occupied(self):
        reservation = self.reserve(4)
        self.grid.complete(reservation, 2)
        original = copy.deepcopy(reservation)
        with patch("placing_grid.atomic_json", wraps=atomic_json) as write:
            result = self.grid.replan(reservation, self.candidates, 1, [2])
        write.assert_called_once()
        self.assertEqual(reservation, original)
        self.assertEqual(result["placement"]["locations"], [self.candidates[1]])
        self.assertEqual([item["id"] for item in result["reservation"]["locations"]], [2, 3])
        current = self.grid.snapshot()
        self.assertEqual(result["placement"]["fingerprint"], current["fingerprint"])
        self.assertEqual(result["reservation"]["fingerprint"], current["fingerprint"])
        self.assertEqual(current["locations"][1], dict(self.candidates[0], occupied=True))
        self.assertEqual(current["locations"][3:], self.candidates[2:])
        self.assertEqual(self.read()["description"], self.data["description"])
        self.assertEqual(self.read()["cell_size_mm"], 80)
        self.grid.validate_reservation(result["reservation"], [3])
        self.grid.complete(result["reservation"], 3)
        self.assertEqual(self.grid.snapshot()["locations"][2], dict(self.candidates[1], occupied=True))

    def test_expanding_uses_other_originally_clear_destinations_in_original_order(self):
        reservation = self.reserve(1)
        self.grid.complete(reservation, 2)
        value = self.read()
        value["locations"].reverse()
        value["locations"].append(location(7))
        self.write(value)
        result = self.grid.replan(reservation, self.candidates, 10, [2])
        self.assertEqual([item["id"] for item in result["placement"]["locations"]], [3, 4, 5, 6])
        self.assertEqual(result["reservation"]["reservation_id"], reservation["reservation_id"])
        self.assertEqual(self.read()["locations"][-1], location(7))
        for pending in result["placement"]["locations"]:
            self.assertFalse(pending["occupied"])
            self.assertNotIn("reservation_id", pending)

    def test_repeated_replans_keep_completed_history_and_support_complete(self):
        reservation = self.reserve(1)
        self.grid.complete(reservation, 2)
        first = self.grid.replan(reservation, self.candidates, 3, [2])
        self.grid.complete(first["reservation"], 3)
        second = self.grid.replan(first["reservation"], self.candidates, 1, [2, 3])
        self.assertEqual([item["id"] for item in second["reservation"]["locations"]], [2, 3, 4])
        self.assertEqual([item["id"] for item in second["placement"]["locations"]], [4])
        self.grid.complete(second["reservation"], 4)
        final = self.grid.replan(second["reservation"], self.candidates, 0, [2, 3, 4])
        self.assertEqual(final["placement"]["locations"], [])
        self.assertEqual([item["id"] for item in final["reservation"]["locations"]], [2, 3, 4])
        self.assertEqual([item["occupied"] for item in self.read()["locations"]],
                         [True, True, True, True, False, False])
        self.assertFalse(any(item.get("reserved") for item in self.read()["locations"]))

    def test_zero_count_without_completed_cups_cleanly_releases_own_reservation(self):
        reservation = self.reserve(2)
        result = self.grid.replan(reservation, self.candidates, 0)
        self.assertIsNone(result["reservation"])
        self.assertEqual(result["placement"]["locations"], [])
        self.assertEqual(self.read(), self.data)

    def test_foreign_occupied_blocked_missing_and_coordinate_changed_targets_are_skipped(self):
        reservation = self.reserve(2)
        self.grid.complete(reservation, 2)
        value = self.read()
        value["locations"][2]["reservation_id"] = "another-batch"
        value["locations"][3]["occupied"] = True
        value["locations"][4]["vision_blocked"] = True
        value["locations"][5]["x"] += 1
        value["locations"].append(location(7))
        self.write(value)
        result = self.grid.replan(reservation, self.candidates, 8, [2])
        self.assertEqual(result["placement"]["locations"], [])
        self.assertEqual(self.read(), value)
        value["locations"].pop(2)
        self.write(value)
        result = self.grid.replan(reservation, self.candidates, 8, [2])
        self.assertEqual(result["placement"]["locations"], [])
        self.assertEqual(self.read(), value)

    def test_released_changed_slot_keeps_new_geometry_and_metadata_without_becoming_a_target(self):
        reservation = self.reserve(3)
        self.grid.complete(reservation, 2)
        value = self.read()
        value["description"] = "operator update"
        value["locations"][2].update(x=999, operator_note={"text": "edited location"})
        value["locations"][3]["operator_note"] = {"text": "metadata only"}
        self.write(value)
        result = self.grid.replan(reservation, self.candidates, 1, [2])
        self.assertEqual([item["id"] for item in result["placement"]["locations"]], [4])
        current = self.read()
        self.assertEqual(current["description"], value["description"])
        self.assertEqual(current["locations"][2],
                         location(3, x=999, label="cell 3", operator_note={"text": "edited location"}))
        self.assertEqual(result["placement"]["locations"][0]["operator_note"],
                         {"text": "metadata only"})
        self.grid.complete(result["reservation"], 4)
        self.assertEqual(self.read()["locations"][3]["operator_note"], {"text": "metadata only"})

    def test_same_token_outside_the_supplied_reservation_is_never_released(self):
        reservation = self.reserve(1)
        value = self.read()
        value["locations"][5].update(occupied=True, reserved=True,
                                     reservation_id=reservation["reservation_id"])
        self.write(value)
        self.grid.replan(reservation, self.candidates, 0)
        self.assertEqual(self.read()["locations"][5], value["locations"][5])

    def test_failed_atomic_write_does_not_publish_partial_release_or_reservation(self):
        reservation = self.reserve(3)
        before = self.path.read_bytes()
        with patch("placing_grid.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(SelectionError, "disk full"):
                self.grid.replan(reservation, self.candidates, 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_inputs_do_not_write(self):
        reservation = self.reserve(2)
        before = self.path.read_bytes()
        cases = [({}, self.candidates, 1, []),
                 (reservation, None, 1, []),
                 (reservation, self.candidates * 2, 1, []),
                 (reservation, [dict(self.candidates[0], occupied=True)], 1, []),
                 (reservation, [dict(self.candidates[0], vision_blocked=True)], 1, [])]
        cases += [(reservation, self.candidates, count, [])
                  for count in (-1, True, 1.0, "1", None)]
        cases += [(reservation, self.candidates, 1, ids)
                  for ids in ([2, 2], [True], [2.0], [4], "2", None)]
        for candidate, locations, count, completed in cases:
            with self.subTest(count=count, completed=completed, locations=locations):
                with self.assertRaises(SelectionError):
                    self.grid.replan(candidate, locations, count, completed)
                self.assertEqual(self.path.read_bytes(), before)

    def test_concurrent_batches_never_take_or_release_each_others_cells(self):
        first = self.reserve(1)
        second = self.reserve(1)
        self.grid.complete(first, 2)
        self.grid.complete(second, 3)

        def replan(arguments):
            reservation, completed = arguments
            return PlacingGrid(self.path).replan(reservation, self.candidates, 3, [completed])

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(replan, [(first, 2), (second, 3)]))
        pending = [result["placement"]["locations"] for result in results]
        self.assertEqual(sorted(len(items) for items in pending), [0, 3])
        self.assertEqual({item["id"] for items in pending for item in items}, {4, 5, 6})
        for result in results:
            ids = [item["id"] for item in result["placement"]["locations"]]
            if ids:
                self.grid.validate_reservation(result["reservation"], ids)


if __name__ == "__main__":
    unittest.main()
