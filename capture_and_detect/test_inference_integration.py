"""Fallback uses retained depth and never weakens cup/robot checks; offline only."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from camera_client import CameraError
from capture_and_detect import CaptureService
from inference_integration import (ProviderDetector, add_inference_arguments,
                                    validate_inference_arguments, create_robot)
from inference_protocol import make_response, make_settings, validate_response
from inference_supervisor import InferenceSupervisor
from robot_client import RobotClient, RobotCommandError
from test_capture_placement import CapturePlacementTests
from web_controller import WebController


class Backend:
    def __init__(self, detections=None, failures=()):
        self.detections = detections or []
        self.calls = []
        self.failures = set(failures)

    def predict(self, request, timeout_s):
        self.calls.append(copy.deepcopy(request))
        if len(self.calls) in self.failures:
            raise TimeoutError("Injected worker timeout")
        return make_response(request, self.detections, inference_ms=10)

    def close(self):
        pass


class ProviderIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.rgb = np.zeros((31, 31, 3), dtype=np.uint8)
        self.depth = np.full((31, 31), 700, dtype=np.uint16)
        self.mask = np.zeros((31, 31), dtype=bool)
        self.mask[5:26, 5:26] = True
        self.detections = [{"class_name": "CUP BASE", "confidence": .9,
                            "bbox_xyxy": [5, 5, 26, 26], "mask": self.mask}]
        self.calibration = SimpleNamespace(to_robot=Mock(return_value=(500, 100, 100)))
        self.K = {"width": 31, "height": 31, "cx": 15, "cy": 15, "fx": 100, "fy": 100}

    def detector(self, local, remote=None):
        supervisor = InferenceSupervisor(local, remote, validate_response=validate_response,
                                         probe_interval_s=600)
        self.addCleanup(supervisor.close)
        return ProviderDetector(supervisor, "a" * 64, make_settings(), self.calibration, self.K)

    def test_mac_and_pi_results_use_identical_local_depth_geometry(self):
        local, remote = Backend(self.detections), Backend(self.detections, failures=(2,))
        detector = self.detector(local, remote)
        first, _ = detector.analyse_observation(self.rgb, self.depth, {"sequence": 10})
        self.assertEqual(detector.inference_metadata["provider"], "mac")
        second, _ = detector.analyse_observation(self.rgb, self.depth, {"sequence": 11})
        self.assertEqual(detector.inference_metadata["provider"], "pi")
        self.assertEqual(first, second)
        self.assertEqual(remote.calls[1], local.calls[0])
        self.calibration.to_robot.assert_called_with(0, 0, 700)

    def test_fallback_does_not_make_invalid_depth_usable(self):
        detector = self.detector(Backend(self.detections), Backend(failures=(1,)))
        self.depth[10:21, 10:21] = 0
        objects, _ = detector.analyse(self.rgb, self.depth)
        self.assertFalse(objects[0]["depth_quality"]["valid"])
        self.assertIsNone(objects[0]["robot_z_mm"])
        self.calibration.to_robot.assert_not_called()

    def test_both_workers_fail_without_coordinates(self):
        detector = self.detector(Backend(failures=(1,)), Backend(failures=(1,)))
        with self.assertRaises(CameraError):
            detector.analyse(self.rgb, self.depth)
        self.calibration.to_robot.assert_not_called()
        self.assertEqual(detector.supervisor.snapshot()["counters"], {"mac": 0, "pi": 0})

    def test_empty_success_does_not_fall_back(self):
        local, remote = Backend(self.detections), Backend()
        detector = self.detector(local, remote)
        objects, _ = detector.analyse(self.rgb, self.depth)
        self.assertEqual(objects, [])
        self.assertEqual(local.calls, [])


class GroupIntegrationTests(CapturePlacementTests):
    def test_scan_restarts_evidence_when_mac_fails_after_first_frame(self):
        local, remote = Backend(), Backend(failures=(2,))
        supervisor = InferenceSupervisor(local, remote, validate_response=validate_response,
                                         probe_interval_s=600)
        self.addCleanup(supervisor.close)
        self.calibration.to_robot = Mock(return_value=(500, 100, 100))
        detector = ProviderDetector(supervisor, "a" * 64, make_settings(), self.calibration, self.K)
        self.service.detector = detector
        self.camera.capture.side_effect = [self.frame(i) for i in (10, 11, 12, 13)]
        result, _ = self.service.capture("scan")
        self.assertEqual(result["placement_observation"]["frame_sequences"], [11, 12, 13])
        self.assertEqual(len(result["model"]["observation_results"]), 3)
        self.assertEqual({r["provider"] for r in result["model"]["observation_results"]}, {"pi"})
        self.assertEqual(result["model"]["inference"]["provider"], "pi")
        self.store.save_capture.assert_called_once()


class SessionClientTests(unittest.TestCase):
    @staticmethod
    def reply(session_id="abc123"):
        return {"success": True, "lease": {"active": True, "session_id": session_id}}

    def test_new_client_requires_session_and_legacy_still_works(self):
        client = RobotClient("http://offline.invalid/api/v1", {}, require_session=True)
        with self.assertRaisesRegex(RobotCommandError, "execution sessions"):
            client._record_lease({"success": True, "lease": {"active": True}}, {})
        legacy = RobotClient("http://offline.invalid/api/v1", {})
        legacy._record_lease({"success": True, "lease": {"active": True}}, {})

    def test_session_header_sent_on_mutations_but_not_acquire(self):
        client = RobotClient("http://offline.invalid/api/v1", {"Authorization": "Bearer fake"})
        client._record_lease(self.reply(), {})
        response = Mock(headers={}, status_code=200)
        response.json.return_value = {"success": True}
        with patch("robot_client.requests.post", return_value=response) as post:
            for endpoint in ("/control/renew", "/control/release", "/robot/rapid/stop", "/robot/rws/gripper"):
                client._post(endpoint)
                self.assertEqual(post.call_args.kwargs["headers"]["X-Control-Session"], "abc123")
            client._post("/control/acquire")
            self.assertNotIn("X-Control-Session", post.call_args.kwargs["headers"])
        self.assertNotIn("X-Control-Session", client.headers)

    def test_renewal_cannot_change_session(self):
        client = RobotClient("http://offline.invalid/api/v1", {})
        client._record_lease(self.reply(), {})
        with self.assertRaisesRegex(RobotCommandError, "session changed"):
            client._record_lease(self.reply("new-session"), {})

    def test_live_failover_requires_gateway_capability_without_moving(self):
        args = SimpleNamespace(live=True, mac_inference_url="http://mac.invalid:8090")
        with patch.object(RobotClient, "_get_object", return_value={}) as read:
            with self.assertRaisesRegex(RobotCommandError, "updated gateway"):
                create_robot(args, "http://pi.invalid/api/v1", {})
            read.assert_called_once_with("/control")

    def test_dry_run_does_not_probe_robot_control(self):
        args = SimpleNamespace(live=False, mac_inference_url="http://mac.invalid:8090")
        with patch.object(RobotClient, "_get_object") as read:
            robot = create_robot(args, "http://pi.invalid/api/v1", {})
            self.assertTrue(robot.dry_run)
            read.assert_not_called()


class PublishedInferenceTests(unittest.TestCase):
    def test_out_of_order_probe_callback_cannot_overwrite_new_status(self):
        controller = WebController(lambda _: None)
        self.addCleanup(controller.close)
        controller.publish_inference({"session_id": "one", "revision": 3, "active_provider": "pi"})
        controller.publish_inference({"session_id": "one", "revision": 2, "active_provider": "mac"})
        self.assertEqual(controller.state()["inference"]["active_provider"], "pi")
        self.assertEqual(controller.status()["inference"]["revision"], 3)


class InferenceArgumentTests(unittest.TestCase):
    def test_backend_deadline_limits_are_rejected_before_startup(self):
        parser = argparse.ArgumentParser()
        add_inference_arguments(parser)
        for flag in ("--mac-timeout", "--pi-timeout", "--inference-startup-timeout"):
            for value in ("0", "-1", "nan", "inf", "301"):
                with self.subTest(flag=flag, value=value), redirect_stderr(io.StringIO()):
                    args = parser.parse_args([flag, value])
                    with self.assertRaises(SystemExit):
                        validate_inference_arguments(args, parser)
            validate_inference_arguments(parser.parse_args([flag, "300"]), parser)

    def test_probe_interval_can_exceed_a_single_request_deadline(self):
        parser = argparse.ArgumentParser()
        add_inference_arguments(parser)
        validate_inference_arguments(parser.parse_args(["--mac-probe-interval", "600"]), parser)


if __name__ == "__main__":
    unittest.main()
