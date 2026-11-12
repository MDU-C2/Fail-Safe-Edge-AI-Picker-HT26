"""Offline operator controls, including stopping while the ordinary worker is blocked."""

from contextlib import redirect_stdout
import copy
import io
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from robot_client import RobotClient
from test_web_controller import WebFixture
from web_controller import WebController, WebError


class WebOperatorControlTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = WebFixture(directory.name)
        self.fixture.robot.request_stop = Mock(return_value={
            "dry_run": True, "stopped": False, "message": "Dry run: stop latched; no robot command sent."})
        self.controller = WebController(self.fixture.factory)
        self.addCleanup(self.controller.close)
        self.controller.start()
        self.wait(lambda state: not state["busy"])

    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = self.controller.state()
            if predicate(state):
                return state
            time.sleep(.003)
        self.fail("Controller did not reach the expected state")

    def submit(self, operation, **fields):
        return self.controller.submit(operation, dict(fields, request_id=uuid.uuid4().hex))

    def scan(self):
        self.submit("scan")
        state = self.wait(lambda state: not state["busy"])
        self.assertIsNone(state["error"])
        return state["inventory"]["capture"]

    def proposal(self):
        capture = self.scan()
        self.submit("select", capture_id=capture, cup_id=1, action="hover")
        state = self.wait(lambda state: not state["busy"])
        self.assertIsNone(state["error"])
        return {key: state["proposal"][key] for key in ("token", "capture_id", "cup_id", "action")}

    def gates(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        return entered, release

    def test_start_scans_and_proposes_a_batch_without_motion_or_model(self):
        for operation, fields in (("start", {}), ("command", {"text": "start picking"})):
            with self.subTest(operation=operation):
                before = len(self.fixture.captures)
                self.submit(operation, **fields)
                state = self.wait(lambda state: not state["busy"])
                self.assertIsNone(state["error"])
                self.assertEqual(self.fixture.captures[before:], ["scan"])
                self.assertEqual(state["proposal"]["action"], "goto_all")
                self.assertEqual(state["proposal"]["cup_ids"], [1, 2])
                self.assertEqual(self.fixture.robot.jobs, [])
                self.fixture.interpreter.interpret.assert_not_called()

    def test_stop_uses_independent_worker_while_camera_call_is_blocked(self):
        entered, release = self.gates()
        self.fixture.capture_gate = release
        self.fixture.capture_entered = entered
        self.submit("scan")
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.submit("stop"), {"accepted": True})
        state = self.wait(lambda state: not state["stop_in_flight"])
        self.fixture.robot.request_stop.assert_called_once()
        self.assertTrue(state["busy"])
        self.assertTrue(state["stop_requested"])
        self.assertTrue(state["recovery_required"])
        self.assertFalse(state["scan_usable"])
        self.assertIsNone(state["proposal"])
        release.set()
        state = self.wait(lambda state: not state["busy"])
        self.assertFalse(state["scan_usable"])
        self.assertEqual(state["stop"]["state"], "previewed")
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_stop_never_waits_for_language_lock_or_resurrects_its_proposal(self):
        self.proposal()
        saved = copy.deepcopy(self.fixture.language.pending)
        self.fixture.language.cancel()
        entered, release = self.gates()
        callback_proposals = []
        def late_proposal(text):
            with self.fixture.language._lock:
                entered.set()
                self.assertTrue(release.wait(2))
                self.fixture.language.pending = copy.deepcopy(saved)
                self.controller.publish_session()
                callback_proposals.append(self.controller.state()["proposal"])
                return {"kind": "proposal"}
        self.fixture.language.propose = late_proposal
        self.submit("command", text="Please hover above cup one")
        self.assertTrue(entered.wait(1))
        self.submit("command", text="stop the robot")
        self.wait(lambda state: not state["stop_in_flight"])
        self.fixture.robot.request_stop.assert_called_once()
        self.assertFalse(release.is_set())
        release.set()
        final = self.wait(lambda state: not state["busy"])
        self.assertEqual(callback_proposals, [None])
        self.assertIsNone(final["proposal"])
        self.assertIsNone(self.fixture.language.pending)
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_busy_remains_true_until_normal_and_stop_workers_both_finish(self):
        camera_entered, camera_release = self.gates()
        stop_entered, stop_release = self.gates()
        self.fixture.capture_gate = camera_release
        self.fixture.capture_entered = camera_entered
        def stop():
            stop_entered.set()
            self.assertTrue(stop_release.wait(2))
            return {"dry_run": True, "stopped": False, "message": "No physical stop sent."}
        self.fixture.robot.request_stop.side_effect = stop
        self.submit("scan")
        self.assertTrue(camera_entered.wait(1))
        self.submit("stop")
        self.assertTrue(stop_entered.wait(1))
        camera_release.set()
        state = self.wait(lambda state: state["operation"] == "stop")
        self.assertTrue(state["busy"])
        self.assertTrue(state["stop_in_flight"])
        with self.assertRaisesRegex(WebError, "busy"):
            self.submit("recovery", acknowledged=True)
        stop_release.set()
        state = self.wait(lambda state: not state["busy"])
        self.assertFalse(state["stop_in_flight"])
        self.assertTrue(state["stop_requested"])

    def test_stop_during_confirmation_inhibits_execution(self):
        payload = self.proposal()
        entered, release = self.gates()
        original = self.fixture.language.confirm
        def delayed(token):
            entered.set()
            self.assertTrue(release.wait(2))
            return original(token)
        self.fixture.language.confirm = delayed
        self.submit("confirm", **payload)
        self.assertTrue(entered.wait(1))
        self.submit("stop")
        self.wait(lambda state: not state["stop_in_flight"])
        release.set()
        state = self.wait(lambda state: not state["busy"])
        self.assertTrue(state["recovery_required"])
        self.assertIsNone(state["proposal"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_stop_invalidates_pending_confirmation_and_needs_explicit_recovery(self):
        payload = self.proposal()
        self.submit("stop")
        state = self.wait(lambda state: not state["busy"])
        self.assertIsNone(state["proposal"])
        for operation, fields in (("confirm", payload), ("start", {}), ("scan", {})):
            with self.subTest(operation=operation), self.assertRaisesRegex(WebError, "latched"):
                self.submit(operation, **fields)
        self.submit("recovery", acknowledged=True)
        state = self.wait(lambda state: not state["busy"])
        self.assertFalse(state["stop_requested"])
        self.assertFalse(state["scan_usable"])
        self.assertIsNone(state["proposal"])
        self.assertIsNone(self.fixture.language.pending)
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_status_and_help_are_cached_and_preserve_pending_selection(self):
        payload = self.proposal()
        with patch.object(self.fixture.session, "status_summary", side_effect=AssertionError("must use cache")):
            for operation, fields in (("status", {}), ("help", {}),
                                      ("command", {"text": "status"}), ("command", {"text": "help"})):
                with self.subTest(operation=operation, fields=fields):
                    self.assertTrue(self.submit(operation, **fields)["accepted"])
        self.assertEqual(self.controller.state()["proposal"]["token"], payload["token"])
        self.assertIn("Cached", self.controller.status()["source"])
        self.assertIn("Software stop", self.controller.help()["message"])
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_quick_commands_and_all_stop_aliases_work_while_worker_is_busy(self):
        entered, release = self.gates()
        self.fixture.capture_gate = release
        self.fixture.capture_entered = entered
        self.submit("scan")
        self.assertTrue(entered.wait(1))
        for text in ("status", "help", "stop", "halt", "stop robot", "stop the robot"):
            with self.subTest(command=text):
                self.assertTrue(self.submit("command", text=text)["accepted"])
                self.wait(lambda state: not state["stop_in_flight"])
                self.assertTrue(self.controller.state()["busy"])
        self.assertEqual(self.fixture.robot.request_stop.call_count, 4)
        self.fixture.interpreter.interpret.assert_not_called()
        release.set()
        self.wait(lambda state: not state["busy"])

    def test_dry_run_software_stop_performs_no_http(self):
        self.fixture.session.robot = RobotClient("http://not-a-real-gateway.invalid", {}, dry_run=True)
        with patch("robot_client.requests.post") as post, patch("robot_client.requests.get") as get:
            self.submit("stop")
            state = self.wait(lambda state: not state["busy"])
        post.assert_not_called()
        get.assert_not_called()
        self.assertEqual(state["stop"]["state"], "previewed")
        self.assertFalse(state["stop"]["stopped"])
        self.assertTrue(state["stop_requested"])

    def test_failed_live_stop_is_unconfirmed_and_cannot_automatically_resume(self):
        self.controller.close()
        self.fixture.robot.dry_run = False
        self.fixture.robot.request_stop.return_value = {"dry_run": False, "stopped": False,
                                                       "message": "Gateway disconnected; stop unconfirmed."}
        self.controller = WebController(self.fixture.factory, mode="live")
        self.addCleanup(self.controller.close)
        self.controller.start()
        self.wait(lambda state: not state["busy"])
        self.submit("stop")
        state = self.wait(lambda state: not state["busy"])
        self.assertEqual(state["stop"]["state"], "unconfirmed")
        self.assertIn("unconfirmed", state["error"])
        self.assertTrue(state["recovery_required"])
        with self.assertRaisesRegex(WebError, "latched"):
            self.submit("start")
        self.assertEqual(self.fixture.robot.jobs, [])


if __name__ == "__main__":
    unittest.main()
