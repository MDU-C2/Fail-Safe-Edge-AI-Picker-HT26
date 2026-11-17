"""Offline integration of gateway discovery, controller status and UI controls."""

import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

from cup_inventory import SelectionError
from gateway_capabilities import parse_gateway_capabilities
from inference_integration import create_robot
from recovery_journal import RecoveryJournal
from robot_client import RobotClient, RobotCommandError
from robot_controls import RobotControls
from test_gateway_capabilities import schema_fixture
from test_web_controller import WebFixture


RUNNING = {"execution_state": "running", "controller_state": "motoron", "operation_mode": "AUTO"}


def response(payload, status=200, content=b"{}"):
    reply = Mock(status_code=status, content=content)
    reply.json.return_value = payload
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

    def test_discovery_is_readonly_and_never_gates_explicit_controls(self):
        actions = self.discover(schema_fixture())
        self.get.assert_called_once_with("http://gateway.invalid:8000/openapi.json", headers={}, timeout=5,
                                        allow_redirects=False)
        self.assertTrue(all(value["supported"] for value in actions.values()))
        self.assertFalse(actions["rapid_reset"]["advertised_supported"])
        for action in actions:
            self.robot.require_control_action(action)
        self.post.assert_not_called()

    def test_native_reader_protocol_is_reported_without_gating_other_controls(self):
        self.robot._controller_status = Mock()
        actions = self.discover(schema_fixture())
        self.assertTrue(all(value["supported"] for value in actions.values()))
        self.assertEqual(actions["collision_status"]["protocol"], "native_read_only")
        for action in ("rapid_reset", "rapid_start", "collision_ack"):
            self.assertFalse(actions[action]["advertised_supported"])
            self.robot.require_control_action(action)
        self.post.assert_not_called()
        self.robot._controller_status.read_collision.assert_not_called()

    def test_failed_refresh_updates_information_without_disabling_controls(self):
        for failure in (requests.Timeout("offline"), response(None), response({}, status=302),
                        response(schema_fixture(), content=b"x" * 2_000_001)):
            with self.subTest(failure=type(failure).__name__):
                self.discover(schema_fixture(native=True, collisions=True))
                self.get.side_effect = failure if isinstance(failure, Exception) else None
                self.get.return_value = failure
                result = self.robot.refresh_operator_capabilities()
                self.assertTrue(all(value["supported"] for value in result.values()))
                self.assertIsNone(result["robot_status"]["advertised_supported"])
                self.robot.require_control_action("home")
        self.post.assert_not_called()

    def test_native_fallback_only_for_missing_advertised_collision_route(self):
        self.robot._controller_status = Mock()
        self.robot._controller_status.read_collision.return_value = "INIT"
        self.robot._operator_gateway = parse_gateway_capabilities(schema_fixture())
        self.robot._get_object = Mock(return_value=copy.deepcopy(RUNNING))
        status = self.robot.read_control_status()
        self.assertEqual(status["collision_state"], "INIT")
        self.assertEqual(status["collision_source"], "controller")
        self.robot._get_object.assert_called_once_with("/robot/rapid")
        self.robot._controller_status.read_collision.assert_called_once_with(self.robot._get_object)

        self.robot._operator_gateway = parse_gateway_capabilities(schema_fixture(collisions=True))
        self.robot._controller_status.reset_mock()
        self.robot._get_object.side_effect = [RUNNING, RobotCommandError("collision route unavailable")]
        status = self.robot.read_control_status()
        self.assertIsNone(status["collision_state"])
        self.assertIn("collision route unavailable", status["collision_error"])
        self.robot._controller_status.read_collision.assert_not_called()

    def test_native_unknown_failed_and_triggered_reads_never_become_clear(self):
        self.robot._operator_gateway = parse_gateway_capabilities(schema_fixture())
        self.robot._get_object = Mock(return_value=RUNNING)
        self.robot._controller_status = Mock()
        for result in (ValueError("joint mismatch"), "invalid", None, "TRIGGERED", "TRIGGERED_ACK"):
            with self.subTest(result=str(result)):
                self.robot._controller_status.read_collision.side_effect = result if isinstance(result, Exception) else None
                self.robot._controller_status.read_collision.return_value = result
                status = self.robot.read_control_status()
                self.assertNotIn(status["collision_state"], ("INIT", "CONFIRMED"))
        self.post.assert_not_called()

    def test_advertised_but_malformed_collision_contract_never_enables_native_fallback(self):
        schema = schema_fixture(collisions=True)
        schema["paths"]["/api/v1/robot/rapid/collision"]["get"]["responses"] = {}
        self.robot._controller_status = Mock()
        actions = self.discover(schema)
        self.assertFalse(actions["collision_status"]["advertised_supported"])
        self.assertTrue(actions["home"]["supported"])
        self.robot._get_object = Mock(side_effect=[RUNNING, RobotCommandError("invalid collision response")])
        self.assertIsNone(self.robot.read_control_status()["collision_state"])
        self.robot._controller_status.read_collision.assert_not_called()

    def test_native_status_uses_runtime_readback_even_when_discovery_is_missing(self):
        for schema in (None, schema_fixture()):
            with self.subTest(schema=schema):
                if schema:
                    del schema["paths"]["/api/v1/robot/arms/{arm}/joints"]
                self.robot._controller_status = Mock()
                self.robot._controller_status.read_collision.return_value = "CONFIRMED"
                actions = self.discover(schema)
                self.assertTrue(all(value["supported"] for value in actions.values()))
                self.robot._get_object = Mock(return_value=RUNNING)
                self.assertEqual(self.robot.read_control_status()["collision_state"], "CONFIRMED")
                self.robot._controller_status.read_collision.assert_called_once_with(self.robot._get_object)

    def test_modern_gateway_preserves_all_controls_and_gateway_collision_read(self):
        self.assertTrue(all(value["supported"] for value in self.discover(schema_fixture(native=True, collisions=True)).values()))
        self.robot._get_object = Mock(side_effect=[RUNNING, {"collision_state": "CONFIRMED"}])
        status = self.robot.read_control_status()
        self.assertEqual(status["collision_state"], "CONFIRMED")
        self.assertEqual(status["collision_source"], "gateway")

    def test_configured_profile_loaded_without_mutations_and_dry_run_skips_it(self):
        with patch("controller_status.load_controller_status_profile", return_value=Mock()) as load:
            self.get.side_effect = None
            self.get.return_value = response(schema_fixture())
            args = SimpleNamespace(live=True, controller_profile="profile.json")
            robot = create_robot(args, self.robot.api, {})
            load.assert_called_once_with("profile.json", self.robot.api)
            self.assertTrue(robot.operator_capabilities["home"]["supported"])
            load.reset_mock()
            self.get.reset_mock()
            args.live = False
            robot = create_robot(args, self.robot.api, {})
            self.assertTrue(all(value["supported"] for value in robot.operator_capabilities.values()))
            load.assert_not_called()
            self.get.assert_not_called()
        self.post.assert_not_called()


class OperatorCapabilityConfirmationTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.fixture = WebFixture(directory, dry_run=False)
        self.session = self.fixture.session
        self.session.recovery = RecoveryJournal(Path(directory) / "recovery.json")
        self.controls = RobotControls(self.session)

    def test_missing_discovery_still_allows_operator_review(self):
        self.fixture.robot.operator_capabilities["rapid_reset"] = {"supported": False, "reason": "Not advertised"}
        self.controls.propose("rapid_reset", None)
        self.assertEqual(self.controls.pending["action"], "rapid_reset")
        self.assertFalse(self.session.recovery.path.exists())
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_discovery_changes_do_not_invalidate_an_operator_confirmation(self):
        for change in ({"supported": False}, {"supported": True, "protocol": "changed"}):
            self.fixture.robot.operator_capabilities["home"] = {"supported": True, "protocol": "old"}
            self.controls.propose("home", "both")
            token = self.controls.pending["token"]
            self.fixture.robot.operator_capabilities["home"] = change
            with patch.object(self.session, "execute_control", return_value={"message": "DONE"}) as execute:
                self.controls.confirm(token)
                execute.assert_called_once_with("home", "both")
            self.assertIsNone(self.controls.pending)

    def test_runtime_control_failure_is_reported_with_recovery(self):
        robot = RobotClient("http://gateway.invalid/api/v1", {})
        robot._operator_gateway = parse_gateway_capabilities(schema_fixture())
        robot._take_control = Mock(side_effect=RobotCommandError("Gateway unavailable"))
        self.session.robot = robot
        with self.assertRaisesRegex(RobotCommandError, "Gateway unavailable"):
            self.session.execute_control("rapid_reset", None)
        robot._take_control.assert_called_once()
        self.assertTrue(self.session.recovery.path.exists())
        self.assertTrue(self.session.motion_uncertain)

    def test_missing_openapi_does_not_block_runtime_status_or_confirmed_home(self):
        from datetime import datetime, timedelta, timezone

        robot = RobotClient("http://gateway.invalid/api/v1", {})
        self.session.robot = robot
        requested = []

        def get(url, **_):
            requested.append(url)
            if url.endswith("/openapi.json"):
                return response({})  # Gateway endpoints work without an API description.
            if url.endswith("/robot/rapid"):
                return response(RUNNING)
            if url.endswith("/robot/rapid/collision"):
                return response({"collision_state": "INIT"})
            if url.endswith("/joints"):
                arm = "left" if "/left/" in url else "right"
                return response({"arm": arm, "joints": list(robot.HOME_JOINTS),
                                 "arm_angle": robot.HOME_ARM_ANGLES[arm]})
            raise AssertionError(f"Unexpected GET {url}")

        def post(url, **_):
            if url.endswith(("/control/acquire", "/control/renew")):
                payload = {"success": True, "lease": {"active": True, "session_id": "offline-test",
                    "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()}}
            elif url.endswith("/control/release"):
                payload = {"success": True, "lease": {"active": False}}
            elif url.endswith("/robot/rws/home"):
                payload = {"accepted": True, "message": "DONE"}
            else:
                raise AssertionError(f"Unexpected POST {url}")
            reply = response(payload)
            reply.headers = {}
            return reply

        with patch("robot_client.requests.get", side_effect=get), \
                patch("robot_client.requests.post", side_effect=post) as sent:
            robot.configure_operator_controls()
            self.assertFalse(robot._operator_gateway["schema_valid"])
            self.assertEqual(robot.read_control_status()["collision_state"], "INIT")
            self.session.execute_control("home", "both")
        self.assertEqual(sum(call.args[0].endswith("/robot/rws/home") for call in sent.call_args_list), 2)
        self.assertTrue(any(url.endswith("/robot/rapid/collision") for url in requested))
        self.assertFalse(self.session.motion_uncertain)
        self.assertFalse(self.session.recovery.required())

    def test_status_refresh_can_recover_unknown_support_without_motion(self):
        robot = self.fixture.robot
        robot.operator_capabilities = {"robot_status": {"supported": None}}
        def refresh():
            robot.operator_capabilities = {"robot_status": {"supported": True}}
        robot.refresh_operator_capabilities = Mock(side_effect=refresh)
        robot.read_control_status = Mock(return_value=RUNNING)
        self.controls.refresh_status()
        self.assertEqual(self.controls.status, RUNNING)
        robot.refresh_operator_capabilities.assert_called_once()
        self.assertEqual(robot.jobs, [])


if __name__ == "__main__":
    unittest.main()
