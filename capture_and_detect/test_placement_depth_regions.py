"""Connected cup-depth attribution, including its accepted touching-object limit."""

import copy
import unittest
from unittest.mock import patch

import numpy as np

from placement_depth_regions import assign_depth_regions, shared_boundaries
from test_placement_cups import PlacementCupFixture


class PlacementDepthRegionTests(PlacementCupFixture):
    def apply_regions(self, objects, *, depth=None, locations=None, baseline=None):
        depth = self.depth if depth is None else depth
        locations = self.locations if locations is None else locations
        if baseline is None:
            owners, _, assignments = self.assign(objects, depth=depth, locations=locations)
        else:
            owners, assignments = baseline
        original_owners, original_assignments = owners.copy(), copy.deepcopy(assignments)
        original_objects = copy.deepcopy(objects)
        result = assign_depth_regions(
            depth, objects, locations, owners, assignments,
            transform=self.vision.transform, translation=self.vision.translation,
            intrinsics=self.K, cell_size=self.vision.cell_size,
            table_z=self.vision.table_z)
        np.testing.assert_array_equal(owners, original_owners)
        self.assertEqual(assignments, original_assignments)
        self.assertEqual(objects, original_objects)
        self.assertEqual(len(result), 3)
        self.assertIsInstance(result[2], list)
        return original_owners, original_assignments, result[0], result[1]

    def broad_cup(self):
        mask = self.mask(310, 350, 224, 256)
        halo = self.mask(350, 366, 224, 256)
        self.depth[mask | halo] = 450.
        return mask, halo, [self.obj(mask)]

    def assert_fallback(self, before, previous, after, assignments):
        np.testing.assert_array_equal(after, before)
        self.assertEqual(assignments, previous)

    def test_broad_halo_uses_depth_region_instead_of_old_25_percent_budget(self):
        mask, halo, objects = self.broad_cup()
        before, previous, after, assignments = self.apply_regions(objects)
        self.assertGreater(halo.sum(), mask.sum() * .25)
        self.assertTrue((before[halo] == 0).any())
        self.assertTrue(np.all(after[mask | halo] == 1))
        self.assertFalse(after[~(mask | halo)].any())
        self.assertEqual(after.dtype, np.int32)
        self.assertEqual(assignments[0]["location_id"], 11)
        self.assertEqual(assignments[0]["depth_assignment_method"], "connected_depth_area")
        self.assertEqual(assignments[0]["depth_region_pixels"], int((mask | halo).sum()))
        self.assertEqual(assignments[0]["depth_region_extra_pixels"],
                         int(np.count_nonzero((before == 0) & (after == 1))))
        # The new evidence supplements the independently accepted segmentation.
        for key in ("object_indices", "detection_ids", "overlap_fraction", "center_mm", "mask_pixels"):
            self.assertEqual(assignments[0][key], previous[0][key])

    def test_area_counts_unique_two_mm_xy_bins_not_depth_pixels(self):
        mask, halo, objects = self.broad_cup()
        _, _, _, assignments = self.apply_regions(objects)
        ys, xs = np.nonzero(mask | halo)
        rays = np.column_stack(((xs - self.K["cx"]) / self.K["fx"],
                                (ys - self.K["cy"]) / self.K["fy"], np.ones(len(xs))))
        physical = (rays @ self.vision.transform.T) * 450. + self.vision.translation
        bins = np.unique(np.floor(physical[:, :2] / 2.).astype(np.int64), axis=0)
        centers = (bins + .5) * 2.
        expected = [int(np.count_nonzero(
            (np.abs(centers[:, 0] - place["x"]) <= 40.)
            & (np.abs(centers[:, 1] - place["y"]) <= 40.))) * 4
            for place in self.locations]
        record = assignments[0]
        self.assertEqual(record["depth_area_mm2"], len(bins) * 4)
        self.assertNotEqual(record["depth_area_mm2"], len(xs) * 4)
        self.assertEqual(record["depth_area_by_location_mm2"], [
            {"location_id": place["id"], "area_mm2": area}
            for place, area in zip(self.locations, expected)])
        self.assertAlmostEqual(record["depth_area_fraction"], expected[0] / (len(bins) * 4), places=4)

    def test_separate_unlabelled_raised_obstacle_keeps_no_owner(self):
        mask, halo, objects = self.broad_cup()
        obstacle = self.mask(376, 388, 234, 246)
        self.depth[obstacle] = 450.
        _, _, after, assignments = self.apply_regions(objects)
        self.assertTrue(np.all(after[mask | halo] == 1))
        self.assertFalse(after[obstacle].any())
        self.assertEqual(assignments[0]["depth_region_pixels"], int((mask | halo).sum()))

    def test_touching_unlabelled_object_is_the_explicitly_accepted_limitation(self):
        mask = self.mask(310, 350, 224, 256)
        bar = self.mask(350, 376, 240, 248)
        self.depth[mask | bar] = 450.
        _, _, after, assignments = self.apply_regions([self.obj(mask)])
        # Geometry alone cannot distinguish this attached bar from cup depth.
        # This test documents the user's accepted behavior, not object safety.
        self.assertTrue(np.all(after[bar] == 1))
        self.assertEqual(assignments[0]["location_id"], 11)
        self.assertEqual(assignments[0]["depth_region_pixels"], int((mask | bar).sum()))

    def test_detecting_that_touching_object_protects_the_entire_region(self):
        mask = self.mask(310, 350, 224, 256)
        bar = self.mask(350, 376, 240, 248)
        self.depth[mask | bar] = 450.
        for with_mask in (True, False):
            with self.subTest(with_mask=with_mask):
                obstacle = self.obj(bar, kind="BLOCK", confidence=.1, detection_id=21)
                if not with_mask:
                    obstacle.pop("segmentation")
                result = self.apply_regions([self.obj(mask), obstacle])
                self.assert_fallback(*result)
                self.assertFalse(result[2][bar].any())

    def test_two_cups_in_one_cell_do_not_jointly_claim_a_connected_bridge(self):
        first = self.mask(300, 316, 230, 250)
        second = self.mask(330, 342, 230, 250)
        bridge = self.mask(316, 330, 230, 250)
        self.depth[first | second | bridge] = 450.
        objects = [self.obj(first), self.obj(second, detection_id=21)]
        owners, _, assignments = self.assign(objects)
        self.assertEqual([a["location_id"] for a in assignments], [11, 11])
        owners[~(first | second)] = 0
        result = self.apply_regions(objects, baseline=(owners, assignments))
        self.assert_fallback(*result)
        self.assertFalse(result[2][bridge].any())

    def test_disconnected_components_of_one_cup_share_one_pooled_area_winner(self):
        first = self.mask(310, 340, 218, 230)
        first_halo = self.mask(340, 350, 218, 230)
        second = self.mask(350, 366, 244, 252)
        second_halo = self.mask(366, 370, 244, 252)
        exact = first | second
        region = exact | first_halo | second_halo
        self.depth[region] = 450.
        objects = [self.obj(exact)]
        owners, _, assignments = self.assign(objects)
        self.assertEqual(assignments[0]["location_id"], 11)
        owners[~exact] = 0
        _, _, after, updated = self.apply_regions(objects, baseline=(owners, assignments))
        # The smaller second component lies entirely in cell3. It belongs to
        # the same identified cup, whose pooled footprint mostly occupies11.
        self.assertTrue(np.all(after[second_halo] == 1))
        self.assertTrue(np.all(after[region] == 1))
        self.assertEqual(updated[0]["depth_region_pixels"], int(region.sum()))
        self.assertGreater(updated[0]["depth_area_fraction"], .5)

    def test_existing_ownership_cannot_substitute_for_exact_segmentation_seeds(self):
        exact = self.mask(310, 340, 218, 230)
        obstacle = self.mask(354, 370, 236, 248)
        self.depth[obstacle] = 450.
        objects = [self.obj(exact)]  # Exact cup pixels remain at table depth.
        owners, _, assignments = self.assign(objects)
        owners[236:248, 354:362] = 1
        result = self.apply_regions(objects, baseline=(owners, assignments))
        self.assert_fallback(*result)
        self.assertFalse(result[2][236:248, 362:370].any())

    def test_exact_seed_must_supply_at_least_half_the_whole_component(self):
        mask = self.mask(310, 342)
        halo = self.mask(342, 374)
        self.depth[mask | halo] = 450.
        objects = [self.obj(mask)]
        _, _, after, assignments = self.apply_regions(objects)
        self.assertTrue(np.all(after[halo] == 1))
        self.assertIn("depth_region_pixels", assignments[0])
        self.depth[240, 374] = 450.  # One extra pixel makes support less than 50%.
        result = self.apply_regions(objects)
        self.assert_fallback(*result)
        self.assertEqual(result[2][240, 374], 0)

    def test_each_component_needs_32_raised_exact_seed_pixels(self):
        mask = self.mask(310, 320, 230, 238)
        halo = self.mask(320, 324, 230, 234)
        for seed_count in (31, 32):
            with self.subTest(seed_count=seed_count):
                depth = self.depth.copy()
                depth.flat[np.flatnonzero(mask)[:seed_count]] = 450.
                depth[halo] = 450.
                result = self.apply_regions([self.obj(mask)], depth=depth)
                if seed_count == 31:
                    self.assert_fallback(*result)
                else:
                    self.assertTrue(np.all(result[2][halo] == 1))
                    self.assertIn("depth_region_pixels", result[3][0])

    def test_component_majority_must_agree_with_exact_cup_owner(self):
        mask, halo = self.mask(330, 350), self.mask(350, 370)
        self.depth[mask | halo] = 450.
        result = self.apply_regions([self.obj(mask)])
        self.assertEqual(result[1][0]["location_id"], 11)
        self.assert_fallback(*result)

    def test_equal_projected_area_winners_do_not_grant_a_region(self):
        mask, halo = self.mask(340, 348), self.mask(348, 354)
        self.depth[mask | halo] = 450.
        result = self.apply_regions([self.obj(mask)])
        self.assertEqual(result[1][0]["location_id"], 11)
        self.assert_fallback(*result)

    def test_region_below_half_total_area_can_keep_its_unique_matching_winner(self):
        mask, halo = self.mask(330, 358), self.mask(358, 378)
        self.depth[mask | halo] = 450.
        _, previous, after, assignments = self.apply_regions(
            [self.obj(mask)], locations=self.locations[:1])
        self.assertEqual(previous[0]["location_id"], 11)
        self.assertTrue(np.all(after[halo] == 1))
        self.assertGreater(assignments[0]["depth_seed_area_fraction"], .5)
        self.assertLess(assignments[0]["depth_area_fraction"], .5)
        self.assertGreater(assignments[0]["depth_area_mm2"],
                           assignments[0]["depth_area_by_location_mm2"][0]["area_mm2"])

    def test_cup_seed_majority_accepts_region_spilling_outside_grid_and_into_neighbour(self):
        # Like the saved P4 case, exact cup depth has a strong cell majority;
        # the attached region spreads beyond the grid and slightly into a
        # neighbour, leaving the correct cell below 50% of the whole region.
        mask = self.mask(310, 350, 248, 270)
        halo = self.mask(310, 350, 270, 289)
        self.depth[mask | halo] = 450.
        _, previous, after, assignments = self.apply_regions([self.obj(mask)])
        self.assertEqual(previous[0]["location_id"], 11)
        record = assignments[0]
        first, neighbour = [entry["area_mm2"] for entry in record["depth_area_by_location_mm2"]]
        self.assertGreater(record["depth_seed_area_fraction"], .75)
        self.assertLess(record["depth_area_fraction"], .5)
        self.assertGreater(first, neighbour)
        self.assertGreater(neighbour, 0)
        self.assertGreater(record["depth_area_mm2"], first + neighbour)
        self.assertTrue(np.all(after[mask | halo] == 1))

    def test_raised_seed_mostly_outside_grid_cannot_borrow_flat_mask_support(self):
        flat = self.mask(306, 330, 220, 240)
        inside = self.mask(330, 342, 244, 252)
        outside = self.mask(420, 440, 244, 252)
        halo = self.mask(342, 346, 244, 252)
        self.depth[inside | outside | halo] = 450.
        result = self.apply_regions([self.obj(flat | inside | outside)])
        # Ordinary assignment includes the plentiful plausible table-height
        # mask pixels. Region attribution must independently check raised seeds.
        self.assertEqual(result[1][0]["location_id"], 11)
        self.assertGreater(result[1][0]["overlap_fraction"], .75)
        self.assert_fallback(*result)

    def test_raised_seed_tie_stays_unresolved_even_when_region_favours_old_owner(self):
        flat = self.mask(306, 330, 220, 240)
        first = self.mask(330, 342, 244, 252)
        second = self.mask(354, 366, 244, 252)
        halo = self.mask(326, 330, 244, 252)
        self.depth[first | second | halo] = 450.
        result = self.apply_regions([self.obj(flat | first | second)])
        self.assertEqual(result[1][0]["location_id"], 11)
        self.assert_fallback(*result)

    def test_raised_seed_disagreement_is_not_overridden_by_region_plurality(self):
        flat = self.mask(306, 330, 220, 240)
        first = self.mask(330, 338, 244, 252)
        second = self.mask(354, 366, 244, 252)
        halo = self.mask(338, 346, 244, 252)
        self.depth[first | second | halo] = 450.
        result = self.apply_regions([self.obj(flat | first | second)])
        # Raised exact seeds favour cell3, but the added left region makes
        # cell11 the region winner. It must not overrule the seed evidence.
        self.assertEqual(result[1][0]["location_id"], 11)
        self.assert_fallback(*result)

    def test_seed_vote_includes_components_too_small_to_grant_region_ownership(self):
        flat = self.mask(306, 330, 220, 240)
        inside = self.mask(330, 338, 244, 252)
        halo = self.mask(338, 342, 244, 252)
        outside = np.zeros_like(inside)
        for y in (178, 190, 202, 214, 226, 238):
            outside |= self.mask(420, 424, y, y + 4)
        self.depth[inside | outside | halo] = 450.
        result = self.apply_regions([self.obj(flat | inside | outside)])
        self.assertEqual(result[1][0]["location_id"], 11)
        # Each outside seed component has only16 pixels and cannot augment
        # ownership. Their combined exact raised footprint still counts.
        self.assertGreater(outside.sum(), inside.sum())
        self.assert_fallback(*result)

    def test_exactly_half_seed_area_in_one_cell_is_a_valid_unique_majority(self):
        first = self.mask(330, 342, 244, 252)
        outside = self.mask(420, 432, 244, 252)
        halo = self.mask(326, 330, 244, 252)
        self.depth[first | outside | halo] = 450.
        _, _, after, assignments = self.apply_regions([self.obj(first | outside)])
        self.assertEqual(assignments[0]["location_id"], 11)
        self.assertAlmostEqual(assignments[0]["depth_seed_area_fraction"], .5)
        self.assertTrue(np.all(after[halo] == 1))

    def test_enclosed_mask_holes_and_invalid_depth_are_never_owned(self):
        mask, halo, _ = self.broad_cup()
        hole = self.mask(324, 328, 236, 240)
        mask[hole] = False
        # The enclosed hole has perfectly matching raised depth, so only the
        # exact segmentation's hole protection can prevent its attribution.
        self.depth[hole] = 450.
        self.depth[228:230, 360:362] = 0.
        self.depth[232:234, 360:362] = np.nan
        objects = [self.obj(mask)]
        _, _, after, assignments = self.apply_regions(objects)
        self.assertFalse(after[hole].any())
        self.assertFalse(after[228:230, 360:362].any())
        self.assertFalse(after[232:234, 360:362].any())
        self.assertEqual(after[240, 364], 1)
        self.assertIn("depth_region_pixels", assignments[0])

    def test_conflicting_owners_and_unrelated_ownership_are_preserved(self):
        mask, halo, objects = self.broad_cup()
        owners, _, assignments = self.assign(objects)
        owners[240, 334] = -1
        owners[240, 364] = -1
        owners[100, 100] = 2
        _, _, after, updated = self.apply_regions(objects, baseline=(owners, assignments))
        self.assertEqual(after[240, 334], -1)
        self.assertEqual(after[240, 364], -1)
        self.assertEqual(after[100, 100], 2)
        self.assertEqual(after[239, 364], 1)
        self.assertEqual(updated[0]["depth_region_pixels"], int((mask | halo).sum()) - 2)

    def test_image_edge_component_falls_back_to_existing_fringe(self):
        mask, halo = self.mask(1, 41, 224, 256), self.mask(0, 1, 224, 256)
        self.depth[mask | halo] = 450.
        locations = [dict(self.locations[0], x=-245.)]
        result = self.apply_regions([self.obj(mask)], locations=locations)
        self.assertEqual(len(result[1]), 1)
        self.assert_fallback(*result)

    def test_component_resource_bound_preserves_baseline_fringe(self):
        mask, halo, objects = self.broad_cup()
        with patch("placement_depth_regions.MAX_REGION_PIXELS", int((mask | halo).sum()) - 1):
            result = self.apply_regions(objects)
        self.assertTrue((result[0][halo] == 1).any())
        self.assert_fallback(*result)

    def test_table_and_too_high_depth_do_not_bridge_separate_regions(self):
        mask = self.mask(310, 350, 224, 256)
        bridge = self.mask(350, 354, 236, 244)
        obstacle = self.mask(354, 366, 236, 244)
        for bridge_depth in (500., 485., 419., 0., np.nan):
            with self.subTest(bridge_depth=bridge_depth):
                depth = self.depth.copy()
                depth[mask | obstacle] = 450.
                depth[bridge] = bridge_depth
                _, _, after, _ = self.apply_regions([self.obj(mask)], depth=depth)
                self.assertFalse(after[obstacle].any())
                self.assertFalse(after[bridge].any())

    def shared_bridge(self, *, first_end=344, second_start=354, right_end=388,
                      locations=None):
        first = self.mask(310, first_end, 224, 256)
        second = self.mask(second_start, right_end, 224, 256)
        whole = self.mask(310, right_end, 224, 256)
        self.depth[whole] = 450.
        objects = [self.obj(first), self.obj(second, detection_id=21)]
        locations = self.locations if locations is None else locations
        owners, _, assignments = self.assign(objects, locations=locations)
        owners[~(first | second)] = 0  # Isolate region evidence from legacy fringe.
        return objects, owners, assignments, whole

    def shared_result(self, fixture, *, locations=None):
        objects, owners, assignments, _ = fixture
        return assign_depth_regions(
            self.depth, objects, self.locations if locations is None else locations,
            owners, assignments, transform=self.vision.transform,
            translation=self.vision.translation, intrinsics=self.K,
            cell_size=self.vision.cell_size, table_z=self.vision.table_z)

    def test_shared_boundary_retains_both_possible_owners_and_per_cup_metadata(self):
        fixture = self.shared_bridge()
        owners, records, shared = self.shared_result(fixture)
        self.assertEqual(len(shared), 1)
        possible_owners, coordinates = shared[0]
        # Values are one-based location indices, never raw P11/P3 IDs.
        self.assertEqual(possible_owners, frozenset((1, 2)))
        self.assertGreater(len(coordinates[0]), 0)
        self.assertTrue(np.all(owners[coordinates] == -1))
        self.assertTrue(np.all(fixture[1][coordinates] == 0))
        self.assertEqual({r['location_id'] for r in records}, {11, 3})
        for record in records:
            self.assertEqual(record['depth_shared_boundary_pixels'], len(coordinates[0]))
            self.assertGreaterEqual(record['depth_split_seam_pixels'], len(coordinates[0]))
            owner = next(i + 1 for i, p in enumerate(self.locations)
                         if p['id'] == record['location_id'])
            self.assertEqual(record['depth_region_pixels'], int(np.count_nonzero(owners == owner)))
        # Shared evidence is not falsely counted as definite ownership twice.
        self.assertEqual(sum(r['depth_region_pixels'] for r in records)
                         + len(coordinates[0]), int(fixture[3].sum()))

    def test_shared_coverage_failure_preserves_the_entire_uncertain_seam(self):
        # Each exclusive part has20/39 exact columns, so it passes the original
        # split. Charging the two uncertain columns makes20/41 insufficient.
        fixture = self.shared_bridge(first_end=330, second_start=370, right_end=390)
        proposed = []
        def observe(*args):
            result = shared_boundaries(*args)
            proposed.extend(result)
            return result
        with patch('placement_depth_regions.shared_boundaries', side_effect=observe):
            owners, records, shared = self.shared_result(fixture)
        self.assertTrue(proposed)  # Geometry alone qualifies; coverage vetoes it.
        self.assertEqual(shared, [])
        self.assertTrue(np.all(owners[224:256, 349:351] == -1))
        self.assertTrue(np.all(owners[224:256, 330:349] == 1))
        self.assertTrue(np.all(owners[224:256, 351:370] == 2))
        self.assertEqual([r['depth_shared_boundary_pixels'] for r in records], [0, 0])
        self.assertEqual([r['depth_split_seam_pixels'] for r in records], [64, 64])

    def test_shared_area_vote_gives_no_beneficial_credit_to_the_current_owner(self):
        # These deliberately overlapping cells each win their seed/part vote
        # by one column of2mm XY bins. The shared bins belong to BOTH cells: adding them to
        # both scores leaves the old winner unchanged, but giving only the
        # competitor its possible new bin makes a tie. The seam must remain.
        locations = [dict(self.locations[0], x=240.), dict(self.locations[1], x=242.)]
        fixture = self.shared_bridge(locations=locations)
        self.assertEqual([a['location_id'] for a in fixture[2]], [11, 3])
        proposed = []
        def observe(*args):
            result = shared_boundaries(*args)
            proposed.extend(result)
            return result
        with patch('placement_depth_regions.shared_boundaries', side_effect=observe):
            owners, records, shared = self.shared_result(fixture, locations=locations)
        self.assertTrue(proposed)  # Rejected by the adverse vote, not geometry.
        self.assertEqual(shared, [])
        self.assertTrue(np.all(owners[224:256, 348:350] == -1))
        self.assertEqual([r['depth_shared_boundary_pixels'] for r in records], [0, 0])
        self.assertTrue(all(r['depth_region_pixels'] > 0 for r in records))

    def test_shared_support_cannot_borrow_neighbors_owned_by_the_other_cup(self):
        for foreign_owner in (False, True):
            with self.subTest(foreign_owner=foreign_owner):
                fixture = self.shared_bridge()
                # Every neighbor within two pixels of the seam is already
                # attributed. Only attribution agreeing with its split part
                # can serve as supporting surface evidence.
                fixture[1][224:256, 346:348] = 2 if foreign_owner else 1
                fixture[1][224:256, 350:352] = 1 if foreign_owner else 2
                original = fixture[1].copy()
                owners, records, shared = self.shared_result(fixture)
                np.testing.assert_array_equal(fixture[1], original)
                if foreign_owner:
                    self.assertEqual(shared, [])
                    self.assertEqual([r['depth_shared_boundary_pixels'] for r in records], [0, 0])
                    self.assertTrue(np.all(owners[224:256, 346:352] == -1))
                else:
                    self.assertEqual(len(shared), 1)
                    self.assertTrue(all(r['depth_shared_boundary_pixels'] > 0 for r in records))


if __name__ == "__main__":
    unittest.main()
