"""Cup-local uncertainty must clear fragments without clearing destinations blindly."""

import copy
from unittest.mock import patch

import numpy as np

from test_placement_cups import PlacementCupFixture


class CupUncertaintyIntegrationTests(PlacementCupFixture):
    def scene(self):
        cup = self.mask()
        self.depth[cup] = 450.  # 100 mm cup; its physical majority remains in P11.
        return [self.obj(cup)]

    def test_detached_nearby_depth_fragment_is_uncertainty_not_a_second_cup(self):
        objects = self.scene()
        original = copy.deepcopy(objects)
        fragment = self.mask(360, 371)
        self.depth[fragment] = 450.
        with patch("placement_vision.cup_uncertainty_regions", return_value=([], [])):
            self.assertEqual(self.states(objects), ["occupied", "occupied"])
        slots = self.vision.assess(self.depth, objects, self.locations)
        self.assertEqual([slot["state"] for slot in slots], ["occupied", "clear"])
        self.assertEqual([slot["cup_count"] for slot in slots], [1, 0])
        self.assertGreater(slots[1]["cup_uncertain_excluded_pixels"], 0)
        self.assertLess(slots[1]["floor_visible_fraction"], 1)
        self.assertIn("depth_uncertainty", slots[0]["assigned_cups"][0])
        self.assertEqual(objects, original)

    def test_labelled_object_in_the_same_fragment_still_blocks(self):
        objects = self.scene()
        fragment = self.mask(360, 371)
        self.depth[fragment] = 450.
        objects.append(self.obj(fragment, kind="OTHER OBJECT", detection_id=21))
        slots = self.vision.assess(self.depth, objects, self.locations)
        self.assertEqual(slots[1]["state"], "occupied")
        self.assertIn("detected_object", slots[1]["evidence"])

    def test_larger_unlabelled_object_retains_a_blocking_core_beyond_cup_margin(self):
        objects = self.scene()
        self.depth[self.mask(360, 402, 228, 252)] = 450.
        slots = self.vision.assess(self.depth, objects, self.locations)
        self.assertEqual(slots[1]["state"], "occupied")
        self.assertIn("raised_depth", slots[1]["evidence"])
        self.assertGreater(slots[1]["raised_depth_pixels"], 8)

    def test_missing_depth_and_tall_occluder_near_a_cup_are_not_exempted(self):
        objects = self.scene()
        fragment = self.mask(360, 365)
        for optical, expected in ((0., "unknown"), (375., "occupied")):
            with self.subTest(optical=optical):
                self.depth[fragment] = optical
                slots = self.vision.assess(self.depth, objects, self.locations)
                self.assertEqual(slots[1]["state"], expected)

    def test_exempted_samples_reduce_visibility_and_cannot_prove_hidden_table_clear(self):
        objects = self.scene()
        # Exercise the consumer's independent visibility gate even if a future
        # attribution helper proposes too much uncertainty beside a known cup.
        rows, columns = np.indices(self.depth.shape)
        regions = [(frozenset({1}), (rows.ravel(), columns.ravel()))]
        with patch("placement_vision.cup_uncertainty_regions", return_value=(regions, [])):
            slots = self.vision.assess(self.depth, objects, self.locations)
        self.assertEqual(slots[0]["state"], "occupied")
        self.assertEqual(slots[1]["state"], "unknown")
        self.assertEqual(slots[1]["reason_code"], "occluded")
        self.assertEqual(slots[1]["floor_visible_fraction"], 0)
