"""Cup counts describe observations; they never cap independent obstacle checks."""

import copy
import unittest

from placement_vision import PlacementVision
from test_placement_cups import PlacementCupFixture


class PlacementCupSummaryTests(PlacementCupFixture):
    def combine(self, *frames):
        if len(frames) == 1:
            frames = frames * 3
        return PlacementVision.combine({"locations": self.locations}, list(frames), [10, 11, 12])

    def cup_frame(self, *, right=False):
        mask = self.mask(365, 381) if right else self.mask()
        depth = self.depth.copy()
        depth[mask] = 450.
        return self.vision.assess(depth, [self.obj(mask)], self.locations)

    def test_cup_parts_are_counted_once_and_frames_are_not_added_together(self):
        mask = self.mask()
        self.depth[mask] = 450.
        objects = [self.obj(mask), self.obj(mask, kind="CUP BASE", detection_id=21, is_cup=True)]
        slots = self.vision.assess(self.depth, objects, self.locations)
        result = self.combine(slots)
        self.assertEqual(result["cup_summary"], {
            "assigned_count": 1, "location_ids": [11],
            "min_assigned_count": 1, "max_assigned_count": 1, "stable_cell_counts": True})
        self.assertEqual([slot["cup_count"] for slot in result["slots"]], [1, 0])
        self.assertEqual([slot["state"] for slot in result["slots"]], ["occupied", "clear"])

    def test_two_cups_in_one_cell_count_twice_without_blocking_another_cell(self):
        first, second = self.mask(310, 322, 222, 232), self.mask(310, 322, 249, 259)
        self.depth[first | second] = 450.
        objects = [self.obj(first), self.obj(second, detection_id=21)]
        result = self.combine(self.vision.assess(self.depth, objects, self.locations))
        self.assertEqual(result["cup_summary"]["assigned_count"], 2)
        self.assertEqual(result["cup_summary"]["location_ids"], [11])
        self.assertEqual([slot["state"] for slot in result["slots"]], ["occupied", "clear"])

    def test_unlabelled_obstacle_is_preserved_even_when_cup_count_is_one(self):
        mask = self.mask()
        self.depth[mask] = 450.
        self.depth[234:242, 370:378] = 450.
        result = self.combine(self.vision.assess(self.depth, [self.obj(mask)], self.locations))
        self.assertEqual(result["cup_summary"]["assigned_count"], 1)
        self.assertEqual(result["cup_summary"]["location_ids"], [11])
        neighbour = result["slots"][1]
        self.assertEqual(neighbour["cup_count"], 0)
        self.assertEqual(neighbour["state"], "occupied")
        self.assertEqual(neighbour["evidence"], ["raised_depth"])
        self.assertIn("without an assigned cup", neighbour["reason"])

    def test_detected_noncup_has_its_own_evidence_without_a_cup_count(self):
        obj = {"class": "BOX", "bbox_xyxy": [310, 230, 320, 240]}
        result = self.combine(self.vision.assess(self.depth, [obj], self.locations))
        self.assertEqual(result["cup_summary"]["assigned_count"], 0)
        self.assertEqual(result["slots"][0]["state"], "occupied")
        self.assertEqual(result["slots"][0]["evidence"], ["detected_object"])

    def test_missing_last_frame_cup_does_not_remove_earlier_occupied_evidence(self):
        cup = self.cup_frame()
        clear = self.vision.assess(self.depth, [], self.locations)
        before = copy.deepcopy([cup, clear])
        result = self.combine(cup, clear, clear)
        self.assertEqual(result["cup_summary"], {
            "assigned_count": 0, "location_ids": [], "min_assigned_count": 0,
            "max_assigned_count": 1, "stable_cell_counts": False})
        self.assertEqual(result["slots"][0]["state"], "occupied")
        self.assertEqual(result["slots"][0]["cup_count"], 0)
        self.assertIn("cup", result["slots"][0]["evidence"])
        self.assertEqual([cup, clear], before)

    def test_same_total_with_changed_cells_is_not_reported_stable(self):
        left, right = self.cup_frame(), self.cup_frame(right=True)
        result = self.combine(left, right, right)
        self.assertEqual(result["cup_summary"], {
            "assigned_count": 1, "location_ids": [3], "min_assigned_count": 1,
            "max_assigned_count": 1, "stable_cell_counts": False})
        self.assertEqual([slot["state"] for slot in result["slots"]], ["occupied", "occupied"])

    def test_legacy_observation_does_not_invent_zero_cups(self):
        slots = self.vision.assess(self.depth, [], self.locations)
        for slot in slots:
            for field in ("assigned_cups", "cup_count", "evidence"):
                slot.pop(field)
        self.assertNotIn("cup_summary", self.combine(slots))


if __name__ == "__main__":
    unittest.main()
