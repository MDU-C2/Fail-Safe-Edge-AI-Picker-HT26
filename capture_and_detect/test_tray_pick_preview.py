"""Offline checks of rack height arithmetic and strict historical input binding."""

import copy
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tray_pick_preview import PreviewError, calculate_preview, main


class TrayPickPreviewTests(unittest.TestCase):
    def setUp(self):
        self.calibration = json.dumps({"tag_offset": [-115, 0, 40]}).encode()
        self.inventory = {
            "schema_version": 2, "units": "mm", "capture": "archived_scan",
            "saved_at": "2020-01-01T00:00:00+00:00",
            "calibration_sha256": hashlib.sha256(self.calibration).hexdigest(),
            "tag_offset_mm": [-115, 0, 40],
            "cups": [{"id": 2, "x": 600, "y": 100, "z": 119.9,
                      "base_detection_id": 4, "selectable": True, "blocked_reason": None}],
        }
        self.measurements = dict(cup_id=2, cup_top_mm=140, cup_height_mm=95,
                                 cup_circumference_mm=240, open_gripper_width_mm=140,
                                 highest_wire_mm=165, upright_wire_mm=120, clearance_mm=30)

    def preview(self, **changes):
        return calculate_preview(self.inventory, self.calibration, **{**self.measurements, **changes})

    def test_measured_example_distinguishes_support_grip_and_cup_only_lift(self):
        report = self.preview()
        self.assertEqual(report["height_calculation"], {
            "cup_diameter_assuming_circular_mm": 76.394,
            "nominal_support_if_vertical_mm": 45,
            "grip_tcp_height_mm": 120, "grip_flange_height_mm": 279,
            "grip_to_lowest_cup_if_vertical_mm": 75,
            "cup_only_lift_tcp_height_mm": 270,
            "cup_only_lift_flange_height_mm": 429,
            "cup_only_bottom_at_lift_mm": 195,
        })
        comparison = report["archived_depth_comparison"]
        self.assertEqual(comparison["observed_top_robot_z_if_inverted_mm"], 159.9)
        self.assertEqual(comparison["observed_minus_measured_top_mm"], 19.9)
        self.assertEqual(report["status"], "UNVALIDATED_HEIGHT_PREVIEW")
        self.assertFalse(report["live_motion_enabled"])

    def test_explicit_grip_assumption_changes_grip_and_carried_cup_clearance(self):
        heights = self.preview(grip_below_top_mm=10)["height_calculation"]
        self.assertEqual(heights["grip_tcp_height_mm"], 130)
        self.assertEqual(heights["grip_to_lowest_cup_if_vertical_mm"], 85)
        self.assertEqual(heights["cup_only_lift_tcp_height_mm"], 280)
        self.assertEqual(heights["cup_only_bottom_at_lift_mm"], 195)

    def test_lift_never_requests_height_below_initial_grip(self):
        heights = self.preview(cup_top_mm=400)["height_calculation"]
        self.assertEqual(heights["nominal_support_if_vertical_mm"], 305)
        self.assertEqual(heights["cup_only_lift_tcp_height_mm"], 380)

    def test_historical_scan_is_explicit_and_not_treated_as_fresh_authorization(self):
        report = self.preview()
        self.assertEqual(report["archived_saved_at"], "2020-01-01T00:00:00+00:00")
        notes = " ".join(report["blocking_notes"])
        for phrase in ("Historical scan", "CUP-ONLY", "MoveAbsJ", "Placement slots",
                       "lower finger", "tabletop is robot-base Z=0", "not validate"):
            self.assertIn(phrase, notes)

    def test_grip_at_wire_tip_height_is_explicitly_flagged(self):
        comparison = self.preview()["wire_height_comparison"]
        self.assertEqual(comparison["grip_tcp_minus_upright_wire_mm"], 0)
        self.assertEqual(comparison["grip_tcp_minus_highest_wire_mm"], -45)
        self.assertTrue(comparison["grip_at_or_below_upright_tips"])
        self.assertFalse(self.preview(grip_below_top_mm=10)["wire_height_comparison"]["grip_at_or_below_upright_tips"])

    def test_bad_measurements_fail_before_arithmetic(self):
        for name in self.measurements.keys() - {"cup_id"}:
            for value in (None, True, "140", 0, -1, float("nan"), float("inf"), 10 ** 1000):
                with self.subTest(name=name, value=str(value)[:20]), self.assertRaises(PreviewError):
                    self.preview(**{name: value})

    def test_inconsistent_geometry_is_rejected(self):
        for change in ({"cup_top_mm": 90}, {"grip_below_top_mm": 95},
                       {"grip_below_top_mm": 0}, {"highest_wire_mm": 110},
                       {"open_gripper_width_mm": 70}):
            with self.subTest(change=change), self.assertRaises(PreviewError):
                self.preview(**change)

    def test_overflow_in_derived_geometry_is_rejected(self):
        with self.assertRaisesRegex(PreviewError, "Derived geometry"):
            self.preview(highest_wire_mm=1e308, clearance_mm=1e308)

    def test_missing_or_unsupported_inventory_is_rejected(self):
        for inventory in (None, [], {}, {**self.inventory, "schema_version": 1},
                          {**self.inventory, "schema_version": True},
                          {**self.inventory, "units": "cm"},
                          {**self.inventory, "capture": ""}):
            with self.subTest(inventory=inventory), self.assertRaises(PreviewError):
                calculate_preview(inventory, self.calibration, **self.measurements)

    def test_calibration_file_mismatch_and_offset_tampering_are_rejected(self):
        with self.assertRaisesRegex(PreviewError, "hash does not match"):
            calculate_preview(self.inventory, self.calibration + b"\n", **self.measurements)
        self.inventory["tag_offset_mm"] = [-115, 0, 0]
        with self.assertRaisesRegex(PreviewError, "same three finite"):
            self.preview()

    def test_blocked_cup_cannot_be_used_as_nominal_target(self):
        for flag, reason in ((False, "no base"), (True, "outside workspace"), (None, None)):
            self.inventory["cups"][0].update(selectable=flag, blocked_reason=reason)
            with self.subTest(flag=flag, reason=reason), self.assertRaisesRegex(PreviewError, "blocked"):
                self.preview()

    def test_cup_requires_unique_id_base_and_finite_measurement(self):
        for change in ({"z": None}, {"base_detection_id": None}, {"y": float("inf")}):
            cup = copy.deepcopy(self.inventory["cups"][0])
            self.inventory["cups"][0].update(change)
            with self.subTest(change=change), self.assertRaises(PreviewError):
                self.preview()
            self.inventory["cups"][0] = cup
        for cup_id in (True, 0, 1, "2"):
            with self.subTest(cup_id=cup_id), self.assertRaises(PreviewError):
                self.preview(cup_id=cup_id)
        self.inventory["cups"].append(copy.deepcopy(self.inventory["cups"][0]))
        with self.assertRaisesRegex(PreviewError, "duplicate"):
            self.preview()

    def test_calculation_does_not_mutate_inventory(self):
        original = copy.deepcopy(self.inventory)
        self.preview()
        self.assertEqual(self.inventory, original)

    def test_cli_requires_explicit_paths_and_exposes_no_live_option(self):
        for arguments in ([], ["--live"], ["--execute"]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(arguments)
            self.assertEqual(error.exception.code, 2)

    def test_cli_read_only_report_runs_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            scan, calibration = path / "cups.json", path / "calibration.json"
            scan.write_text(json.dumps(self.inventory))
            calibration.write_bytes(self.calibration)
            before = {f.name: f.read_bytes() for f in path.iterdir()}
            arguments = ["--inventory", str(scan), "--calibration", str(calibration)]
            for name, value in self.measurements.items():
                arguments.extend(["--" + name.replace("_", "-"), str(value)])
            output = io.StringIO()
            with patch("socket.socket", side_effect=AssertionError("No network is allowed")), redirect_stdout(output):
                main(arguments)
            report = json.loads(output.getvalue())
            self.assertEqual(report["cup_id"], 2)
            self.assertFalse(report["live_motion_enabled"])
            self.assertEqual({f.name: f.read_bytes() for f in path.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
