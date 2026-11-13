"""Observed shared cup groups retain bounded foreign-cell edge attribution."""

import copy
import json
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from placement_depth_group import shared_group_region


class SharedDepthGroupTests(unittest.TestCase):
    @staticmethod
    def fixture():
        shape = (80, 260)
        component = np.zeros(shape, dtype=bool)
        component[10:70, 10:230] = True
        first, second = np.zeros(shape, bool), np.zeros(shape, bool)
        first[10:70, 10:90] = True
        second[10:70, 110:190] = True
        yy, xx = np.indices(shape)
        points = np.stack((xx, yy, np.full(shape, 60)), axis=-1).astype(float)
        # Optical depth / focal length is one: one image pixel is one mm.
        return {
            "component": component, "seeds": [first, second],
            "points": points, "optical_depth": np.full(shape, 500.),
            "eligible": np.ones(shape, bool),
            "locations": [{"x": 50., "y": 40.}, {"x": 150., "y": 40.},
                          {"x": 240., "y": 40.}],
            "owner_indices": [0, 1], "cell_size": 80.,
            "min_depth_scale": 1., "focal_length": 500.,
        }

    @staticmethod
    def small_fixture(shape, first, second, *, cell_size=40.):
        yy, xx = np.indices(shape)
        return {
            "component": np.ones(shape, bool), "seeds": [first, second],
            "points": np.stack((xx, yy, np.full(shape, 60)), axis=-1).astype(float),
            "optical_depth": np.full(shape, 500.), "eligible": np.ones(shape, bool),
            "locations": [{"x": 5., "y": 10.}, {"x": 20., "y": 10.}],
            "owner_indices": [0, 1], "cell_size": cell_size,
            "min_depth_scale": 1., "focal_length": 500.,
        }

    def apply(self, data):
        if "seed_hulls" not in data:
            data["seed_hulls"] = [cv2.convexHull(data["points"][seed, :2].astype(np.float32))
                                  for seed in data["seeds"]]
        before = copy.deepcopy(data)
        result = shared_group_region(**data)
        for key, value in before.items():
            if isinstance(value, np.ndarray):
                np.testing.assert_array_equal(value, data[key])
            elif isinstance(value, list) and value and isinstance(value[0], np.ndarray):
                for old, new in zip(value, data[key]):
                    np.testing.assert_array_equal(old, new)
            else:
                self.assertEqual(value, data[key])
        return result

    def test_foreign_cell_edge_is_bounded_and_metadata_is_serializable(self):
        data = self.fixture()
        selected, metadata = self.apply(data)
        self.assertTrue(selected[40, 100])  # Observed gap outside every cell.
        self.assertTrue(selected[40, 195])  # Outside cells, beyond the cup mask.
        self.assertTrue(selected[40, 200])  # Within the foreign cell and envelope.
        self.assertTrue(selected[40, 209])  # Exactly twenty mm from the mask/hull.
        self.assertFalse(selected[40, 210])  # Twenty-one mm is not attributable.
        self.assertFalse(selected[~data["component"]].any())
        self.assertFalse(selected[data["seeds"][0] | data["seeds"][1]].any())
        self.assertEqual(metadata["component_pixels"], 13200)
        self.assertAlmostEqual(metadata["exclusive_seed_fraction"], 9600 / 13200)
        self.assertEqual(metadata["foreign_cell_pixels"], 1800)
        self.assertEqual(metadata["foreign_cell_exempted_pixels"], 600)
        self.assertEqual(metadata["foreign_cell_retained_pixels"], 1200)
        self.assertEqual(metadata["shared_pixels"], int(selected.sum()))
        self.assertEqual(metadata["edge_tolerance_mm"], 20.)
        json.dumps(metadata, allow_nan=False)

    def test_both_proximity_bounds_must_belong_to_the_same_cup(self):
        data = self.fixture()
        # Image support is close only to cup two, while physical support is
        # close only to cup one. Combining those separate proofs is invalid.
        data["points"][40, 200] = (69., 13.8, 60.)
        data["optical_depth"][40, 200] = 172.5
        data["locations"].append({"x": 69., "y": 13.8})
        selected, _ = self.apply(data)
        self.assertFalse(selected[40, 200])

    def test_physical_hull_limit_is_independent_of_image_distance(self):
        data = self.fixture()
        data["points"][40, 200, 0] = 210.  # Image eleven mm; physical twenty-one.
        selected, _ = self.apply(data)
        self.assertFalse(selected[40, 200])
        data["points"][40, 200, 0] = 209.
        selected, _ = self.apply(data)
        self.assertTrue(selected[40, 200])

    def test_image_limit_is_independent_of_physical_hull_distance(self):
        data = self.fixture()
        data["points"][40, 210, 0] = 209.  # Physical twenty mm; image twenty-one.
        selected, _ = self.apply(data)
        self.assertFalse(selected[40, 210])

    def test_exclusive_exact_support_accepts_half_but_not_one_pixel_less(self):
        first, second = np.zeros((20, 20), bool), np.zeros((20, 20), bool)
        first[:10, :10], second[:10, 10:] = True, True
        data = self.small_fixture(first.shape, first, second)
        _, metadata = self.apply(data)
        self.assertEqual(metadata["exclusive_seed_fraction"], .5)
        data["seeds"][0][0, 0] = False
        self.assertIsNone(self.apply(data))

    def test_each_cup_requires_32_exclusive_component_anchors(self):
        first, second = np.zeros((20, 30), bool), np.zeros((20, 30), bool)
        first[:8, :4], second[:, 10:] = True, True
        data = self.small_fixture(first.shape, first, second)
        self.assertIsNotNone(self.apply(data))
        data["seeds"][0][0, 0] = False
        self.assertIsNone(self.apply(data))  # Group support still exceeds half.

    def test_overlapping_seeds_cannot_supply_support_or_receive_shared_ownership(self):
        data = self.fixture()
        data["seeds"][1][20:25, 20:25] = True
        selected, metadata = self.apply(data)
        self.assertAlmostEqual(metadata["exclusive_seed_fraction"], (9600 - 25) / 13200)
        self.assertFalse(selected[20:25, 20:25].any())
        # A duplicated mask does not create a second identified anchor.
        data["seeds"][1] = data["seeds"][0].copy()
        self.assertIsNone(self.apply(data))

    def test_full_masks_outside_component_do_not_inflate_exclusive_support(self):
        data = self.fixture()
        data["seeds"][0][:10, 10:90] = True
        selected, metadata = self.apply(data)
        self.assertAlmostEqual(metadata["exclusive_seed_fraction"], 9600 / 13200)
        self.assertFalse(selected[:10].any())

    def test_physical_vote_counts_unique_bins_including_outside_all_cells(self):
        data = self.fixture()
        # Pixel support remains strong, but more than half the observed physical
        # footprint is outside the owners' cells. Exact masks cannot rescue it.
        data["locations"] = [{"x": 30., "y": 40.}, {"x": 150., "y": 40.}]
        data["cell_size"] = 20.
        self.assertIsNone(self.apply(data))

    def test_overlapping_owner_cells_are_not_double_counted_in_union_area(self):
        data = self.fixture()
        data["locations"] = [{"x": 50., "y": 40.}, {"x": 51., "y": 40.}]
        # Both cells cover almost the same 80 mm of a 220 mm component.
        self.assertIsNone(self.apply(data))

    def test_physical_union_accepts_exactly_half_but_not_one_bin_column_less(self):
        data = self.fixture()
        data["component"][:] = False
        data["component"][20:60, 10:230] = True
        data["locations"] = [{"x": 37.5, "y": 40.}, {"x": 147., "y": 40.}]
        data["cell_size"] = 55.
        _, metadata = self.apply(data)
        self.assertEqual(metadata["owner_area_fraction"], .5)
        data["locations"][0]["x"] = 37.
        self.assertIsNone(self.apply(data))

    def test_dense_pixels_cannot_outvote_a_larger_unique_physical_footprint(self):
        data = self.fixture()
        yy, xx = np.indices(data["component"].shape)
        exact = data["seeds"][0] | data["seeds"][1]
        for seed, center in zip(data["seeds"], (50., 150.)):
            # Many samples of a small observed surface still count its bins
            # once, rather than contributing a vote for every image pixel.
            data["points"][seed, 0] = center + xx[seed] % 4
            data["points"][seed, 1] = 40. + yy[seed] % 4
        outside = data["component"] & ~exact
        data["points"][outside, 0] = 1000. + 2 * xx[outside]
        data["points"][outside, 1] = 1000. + 2 * yy[outside]
        self.assertGreater(exact.sum(), data["component"].sum() / 2)
        self.assertIsNone(self.apply(data))

    def test_conflicts_and_existing_owners_remain_ineligible(self):
        data = self.fixture()
        data["eligible"][40, 100:105] = False
        data["eligible"][40, 200:205] = False
        selected, _ = self.apply(data)
        self.assertFalse(selected[~data["eligible"]].any())
        self.assertTrue(selected[41, 200])
        data["eligible"][:] = False
        self.assertIsNone(self.apply(data))

    def test_missing_component_pixels_and_enclosed_holes_are_never_filled(self):
        data = self.fixture()
        data["component"][35:45, 95:105] = False
        selected, _ = self.apply(data)
        self.assertFalse(selected[35:45, 95:105].any())
        self.assertFalse(selected[~data["component"]].any())

    def test_seed_order_preserves_selected_geometry(self):
        data = self.fixture()
        forward, metadata = self.apply(data)
        data["seeds"].reverse()
        data["seed_hulls"].reverse()
        data["owner_indices"].reverse()
        reverse, reversed_metadata = self.apply(data)
        np.testing.assert_array_equal(forward, reverse)
        self.assertEqual(metadata, reversed_metadata)

    def test_nonfinite_or_nonpositive_observed_support_rejects(self):
        for key, index, value in (
                ("points", (40, 200, 0), np.nan),
                ("points", (40, 20, 2), np.inf),
                ("optical_depth", (40, 200), np.nan),
                ("optical_depth", (40, 200), 0.),
                ("optical_depth", (40, 20), -1.)):
            with self.subTest(key=key, index=index, value=value):
                data = self.fixture()
                data[key][index] = value
                # Prepare valid hulls before testing invalid non-hull support.
                data["seed_hulls"] = [cv2.convexHull(
                    self.fixture()["points"][seed, :2].astype(np.float32)) for seed in data["seeds"]]
                self.assertIsNone(self.apply(data))

    def test_invalid_depth_in_full_seed_outside_component_also_rejects(self):
        data = self.fixture()
        data["seeds"][0][5, 20] = True
        data["optical_depth"][5, 20] = 0
        self.assertIsNone(self.apply(data))

    def test_malformed_geometry_is_rejected_before_distance_maps(self):
        invalid = (
            ("owner_indices", [0, 0]), ("owner_indices", [False, 1]),
            ("owner_indices", [0, 99]), ("cell_size", True),
            ("cell_size", 19.), ("min_depth_scale", 0.),
            ("focal_length", float("inf")), ("locations", [{"x": 0., "y": np.nan}]),
            ("component", np.ones((400, 400), bool)),
            ("eligible", np.ones((80, 260), np.uint8)),
        )
        for key, value in invalid:
            with self.subTest(key=key, value=str(value)[:30]):
                data = self.fixture()
                data["seed_hulls"] = [cv2.convexHull(data["points"][seed, :2].astype(np.float32))
                                      for seed in data["seeds"]]
                data[key] = value
                with patch("placement_depth_group.cv2.distanceTransform") as distance:
                    self.assertIsNone(self.apply(data))
                distance.assert_not_called()

    def test_degenerate_and_nonfinite_hulls_reject(self):
        for hull in (np.array([[[0., 0.]], [[1., 0.]], [[2., 0.]]]),
                     np.array([[[0., 0.]], [[1., 0.]], [[np.nan, 1.]]])):
            data = self.fixture()
            data["seed_hulls"] = [hull, cv2.convexHull(data["points"][data["seeds"][1], :2].astype(np.float32))]
            self.assertIsNone(self.apply(data))


if __name__ == "__main__":
    unittest.main()
