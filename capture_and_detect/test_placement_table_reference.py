"""Frozen floor references require independently empty, repeatable geometry."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

from cup_inventory import SelectionError
from placement_table_reference import TableReference, fit_reference


class TableReferenceTests(unittest.TestCase):
    def setUp(self):
        self.calibration = SimpleNamespace(
            digest="a" * 64, scale=2.0, R=np.diag([1., -1., -1.]),
            t=np.array([315., 83., 960.]), offset=(-115., 17., 40.))
        self.K = {"width": 640, "height": 480, "fx": 600., "fy": 600., "cx": 320., "cy": 240.}
        self.locations = [{"id": 1, "x": 150., "y": 50., "z": 230., "occupied": False},
                          {"id": 2, "x": 250., "y": 50., "z": 230., "occupied": False},
                          {"id": 3, "x": 150., "y": 150., "z": 230., "occupied": False},
                          {"id": 4, "x": 250., "y": 150., "z": 230., "occupied": False}]
        self.source = {"capture": "independent_empty_scan", "depth_sha256": "b" * 64,
                       "frame_sequences": [10, 12, 14]}
        self.slots = [{"id": loc["id"], "state": "clear", "cup_count": 0, "assigned_cups": [],
                       "evidence": [], "cup_excluded_pixels": 0, "cup_shared_excluded_pixels": 0,
                       "visible_fraction": 1., "floor_visible_fraction": 1.,
                       "valid_fraction": 1., "table_fraction": 1.} for loc in self.locations]
        self.depth = self.plane_depth([.02, -.01, -12.])

    def plane_depth(self, plane):
        yy, xx = np.indices((480, 640))
        rays = np.stack(((xx-320)/600, (yy-240)/600, np.ones(xx.shape)), axis=-1)
        direction = rays @ (self.calibration.scale * self.calibration.R).T
        origin = self.calibration.t + np.asarray(self.calibration.offset)
        a, b, c = plane
        return (a*origin[0] + b*origin[1] + c - origin[2]) / (
            direction[:, :, 2] - a*direction[:, :, 0] - b*direction[:, :, 1])

    def fit(self, depth=None, **overrides):
        kwargs = {"calibration": self.calibration, "intrinsics": self.K, "cell_size": 80.,
                  "table_z": 0., "source": self.source, "baseline_slots": self.slots}
        kwargs.update(overrides)
        return fit_reference(self.depth if depth is None else depth, self.locations, **kwargs)

    def load(self, value, **overrides):
        kwargs = {"calibration": self.calibration, "intrinsics": self.K,
                  "locations": self.locations, "cell_size": 80., "table_z": 0.}
        kwargs.update(overrides)
        return TableReference.from_dict(value, **kwargs)

    def test_recovers_spatial_plane_in_physical_millimetres(self):
        value = self.fit()
        reference = self.load(value)
        np.testing.assert_allclose(value["plane_z_mm"], [.02, -.01, -12.], atol=1e-10)
        np.testing.assert_allclose(reference.expected_height([150., 250.], [50., 150.]), [-9.5, -8.5])
        self.assertLess(value["fit"]["rms_mm"], 1e-10)
        self.assertGreater(value["fit"]["inlier_bin_count"], 100)
        self.assertEqual(reference.metadata["source_depth_sha256"], self.source["depth_sha256"])

    def test_reference_is_frozen_and_does_not_refit_from_subsequent_scene(self):
        value = self.fit()
        reference = self.load(value)
        expected = reference.expected_height(200., 100.)
        value["plane_z_mm"][2] = 1000.
        value["source"]["capture"] = "modified"
        metadata = reference.metadata
        metadata["plane_z_mm"][2] = 1000.
        self.assertEqual(reference.expected_height(200., 100.), expected)
        self.assertEqual(reference.metadata["source_capture"], "independent_empty_scan")

    def test_nonzero_nominal_table_height_is_not_subtracted_from_output(self):
        self.depth = self.plane_depth([0., 0., 65.])
        value = self.fit(table_z=75.)
        reference = self.load(value, table_z=75.)
        self.assertAlmostEqual(float(reference.expected_height(200., 100.)), 65.)

    def test_occupancy_reservations_and_messages_do_not_change_geometry_identity(self):
        value = self.fit()
        locations = copy.deepcopy(self.locations)
        for location in locations:
            location.update(occupied=True, reserved=True, reservation="new-token", vision_blocked=True)
        self.load(value, locations=locations)

    def test_calibration_camera_grid_and_table_changes_reject_saved_reference(self):
        value = self.fit()
        for kind in ("digest", "scale", "rotation", "translation", "offset", "intrinsics", "size", "xy", "z", "order", "table"):
            with self.subTest(kind=kind):
                calibration, K, locations = copy.deepcopy(self.calibration), dict(self.K), copy.deepcopy(self.locations)
                kwargs = {"calibration": calibration, "intrinsics": K, "locations": locations}
                if kind == "digest": calibration.digest = "c" * 64
                elif kind == "scale": calibration.scale += .01
                elif kind == "rotation": calibration.R[0, 0] += .01
                elif kind == "translation": calibration.t[0] += 1
                elif kind == "offset": calibration.offset = (-114., 17., 40.)
                elif kind == "intrinsics": K["fx"] += 1
                elif kind == "size": kwargs["cell_size"] = 90.
                elif kind == "xy": locations[0]["x"] += 1
                elif kind == "z": locations[0]["z"] += 1
                elif kind == "order": locations.reverse()
                else: kwargs["table_z"] = 1.
                with self.assertRaises(SelectionError): self.load(value, **kwargs)

    def test_unknown_or_partially_hidden_baselines_cannot_train_a_reference(self):
        for key, value in (("state", "unknown"), ("cup_count", 1), ("assigned_cups", [{"location_id": 1}]),
                           ("evidence", ["raised_depth"]), ("cup_excluded_pixels", 1),
                           ("cup_shared_excluded_pixels", 1), ("visible_fraction", .999),
                           ("floor_visible_fraction", .999), ("valid_fraction", .994),
                           ("table_fraction", .994)):
            with self.subTest(key=key):
                slots = copy.deepcopy(self.slots)
                slots[0][key] = value
                with self.assertRaises(SelectionError): self.fit(baseline_slots=slots)

    def test_slot_ids_must_match_the_whole_reference_grid(self):
        with self.assertRaises(SelectionError): self.fit(baseline_slots=self.slots[:-1])
        with self.assertRaises(SelectionError): self.fit(baseline_slots=list(reversed(self.slots)))

    def test_an_existing_correction_cannot_train_its_successor(self):
        slots = copy.deepcopy(self.slots)
        for slot in slots:
            slot["table_reference_applied"] = True
        with self.assertRaises(SelectionError): self.fit(baseline_slots=slots)

    def test_source_must_record_a_known_image_and_three_advancing_frames(self):
        for source in ({}, {**self.source, "capture": ""}, {**self.source, "depth_sha256": "bad"},
                       {**self.source, "frame_sequences": [1, 1, 2]},
                       {**self.source, "frame_sequences": [True, 2, 3]},
                       {**self.source, "extra": float("nan")}):
            with self.subTest(source=source), self.assertRaises(SelectionError): self.fit(source=source)

    def test_missing_depth_cluster_is_rejected_even_if_overall_coverage_is_high(self):
        depth = self.depth.copy()
        depth[268:271, 288:291] = 0.  #Inside P1, only nine pixels.
        with self.assertRaises(SelectionError): self.fit(depth)

    def test_below_table_feature_cannot_be_learned_as_a_surface(self):
        depth = self.depth.copy()
        depth[268:271, 288:291] = 525.  #Z=-50 in this calibrated fixture.
        with self.assertRaises(SelectionError): self.fit(depth)

    def test_sparse_small_noise_does_not_tilt_the_reference_to_an_object(self):
        depth = self.depth.copy()
        depth[260:281:10, 280:301:10] = 490.  #Isolated +20mm points, within old table tolerance.
        value = self.fit(depth)
        np.testing.assert_allclose(value["plane_z_mm"], [.02, -.01, -12.], atol=.03)
        self.assertLess(value["fit"]["rms_mm"], .5)

    def test_large_nonplanar_surface_fails_quality_check(self):
        # Both levels individually satisfy the existing +/-30mm band, but a
        # checkerboard of +20/-20 cannot become a trusted empty-table plane.
        yy, xx = np.indices(self.depth.shape)
        depth = np.where(((xx//12 + yy//12) % 2).astype(bool), 490., 510.)
        with self.assertRaises(SelectionError): self.fit(depth)

    def test_loaded_artifact_cannot_widen_reference_bounds_or_claim_poor_fit(self):
        value = self.fit()
        for kind in ("offset", "tilt", "nan", "rms", "cell_error", "bins", "inlier_fraction", "schema"):
            with self.subTest(kind=kind):
                broken = copy.deepcopy(value)
                if kind == "offset": broken["plane_z_mm"] = [0., 0., -31.]
                elif kind == "tilt": broken["plane_z_mm"] = [.2, 0., -40.]
                elif kind == "nan": broken["plane_z_mm"][0] = float("nan")
                elif kind == "rms": broken["fit"]["rms_mm"] = 5.01
                elif kind == "cell_error": broken["fit"]["max_cell_residual_p99_mm"] = 15.01
                elif kind == "bins": broken["fit"]["inlier_bin_count"] = 2
                elif kind == "inlier_fraction": broken["fit"]["inlier_bin_count"] = 100
                else: broken["schema_version"] = True
                with self.assertRaises(SelectionError): self.load(broken)

    def test_expected_height_cannot_extrapolate_an_arbitrary_negative_plane(self):
        reference = self.load(self.fit())
        np.testing.assert_allclose(reference.expected_height([-1e9, 1e9], 0.), [-30., 30.])
        self.assertTrue(np.isnan(reference.expected_height(np.nan, 0.)))
        self.assertTrue(np.isnan(reference.expected_height(0., np.inf)))

    def test_resource_limit_rejects_before_floor_mask_allocations(self):
        with patch("placement_table_reference.MAX_FIT_PIXELS", 10), \
                patch("placement_table_reference.cv2.fillConvexPoly") as fill:
            with self.assertRaises(SelectionError): self.fit()
        fill.assert_not_called()

    def test_finite_but_overflowing_calibration_cannot_reach_projection(self):
        for kind in ("scale", "origin"):
            with self.subTest(kind=kind):
                calibration = copy.deepcopy(self.calibration)
                if kind == "scale":
                    calibration.scale = 1e308
                else:
                    calibration.t[:] = 1e308
                    calibration.offset = (1e308, 1e308, 1e308)
                with self.assertRaises(SelectionError): self.fit(calibration=calibration)

    def test_fitting_does_not_modify_depth_slots_or_geometry(self):
        originals = self.depth.copy(), copy.deepcopy(self.slots), copy.deepcopy(self.locations)
        self.fit()
        np.testing.assert_array_equal(self.depth, originals[0])
        self.assertEqual(self.slots, originals[1])
        self.assertEqual(self.locations, originals[2])


if __name__ == "__main__":
    unittest.main()
