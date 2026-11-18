"""Camera-only experiment never imports robot control or sends mutations."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from camera_client import CameraError
from check_inference_switching import ReadOnlyCameraSession, one_observation, run_check
from inference_protocol import decode_request, make_response, make_settings


class CheckInferenceSwitchingTests(unittest.TestCase):
    def camera(self, bad_depth=False):
        rgb = np.arange(45, dtype=np.uint8).reshape(3, 5, 3)
        depth = np.full((2, 5) if bad_depth else (3, 5), 700, dtype=np.uint16)
        return SimpleNamespace(capture=Mock(return_value=(rgb, depth, {"sequence": 8, "timestamp": "frame-time"})),
                               session=SimpleNamespace(close=Mock()))

    def supervisor(self):
        supervisor = Mock()
        def predict(request):
            np.testing.assert_array_equal(decode_request(request), np.arange(45, dtype=np.uint8).reshape(3, 5, 3))
            result = make_response(request, [])
            result["_inference"] = {"provider": "pi", "duration_ms": 15}
            return result
        supervisor.predict.side_effect = predict
        supervisor.snapshot.return_value = {"counters": {"mac": 0, "pi": 1}, "recovery": {"successes": 0}}
        return supervisor

    def test_one_observation_uses_exact_frame_and_retains_depth_dimensions(self):
        camera, supervisor = self.camera(), self.supervisor()
        record = one_observation(camera, supervisor, "a" * 64, make_settings())
        self.assertEqual((record["sequence"], record["provider"]), (8, "pi"))
        self.assertEqual(record["depth_shape"], [3, 5])
        self.assertEqual(record["valid_depth_pixels"], 15)
        self.assertEqual(record["counters"], {"mac": 0, "pi": 1})
        camera.capture.assert_called_once_with()

    def test_mismatched_depth_prevents_inference(self):
        supervisor = self.supervisor()
        with self.assertRaises(CameraError):
            one_observation(self.camera(bad_depth=True), supervisor, "a" * 64, make_settings())
        supervisor.predict.assert_not_called()

    def test_transport_allows_only_camera_reads_and_disables_redirects(self):
        session = ReadOnlyCameraSession("http://gateway/api/v1")
        session._session.close()
        session._session = Mock()
        session.get("http://gateway/api/v1/camera/rgb", headers={})
        session._session.get.assert_called_once_with("http://gateway/api/v1/camera/rgb", allow_redirects=False, headers={})
        with self.assertRaises(CameraError):
            session.get("http://gateway/api/v1/robot/rws/status")
        self.assertFalse(hasattr(session, "post"))
        self.assertFalse(hasattr(session, "request"))

    def test_capture_failure_closes_resources_and_saves_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            args = SimpleNamespace(output=folder, observations=2, interval=2, mac_timeout=2,
                                   pi_timeout=5, probe_interval=3)
            camera = self.camera()
            camera.capture.side_effect = CameraError("Synthetic camera failure")
            local, remote, supervisor = Mock(model_hash="a" * 64), Mock(), self.supervisor()
            exit_code = run_check(args, camera=camera, local=local, remote=remote,
                                 supervisor_factory=Mock(return_value=supervisor), emit=Mock(), sleep=Mock())
            self.assertEqual(exit_code, 1)
            local.start.assert_called_once_with()
            supervisor.close.assert_called_once_with()
            camera.session.close.assert_called_once_with()
            summary = json.loads((Path(folder) / "summary.json").read_text())
            self.assertEqual(summary["scope"], "camera-only")
            self.assertEqual(summary["outcome"], "failed")
            self.assertEqual(summary["observations_accepted"], 0)


if __name__ == "__main__":
    unittest.main()
