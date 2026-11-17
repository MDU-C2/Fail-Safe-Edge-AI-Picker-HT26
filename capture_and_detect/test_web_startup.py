"""Startup diagnostics use fake camera/model services and never contact hardware."""

from contextlib import redirect_stderr, redirect_stdout
import io
import os
import time
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from camera_client import CameraError
from test_web_controller import WebFixture
from web_app import hardware_factory, main
from web_controller import WebController


class WebStartupTests(unittest.TestCase):
    def setUp(self):
        network = patch("requests.Session.request", side_effect=AssertionError("Network forbidden"))
        self.request = network.start()
        self.addCleanup(network.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.args = SimpleNamespace(gateway="http://offline.invalid", calibration="unused.json",
                                    placing_grid=str(Path(directory.name) / "placing-grid.json"))

    def start(self, factory):
        controller = WebController(factory, mode="live")
        self.addCleanup(controller.close)
        output = io.StringIO()
        with redirect_stderr(output):
            controller.start()
            deadline = time.monotonic() + 3
            while controller.state()["busy"]:
                if time.monotonic() > deadline:
                    self.fail("Startup did not finish")
                time.sleep(0.005)
        self.request.assert_not_called()
        return controller.state(), output.getvalue()

    def test_camera_failure_is_visible_before_model_loading(self):
        detail = "Camera gateway request /camera/intrinsics failed (ConnectionError)"
        camera = Mock()
        camera.intrinsics.side_effect = CameraError(detail)
        with patch("picker_workflow.load_token", return_value="private-gateway-token"), \
                patch("picker_workflow.Calibration"), \
                patch("picker_workflow.load_model") as load_model, \
                patch("camera_client.CameraClient", return_value=camera), \
                patch("inference_integration.create_detector") as create_detector, \
                patch("inference_integration.create_robot") as create_robot:
            state, output = self.start(hardware_factory(self.args))

        self.assertFalse(state["ready"])
        self.assertFalse(state["busy"])
        self.assertEqual(state["error"], detail)
        self.assertEqual(output, f"Startup failed: {detail}\n")
        self.assertEqual([event["message"] for event in state["events"]], [
            "Loading gateway credentials and calibration…",
            "Reading camera configuration from the gateway…",
            f"Startup failed: {detail}",
        ])
        self.assertNotIn("private-gateway-token", output)
        camera.intrinsics.assert_called_once_with()
        create_detector.assert_not_called()
        load_model.assert_not_called()
        create_robot.assert_not_called()

    def test_model_loading_stage_follows_successful_camera_configuration(self):
        intrinsics = {"fx": 500, "fy": 500}
        camera = Mock()
        camera.intrinsics.return_value = intrinsics
        calibration = Mock()
        with patch("picker_workflow.load_token", return_value="private-gateway-token"), \
                patch("picker_workflow.Calibration", return_value=calibration), \
                patch("camera_client.CameraClient", return_value=camera), \
                patch("inference_integration.create_detector", side_effect=SystemExit("Model not found: best.pt")) as detector:
            state, output = self.start(hardware_factory(self.args))

        self.assertEqual(detector.call_args.args, (self.args, calibration, intrinsics))
        self.assertEqual([event["message"] for event in state["events"]], [
            "Loading gateway credentials and calibration…",
            "Reading camera configuration from the gateway…",
            "Loading detection model…",
            "Startup failed: Model not found: best.pt",
        ])
        self.assertEqual(state["error"], "Model not found: best.pt")
        self.assertIn("Startup failed: Model not found: best.pt", output)
        self.assertNotIn("Traceback", output)

    def test_startup_diagnostics_redact_credentials_from_all_public_outputs(self):
        def failure(controller):
            raise RuntimeError("Gateway failed: gateway-secret; inference-secret; Bearer another-secret")

        with patch.dict(os.environ, {"GATEWAY_TOKEN": "gateway-secret", "INFERENCE_TOKEN": "inference-secret"}):
            state, output = self.start(failure)

        expected = "Gateway failed: [redacted]; [redacted]; Bearer [redacted]"
        self.assertEqual(state["error"], expected)
        self.assertEqual(state["events"][-1]["message"], f"Startup failed: {expected}")
        self.assertEqual(output, f"Startup failed: {expected}\n")

    def test_hardware_factory_handover_finishes_at_clearance_without_automatic_home(self):
        fixture = WebFixture(Path(self.args.placing_grid).parent / "fixture")
        self.args.placing_grid = str(fixture.session.placing_grid.path)
        self.args.output = str(Path(self.args.placing_grid).parent / "captures")
        self.args.grid_table_z, self.args.grid_cell_size = 0, 80
        self.args.speed, self.args.max_result_age = None, 30
        self.args.match_mm, self.args.match_px = 20, 40
        self.args.verification_frames = 1
        calibration = SimpleNamespace(digest="cal-1", offset=(0, 0, 0))
        captures = Mock()
        def capture(purpose):
            import copy
            import test_placement_verification as placement_fixture
            result, _ = fixture.capture(purpose)
            observation = placement_fixture.PlacementVerificationTests.observed()["placement_observation"]
            observation["grid_snapshot"] = fixture.session.placing_grid.snapshot()
            prototype = observation["slots"][0]
            observation["slots"] = [dict(copy.deepcopy(prototype), id=slot["id"])
                                    for slot in observation["grid_snapshot"]["locations"]]
            result["placement_observation"] = observation
            return result, time.monotonic()
        captures.capture.side_effect = capture
        with patch("picker_workflow.load_token", return_value="offline-token"), \
                patch("picker_workflow.Calibration", return_value=calibration), \
                patch("camera_client.CameraClient"), \
                patch("inference_integration.create_detector", return_value=(Mock(), "offline.pt")), \
                patch("inference_integration.create_robot", return_value=fixture.robot), \
                patch("picker_workflow.CaptureService", return_value=captures), \
                redirect_stdout(io.StringIO()):
            session, _ = hardware_factory(self.args)(Mock())
            self.assertFalse(session.home_after_cup)
            self.assertFalse(session.motion_settings()["home_after_cup"])
            scan = session.scan()
            session.execute_selection(action="goto", capture_id=scan["capture"], cup_id=1)
        self.assertEqual(fixture.robot.jobs[-1]["name"], "right_close")
        self.assertIn("left_clear", [job["name"] for job in fixture.robot.jobs])
        self.assertFalse(any("home" in job["name"] for job in fixture.robot.jobs))
        self.request.assert_not_called()


class ManagedStartupCleanupTests(unittest.TestCase):
    def test_failure_before_or_during_server_start_closes_local_resources(self):
        for stage in ("create_app", "process_lock", "server_start"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                resource = Mock()
                controller = SimpleNamespace(close=Mock(side_effect=resource.close))
                process_lock = Mock()
                process_lock.__enter__ = Mock(return_value=None)
                process_lock.__exit__ = Mock(return_value=False)
                if stage == "process_lock":
                    process_lock.__enter__.side_effect = RuntimeError("startup rejected")
                with patch("requests.Session.request", side_effect=AssertionError("Network forbidden")), \
                        patch("device_dashboard.DeviceDashboard.from_config", return_value=controller), \
                        patch("web_app.create_app", side_effect=RuntimeError("startup rejected") if stage == "create_app" else None), \
                        patch("web_app.ProcessLock", return_value=process_lock), \
                        patch("uvicorn.run", side_effect=RuntimeError("startup rejected")), \
                        redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(RuntimeError, "startup rejected"):
                        main(["--device-config", str(Path(temporary) / "devices.json"), "--live"])
                controller.close.assert_called_once_with()
                resource.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
