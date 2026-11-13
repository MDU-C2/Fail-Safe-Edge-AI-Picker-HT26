"""Depth bridges between separately identified cups, using synthetic geometry."""

import copy
import unittest
from unittest.mock import patch

import numpy as np

from placement_depth_regions import assign_depth_regions
from placement_depth_split import split_component
from test_placement_cups import PlacementCupFixture


class DepthSplitHelperTests(unittest.TestCase):
    @staticmethod
    def bridge():
        component = np.zeros((48, 96), dtype=bool)
        component[4:44, 4:92] = True
        first, second = np.zeros_like(component), np.zeros_like(component)
        first[4:44, 4:40] = True
        second[4:44, 56:92] = True
        return component, [first, second]

    def test_bridge_splits_into_anchored_disjoint_parts_and_uncertain_seam(self):
        component, seeds = self.bridge()
        before = component.copy(), [seed.copy() for seed in seeds]
        parts, seam = split_component(component, seeds)
        self.assertEqual(len(parts), 2)
        self.assertTrue(np.all(parts[0][seeds[0]]))
        self.assertTrue(np.all(parts[1][seeds[1]]))
        self.assertFalse((parts[0] & parts[1]).any())
        self.assertTrue(seam[20, 47])
        self.assertTrue(seam[20, 48])
        self.assertFalse((seam & (seeds[0] | seeds[1])).any())
        np.testing.assert_array_equal(parts[0] | parts[1] | seam, component)
        np.testing.assert_array_equal(component, before[0])
        for seed, original in zip(seeds, before[1]):
            np.testing.assert_array_equal(seed, original)

    def test_seed_order_changes_only_partition_order(self):
        component, seeds = self.bridge()
        forward, first_seam = split_component(component, seeds)
        reverse, second_seam = split_component(component, list(reversed(seeds)))
        np.testing.assert_array_equal(forward[0], reverse[1])
        np.testing.assert_array_equal(forward[1], reverse[0])
        np.testing.assert_array_equal(first_seam, second_seam)

    def test_overlapping_seed_pixels_remain_conflicts_without_losing_supported_parts(self):
        component, seeds = self.bridge()
        seeds[1][20, 30] = True
        originals = [seed.copy() for seed in seeds]
        parts, seam = split_component(component, seeds)
        overlap = seeds[0] & seeds[1]
        self.assertTrue(np.all(seam[overlap]))
        self.assertFalse((parts[0] | parts[1])[overlap].any())
        for index in (0, 1):
            self.assertTrue(np.all(parts[index][seeds[index] & ~overlap]))
            np.testing.assert_array_equal(seeds[index], originals[index])
        np.testing.assert_array_equal(parts[0] | parts[1] | seam, component)
        reverse, reversed_seam = split_component(component, list(reversed(seeds)))
        np.testing.assert_array_equal(parts[0], reverse[1])
        np.testing.assert_array_equal(parts[1], reverse[0])
        np.testing.assert_array_equal(seam, reversed_seam)

    def test_shared_pixels_cannot_supply_the_minimum_exclusive_seed_count(self):
        component, seeds = self.bridge()
        seeds[1] = seeds[0].copy()
        seeds[1][20, 60:91] = True  # Only 31 exclusive samples for cup two.
        with patch("placement_depth_split.cv2.distanceTransform") as distance:
            self.assertIsNone(split_component(component, seeds))
        distance.assert_not_called()

    def test_overlap_does_not_rescue_an_under_supported_partition(self):
        component, seeds = self.bridge()
        seeds[1][:] = False
        seeds[1][20:28, 76:84] = True
        seeds[1][4:44, 4:30] = True  # These belong to both cups, not cup two.
        self.assertIsNone(split_component(component, seeds))

    def test_shared_seed_strip_cannot_anchor_an_island_across_conflicted_pixels(self):
        component = np.zeros((24, 40), dtype=bool)
        component[4:20, 4:36] = True
        first, second = np.zeros_like(component), np.zeros_like(component)
        first[4:20, 4:20] = True
        second[4:20, 18:36] = True
        parts, seam = split_component(component, [first, second])
        self.assertTrue(np.all(seam[4:20, 18:20]))
        self.assertFalse(parts[0][:, 18:].any())
        self.assertFalse(parts[1][:, :20].any())

    def test_one_partition_with_insufficient_seed_coverage_rejects_all(self):
        component, seeds = self.bridge()
        seeds[1][:] = False
        seeds[1][20:28, 76:84] = True  #64 real seeds, but a much larger partition.
        self.assertIsNone(split_component(component, seeds))

    def test_adjacent_exact_seeds_are_not_erased_by_the_uncertainty_margin(self):
        component = np.zeros((16, 24), dtype=bool)
        component[4:12, 4:20] = True
        first, second = np.zeros_like(component), np.zeros_like(component)
        first[4:12, 4:12] = True
        second[4:12, 12:20] = True
        parts, seam = split_component(component, [first, second])
        np.testing.assert_array_equal(parts[0], first)
        np.testing.assert_array_equal(parts[1], second)
        self.assertFalse(seam.any())

    def test_nearest_mask_island_without_a_path_to_its_own_seed_is_not_claimed(self):
        # The top-left lobe is nearer the left seed in straight-line distance,
        # but the observed surface reaches it only by going through the right
        # cup's region. It must not receive ownership across the empty gap.
        component = np.zeros((64, 72), dtype=bool)
        first, second = np.zeros_like(component), np.zeros_like(component)
        first[30:50, 2:22] = True
        second[30:50, 42:62] = True
        component |= first | second
        component[38:42, 22:42] = True
        component[10:14, 2:62] = True
        component[14:30, 58:62] = True
        component[14:20, 2:12] = True
        parts, seam = split_component(component, [first, second])
        self.assertFalse(parts[0][10:20, 2:12].any())
        self.assertFalse(parts[1][10:20, 2:12].any())
        self.assertTrue(np.all(seam[10:20, 2:12]))
        self.assertTrue(np.all(parts[0][first]))
        self.assertTrue(np.all(parts[1][second]))

    def test_resource_limit_rejects_before_distance_maps_are_allocated(self):
        component, seeds = self.bridge()
        with patch("placement_depth_split.MAX_COMPONENT_PIXELS", component.size - 1), \
                patch("placement_depth_split.cv2.distanceTransform") as distance:
            self.assertIsNone(split_component(component, seeds))
        distance.assert_not_called()


