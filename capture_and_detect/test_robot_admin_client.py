"""Original gateway status and Stop controls; all HTTP is mocked."""
import unittest
from unittest.mock import patch

import requests

from robot_client import RobotClient, RobotCommandError
from test_robot_client import response, lease_response


STATUS = {"execution_state": "stopped", "controller_state": "motoron", "operation_mode": "AUTO"}


class OriginalGatewayControlTests(unittest.TestCase):
    def setUp(self):
        self.client = RobotClient("http://offline.invalid/api/v1", {})
        self.calls = []
        self.stop_reply = response(STATUS)
        self.post_mock = self.enterContext(patch("robot_client.requests.post", side_effect=self.post))
        self.get_mock = self.enterContext(patch("robot_client.requests.get", return_value=response(STATUS)))

    def post(self, url, **kwargs):
        endpoint = url.split("/api/v1")[-1]
        self.calls.append(endpoint)
        self.assertNotIn("X-Control-Session", kwargs["headers"])
        if endpoint in {"/control/acquire", "/control/renew"}:
            return lease_response()
        if endpoint == "/control/release":
            return response({"success": True})
        if endpoint == "/robot/rapid/stop":
            if isinstance(self.stop_reply, Exception):
                raise self.stop_reply
            return self.stop_reply
        raise AssertionError(f"Unexpected mutation {endpoint}")

    def test_dry_run_never_contacts_gateway(self):
        self.client.dry_run = True
        self.assertTrue(self.client.read_control_status()["dry_run"])
        self.assertTrue(self.client.request_stop()["dry_run"])
        self.post_mock.assert_not_called()
        self.get_mock.assert_not_called()

    def test_status_is_read_only_and_uses_only_original_route(self):
        status = self.client.read_control_status()
        self.assertEqual({key: status[key] for key in STATUS}, STATUS)
        self.assertNotIn("collision_state", status)
        self.assertIn("read_at", status)
        self.get_mock.assert_called_once_with("http://offline.invalid/api/v1/robot/rapid", headers={}, timeout=5)
        self.post_mock.assert_not_called()

    def test_bad_controller_status_is_rejected(self):
        self.get_mock.return_value = response({})
        with self.assertRaises(RobotCommandError):
            self.client.read_control_status()

    def test_removed_controls_reject_before_any_network(self):
        for action in ("rapid_reset", "rapid_start", "collision_ack", "collision_status", "motoron"):
            with self.subTest(action=action), self.assertRaises(RobotCommandError):
                self.client.require_control_action(action)
        self.assertFalse(hasattr(self.client, "control_action"))
        self.post_mock.assert_not_called()
        self.get_mock.assert_not_called()

    def test_stop_uses_original_lease_without_session_or_generation(self):
        self.assertTrue(self.client.request_stop()["stopped"])
        self.assertEqual(self.calls, ["/control/acquire", "/robot/rapid/stop", "/control/release"])
        self.assertTrue(self.client.stop_requested)
        self.get_mock.assert_not_called()

    def test_unconfirmed_stop_and_timeout_stay_latched_without_retry(self):
        for result in (response({"execution_state": "running"}), requests.Timeout("lost response")):
            with self.subTest(result=str(result)):
                self.calls.clear()
                self.stop_reply = result
                with self.assertRaises(RobotCommandError):
                    self.client.request_stop()
                self.assertEqual(self.calls.count("/robot/rapid/stop"), 1)
                self.assertTrue(self.client.stop_requested)
                self.assertEqual(self.calls[-1], "/control/release")


if __name__ == "__main__":
    unittest.main()
