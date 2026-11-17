"""Original-main gateway compatibility; every HTTP request is mocked."""

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

from cup_inventory import SelectionError
from gateway_capabilities import ACTIONS, parse_gateway_capabilities
from inference_integration import create_robot
from recovery_journal import RecoveryJournal
from robot_client import RobotClient, RobotCommandError
from robot_controls import RobotControls
from test_web_controller import WebFixture


RUNNING = {"execution_state": "running", "controller_state": "motoron", "operation_mode": "AUTO"}
REMOVED = ("rapid_reset", "rapid_start", "collision_ack", "collision_status")


def original_gateway_schema():
    """Use the restored gateway contract itself, without adding picker extensions."""
    path = Path(__file__).resolve().parents[1] / "gateway/cpp/openapi/openapi.json"
    return json.loads(path.read_text())


def response(payload, status=200, content=b"{}"):
    reply = Mock(status_code=status, content=content, headers={})
    reply.json.return_value = payload
    if status >= 400:
        reply.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
    return reply


class OperatorGatewayTests(unittest.TestCase):
    def setUp(self):
        self.get = self.enterContext(patch("robot_client.requests.get", side_effect=AssertionError("Unexpected HTTP GET")))
        self.post = self.enterContext(patch("robot_client.requests.post", side_effect=AssertionError("No live robot writes")))
        self.robot = RobotClient("http://gateway.invalid:8000/api/v1", {})

    def discover(self, schema):
        reply = response(schema)
        self.get.side_effect = None
        self.get.return_value = reply
        result = self.robot.refresh_operator_capabilities()
        reply.close.assert_called_once()
        return result

    def test_original_gateway_discovery_exposes_only_supported_controls_without_writes(self):
        actions = self.discover(original_gateway_schema())
        self.get.assert_called_once_with("http://gateway.invalid:8000/openapi.json", headers={}, timeout=5,
                                        allow_redirects=False)
        self.assertEqual(set(actions), {"home", "open", "grip", "robot_status", "stop"})
        self.assertTrue(all(value["supported"] is True for value in actions.values()))
        self.assertTrue(self.robot._operator_gateway["protocols"]["blended_path"])
        for action in actions:
            self.robot.require_control_action(action)
        self.post.assert_not_called()

    def test_removed_actions_are_rejected_without_network_even_before_discovery(self):
        for robot in (self.robot, RobotClient(self.robot.api, {}, dry_run=True)):
            for action in (*REMOVED, "move", "raw_endpoint"):
                with self.subTest(action=action, dry_run=robot.dry_run):
                    with self.assertRaisesRegex(RobotCommandError, "not supported"):
                        robot.require_control_action(action)
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_missing_home_route_disables_home_without_disabling_grippers(self):
        schema = original_gateway_schema()
        del schema["paths"]["/api/v1/robot/rws/home"]
        actions = self.discover(schema)
        self.assertFalse(actions["home"]["supported"])
        self.assertTrue(actions["open"]["supported"])
        self.assertTrue(actions["grip"]["supported"])
        with self.assertRaises(RobotCommandError):
            self.robot.require_control_action("home")
        self.post.assert_not_called()

    def test_failed_refresh_disables_prior_manual_support(self):
        for failure in (requests.Timeout("offline"), response(None), response({}, status=302),
                        response(original_gateway_schema(), content=b"x" * 2_000_001),
                        response({"detail": "unavailable"}, status=503)):
            with self.subTest(failure=type(failure).__name__):
                self.discover(original_gateway_schema())
                self.get.side_effect = failure if isinstance(failure, Exception) else None
                self.get.return_value = failure
                result = self.robot.refresh_operator_capabilities()
                for action in ("home", "open", "grip"):
                    self.assertFalse(result[action]["supported"])
                    with self.assertRaises(RobotCommandError):
                        self.robot.require_control_action(action)
                self.assertIsNone(result["robot_status"]["supported"])
        self.post.assert_not_called()

    def test_runtime_status_uses_only_original_status_endpoint_after_failed_discovery(self):
        self.discover(None)
        self.get.reset_mock()
        self.get.return_value = response(RUNNING)
        status = self.robot.read_control_status()
        self.assertEqual({key: status[key] for key in RUNNING}, RUNNING)
        self.assertEqual(set(status), {*RUNNING, "read_at"})
        self.get.assert_called_once_with(self.robot.api + "/robot/rapid", headers={}, timeout=5)
        self.post.assert_not_called()

    def test_status_never_returns_removed_gateway_fields(self):
        self.get.side_effect = None
        self.get.return_value = response({**RUNNING, "collision_state": "TRIGGERED", "stop_generation": 8})
        status = self.robot.read_control_status()
        self.assertEqual(set(status), {*RUNNING, "read_at"})
        self.assertEqual(self.get.call_count, 1)
        self.post.assert_not_called()

    def test_incomplete_status_is_rejected_without_extra_endpoint_probes(self):
        for field in RUNNING:
            with self.subTest(field=field):
                status = dict(RUNNING)
                del status[field]
                self.get.side_effect = None
                self.get.return_value = response(status)
                with self.assertRaisesRegex(RobotCommandError, "incomplete"):
                    self.robot.read_control_status()
        self.assertEqual(self.get.call_count, 3)
        self.post.assert_not_called()

    def test_legacy_controller_profile_is_ignored_and_dry_run_uses_no_network(self):
        self.get.side_effect = None
        self.get.return_value = response(original_gateway_schema())
        args = SimpleNamespace(live=True, controller_profile="missing-profile.json")
        robot = create_robot(args, self.robot.api, {})
        self.assertEqual(set(robot.operator_capabilities), set(ACTIONS))
        self.assertTrue(robot.operator_capabilities["home"]["supported"])
        self.get.assert_called_once_with("http://gateway.invalid:8000/openapi.json", headers={}, timeout=5,
                                        allow_redirects=False)
        self.get.reset_mock()
        args.live = False
        robot = create_robot(args, self.robot.api, {})
        self.assertEqual(set(robot.operator_capabilities), set(ACTIONS))
        self.assertTrue(all(value["supported"] for value in robot.operator_capabilities.values()))
        self.assertNotIn("collision_state", robot.read_control_status())
        self.get.assert_not_called()
        self.post.assert_not_called()


class OperatorCapabilityConfirmationTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.fixture = WebFixture(directory, dry_run=False)
        self.session = self.fixture.session
        self.session.recovery = RecoveryJournal(Path(directory) / "recovery.json")
        self.robot = RobotClient("http://gateway.invalid/api/v1", {})
        self.session.robot = self.robot
        self.controls = RobotControls(self.session)
        self.schema = original_gateway_schema()
        self.requests = []
        self.home_reply = {"accepted": True, "message": "DONE"}
        self.get = self.enterContext(patch("robot_client.requests.get", side_effect=self.serve_get))
        self.post = self.enterContext(patch("robot_client.requests.post", side_effect=self.serve_post))

    def serve_get(self, url, **kwargs):
        self.requests.append(("GET", url, kwargs))
        if url.endswith("/openapi.json"):
            return response(self.schema)
        if url.endswith("/robot/rapid"):
            return response(RUNNING)
        if url.endswith("/joints"):
            arm = "left" if "/left/" in url else "right"
            return response({"arm": arm, "joints": list(self.robot.HOME_JOINTS),
                             "arm_angle": self.robot.HOME_ARM_ANGLES[arm]})
        raise AssertionError(f"Unexpected GET {url}")

    def serve_post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        self.assertNotIn("X-Control-Session", kwargs.get("headers", {}))
        if url.endswith(("/control/acquire", "/control/renew")):
            now = datetime.now(timezone.utc)
            return response({"success": True, "lease": {"active": True, "client_id": "operator",
                "acquired_at": now.isoformat(), "expires_at": (now + timedelta(seconds=60)).isoformat()}})
        if url.endswith("/control/release"):
            return response({"success": True, "lease": {"active": False}})
        if url.endswith("/robot/rws/home"):
            return response(copy.deepcopy(self.home_reply))
        if url.endswith("/robot/rws/gripper"):
            return response({"accepted": True, "message": "DONE"})
        if url.endswith("/robot/rapid/stop"):
            return response({**RUNNING, "execution_state": "stopped"})
        raise AssertionError(f"Unexpected POST {url}")

    def test_original_schema_manual_both_home_verifies_joints_and_releases_client_lease(self):
        self.robot.configure_operator_controls()
        self.controls.propose("home", "both")
        self.post.assert_not_called()
        self.controls.confirm(self.controls.pending["token"])
        posts = [(url, kwargs) for method, url, kwargs in self.requests if method == "POST"]
        self.assertEqual([url.rsplit("/", 1)[-1] for url, _ in posts], ["acquire", "home", "home", "release"])
        self.assertEqual([kwargs["json"]["arm"] for url, kwargs in posts if url.endswith("/home")], ["right", "left"])
        self.assertEqual([kwargs["json"]["speed"] for url, kwargs in posts if url.endswith("/home")], [100, 100])
        self.assertEqual(sum(url.endswith("/robot/rapid") for method, url, _ in self.requests if method == "GET"), 2)
        self.assertEqual(sum(url.endswith("/joints") for method, url, _ in self.requests if method == "GET"), 2)
        self.assertFalse(self.session.motion_uncertain)
        self.assertFalse(self.session.recovery.required())

    def test_original_schema_manual_grippers_complete_without_collision_metadata(self):
        self.robot.configure_operator_controls()
        self.session.execute_control("open", "both")
        sent = [call.kwargs["json"] for call in self.post.call_args_list if call.args[0].endswith("/gripper")]
        self.assertEqual(sent, [{"arm": "left", "wait": True, "action": "open"},
                                {"arm": "right", "wait": True, "action": "open"}])
        self.assertFalse(self.session.recovery.required())

    def test_original_gateway_stop_uses_ordinary_lease_without_discovery(self):
        result = self.robot.request_stop()
        self.assertTrue(result["stopped"])
        self.assertTrue(self.robot.stop_requested)
        self.get.assert_not_called()
        self.assertEqual([call.args[0].rsplit("/", 1)[-1] for call in self.post.call_args_list],
                         ["acquire", "stop", "release"])

    def test_removed_actions_are_rejected_before_discovery_or_control_requests(self):
        for action in REMOVED:
            with self.subTest(action=action):
                with self.assertRaises(SelectionError):
                    self.controls.propose(action, None)
                with self.assertRaises(SelectionError):
                    self.session.execute_control(action, None)
        self.get.assert_not_called()
        self.post.assert_not_called()
        self.assertFalse(self.session.recovery.path.exists())

    def test_failed_discovery_blocks_manual_controls_but_status_refresh_can_recover(self):
        self.schema = None
        self.controls.refresh_status()
        self.assertEqual({key: self.controls.status[key] for key in RUNNING}, RUNNING)
        self.assertIsNone(self.controls.capabilities()["robot_status"]["supported"])
        with self.assertRaises(SelectionError):
            self.controls.propose("home", "both")
        with self.assertRaises(RobotCommandError):
            self.session.execute_control("home", "both")
        self.post.assert_not_called()
        self.assertFalse(self.session.recovery.path.exists())
        self.schema = original_gateway_schema()
        self.controls.refresh_status()
        self.assertTrue(self.controls.capabilities()["home"]["supported"])
        self.controls.propose("home", "both")
        self.assertEqual(self.controls.pending["action"], "home")
        self.post.assert_not_called()

    def test_lost_capability_after_preview_blocks_confirmation_without_recovery_journal(self):
        self.robot.configure_operator_controls()
        self.controls.propose("home", "both")
        token = self.controls.pending["token"]
        del self.schema["paths"]["/api/v1/robot/rws/home"]
        self.controls.refresh_status()
        with self.assertRaises(SelectionError):
            self.controls.confirm(token)
        self.assertIsNone(self.controls.pending)
        self.post.assert_not_called()
        self.assertFalse(self.session.recovery.path.exists())

    def test_unconfirmed_first_home_prevents_second_and_preserves_recovery(self):
        self.robot.configure_operator_controls()
        self.home_reply = {"accepted": False, "message": "ERR unreachable"}
        with self.assertRaisesRegex(RobotCommandError, "ERR unreachable"):
            self.session.execute_control("home", "both")
        self.assertEqual([call.args[0].rsplit("/", 1)[-1] for call in self.post.call_args_list],
                         ["acquire", "home", "release"])
        self.assertTrue(self.session.recovery.required())
        self.assertTrue(self.session.motion_uncertain)


if __name__ == "__main__":
    unittest.main()
