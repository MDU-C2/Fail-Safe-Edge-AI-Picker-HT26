"""Offline robot administration and Stop races; all HTTP is mocked."""
import threading
import unittest
from unittest.mock import patch

from robot_client import RobotClient, RobotCommandError
from test_robot_client import response, lease_response


STATUS = {"execution_state": "stopped", "controller_state": "motoron", "operation_mode": "AUTO", "stop_generation": 0}


class AdminClientTests(unittest.TestCase):
    def setUp(self):
        self.client = RobotClient("http://offline.invalid/api/v1", {})
        self.calls = []
        self.action_hook = None
        self.action_reply = None
        self.collision = "CONFIRMED"
        self.post_patch = patch("robot_client.requests.post", side_effect=self.post)
        self.get_patch = patch("robot_client.requests.get", side_effect=self.get)
        self.post_patch.start()
        self.get_patch.start()
        self.addCleanup(self.post_patch.stop)
        self.addCleanup(self.get_patch.stop)

    def post(self, url, **kwargs):
        endpoint = url.split("/api/v1")[-1]
        self.calls.append((endpoint, kwargs.get("json")))
        if endpoint in {"/control/acquire", "/control/renew"}:
            return lease_response()
        if endpoint == "/control/release":
            return response({"success": True})
        if endpoint == "/robot/rapid/stop":
            return response(STATUS)
        if self.action_hook:
            self.action_hook()
        if self.action_reply is not None:
            return self.action_reply
        if endpoint == "/robot/rapid/collision/ack":
            return response({"collision_state": self.collision})
        return response({**STATUS, "execution_state": "running" if kwargs["json"]["start"] else "stopped"})

    def get(self, url, **kwargs):
        endpoint = url.split("/api/v1")[-1]
        self.calls.append((endpoint, None))
        return response({"collision_state": self.collision} if endpoint.endswith("collision") else STATUS)

    def test_dry_run_never_contacts_gateway(self):
        self.client.dry_run = True
        self.client.read_control_status()
        for action in ("rapid_reset", "rapid_start", "collision_ack"):
            self.assertTrue(self.client.control_action(action)["dry_run"])
        self.assertEqual(self.calls, [])

    def test_status_is_read_only(self):
        status = self.client.read_control_status()
        self.assertEqual(status["collision_state"], "CONFIRMED")
        self.assertIn("read_at", status)
        self.assertEqual([item[0] for item in self.calls], ["/robot/rapid", "/robot/rapid/collision"])

    def test_unavailable_collision_is_not_clear(self):
        with patch("robot_client.requests.get", side_effect=[response(STATUS), response({}, 404)]):
            status = self.client.read_control_status()
        self.assertIsNone(status["collision_state"])
        self.assertIn("collision_error", status)

    def test_bad_controller_status_is_rejected(self):
        with patch("robot_client.requests.get", return_value=response({})):
            with self.assertRaises(RobotCommandError):
                self.client.read_control_status()

    def test_fresh_start_never_calls_resume_endpoint(self):
        result = self.client.control_action("rapid_start")
        self.assertEqual(result["status"]["execution_state"], "running")
        self.assertIn(("/robot/rapid/reset", {"start": True, "stop_generation": 0}), self.calls)
        self.assertNotIn("/robot/rapid/start", [item[0] for item in self.calls])

    def test_reset_leaves_stopped(self):
        self.assertEqual(self.client.control_action("rapid_reset")["status"]["execution_state"], "stopped")
        self.assertIn(("/robot/rapid/reset", {"start": False, "stop_generation": 0}), self.calls)

    def test_failed_reset_not_retried_or_replaced_by_start(self):
        self.action_reply = response({}, 409)
        with self.assertRaises(RobotCommandError):
            self.client.control_action("rapid_start")
        self.assertEqual(sum(path == "/robot/rapid/reset" for path, _ in self.calls), 1)
        self.assertEqual(self.calls[-1][0], "/control/release")

    def test_old_gateway_without_cancellation_token_fails_closed(self):
        with patch("robot_client.requests.get", return_value=response({key: value for key, value in STATUS.items() if key != "stop_generation"})):
            with self.assertRaisesRegex(RobotCommandError, "Update the Pi gateway"):
                self.client.control_action("rapid_start")
        self.assertFalse(any(path.startswith("/robot/") for path, _ in self.calls))

    def test_controller_acknowledgement_rejection_is_explained(self):
        self.action_reply = response({"detail": "Robot controller: Local Client privileges required"}, 409)
        with self.assertRaisesRegex(RobotCommandError, "Local Client privileges"):
            self.client.control_action("collision_ack")
        self.assertEqual(sum(path == "/robot/rapid/collision/ack" for path, _ in self.calls), 1)

    def test_unconfirmed_start_rejected(self):
        self.action_reply = response(STATUS)
        with self.assertRaisesRegex(RobotCommandError, "did not confirm running"):
            self.client.control_action("rapid_start")

    def test_collision_requires_post_readback(self):
        self.action_reply = response({"collision_state": "CONFIRMED"})
        self.collision = "TRIGGERED"
        with self.assertRaisesRegex(RobotCommandError, "readback failed"):
            self.client.control_action("collision_ack")
        self.assertFalse(any("home" in path or path.endswith("/reset") for path, _ in self.calls))

    def test_pending_collision_ack_stays_blocked(self):
        self.collision = "TRIGGERED_ACK"
        with self.assertRaisesRegex(RobotCommandError, "not confirmed"):
            self.client.control_action("collision_ack")

    def test_unknown_or_busy_action_sends_nothing(self):
        with self.assertRaises(RobotCommandError):
            self.client.control_action("motoron")
        self.client._execution_lock.acquire()
        try:
            with self.assertRaises(RobotCommandError):
                self.client.control_action("rapid_reset")
        finally:
            self.client._execution_lock.release()
        self.assertEqual(self.calls, [])

    def test_latched_stop_prevents_action(self):
        self.client.latch_stop()
        with self.assertRaises(RobotCommandError):
            self.client.control_action("rapid_start")
        self.assertEqual(self.calls, [])

    def test_stop_reaches_gateway_while_program_response_is_pending(self):
        entered, release = threading.Event(), threading.Event()
        self.action_hook = lambda: (entered.set(), release.wait(2))
        errors = []

        def start():
            try:
                self.client.control_action("rapid_start")
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=start)
        worker.start()
        self.assertTrue(entered.wait(1))
        self.client.latch_stop()
        stopper = threading.Thread(target=self.client.request_stop)
        stopper.start()
        stopper.join(1)
        self.assertFalse(stopper.is_alive(), "Stop must not wait for the initialization response")
        release.set()
        worker.join(2)
        stopper.join(2)
        self.assertFalse(worker.is_alive() or stopper.is_alive())
        self.assertEqual([path for path, body in self.calls if path.startswith("/robot/") and (body is not None or path.endswith("/stop"))],
                         ["/robot/rapid/reset", "/robot/rapid/stop"])
        self.assertTrue(errors and isinstance(errors[0], RobotCommandError))


if __name__ == "__main__":
    unittest.main()
