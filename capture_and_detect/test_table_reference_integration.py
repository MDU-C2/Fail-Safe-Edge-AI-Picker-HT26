"""Frozen floor references never relax raised-object or missing-depth checks."""
import hashlib
import json
import unittest

import numpy as np

from capture_and_detect import CaptureService
from cup_inventory import SelectionError
from placement_table_reference import fit_reference
from placement_vision import PlacementVision
import test_capture_placement as capture_fixtures


class TableReferenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f = capture_fixtures.CapturePlacementTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.calibration.digest = "a" * 64
        self.locations = self.f.grid.snapshot()["locations"]
        self.depth = np.full((480, 640), 1015, dtype=np.uint16)
        self.baseline = PlacementVision(self.f.calibration, self.f.K)
        slots = self.baseline.assess(self.depth, [], self.locations)
        self.reference = fit_reference(
            self.depth, self.locations, calibration=self.f.calibration,
            intrinsics=self.f.K, cell_size=80, source={"capture": "independent-empty-scan",
                "depth_sha256": hashlib.sha256(self.depth.tobytes()).hexdigest(),
                "frame_sequences": [10, 11, 12]}, baseline_slots=slots)
        self.path = self.f.grid.path.with_suffix(".table-reference.json")

    def service(self, *, write=True):
        if write:
            self.path.write_text(json.dumps(self.reference))
        return CaptureService(self.f.camera, self.f.detector, self.f.store, "offline.pt",
                              placing_grid=self.f.grid)

    def assess(self, optical_depth):
        vision = PlacementVision(self.f.calibration, self.f.K, table_reference=self.reference)
        depth = np.full_like(self.depth, optical_depth)
        return vision.assess(depth, [], self.locations)

    def test_corrects_negative_bias_without_changing_original_positive_band(self):
        raw = self.baseline.assess(np.full_like(self.depth, 1035), [], self.locations)
        self.assertTrue(all(s["state"] == "unknown" for s in raw))
        fixed = self.assess(1035)
        self.assertTrue(all(s["state"] == "clear" for s in fixed))
        self.assertTrue(all(s["table_fraction"] == 1 and s["nominal_table_fraction"] == 0
                            and s["table_reference_applied"] for s in fixed))
        # A symmetric shift would falsely reject +25 mm as +40 mm above the
        # measured reference. The positive side stays exactly as before.
        self.assertTrue(all(s["state"] == "clear" for s in self.assess(975)))
        self.assertTrue(all(s["state"] == "occupied" for s in self.assess(950)))

    def test_deep_depressions_and_missing_depth_still_require_inspection(self):
        for optical_depth, reason in ((1100, "table_depth"), (0, "invalid_depth")):
            with self.subTest(optical_depth=optical_depth):
                result = self.assess(optical_depth)
                self.assertTrue(all(s["state"] == "unknown" and s["reason_code"] == reason
                                    for s in result))

    def test_capture_loads_sidecar_and_records_reference_without_grid_writes(self):
        service = self.service()
        grid_before, reference_before = self.f.grid.path.read_bytes(), self.path.read_bytes()
        self.f.camera.capture.side_effect = [
            (rgb, np.full_like(depth, 1035), frame) for rgb, depth, frame in self.f.frames]
        inventory, _ = service.capture("scan")
        observation = inventory["placement_observation"]
        self.assertTrue(all(s["state"] == "clear" for s in observation["slots"]))
        self.assertEqual(observation["table_reference"]["source_capture"], "independent-empty-scan")
        self.assertEqual(observation["table_reference"]["file_sha256"],
                         hashlib.sha256(reference_before).hexdigest())
        self.assertEqual(self.f.grid.path.read_bytes(), grid_before)
        self.assertEqual(self.path.read_bytes(), reference_before)

    def test_reference_change_mid_scan_rejects_before_saving(self):
        service = self.service()
        def capture():
            self.path.write_text(json.dumps({**self.reference, "changed": True}))
            return self.f.frames[0]
        self.f.camera.capture = capture
        with self.assertRaisesRegex(SelectionError, "reference changed"):
            service.capture("verification")
        self.f.store.save_capture.assert_not_called()

    def test_added_or_removed_reference_requires_restart(self):
        service = self.service(write=False)
        self.path.write_text(json.dumps(self.reference))
        with self.assertRaisesRegex(SelectionError, "reference changed"):
            service.capture("verification")
        self.f.camera.capture.assert_not_called()
        service = self.service()
        self.path.unlink()
        with self.assertRaisesRegex(SelectionError, "reference changed"):
            service.capture("verification")
        self.f.camera.capture.assert_not_called()

    def test_geometry_or_calibration_mismatch_cannot_fall_back_to_clear(self):
        for field in ("grid", "calibration"):
            with self.subTest(field=field):
                service = self.service()
                if field == "grid":
                    value = json.loads(self.f.grid.path.read_text())
                    value["locations"][0]["x"] += 1
                    self.f.grid.path.write_text(json.dumps(value))
                else:
                    self.f.calibration.digest = "b" * 64
                self.f.camera.capture.side_effect = self.f.frames
                with self.assertRaisesRegex(SelectionError, "geometry changed"):
                    service.capture("verification")
                self.f.store.save_capture.assert_not_called()

    def test_reservation_changes_do_not_change_reference_identity(self):
        service = self.service()
        value = json.loads(self.f.grid.path.read_text())
        value["locations"][0].update(occupied=True, reserved=True, reservation_id="later-batch")
        self.f.grid.path.write_text(json.dumps(value))
        inventory, _ = service.capture("verification")
        self.assertIn("table_reference", inventory["placement_observation"])
        self.assertTrue(inventory["placement_observation"]["slots"][0]["reserved"])

    def test_malformed_and_oversized_reference_never_silently_disable_checks(self):
        for value in ("null", "[]", "{", "x" * 65537):
            with self.subTest(value=value[:10]):
                self.path.write_text(value)
                with self.assertRaises((SelectionError, ValueError)):
                    self.service(write=False)
        self.f.camera.capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
