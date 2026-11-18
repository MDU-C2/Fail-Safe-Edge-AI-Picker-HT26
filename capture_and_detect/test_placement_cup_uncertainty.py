"""Offline contracts for observed, cup-local depth uncertainty attribution."""

import copy
import json
import unittest

import numpy as np

from detection_masks import encode_mask
from placement_cup_uncertainty import (MAX_CROP_PIXELS, MAX_CUPS, MAX_FRAME_PIXELS,
                                       MAX_LOCATIONS, MAX_OBJECTS, cup_uncertainty_regions)


class CupUncertaintyTests(unittest.TestCase):
    @staticmethod
    def fixture(*, cx=160., boxes=((128, 108, 152, 132),), depths=None):
        """A downward camera; every test point is back-projected from its depth."""
        shape = (240, 320)
        depth = np.full(shape, 600., dtype=np.float32)
        intrinsics = {"width": shape[1], "height": shape[0], "fx": 500.,
                      "fy": 500., "cx": cx, "cy": 120.}
        objects, locations, assignments = [], [], []
        for index, (x0, y0, x1, y1) in enumerate(boxes):
            optical = 550. if depths is None else depths[index]
            mask = np.zeros(shape, bool)
            mask[y0:y1, x0:x1] = True
            depth[mask] = optical
            objects.append(CupUncertaintyTests.object(mask, detection_id=20 + index))
            location_id = (11, 3, 27, 5)[index]
            locations.append({"id": location_id,
                              "x": ((x0 + x1 - 1) / 2 - cx) * optical / 500.,
                              "y": -((y0 + y1 - 1) / 2 - 120.) * optical / 500.})
            assignments.append({"location_id": location_id, "object_indices": [index]})
        return dict(depth=depth, objects=objects, locations=locations,
                    assignments=assignments, transform=np.diag([1., -1., -1.]),
                    translation=np.array([0., 0., 600.]), intrinsics=intrinsics,
                    cell_size=40., radius_mm=40.)

    @staticmethod
    def object(mask, *, kind="CUP", confidence=.9, detection_id=20):
        rows, columns = np.nonzero(mask)
        return {"class": kind, "confidence": confidence, "detection_id": detection_id,
                "bbox_xyxy": [int(columns.min()), int(rows.min()),
                              int(columns.max()) + 1, int(rows.max()) + 1],
                "segmentation": encode_mask(mask)}

    def evaluate(self, data):
        original = copy.deepcopy(data)
        result = cup_uncertainty_regions(**data)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        regions, evidence = result
        self.assertIsInstance(regions, list)
        self.assertIsInstance(evidence, list)
        self.assertTrue(all(isinstance(record, dict) for record in evidence))
        json.dumps(evidence, allow_nan=False)
        for key in original:
            if isinstance(original[key], np.ndarray):
                np.testing.assert_array_equal(data[key], original[key])
            else:
                self.assertEqual(data[key], original[key])
        selected = np.zeros(data["depth"].shape, bool)
        owners = {}
        for possible_owners, coordinates in regions:
            self.assertIsInstance(possible_owners, frozenset)
            self.assertTrue(possible_owners)
            self.assertTrue(all(type(i) is int and 1 <= i <= len(data["locations"])
                                for i in possible_owners))
            self.assertEqual(len(coordinates), 2)
            rows, columns = coordinates
            self.assertEqual(np.shape(rows), np.shape(columns))
            self.assertEqual(np.ndim(rows), 1)
            self.assertGreater(len(rows), 0)
            self.assertFalse(selected[rows, columns].any(), "Overlap must merge owner sets")
            selected[rows, columns] = True
            for row, column in zip(rows, columns):
                owners[(int(row), int(column))] = possible_owners
        return selected, owners, evidence

    def assert_no_regions(self, data):
        regions, evidence = cup_uncertainty_regions(**data)
        self.assertEqual(regions, [])
        self.assertEqual(evidence, [])

    @staticmethod
    def cover_sample_with_foreign_cell(data, row, column):
        optical = float(data["depth"][row, column])
        data["locations"].append({"id": 99,
            "x": (column-data["intrinsics"]["cx"]) * optical / data["intrinsics"]["fx"],
            "y": -(row-data["intrinsics"]["cy"]) * optical / data["intrinsics"]["fy"]})

    def tail_fixture(self, *, cup_height=50., tail_height=50.):
        data = self.fixture(depths=(600.-cup_height,))
        data["intrinsics"].update(fx=550., fy=550.)  # One pixel = one mm at 50 mm height.
        data["locations"][0].update(x=-20.5, y=.5)
        data["depth"][120, 152:233] = 600.-tail_height  # Observed path from the cup.
        return data

    def test_disconnected_fragment_near_one_cup_needs_no_component_anchor(self):
        data = self.fixture()
        data["depth"][116:124, 160:168] = 550.
        selected, owners, evidence = self.evaluate(data)
        self.assertTrue(selected[116:124, 160:168].all())
        self.assertFalse(selected[108:132, 128:152].any())
        self.assertFalse(selected[data["depth"] == 600.].any())
        self.assertEqual(owners[(120, 164)], frozenset({1}))  # P11 is owner index 1.
        self.assertTrue(evidence)

    def test_small_fragment_can_qualify_after_a_height_step(self):
        data = self.fixture()
        data["depth"][116:124, 152:160] = 565.  # 35 mm high; cup is 50 mm high.
        selected, _, _ = self.evaluate(data)
        self.assertTrue(selected[116:124, 152:160].all())

    def test_large_region_does_not_require_half_exact_coverage_or_expand_past_halo(self):
        data = self.fixture()
        data["depth"][70:170, 80:260] = 550.
        self.cover_sample_with_foreign_cell(data, 120, 220)
        selected, _, _ = self.evaluate(data)
        self.assertTrue(selected[120, 160])
        self.assertFalse(selected[120, 220])
        self.assertFalse(selected[80, 250])
        self.assertFalse(selected[108:132, 128:152].any())

    def test_radius_boundary_is_inclusive_without_claiming_the_next_pixel(self):
        data = self.fixture()
        data["radius_mm"] = 39.6  # 36 image pixels at 550 / 500 mm per pixel.
        data["depth"][120, 187:189] = 550.
        self.cover_sample_with_foreign_cell(data, 120, 187)
        selected, _, _ = self.evaluate(data)
        self.assertTrue(selected[120, 187])
        self.assertFalse(selected[120, 188])

    def test_image_distance_cannot_borrow_a_near_physical_hull(self):
        data = self.fixture(cx=0.)
        # 36 px * 1.12 = 40.32 mm in image space; physical hull gap is 37.76 mm.
        data["depth"][120, 92] = 560.
        self.cover_sample_with_foreign_cell(data, 120, 92)
        self.assert_no_regions(data)

    def test_physical_hull_distance_cannot_borrow_a_near_image_mask(self):
        data = self.fixture(cx=0.)
        # 35 px * 1.12 = 39.2 mm in image space; physical hull gap is 42.22 mm.
        data["depth"][120, 186] = 560.
        self.cover_sample_with_foreign_cell(data, 120, 186)
        self.assert_no_regions(data)

    def test_image_and_physical_support_must_come_from_the_same_cup(self):
        data = self.fixture(cx=0., boxes=((128, 108, 152, 132), (220, 108, 244, 132)),
                            depths=(450., 550.))
        # Near cup one only in the image; near cup two only in physical XY.
        data["depth"][120, 180] = 565.
        self.cover_sample_with_foreign_cell(data, 120, 180)
        self.assert_no_regions(data)

    def test_exact_masks_and_enclosed_holes_are_never_exempted(self):
        data = self.fixture()
        mask = np.zeros(data["depth"].shape, bool)
        mask[108:132, 128:152] = True
        mask[116:124, 136:144] = False
        data["objects"][0] = self.object(mask)
        data["depth"][116:124, 160:168] = 550.
        selected, _, _ = self.evaluate(data)
        self.assertFalse(selected[108:132, 128:152].any())
        self.assertTrue(selected[116:124, 160:168].all())

    def test_labelled_mask_or_box_protects_even_a_low_confidence_object(self):
        for with_mask in (True, False):
            with self.subTest(with_mask=with_mask):
                data = self.fixture()
                obstacle = np.zeros(data["depth"].shape, bool)
                obstacle[116:124, 160:168] = True
                data["depth"][obstacle] = 550.
                label = self.object(obstacle, kind="BLOCK", confidence=.1, detection_id=99)
                if not with_mask:
                    label.pop("segmentation")
                data["objects"].append(label)
                self.assert_no_regions(data)

    def test_all_exact_detected_masks_stay_protected_including_unassigned_cup(self):
        data = self.fixture()
        mask = np.zeros(data["depth"].shape, bool)
        mask[116:124, 160:168] = True
        data["depth"][mask] = 550.
        data["objects"].append(self.object(mask, confidence=.2, detection_id=99))
        self.assert_no_regions(data)

    def test_overlapping_halos_merge_one_based_owner_sets_without_duplicate_pixels(self):
        data = self.fixture(boxes=((128, 108, 152, 132), (188, 108, 212, 132)))
        for column in (125, 170, 215):
            data["depth"][118:122, column:column + 2] = 550.
        selected, owners, _ = self.evaluate(data)
        self.assertEqual(owners[(120, 125)], frozenset({1}))
        self.assertEqual(owners[(120, 170)], frozenset({1, 2}))
        self.assertEqual(owners[(120, 215)], frozenset({2}))
        self.assertEqual(int(selected.sum()), 24)

    def test_connected_low_tail_outside_grid_extends_from_41_to_80_mm_only(self):
        data = self.tail_fixture()
        selected, _, evidence = self.evaluate(data)
        self.assertTrue(selected[120, 191])  # Forty mm: ordinary edge allowance.
        self.assertTrue(selected[120, 192])  # Forty-one mm: projected tail.
        self.assertTrue(selected[120, 231])  # Exactly eighty mm.
        self.assertFalse(selected[120, 232])
        self.assertEqual(evidence[0]["edge_uncertain_pixels"], 40)
        self.assertEqual(evidence[0]["projected_tail_pixels"], 40)
        self.assertEqual(evidence[0]["uncertain_pixels"], 80)

    def test_connected_tail_height_70_is_inclusive_but_71_is_not_exempted(self):
        data = self.tail_fixture(cup_height=80., tail_height=70.)
        # Both heights pass the cup's 85 mm ceiling and both 80 mm distances.
        data["depth"][120, 210] = 529.  # 71 mm; still part of the observed path.
        selected, _, evidence = self.evaluate(data)
        self.assertTrue(selected[120, 180])
        self.assertTrue(selected[120, 209])  # Exactly 70 mm, beyond the 40 mm edge band.
        self.assertFalse(selected[120, 210])
        self.assertTrue(selected[120, 211])  # Height veto does not invent a depth gap.
        self.assertGreater(evidence[0]["projected_tail_pixels"], 0)
        self.assertEqual(evidence[0]["tail_maximum_height_mm"], 70.)

    def test_connected_80_mm_obstacle_core_beyond_edge_band_remains_unclaimed(self):
        data = self.tail_fixture(cup_height=90., tail_height=70.)
        data["depth"][110:130, 202:222] = 520.  # A coherent 80 mm obstacle on the path.
        selected, _, evidence = self.evaluate(data)
        self.assertTrue(selected[120, 200])
        self.assertFalse(selected[110:130, 202:222].any())
        self.assertGreater(evidence[0]["projected_tail_pixels"], 0)

    def test_70_mm_tail_keeps_foreign_cell_and_labelled_object_protections(self):
        for protection in ("foreign_cell", "mask", "bbox"):
            with self.subTest(protection=protection):
                data = self.tail_fixture(cup_height=80., tail_height=70.)
                if protection == "foreign_cell":
                    self.cover_sample_with_foreign_cell(data, 120, 210)
                else:
                    mask = np.zeros(data["depth"].shape, bool)
                    mask[118:123, 208:213] = True
                    data["depth"][mask] = 530.
                    label = self.object(mask, kind="BLOCK", confidence=.1, detection_id=99)
                    if protection == "bbox":
                        label.pop("segmentation")
                    data["objects"].append(label)
                selected, _, _ = self.evaluate(data)
                self.assertTrue(selected[120, 180])
                self.assertFalse(selected[120, 210])

    def test_tail_inside_any_physical_cell_keeps_the_40_mm_limit(self):
        for own_cell in (False, True):
            with self.subTest(own_cell=own_cell):
                data = self.tail_fixture()
                if own_cell:
                    data["cell_size"] = 200.
                else:
                    self.cover_sample_with_foreign_cell(data, 120, 210)
                selected, _, _ = self.evaluate(data)
                self.assertTrue(selected[120, 180])
                self.assertFalse(selected[120, 210])
                if not own_cell:
                    # The observed path may cross that cell; only its outside tail is exempted.
                    self.assertTrue(selected[120, 231])

    def test_detached_41_to_80_mm_speck_cannot_use_tail_allowance(self):
        data = self.fixture()
        data["depth"][120, 210] = 550.
        self.assert_no_regions(data)

    def test_tail_cannot_cross_table_or_missing_depth_gap(self):
        for optical in (600., 0., np.nan):
            with self.subTest(optical=optical):
                data = self.tail_fixture()
                data["depth"][120, 185] = optical
                selected, _, _ = self.evaluate(data)
                self.assertTrue(selected[120, 180])
                self.assertFalse(selected[120, 210])

    def test_tail_cannot_borrow_a_path_through_a_labelled_object(self):
        for with_mask in (True, False):
            with self.subTest(with_mask=with_mask):
                data = self.tail_fixture()
                mask = np.zeros(data["depth"].shape, bool)
                mask[120, 185] = True
                label = self.object(mask, kind="BLOCK", confidence=.1, detection_id=99)
                if not with_mask:
                    label.pop("segmentation")
                data["objects"].append(label)
                selected, _, _ = self.evaluate(data)
                self.assertTrue(selected[120, 180])
                self.assertFalse(selected[120, 210])

    def test_another_cups_exact_mask_cannot_supply_the_first_cups_tail_path(self):
        data = self.tail_fixture()
        mask = np.zeros(data["depth"].shape, bool)
        mask[118:122, 184:192] = True
        data["depth"][mask] = 550.
        data["objects"].append(self.object(mask, detection_id=21))
        data["locations"].append({"id": 3, "x": 27.5, "y": .5})
        data["assignments"].append({"location_id": 3, "object_indices": [1]})
        selected, owners, _ = self.evaluate(data)
        self.assertFalse(selected[mask].any())
        self.assertEqual(owners[(120, 231)], frozenset({2}))

    def test_high_arm_and_depth_above_cup_height_limit_stay_unclaimed(self):
        for optical in (430., 500., 544.9):  # Heights 170, 100 and 55.1 mm.
            with self.subTest(optical=optical):
                data = self.fixture()
                data["depth"][116:124, 160:168] = optical
                self.assert_no_regions(data)
        data = self.fixture()
        data["depth"][116:124, 160:168] = 545.  # Exactly max seed height + 5 mm.
        selected, _, _ = self.evaluate(data)
        self.assertTrue(selected[116:124, 160:168].all())

    def test_60_mm_cup_allows_65_mm_edge_but_global_160_mm_ceiling_still_applies(self):
        for seed_optical, accepted_optical, rejected_optical in ((540., 535., 534.9),
                                                               (440., 440., 439.9)):
            with self.subTest(seed_optical=seed_optical):
                data = self.fixture(depths=(seed_optical,))
                data["depth"][116:124, 160:168] = accepted_optical
                selected, _, _ = self.evaluate(data)
                self.assertTrue(selected[116:124, 160:168].all())
                data["depth"][116:124, 160:168] = rejected_optical
                self.assert_no_regions(data)

    def test_low_tail_also_obeys_its_cups_observed_height_ceiling(self):
        data = self.tail_fixture(cup_height=50., tail_height=55.)
        data["depth"][120, 210] = 544.  # 56 mm, below 70 but above this cup's 55 mm ceiling.
        selected, _, evidence = self.evaluate(data)
        self.assertTrue(selected[120, 209])
        self.assertFalse(selected[120, 210])
        self.assertEqual(evidence[0]["tail_maximum_height_mm"], 55.)

    def test_table_depth_invalid_depth_and_exact_30_mm_height_do_not_qualify(self):
        for optical in (600., 570., 0., -1., np.nan, np.inf):
            with self.subTest(optical=optical):
                data = self.fixture()
                data["depth"][116:124, 160:168] = optical
                self.assert_no_regions(data)

    def test_exact_support_accepts_95_percent_but_not_less(self):
        for invalid_depth in (0., np.nan, 400.):
            with self.subTest(invalid_depth=invalid_depth):
                data = self.fixture(boxes=((128, 110, 148, 130),))
                data["depth"][110, 128:148] = invalid_depth  # 20 of 400 = 5%.
                data["depth"][116:124, 156:164] = 550.
                selected, _, _ = self.evaluate(data)
                self.assertTrue(selected[116:124, 156:164].all())
                self.assertFalse(selected[110, 128:148].any())
                data["depth"][111, 128] = invalid_depth
                self.assert_no_regions(data)

    def test_too_few_exact_seeds_and_unqualified_detection_do_not_grant_a_halo(self):
        data = self.fixture(boxes=((128, 110, 133, 116),))  # Thirty exact pixels.
        data["depth"][118:122, 140:144] = 550.
        self.assert_no_regions(data)
        for change in ({"confidence": .49}, {"class": "BLOCK"}, {"segmentation": None}):
            with self.subTest(change=change):
                data = self.fixture()
                data["depth"][116:124, 160:168] = 550.
                data["objects"][0].update(change)
                self.assert_no_regions(data)

    def test_exact_physical_owner_must_be_unique_and_match_assignment(self):
        for mode in ("tie", "disagreement", "outside"):
            with self.subTest(mode=mode):
                data = self.fixture()
                data["depth"][116:124, 160:168] = 550.
                if mode == "tie":
                    data["locations"].append(dict(data["locations"][0], id=3))
                elif mode == "disagreement":
                    data["locations"].append(dict(data["locations"][0], id=3))
                    data["locations"][0]["x"] += 100.
                else:
                    data["locations"][0]["x"] += 100.
                self.assert_no_regions(data)

    def test_nonzero_table_height_uses_relative_observed_heights(self):
        data = self.fixture()
        data["table_z"] = 100.
        data["translation"][2] += 100.
        data["depth"][116:124, 160:168] = 550.
        selected, _, _ = self.evaluate(data)
        self.assertTrue(selected[116:124, 160:168].all())

    def test_nonzero_table_height_keeps_tail_cap_relative_to_the_table(self):
        data = self.tail_fixture(cup_height=80., tail_height=70.)
        data["table_z"] = 100.
        data["translation"][2] += 100.
        data["depth"][120, 210] = 529.  # World Z171, relative height71.
        selected, _, evidence = self.evaluate(data)
        self.assertTrue(selected[120, 209])  # World Z170, relative height70.
        self.assertFalse(selected[120, 210])
        self.assertEqual(evidence[0]["tail_maximum_height_mm"], 70.)

    def test_malformed_geometry_and_containers_fail_without_exemptions(self):
        changes = (
            ("objects", [None]), ("assignments", [None]), ("locations", []),
            ("transform", np.eye(2)), ("translation", np.array([0., 0., np.nan])),
            ("cell_size", 19.), ("cell_size", True), ("radius_mm", np.nan),
            ("radius_mm", 0.), ("check_height", 30.),
            ("depth", np.ones((2, 2, 2))),
        )
        for key, value in changes:
            with self.subTest(key=key, value=str(value)[:30]):
                data = self.fixture()
                data["depth"][116:124, 160:168] = 550.
                data[key] = value
                self.assert_no_regions(data)
        for indices in ([-1], [True], [0, 0], [99]):
            data = self.fixture()
            data["depth"][116:124, 160:168] = 550.
            data["assignments"][0]["object_indices"] = indices
            self.assert_no_regions(data)

    def test_global_resource_limits_reject_without_partial_attribution(self):
        for resource in ("objects", "assignments", "locations", "frame"):
            with self.subTest(resource=resource):
                data = self.fixture()
                data["depth"][116:124, 160:168] = 550.
                if resource == "objects":
                    data["objects"] *= MAX_OBJECTS + 1
                elif resource == "assignments":
                    data["assignments"] *= MAX_CUPS + 1
                elif resource == "locations":
                    data["locations"] = [dict(data["locations"][0], id=i) for i in range(MAX_LOCATIONS + 1)]
                else:
                    side = int(np.sqrt(MAX_FRAME_PIXELS)) + 1
                    data["depth"] = np.broadcast_to(np.float32(600.), (side, side))
                    data["intrinsics"].update(width=side, height=side)
                self.assert_no_regions(data)

    def test_crop_at_resource_bound_can_qualify_but_larger_crop_cannot(self):
        # At these intrinsics the 80 mm search requires 148 pixels of padding.
        crop_side = int(np.sqrt(MAX_CROP_PIXELS))
        self.assertEqual(crop_side ** 2, MAX_CROP_PIXELS)
        for excess in (0, 4):
            with self.subTest(excess=excess):
                data = self.fixture()
                data["depth"] = np.full((800, 800), 600., np.float32)
                width = crop_side - 2 * 148 + excess
                start, end = 400 - width // 2, 400 + width // 2
                mask = np.zeros((800, 800), bool)
                mask[start:end, start:end] = True
                data["depth"][mask] = 550.
                data["depth"][399:401, end:end + 2] = 550.
                data["objects"] = [self.object(mask)]
                data["intrinsics"].update(width=800, height=800, cx=400., cy=400., fx=1000., fy=1000.)
                data["locations"] = [{"id": 11, "x": -.275, "y": .275}]
                data["cell_size"] = 200.
                if excess:
                    self.assert_no_regions(data)
                else:
                    selected, _, _ = self.evaluate(data)
                    self.assertTrue(selected[399:401, end:end + 2].all())

    def test_all_32_cup_membership_bits_work_at_group_limit(self):
        data = self.fixture()
        data["depth"][:] = 600.
        data["objects"], data["locations"], data["assignments"] = [], [], []
        data["cell_size"] = 20.
        for index in range(MAX_CUPS):
            x, y = 8 + (index % 8) * 40, 20 + (index // 8) * 50
            mask = np.zeros(data["depth"].shape, bool)
            mask[y:y + 4, x:x + 8] = True
            data["depth"][mask] = 550.
            data["depth"][y + 2, x + 9] = 550.
            data["objects"].append(self.object(mask, detection_id=index))
            data["locations"].append({"id": 100 + index, "x": (x + 3.5 - 160.) * 1.1,
                                      "y": -(y + 1.5 - 120.) * 1.1})
            data["assignments"].append({"location_id": 100 + index, "object_indices": [index]})
        _, owners, evidence = self.evaluate(data)
        self.assertEqual(len(evidence), MAX_CUPS)
        self.assertTrue(any(MAX_CUPS in group for group in owners.values()))


if __name__ == "__main__":
    unittest.main()
