"""Keep the original V4 terminal entry point separate from the UI workflow.

The hashes pin the source files from vision_detection4 commit
52f54791e08709ce8e44d1f39b5687085ba8af21.  These tests do not require git,
Ultralytics, model weights, a gateway, or a robot.
"""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import copy
import hashlib
import importlib.util
import io
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


HERE = Path(__file__).resolve().parent
V4_SOURCE_HASHES = {
    "capture_and_detect.py": "f0f8402368f5d87b1cbe96445f9d34d039438be0b2303fc4eaeab96c77c1dd2a",
    "depth_average.py": "bb03d016435202c2618de394bad169e255e9c1fadecafa6b27e6fcad2f082190",
}


def load_v4_terminal():
    """Import in isolation, without replacing another test's picker module."""
    depth_spec = importlib.util.spec_from_file_location(
        "_v4_terminal_test_depth", HERE / "depth_average.py"
    )
    depth = importlib.util.module_from_spec(depth_spec)
    depth_spec.loader.exec_module(depth)
    ultralytics = types.ModuleType("ultralytics")
    ultralytics.YOLO = Mock(side_effect=AssertionError("Model loading is not part of these tests"))
    picker_spec = importlib.util.spec_from_file_location(
        "_v4_terminal_test_picker", HERE / "capture_and_detect.py"
    )
    picker = importlib.util.module_from_spec(picker_spec)
    with patch.dict(sys.modules, {"ultralytics": ultralytics, "depth_average": depth}):
        picker_spec.loader.exec_module(picker)
    return picker


def base(confidence, x):
    return {
        "class": "CUP BASE",
        "confidence": confidence,
        "is_cup": True,
        "robot_x_mm": x,
        "robot_y_mm": 50.0,
        "robot_z_mm": 30.0,
        "diameter_mm": 65.0,
    }


class V4SourceParityTests(unittest.TestCase):
    def test_terminal_sources_are_byte_for_byte_original_v4(self):
        for name, expected in V4_SOURCE_HASHES.items():
            with self.subTest(file=name):
                self.assertEqual(hashlib.sha256((HERE / name).read_bytes()).hexdigest(), expected)


