"""Thin shared cup boundaries use observed geometry and preserve uncertainty."""

import unittest
from unittest.mock import patch

import numpy as np

from placement_depth_shared import shared_boundaries


class SharedDepthBoundaryTests(unittest.TestCase):
    @staticmethod
    def fixture(width=60, gap=2):
        height = 40
        component = np.ones((height, width), dtype=bool)
        left_edge = (width - gap) // 2
        right_edge = left_edge + gap
        first, second = np.zeros_like(component), np.zeros_like(component)
        first[:, :left_edge] = True
        second[:, right_edge:] = True
        first_seed, second_seed = np.zeros_like(component), np.zeros_like(component)
        first_seed[:, :max(1, left_edge - 5)] = True
        second_seed[:, min(width - 1, right_edge + 5):] = True
        seam = np.zeros_like(component)
        seam[:, left_edge:right_edge] = True
        owners = np.zeros(component.shape, dtype=np.int32)
        yy, xx = np.indices(component.shape)
        points = np.stack((xx, yy, np.full(component.shape, 40)), axis=-1).astype(float)
        return component, [first_seed, second_seed], [first, second], seam, owners, points

    def test_thin_continuous_boundary_has_two_explicit_owners_without_mutating_inputs(self):
        args = self.fixture()
        originals = [args[0].copy(), [a.copy() for a in args[1]], [a.copy() for a in args[2]],
                     args[3].copy(), args[4].copy(), args[5].copy()]
        result = shared_boundaries(*args)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][:2], (0, 1))
        np.testing.assert_array_equal(result[0][2], args[3])
        for original, actual in zip(originals, args):
            if isinstance(original, list):
                for first, second in zip(original, actual):
                    np.testing.assert_array_equal(first, second)
            else:
                np.testing.assert_array_equal(original, actual)

    def test_thin_high_spike_rejects_its_entire_connected_boundary(self):
        args = self.fixture()
        args[5][20, 29:31, 2] = 160.
        self.assertEqual(shared_boundaries(*args), [])

    def test_both_partitions_need_nearby_physical_support(self):
        args = self.fixture()
        args[5][args[2][1], 2] = 65.  #Only the right partition is discontinuous.
        self.assertEqual(shared_boundaries(*args), [])

    def test_conflicted_partition_samples_cannot_support_a_boundary(self):
        args = self.fixture()
        args[4][:, 27:29] = -1
        args[4][:, 31:33] = -1
        self.assertEqual(shared_boundaries(*args), [])

    def test_conflicted_direct_contacts_cannot_be_replaced_by_distant_support(self):
        args = self.fixture()
        # Physical support still exists two pixels away, but the observed
        # shared piece cannot touch either usable partition through the gap.
        args[4][:, 28] = -1
        args[4][:, 31] = -1
        self.assertEqual(shared_boundaries(*args), [])

    def test_non_tied_discarded_pixels_are_not_shared_despite_nearby_parts(self):
        args = self.fixture()
        # ``seam`` also contains pixels removed by seed connectivity. Merely
        # touching both accepted parts must not turn a clearly nearer discarded
        # pixel into an uncertain boundary between the two exact masks.
        args[1][0][:] = False
        args[1][0][:, :20] = True
        args[1][1][:] = False
        args[1][1][:, 50:] = True
        self.assertEqual(shared_boundaries(*args), [])

    def test_original_conflicts_and_every_nonzero_owner_remain_excluded(self):
        args = self.fixture()
        for row, owner in enumerate((-1, -2, 1, 2, 99), 10):
            args[4][row, 29] = owner
        result = shared_boundaries(*args)
        self.assertEqual(len(result), 1)
        shared = result[0][2]
        self.assertFalse(shared[args[4] != 0].any())
        self.assertEqual(shared.sum(), args[3].sum() - 5)

    def test_three_pixel_gap_cannot_jump_to_partitions_despite_two_pixel_distances(self):
        # Its middle column is geometrically within two pixels of both sides,
        # but it does not directly touch either accepted partition.
        self.assertEqual(shared_boundaries(*self.fixture(gap=3)), [])

    def test_wider_boundary_is_not_shared(self):
        self.assertEqual(shared_boundaries(*self.fixture(gap=5)), [])

    def test_table_gap_prevents_a_detached_candidate_from_reaching_both_parts(self):
        args = self.fixture()
        args[0][:, 28] = False
        args[2][0][:, 28] = False
        self.assertEqual(shared_boundaries(*args), [])

    def test_three_way_neighbourhood_remains_ambiguous(self):
        args = self.fixture()
        third = np.zeros_like(args[0])
        third[:2, :30] = True
        third[2:21, 29] = True
        third_seed = np.zeros_like(third)
        third_seed[:2, :16] = True
        # The third seed is far away, but its accepted surface approaches the
        # first two cups' tied seam. It must still prevent a two-owner claim.
        for seed, partition in zip(args[1], args[2]):
            seed[third] = False
            partition[third] = False
        args[1].append(third_seed)
        args[2].append(third)
        args[3][:] = False
        args[3][21:23, 29:31] = True
        self.assertEqual(shared_boundaries(*args), [])

    def test_missing_observed_pixels_are_never_filled(self):
        args = self.fixture()
        args[0][10:12, 29:31] = False
        result = shared_boundaries(*args)
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0][2][10:12, 29:31].any())
        self.assertEqual(result[0][2].sum(), args[3].sum() - 4)

    def test_shared_area_budget_accepts_exactly_five_percent(self):
        args = self.fixture(width=40)
        self.assertEqual(args[3].sum(), args[0].sum() * .05)
        result = shared_boundaries(*args)
        self.assertEqual(len(result), 1)
        np.testing.assert_array_equal(result[0][2], args[3])

    def test_shared_area_budget_rejects_excess_instead_of_trimming(self):
        args = self.fixture(width=30)
        self.assertGreater(args[3].sum(), args[0].sum() * .05)
        self.assertEqual(shared_boundaries(*args), [])

    def test_reversing_partition_order_preserves_geometry(self):
        args = self.fixture()
        forward = shared_boundaries(*args)
        reversed_args = (args[0], list(reversed(args[1])), list(reversed(args[2])),
                         args[3], args[4], args[5])
        reverse = shared_boundaries(*reversed_args)
        np.testing.assert_array_equal(forward[0][2], reverse[0][2])

    def test_unsigned_physical_coordinates_do_not_wrap_distance_subtraction(self):
        args = list(self.fixture())
        args[5] = args[5].astype(np.uint16)
        result = shared_boundaries(*args)
        self.assertEqual(len(result), 1)
        np.testing.assert_array_equal(result[0][2], args[3])

    def test_nonfinite_boundary_or_partition_samples_do_not_grant_ownership(self):
        for kind in ("seam", "partition"):
            with self.subTest(kind=kind):
                args = self.fixture()
                if kind == "seam":
                    args[5][20, 29, 2] = np.nan
                else:
                    args[5][args[2][1], :] = np.inf
                self.assertEqual(shared_boundaries(*args), [])

    def test_invalid_shapes_types_and_partitions_fail_closed(self):
        for kind in ("component_type", "seam_shape", "owner_type", "point_shape", "complex_points",
                     "missing_seed", "seed_outside_part", "overlapping_parts", "five_parts"):
            with self.subTest(kind=kind):
                args = list(self.fixture())
                if kind == "component_type":
                    args[0] = args[0].astype(np.uint8)
                elif kind == "seam_shape":
                    args[3] = args[3][:-1]
                elif kind == "owner_type":
                    args[4] = args[4].astype(float)
                elif kind == "point_shape":
                    args[5] = args[5][:, :, :2]
                elif kind == "complex_points":
                    args[5] = args[5].astype(complex)
                elif kind == "missing_seed":
                    args[1][0][:] = False
                elif kind == "seed_outside_part":
                    args[1][0][20, 29] = True
                elif kind == "overlapping_parts":
                    args[2][1][20, 20] = True
                else:
                    args[1].extend([args[1][0]] * 3)
                    args[2].extend([args[2][0]] * 3)
                self.assertEqual(shared_boundaries(*args), [])

    def test_crop_limit_rejects_before_neighbourhood_allocations(self):
        args = self.fixture()
        with patch("placement_depth_shared.MAX_COMPONENT_PIXELS", args[0].size - 1), \
                patch("placement_depth_shared.cv2.dilate") as dilate, \
                patch("placement_depth_shared.cv2.distanceTransform") as distance:
            self.assertEqual(shared_boundaries(*args), [])
        dilate.assert_not_called()
        distance.assert_not_called()


if __name__ == "__main__":
    unittest.main()
