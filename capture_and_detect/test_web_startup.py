"""Startup diagnostics use fake camera/model services and never contact hardware."""

from contextlib import redirect_stderr
import io
import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from camera_client import CameraError
from web_app import hardware_factory
from web_controller import WebController


class WebStartupTests(unittest.TestCase):
    def setUp(self):
        network = patch("requests.Session.request", side_effect=AssertionError("Network forbidden"))
        self.request = network.start()
        self.addCleanup(network.stop)
        self.args = SimpleNamespace(gateway="http://offline.invalid", calibration="unused.json")

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
        with patch("capture_and_detect.load_token", return_value="private-gateway-token"), \
                patch("capture_and_detect.Calibration"), \
                patch("capture_and_detect.load_model") as load_model, \
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
            "Reading camera configuration from the Pi…",
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
        with patch("capture_and_detect.load_token", return_value="private-gateway-token"), \
                patch("capture_and_detect.Calibration", return_value=calibration), \
                patch("camera_client.CameraClient", return_value=camera), \
                patch("inference_integration.create_detector", side_effect=SystemExit("Model not found: best.pt")) as detector:
            state, output = self.start(hardware_factory(self.args))

        self.assertEqual(detector.call_args.args, (self.args, calibration, intrinsics))
        self.assertEqual([event["message"] for event in state["events"]], [
            "Loading gateway credentials and calibration…",
            "Reading camera configuration from the Pi…",
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


if __name__ == "__main__":
    unittest.main()