class V4TerminalTests(unittest.TestCase):
    def setUp(self):
        self.v4 = load_v4_terminal()
        self.output = io.StringIO()
        self.addCleanup(patch.stopall)
        patch.object(self.v4, "log").start()
        patch.object(self.v4, "print_objects").start()
        patch.object(
            self.v4.requests, "get", side_effect=AssertionError("Unexpected gateway access")
        ).start()
        patch.object(
            self.v4.requests, "post", side_effect=AssertionError("Unexpected gateway access")
        ).start()

    def workflow(self, *, left_only=False, outcomes=None):
        """Exercise the real Picker and find_cups using in-memory boundaries."""
        events = []
        objects = [base(0.60, 500.0), base(0.95, 600.0), base(0.80, 900.0), base(0.75, 550.0)]
        gateway = Mock()

        @contextmanager
        def control():
            events.append("acquire")
            try:
                yield
            finally:
                events.append("release")

        gateway.control = control
        detector = Mock(K={"fx": 1.0, "fy": 1.0, "cx": 0.0, "cy": 0.0})
        detector.analyse.return_value = (objects, "annotated")
        handover = Mock()
        results = iter(outcomes) if outcomes is not None else None

        def move(cup, only_left, place):
            events.append(("move", cup["x"], only_left, None if place is None else place["id"]))
            return True if results is None else next(results)

        handover.run.side_effect = move
        snapshots = []
        store = Mock(folder="in-memory")
        store.save_cups.side_effect = lambda cups, stamp: snapshots.append(copy.deepcopy(cups))
        places = {"locations": [{"id": i, "occupied": False} for i in range(1, 5)]}
        load_places = patch.object(self.v4, "load_places", return_value=places).start()
        save_places = patch.object(self.v4, "save_places").start()

        def capture(gateway_arg, count):
            self.assertIs(gateway_arg, gateway)
            self.assertEqual(count, 5)
            events.append("capture")
            return "rgb", "depth", ["one", "two", "three", "four", "five"]

        capture_averaged = patch.object(
            self.v4.depth_average, "capture_averaged", side_effect=capture
        ).start()
        picker = self.v4.Picker(gateway, detector, handover, store, left_only)
        return types.SimpleNamespace(
            picker=picker, events=events, handover=handover, store=store, snapshots=snapshots,
            places=places, load_places=load_places, save_places=save_places,
            capture=capture_averaged, detector=detector,
        )

    def test_enter_captures_once_then_moves_every_reachable_cup_in_confidence_order(self):
        run = self.workflow()
        with patch("builtins.input", side_effect=["", "q"]):
            run.picker.run()

        self.assertEqual(run.picker.captures, 1)
        self.assertEqual(run.capture.call_count, 1)
        self.assertEqual(run.detector.analyse.call_count, 1)
        self.assertEqual(run.events, [
            "capture", "acquire", ("move", 600.0, False, 1), "release",
            "acquire", ("move", 550.0, False, 2), "release",
            "acquire", ("move", 500.0, False, 3), "release",
        ])
        self.assertEqual(
            [(cup["x"], cup["status"]) for cup in run.snapshots[-1]],
            [(600.0, "placed"), (900.0, "out of reach"), (550.0, "placed"), (500.0, "placed")],
        )
        self.assertEqual([place["occupied"] for place in run.places["locations"]],
                         [True, True, True, False])
        self.assertEqual(run.save_places.call_count, 3)
        self.assertEqual(run.store.save_grip.call_count, 3)

    def test_next_enter_is_the_only_trigger_for_another_capture(self):
        run = self.workflow(left_only=True)
        with patch("builtins.input", side_effect=["", "", "q"]):
            run.picker.run()

        self.assertEqual(run.picker.captures, 2)
        self.assertEqual(run.capture.call_count, 2)
        self.assertEqual(run.handover.run.call_count, 6)
        self.assertEqual([index for index, event in enumerate(run.events) if event == "capture"],
                         [0, 10])

    def test_failed_cup_stops_the_batch_without_recapture_or_occupying_its_destination(self):
        run = self.workflow(outcomes=[True, False])
        run.picker.capture_once()

        self.assertEqual(run.capture.call_count, 1)
        self.assertEqual(run.handover.run.call_count, 2)
        self.assertEqual([cup["status"] for cup in run.snapshots[-1]],
                         ["placed", "out of reach", "failed", "waiting"])
        self.assertEqual([place["occupied"] for place in run.places["locations"]],
                         [True, False, False, False])
        self.assertEqual(run.save_places.call_count, 1)

    def test_left_only_uses_the_same_capture_without_loading_or_writing_a_placing_grid(self):
        run = self.workflow(left_only=True)
        run.picker.capture_once()

        self.assertEqual(run.capture.call_count, 1)
        self.assertEqual([event for event in run.events if isinstance(event, tuple)], [
            ("move", 600.0, True, None), ("move", 550.0, True, None), ("move", 500.0, True, None),
        ])
        run.load_places.assert_not_called()
        run.save_places.assert_not_called()

    def test_main_constructs_the_original_gateway_arms_handover_and_picker(self):
        calibration = types.SimpleNamespace(offset=(11.0, 22.0, 0.0))
        model = types.SimpleNamespace(names={0: "CUP", 1: "CUP BASE", 2: "CUP HANDLE"})
        intrinsics = {"fx": 600.0, "fy": 600.0, "cx": 320.0, "cy": 240.0}
        captured = []

        def record_run(picker):
            captured.append(picker)

        with (
            patch.object(sys, "argv", ["capture_and_detect.py", "--gateway", "http://v4.invalid:8000",
                                       "--conf", "0.7", "--left-only", "--camera-z", "--depth-frames", "3"]),
            patch.object(self.v4, "load_token", return_value="offline-test-token"),
            patch.object(self.v4, "CaptureStore", return_value=Mock(folder="in-memory")) as store,
            patch.object(self.v4, "Calibration", return_value=calibration) as load_calibration,
            patch.object(self.v4, "load_model", return_value=model),
            patch.object(self.v4.Gateway, "intrinsics", return_value=intrinsics),
            patch.object(self.v4.Picker, "run", record_run),
        ):
            self.v4.main()

        self.assertEqual(len(captured), 1)
        picker = captured[0]
        self.assertIs(type(picker), self.v4.Picker)
        self.assertIs(type(picker.gateway), self.v4.Gateway)
        self.assertEqual(picker.gateway.api, "http://v4.invalid:8000/api/v1")
        self.assertIs(type(picker.detector), self.v4.Detector)
        self.assertIs(picker.detector.model, model)
        self.assertEqual(picker.detector.conf, 0.7)
        self.assertIs(type(picker.handover), self.v4.Handover)
        self.assertIs(type(picker.handover.left), self.v4.Arm)
        self.assertIs(type(picker.handover.right), self.v4.Arm)
        self.assertIs(picker.handover.left.gateway, picker.gateway)
        self.assertIs(picker.handover.right.gateway, picker.gateway)
        self.assertEqual(picker.handover.left.offset, calibration.offset)
        self.assertEqual(picker.handover.right.offset, (0, 0, 0))
        self.assertTrue(picker.left_only)
        self.assertTrue(self.v4.USE_CAMERA_Z)
        self.assertEqual(self.v4.DEPTH_FRAMES, 3)
        store.assert_called_once_with(str(HERE / "Data" / "Detections"))
        load_calibration.assert_called_once_with(HERE.parent / "Calibration/calibration.json")

    def test_help_exposes_the_original_terminal_flags_without_starting_hardware(self):
        with (
            patch.object(sys, "argv", ["capture_and_detect.py", "--help"]),
            patch.object(self.v4, "load_token") as token,
            redirect_stdout(self.output),
            self.assertRaises(SystemExit) as exited,
        ):
            self.v4.main()
        self.assertEqual(exited.exception.code, 0)
        for option in ("--output", "--gateway", "--conf", "--left-only", "--camera-z", "--depth-frames"):
            self.assertIn(option, self.output.getvalue())
        self.assertNotIn("--web", self.output.getvalue())
        self.assertNotIn("--home-after-cup", self.output.getvalue())
        token.assert_not_called()

    def test_web_flag_is_rejected_by_the_original_terminal_parser(self):
        with (
            patch.object(sys, "argv", ["capture_and_detect.py", "--web"]),
            patch.object(self.v4, "load_token") as token,
            redirect_stderr(self.output),
            self.assertRaises(SystemExit) as exited,
        ):
            self.v4.main()
        self.assertEqual(exited.exception.code, 2)
        self.assertIn("unrecognized arguments: --web", self.output.getvalue())
        token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