class DepthSplitIntegrationTests(PlacementCupFixture):
    def bridge(self, *, y0=224, y1=256):
        first = self.mask(310, 344, y0, y1)
        second = self.mask(354, 388, y0, y1)
        connection = self.mask(344, 354, y0, y1)
        self.depth[first | second | connection] = 450.
        return first, second, connection

    def baseline(self, objects, exact):
        owners, _, assignments = self.assign(objects)
        # Isolate new attribution from the independently tested legacy fringe.
        owners[~exact] = 0
        return owners, assignments

    def augment(self, objects, baseline):
        owners, assignments = baseline
        prior_owners, prior_assignments = owners.copy(), copy.deepcopy(assignments)
        prior_objects = copy.deepcopy(objects)
        result = assign_depth_regions(
            self.depth, objects, self.locations, owners, assignments,
            transform=self.vision.transform, translation=self.vision.translation,
            intrinsics=self.K, cell_size=self.vision.cell_size, table_z=self.vision.table_z)
        np.testing.assert_array_equal(owners, prior_owners)
        self.assertEqual(assignments, prior_assignments)
        self.assertEqual(objects, prior_objects)
        return result

    def assert_fallback(self, result, baseline):
        np.testing.assert_array_equal(result[0], baseline[0])
        self.assertEqual(result[1], baseline[1])

    def test_neighbouring_cups_claim_their_own_side_of_connected_depth(self):
        first, second, connection = self.bridge()
        objects = [self.obj(first, detection_id=20), self.obj(second, detection_id=21)]
        before = self.baseline(objects, first | second)
        self.assertEqual([a["location_id"] for a in before[1]], [11, 3])
        owners, assignments, _ = self.augment(objects, before)
        self.assertTrue(np.all(owners[first] == 1))
        self.assertTrue(np.all(owners[second] == 2))
        self.assertEqual(owners[240, 345], 1)
        self.assertEqual(owners[240, 352], 2)
        self.assertEqual(owners[240, 348], -1)
        self.assertFalse(owners[~(first | second | connection)].any())
        self.assertEqual(len(assignments), 2)
        self.assertTrue(all(a.get("depth_assignment_method") == "connected_depth_area"
                            for a in assignments))

    def test_accepted_split_seam_revokes_old_fringe_but_preserves_exact_and_conflicts(self):
        first, second, _ = self.bridge()
        objects = [self.obj(first), self.obj(second, detection_id=21)]
        before = self.baseline(objects, first | second)
        before[0][240, 348] = 1
        before[0][240, 349] = 2
        before[0][240, 345] = -1
        owners, _, _ = self.augment(objects, before)
        self.assertEqual(owners[240, 348], -1)
        self.assertEqual(owners[240, 349], -1)
        self.assertEqual(owners[240, 345], -1)
        self.assertTrue(np.all(owners[first] == 1))
        self.assertTrue(np.all(owners[second] == 2))

    def test_two_cups_sharing_one_cell_keep_the_original_fallback(self):
        first, second = self.mask(300, 316), self.mask(330, 342)
        connection = self.mask(316, 330)
        self.depth[first | second | connection] = 450.
        objects = [self.obj(first), self.obj(second, detection_id=21)]
        before = self.baseline(objects, first | second)
        self.assertEqual([a["location_id"] for a in before[1]], [11, 11])
        self.assert_fallback(self.augment(objects, before), before)

    def test_overlap_remains_unresolved_while_exclusive_cup_evidence_is_retained(self):
        first, second = self.mask(330, 358), self.mask(342, 370)
        self.depth[first | second] = 450.
        objects = [self.obj(first), self.obj(second, detection_id=21)]
        before = self.baseline(objects, first | second)
        self.assertTrue(np.all(before[0][first & second] == -1))
        owners, assignments, shared = self.augment(objects, before)
        self.assertTrue(np.all(owners[first & second] == -1))
        self.assertTrue(np.all(owners[first & ~second] == 1))
        self.assertTrue(np.all(owners[second & ~first] == 2))
        self.assertTrue(all(a.get("depth_assignment_method") == "connected_depth_area"
                            for a in assignments))
        self.assertEqual(shared, [])

    def test_tiny_mask_overlap_does_not_discard_supported_depth_or_claim_separate_obstacle(self):
        first, second, connection = self.bridge()
        second[240, 343] = True  # One ambiguous exact pixel inside cup one.
        obstacle = self.mask(350, 356, 270, 276)
        self.depth[obstacle] = 450.
        objects = [self.obj(first), self.obj(second, detection_id=21)]
        before = self.baseline(objects, first | second)
        owners, assignments, _ = self.augment(objects, before)
        self.assertEqual(owners[240, 343], -1)
        self.assertEqual(owners[240, 345], 1)
        self.assertEqual(owners[240, 352], 2)
        self.assertFalse(owners[obstacle].any())
        self.assertTrue(all(a.get("depth_assignment_method") == "connected_depth_area"
                            for a in assignments))

    def test_third_label_on_bridge_vetoes_the_entire_split_for_mask_or_box(self):
        first, second, _ = self.bridge()
        obstacle = self.mask(347, 351, 234, 238)
        for with_mask, overlap in ((True, False), (False, False), (True, True), (False, True)):
            with self.subTest(with_mask=with_mask, overlap=overlap):
                second[240, 343] = overlap
                other = self.obj(obstacle, kind="BLOCK", detection_id=22)
                if not with_mask:
                    other.pop("segmentation")
                objects = [self.obj(first), self.obj(second, detection_id=21), other]
                before = self.baseline(objects, first | second)
                self.assert_fallback(self.augment(objects, before), before)

    def test_one_unsupported_partition_rejects_all_new_ownership_and_seam_changes(self):
        first, second = self.mask(310, 344), self.mask(354, 360)
        whole = self.mask(310, 398)
        self.depth[whole] = 450.
        objects = [self.obj(first), self.obj(second, detection_id=21)]
        before = self.baseline(objects, first | second)
        before[0][240, 348] = 1  # Rejection must not revoke existing fringe.
        self.assert_fallback(self.augment(objects, before), before)

    def test_detection_order_does_not_change_the_split_or_cell_ownership(self):
        first, second, _ = self.bridge()
        objects = [self.obj(first), self.obj(second, detection_id=21)]
        forward = self.augment(objects, self.baseline(objects, first | second))
        reverse_objects = list(reversed(objects))
        reverse = self.augment(reverse_objects, self.baseline(reverse_objects, first | second))
        np.testing.assert_array_equal(forward[0], reverse[0])
        for records in (forward[1], reverse[1]):
            self.assertTrue(all("depth_region_pixels" in record for record in records))
        evidence = lambda records: {record["location_id"]: (
            record["depth_area_mm2"], record["depth_region_pixels"]) for record in records}
        self.assertEqual(evidence(forward[1]), evidence(reverse[1]))

    def test_late_pooled_rejection_cancels_split_but_can_keep_explicit_group_support(self):
        first, second, connection = self.bridge(y0=228, y1=240)
        other_seed = self.mask(360, 378, 252, 266)
        other_halo = self.mask(378, 396, 252, 266)
        self.depth[other_seed | other_halo] = 450.
        objects = [self.obj(first | other_seed), self.obj(second, detection_id=21)]
        before = self.baseline(objects, first | second | other_seed)
        self.assertEqual([a["location_id"] for a in before[1]], [11, 3])
        # Cup11's exact seeds still favour11, but adding its separate depth
        # region makes its pooled region favour3. Neither cup may retain a
        # partial shared split or apply a seam after that final rejection.
        before[0][234, 348] = 1
        result = self.augment(objects, before)
        np.testing.assert_array_equal(result[0], before[0])
        self.assertTrue(all("depth_assignment_method" not in r for r in result[1]))
        # The newly accepted group tolerance is independent of the rejected
        # individual split. It never chooses one cup for the bridge or claims
        # the separate region whose physical owner was rejected.
        self.assertEqual(len(result[2]), 1)
        possible_owners, coordinates = result[2][0]
        self.assertEqual(possible_owners, frozenset({1, 2}))
        shared = np.zeros(self.depth.shape, dtype=bool)
        shared[coordinates] = True
        self.assertTrue(np.all(connection[shared]))
        self.assertFalse(shared[234, 348])  # Existing fringe remains untouched.
        self.assertTrue(shared[234, 352])
        self.assertTrue(all(r["depth_shared_groups"][0]["location_ids"] == [11, 3] for r in result[1]))
        self.assertFalse(result[0][other_halo].any())
        self.assertEqual(result[0][234, 352], 0)


if __name__ == "__main__":
    unittest.main()
