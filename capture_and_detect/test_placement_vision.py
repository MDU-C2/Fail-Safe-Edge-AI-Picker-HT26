"""Offline checks for camera placement evidence; never contact a camera or robot."""

import copy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from cup_inventory import SelectionError
from placement_vision import PlacementVision, RAY_CHUNK_PIXELS


class PlacementVisionTests(unittest.TestCase):
    def setUp(self):
        # A down-looking camera with deliberately nonzero XYZ tag offsets and
        # a non-unit scale makes omitted offsets/scaling visible in the tests.
        self.calibration = SimpleNamespace(
            scale=2.0, R=np.diag([1.0, -1.0, -1.0]),
            t=np.array([315.0, 83.0, 960.0]), offset=(-115.0, 17.0, 40.0),
        )
        self.K = {"width": 640, "height": 480, "fx": 600.0, "fy": 600.0,
                  "cx": 320.0, "cy": 240.0}
        self.vision = PlacementVision(self.calibration, self.K)
        self.location = {"id": 1, "x": 200.0, "y": 100.0, "z": 230.0,
                         "occupied": True}
        self.snapshot = {"fingerprint": "grid-original", "locations": [self.location]}
        self.depth = np.full((480, 640), 500.0)

    def assess(self, depth=None, objects=(), location=None):
        return self.vision.assess(self.depth if depth is None else depth, objects,
                                  [self.location if location is None else location])[0]

    def observation(self, state="clear"):
        observation = [self.assess()]
        observation[0]["state"] = state
        return observation

    def test_inverse_projection_applies_scale_rotation_and_all_three_offsets(self):
        camera_points = np.array([[40.0, -30.0, 500.0], [-20.0, 12.0, 420.0]])
        physical = (self.calibration.scale * camera_points @ self.calibration.R.T
                    + self.calibration.t + self.calibration.offset)
        expected = np.column_stack((600 * camera_points[:, 0] / camera_points[:, 2] + 320,
                                    600 * camera_points[:, 1] / camera_points[:, 2] + 240))
        np.testing.assert_allclose(self.vision.project(physical), expected, atol=1e-10)

    def test_cell_projects_table_plane_and_ignores_flange_height(self):
        floor, volume = self.vision.polygons(self.location)
        high_flange = dict(self.location, z=530.0)
        other_floor, other_volume = self.vision.polygons(high_flange)
        np.testing.assert_allclose(floor, [[296, 264], [344, 264], [344, 216], [296, 216]])
        np.testing.assert_allclose(floor, other_floor)
        np.testing.assert_allclose(volume, other_volume)
        self.assertGreater(np.ptp(volume[:, 0]), np.ptp(floor[:, 0]))

    def test_complete_table_depth_is_clear(self):
        result = self.assess()
        self.assertEqual(result["state"], "clear")
        self.assertEqual(result["valid_fraction"], 1.0)
        self.assertEqual(result["table_fraction"], 1.0)

    def test_table_variation_up_to_30_mm_is_clear(self):
        # With this calibration, physical height is 1000 - 2 * optical depth.
        for height in (-30.0, -24.0, 24.0, 30.0):
            with self.subTest(height=height):
                depth = np.full(self.depth.shape, (1000.0 - height) / 2)
                self.assertEqual(self.assess(depth)["state"], "clear")

    def test_low_surface_feature_is_clear_without_a_cup_detection(self):
        self.depth[230:250, 310:330] = 487.5  # A 25 mm-high surface feature.
        self.assertEqual(self.assess()["state"], "clear")

    def test_depth_just_outside_30_mm_band_still_blocks(self):
        for height, expected in ((30.5, "occupied"), (-30.5, "unknown")):
            with self.subTest(height=height):
                depth = self.depth.copy()
                depth[230:250, 310:330] = (1000.0 - height) / 2
                self.assertEqual(self.assess(depth)["state"], expected)

    def test_cup_detection_vetoes_a_feature_inside_the_relaxed_height_band(self):
        self.depth[230:250, 310:330] = 487.5
        obj = {"class": "CUP", "bbox_xyxy": [310, 230, 330, 250]}
        self.assertEqual(self.assess(objects=[obj])["state"], "occupied")

    def test_missed_yolo_cup_still_blocks_from_raised_depth_cluster(self):
        self.depth[238:243, 318:323] = 450.0
        self.assertEqual(self.assess(objects=[])["state"], "occupied")

    def test_small_contiguous_raised_cluster_blocks_despite_table_majority(self):
        self.depth[239:241, 318:322] = 450.0  # Eight connected elevated pixels.
        self.assertEqual(self.assess()["state"], "occupied")

    def test_hollow_cup_rim_blocks_when_median_depth_is_empty_table(self):
        rim = np.zeros(self.depth.shape, np.uint8)
        cv2.circle(rim, (320, 240), 15, 1, 1)
        self.depth[rim.astype(bool)] = 450.0
        self.assertEqual(float(np.median(self.depth[210:271, 290:351])), 500.0)
        self.assertEqual(self.assess(objects=[])["state"], "occupied")

    def test_parallax_volume_checks_obstacles_outside_table_polygon(self):
        # Floor ends at x344; the volume extends to approximately349.
        self.depth[238:242, 346:349] = 450.0
        self.assertEqual(self.assess()["state"], "occupied")

    def test_neighbouring_object_behind_cell_does_not_occupy_clear_destination(self):
        location = dict(self.location, x=500.0)
        # This cell covers physical X460..540. These pixels are physical
        # X557..558.5/Y95.5..106/Z100: outside it and behind its ray exits,
        # although they overlap the image silhouette of its elevated volume.
        self.depth[236:244, 558:560] = 450.0
        result = self.assess(location=location)
        self.assertEqual(result["state"], "clear")
        self.assertEqual(result["table_fraction"], 1.0)

    def test_object_only_occupies_cell_it_is_physically_inside(self):
        self.depth[236:244, 558:560] = 450.0
        locations = [dict(self.location, x=500.0), dict(self.location, id=2, x=580.0)]
        self.assertEqual([slot["state"] for slot in self.vision.assess(self.depth, [], locations)],
                         ["clear", "occupied"])

    def test_foreground_object_outside_xy_still_blocks_destination_it_hides(self):
        # Physical X420..421/Y97..104/Z400 lies outside the X460..540 cell,
        # but is in front of it along these rays. Its pixels are outside the
        # floor polygon, so floor agreement alone must not make this clear.
        self.depth[236:244, 540:542] = 300.0
        result = self.assess(location=dict(self.location, x=500.0))
        self.assertEqual(result["table_fraction"], 1.0)
        self.assertEqual(result["state"], "occupied")

    def test_object_overhanging_cell_boundary_remains_occupied(self):
        # Physical X536..543.5 spans the destination's X540 boundary.
        self.depth[236:244, 544:550] = 450.0
        self.assertEqual(self.assess(location=dict(self.location, x=500.0))["state"], "occupied")

    def test_exit_boundary_and_depth_uncertainty_do_not_confirm_clear(self):
        location = dict(self.location, x=500.0)
        for depth in (340.0 / (2 * (557.5 - 320) / 600), 439.0):
            with self.subTest(depth=depth):
                self.depth[236:244, 558:560] = depth
                self.assertEqual(self.assess(location=location)["state"], "occupied")

    def test_rounded_silhouette_pixels_without_a_full_ray_hit_stay_conservative(self):
        # The rasterized silhouette includes u563, but the true projected
        # boundary is u562.857. Do not infer clearance from this ambiguous rim.
        self.depth[236:244, 563] = 450.0
        self.assertEqual(self.assess(location=dict(self.location, x=500.0))["state"], "occupied")

    def test_tilted_scaled_calibration_excludes_only_confirmed_background_object(self):
        angle = 0.1
        tilt = np.array([[np.cos(angle), 0, np.sin(angle)], [0, 1, 0],
                         [-np.sin(angle), 0, np.cos(angle)]])
        calibration = copy.deepcopy(self.calibration)
        calibration.R = tilt @ calibration.R
        vision = PlacementVision(calibration, self.K)
        yy, xx = np.indices(self.depth.shape)
        rays = np.stack(((xx - 320) / 600, (yy - 240) / 600, np.ones(xx.shape)), axis=-1) @ vision.transform.T
        depth = -vision.translation[2] / rays[..., 2]
        location = dict(self.location, x=500.0)
        point = np.array([557.0, 100.0, 100.0])
        u, v = np.rint(vision.project([point])[0]).astype(int)
        optical_depth = np.linalg.solve(vision.transform, point - vision.translation)[2]
        depth[v-4:v+4, u:u+2] = optical_depth
        self.assertEqual(vision.assess(depth, [], [location])[0]["state"], "clear")
        # Move the same image patch in front of the checked cell.
        depth[v-4:v+4, u:u+2] = optical_depth - 150
        self.assertEqual(vision.assess(depth, [], [location])[0]["state"], "occupied")

    def test_parallel_corner_rays_do_not_divide_by_zero_or_clear_a_missed_cell(self):
        # One corner has both normalized image coordinates exactly zero.
        u, v, depth = np.array([[319.5]]), np.array([[239.5]]), np.array([[450.0]])
        with np.errstate(divide="raise", invalid="raise", over="raise"):
            for location in (self.location, dict(self.location, x=500.0)):
                self.assertFalse(self.vision._clearly_behind_volume(depth, u, v, location).item())

    def test_row_blocks_exactly_preserve_original_ray_decisions(self):
        # Exercise multiple block boundaries and a final partial block, with
        # grazing/parallel rays, invalid depth, and both foreground/background.
        yy, xx = np.indices((79, 233))
        u, v = xx + 319.5, yy + 200.5
        rng = np.random.default_rng(490)
        depth = rng.uniform(200.0, 600.0, xx.shape)
        depth.flat[::43] = np.nan
        depth.flat[::47] = np.inf
        depth.flat[::53] = 0.0
        tilted = copy.deepcopy(self.calibration)
        angle = .1
        tilted.R = np.array([[np.cos(angle), 0, np.sin(angle)], [0, 1, 0],
                             [-np.sin(angle), 0, np.cos(angle)]]) @ tilted.R
        for vision in (self.vision, PlacementVision(tilted, self.K)):
            for location in (self.location, dict(self.location, x=500.0)):
                with self.subTest(tilted=vision is not self.vision, location=location["x"]), \
                        np.errstate(invalid="ignore"):
                    original = vision._clearly_behind_volume_chunk(depth, u, v, location)
                    bounded = vision._clearly_behind_volume(depth, u, v, location)
                    np.testing.assert_array_equal(bounded, original)
        # The fixture must cover both outcomes, rather than agreeing on an
        # entirely conservative all-false result.
        self.assertTrue(original.any())
        self.assertFalse(original.all())

    def test_large_ray_check_uses_small_views_without_copying_source_frames(self):
        depth = np.full((37, 960), 450.0)
        v, u = np.indices(depth.shape)
        original = self.vision._clearly_behind_volume_chunk
        blocks = []

        def check_block(z, x, y, location):
            self.assertLessEqual(z.size, RAY_CHUNK_PIXELS)
            self.assertTrue(np.shares_memory(z, depth))
            self.assertTrue(np.shares_memory(x, u))
            self.assertTrue(np.shares_memory(y, v))
            blocks.append(z.shape[0])
            return original(z, x, y, location)

        with patch.object(self.vision, "_clearly_behind_volume_chunk", side_effect=check_block):
            result = self.vision._clearly_behind_volume(depth, u, v, self.location)
        self.assertEqual(result.shape, depth.shape)
        self.assertEqual(result.dtype, np.bool_)
        self.assertEqual(sum(blocks), depth.shape[0])
        self.assertGreater(len(blocks), 1)

    def test_above_checked_height_inside_xy_is_not_ignored_for_an_upward_camera(self):
        calibration = SimpleNamespace(scale=2.0, R=np.eye(3), t=np.array([315., 83., -140.]),
                                      offset=(-115., 17., 40.))
        vision = PlacementVision(calibration, self.K)
        # Camera is below the table; this point lies beyond the prism at Z300,
        # but still directly above its XY footprint and must not be ignored.
        self.assertFalse(vision._clearly_behind_volume(np.array([[200.]]), np.array([[320.]]),
                                                       np.array([[240.]]), self.location).item())

    def test_yolo_overlap_still_vetoes_depth_that_is_behind_cell(self):
        self.depth[236:244, 558:560] = 450.0
        obj = {"class": "CUP", "bbox_xyxy": [558, 236, 560, 244]}
        self.assertEqual(self.assess(objects=[obj], location=dict(self.location, x=500.0))["state"], "occupied")

    def test_lower_surface_outside_floor_does_not_require_table_agreement(self):
        self.depth[238:242, 346:349] = 530.0
        self.assertEqual(self.assess()["state"], "clear")

    def test_missing_depth_outside_floor_still_blocks_clear(self):
        self.depth[238:242, 346:349] = 0.0
        self.assertEqual(self.assess()["state"], "unknown")

    def test_rejected_cup_base_detection_still_blocks_clear_depth(self):
        obj = {"class": "CUP BASE", "bbox_xyxy": [310, 230, 330, 250],
               "is_cup": False, "cup_height": False, "confidence": 0.2,
               "selectable": False, "blocked_reason": "outside picking workspace"}
        self.assertEqual(self.assess(objects=[obj])["state"], "occupied")

    def test_nonoverlapping_cup_does_not_block_cell(self):
        obj = {"class": "CUP", "bbox_xyxy": [10, 10, 50, 50]}
        self.assertEqual(self.assess(objects=[obj])["state"], "clear")

    def test_missing_depth_cluster_is_unknown_even_with_high_overall_coverage(self):
        self.depth[239:242, 319:322] = 0.0
        result = self.assess()
        self.assertGreater(result["valid_fraction"], 0.995)
        self.assertEqual(result["state"], "unknown")

    def test_distributed_missing_depth_requires_nearly_complete_coverage(self):
        self.depth[219:261:4, 299:341:4] = 0.0
        self.assertEqual(self.assess()["state"], "unknown")

    def test_nonfinite_depth_cannot_confirm_clear(self):
        for value in (np.nan, np.inf):
            with self.subTest(value=value):
                depth = self.depth.copy()
                depth[237:244, 317:324] = value
                with np.errstate(invalid="ignore"):
                    self.assertEqual(self.assess(depth)["state"], "unknown")

    def test_below_table_depth_is_unknown(self):
        self.depth[237:244, 317:324] = 530.0
        self.assertEqual(self.assess()["state"], "unknown")

    def test_out_of_view_or_behind_camera_is_unknown(self):
        self.assertEqual(self.assess(location=dict(self.location, x=2000))["state"], "unknown")
        self.vision = PlacementVision(self.calibration, self.K, table_z=1200)
        self.assertEqual(self.assess()["state"], "unknown")

    def test_depth_shape_mismatch_fails_closed(self):
        with self.assertRaises(SelectionError):
            self.assess(np.full((240, 320), 500.0))

    def test_three_distinct_advancing_clear_frames_combine(self):
        observations = [self.observation() for _ in range(3)]
        snapshot = {"fingerprint": "grid-original", "locations": [self.location]}
        combined = self.vision.combine(snapshot, observations, [100, 101, 105])
        self.assertEqual(combined["slots"][0]["state"], "clear")
        self.assertEqual(combined["frame_sequences"], [100, 101, 105])
        self.assertEqual(combined["frame_count"], 3)
        self.assertFalse(combined["applied"])
        self.assertEqual(combined["released_ids"], [])
        combined["slots"][0]["state"] = "occupied"
        self.assertEqual(observations[-1][0]["state"], "clear")

    def test_any_occupied_observation_blocks_combined_release(self):
        for index in range(3):
            with self.subTest(index=index):
                observations = [self.observation() for _ in range(3)]
                observations[index][0]["state"] = "occupied"
                result = self.vision.combine(self.snapshot, observations, [3, 4, 5])
                self.assertEqual(result["slots"][0]["state"], "occupied")

    def test_any_unknown_observation_prevents_combined_clear(self):
        observations = [self.observation(), self.observation("unknown"), self.observation()]
        result = self.vision.combine(self.snapshot, observations, [3, 4, 5])
        self.assertEqual(result["slots"][0]["state"], "unknown")

    def test_repeat_reordered_or_invalid_sequences_are_rejected(self):
        observations = [self.observation() for _ in range(3)]
        for sequences in ([1, 1, 2], [3, 2, 4], [1, 2], [1, 2, 3, 4],
                          [-1, 0, 1], [True, 2, 3], [1.0, 2, 3]):
            with self.subTest(sequences=sequences), self.assertRaises(SelectionError):
                self.vision.combine(self.snapshot, observations, sequences)

    def test_requires_three_observations(self):
        for count in (0, 1, 2, 4):
            with self.subTest(count=count), self.assertRaises(SelectionError):
                self.vision.combine(self.snapshot, [self.observation() for _ in range(count)], [1, 2, 3])

    def test_changed_slot_id_rejected_between_frames(self):
        observations = [self.observation() for _ in range(3)]
        observations[1][0]["id"] = 2
        with self.assertRaises(SelectionError):
            self.vision.combine(self.snapshot, observations, [1, 2, 3])

    def test_missing_or_additional_slot_rejected_between_frames(self):
        for count in (0, 2):
            with self.subTest(count=count):
                observations = [self.observation() for _ in range(3)]
                observations[1] = [copy.deepcopy(observations[1][0]) for _ in range(count)]
                with self.assertRaises(SelectionError):
                    self.vision.combine(self.snapshot, observations, [1, 2, 3])

    def test_changed_polygon_rejected_between_frames(self):
        observations = [self.observation() for _ in range(3)]
        observations[1][0]["polygon_px"][0][0] += 1
        with self.assertRaises(SelectionError):
            self.vision.combine(self.snapshot, observations, [1, 2, 3])

    def test_consistent_observations_must_match_snapshot_slot_ids(self):
        observations = [self.observation() for _ in range(3)]
        for observation in observations:
            observation[0]["id"] = 2
        with self.assertRaises(SelectionError):
            self.vision.combine(self.snapshot, observations, [1, 2, 3])

    def test_combine_keeps_lowest_depth_coverage(self):
        observations = [self.observation() for _ in range(3)]
        observations[0][0]["valid_fraction"] = 0.996
        result = self.vision.combine(self.snapshot, observations, [1, 2, 3])
        self.assertEqual(result["slots"][0]["valid_fraction"], 0.996)

    def test_reserved_overlay_uses_reservation_color_without_changing_evidence(self):
        slot = self.assess(location=dict(self.location, reserved=True))
        saved = copy.deepcopy(slot)
        image = np.zeros((480, 640, 3), np.uint8)
        rendered = self.vision.draw(image, [slot])
        self.assertIs(rendered, image)
        np.testing.assert_array_equal(rendered[216, 320], [0, 185, 255])
        self.assertEqual(slot, saved)
        self.assertEqual(slot["state"], "clear")

    def test_invalid_overlay_polygons_do_not_change_image(self):
        image = np.zeros((480, 640, 3), np.uint8)
        for polygon in ([], [[np.nan, 1]] * 4, [[1e10, 1e10]] * 4):
            slot = {"id": 1, "state": "unknown", "polygon_px": polygon}
            self.vision.draw(image, [slot])
        self.assertFalse(image.any())


if __name__ == "__main__":
    unittest.main()
