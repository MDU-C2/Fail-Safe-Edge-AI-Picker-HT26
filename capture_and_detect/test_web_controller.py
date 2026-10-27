"""Offline tests for web proposals and serialized robot access; no hardware is used."""

from contextlib import redirect_stdout
import copy
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock

from cup_commands import CupSession
from cup_inventory import SelectionError
from language_commands import LanguageController
from robot_client import RobotCommandError
from test_cup_selection import RobotDouble, detection, inventory
from web_controller import WebController, WebError


class WebFixture:
    """Use the real selection pipeline with an entirely in-memory camera and robot."""

    def __init__(self, directory, *, dry_run=True):
        self.now = 100.0
        self.sequence = 10
        self.captures = []
        self.capture_gate = None
        self.capture_entered = threading.Event()
        self.capture_error = None
        self.robot = RobotDouble()
        self.robot.dry_run = dry_run
        self.interpreter = Mock()
        self.interpreter.interpret.return_value = {
            "action": "hover", "selector": "id", "cup_id": 1, "message": "Hover above cup 1."}
        self.session = CupSession(self.capture, Mock(), self.robot, "cal-1", clock=lambda: self.now)
        self.language = LanguageController(self.session, self.interpreter,
                                           audit_dir=Path(directory), clock=lambda: self.now)

    def capture(self, kind):
        self.capture_entered.set()
        if self.capture_gate is not None:
            if not self.capture_gate.wait(timeout=3):
                raise RuntimeError("Test camera gate timed out")
        if self.capture_error is not None:
            raise self.capture_error
        self.sequence += 1
        self.captures.append(kind)
        result = inventory(capture=f"{kind}-{self.sequence}", sequence=self.sequence, objects=[
            detection("CUP", 100), detection("CUP BASE", 100),
            detection("CUP", 200, x=550), detection("CUP BASE", 200, x=550),
            detection("CUP", 300, x=600),
        ])
        return result, self.now

    def factory(self, controller):
        self.controller = controller
        return self.session, self.language


class DirectWebSelectionTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = WebFixture(directory.name)
        self.session = self.fixture.session
        self.language = self.fixture.language
        self.session.scan()

    def propose(self, **changes):
        values = {"action": "hover", "capture_id": self.session.inventory["capture"], "cup_id": 1}
        values.update(changes)
        return self.language.propose_selection(**values)

    def test_direct_selection_requires_confirmation_and_skips_model(self):
        result = self.propose()
        self.assertEqual(result["kind"], "proposal")
        self.assertEqual(self.fixture.robot.commands, [])
        self.assertEqual(self.fixture.captures, ["scan"])
        self.fixture.interpreter.interpret.assert_not_called()
        confirmed = self.language.confirm(result["token"])
        self.assertEqual(confirmed["action"], "hover")
        self.assertEqual(self.fixture.captures, ["scan", "verification"])
        self.assertEqual(len(self.fixture.robot.jobs), 2)
        self.fixture.interpreter.interpret.assert_not_called()

    def test_stale_capture_unknown_blocked_and_invalid_ids_never_propose(self):
        invalid = [{"capture_id": "old-image"}, {"cup_id": 99}, {"cup_id": 3},
                   {"cup_id": True}, {"cup_id": 1.0}, {"cup_id": "1"},
                   {"action": "home"}, {"action": "grip"}, {"action": "reset"}]
        for changes in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.propose(**changes)
                self.assertIsNone(self.language.pending)
                self.assertEqual(self.fixture.robot.commands, [])

    def test_direct_proposal_obeys_expiration_and_scan_fingerprint(self):
        first = self.propose()
        self.fixture.now += 61
        with self.assertRaisesRegex(SelectionError, "expired"):
            self.language.confirm(first["token"])
        second = self.propose()
        self.session.inventory["cups"][0]["status"] = "changed"
        with self.assertRaisesRegex(SelectionError, "changed"):
            self.language.confirm(second["token"])
        self.assertEqual(self.fixture.robot.commands, [])

    def test_failed_new_selection_cancels_previous_proposal(self):
        first = self.propose()
        with self.assertRaises(ValueError):
            self.propose(cup_id=99)
        with self.assertRaisesRegex(SelectionError, "No pending"):
            self.language.confirm(first["token"])
        self.assertEqual(self.fixture.robot.commands, [])


class WebControllerTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = WebFixture(directory.name)
        self.controller = WebController(self.fixture.factory)
        self.addCleanup(self.controller.close)
        self.controller.start()
        self.wait_idle()
        self.assertTrue(self.controller.state()["ready"])

    def wait_idle(self, controller=None):
        controller = controller or self.controller
        deadline = time.monotonic() + 3
        while controller.state()["busy"]:
            if time.monotonic() > deadline:
                self.fail("Controller worker did not complete within 3 seconds")
            time.sleep(0.005)
        return controller.state()

    def submit(self, operation, **payload):
        payload.setdefault("request_id", uuid.uuid4().hex)
        self.assertEqual(self.controller.submit(operation, payload), {"accepted": True})
        return self.wait_idle()

    def scan(self):
        state = self.submit("scan")
        self.assertIsNone(state["error"])
        self.assertTrue(state["scan_usable"])
        return state["inventory"]["capture"]

    def propose(self, *, action="hover", cup_id=1):
        capture = self.scan()
        state = self.submit("select", capture_id=capture, cup_id=cup_id, action=action)
        self.assertIsNone(state["error"])
        self.assertIsNotNone(state["proposal"])
        return {key: state["proposal"][key] for key in ("token", "capture_id", "cup_id", "action")}

    def test_button_selection_and_exact_terminal_commands_only_propose(self):
        capture = self.scan()
        for operation, payload in (("select", {"capture_id": capture, "cup_id": 1, "action": "hover"}),
                                   ("command", {"text": "goto 2"}),
                                   ("command", {"text": "hover 1"})):
            with self.subTest(operation=operation, payload=payload):
                state = self.submit(operation, **payload)
                self.assertIsNone(state["error"])
                self.assertIsNotNone(state["proposal"])
                self.assertEqual(self.fixture.robot.jobs, [])
                self.fixture.interpreter.interpret.assert_not_called()

    def test_natural_request_proposes_then_confirmation_uses_real_pipeline(self):
        self.scan()
        state = self.submit("command", text="Please hover above the first cup")
        self.assertEqual(state["proposal"]["cup_id"], 1)
        self.assertEqual(self.fixture.robot.jobs, [])
        self.fixture.interpreter.interpret.assert_called_once()
        payload = {key: state["proposal"][key] for key in ("token", "capture_id", "cup_id", "action")}
        final = self.submit("confirm", **payload)
        self.assertIsNone(final["error"])
        self.assertIsNone(final["proposal"])
        self.assertIn("no robot commands", final["message"])
        self.assertEqual(self.fixture.captures, ["scan", "verification"])
        self.assertEqual([job["name"] for job in self.fixture.robot.jobs], ["left_ready", "approach_cup"])

    def test_duplicate_request_id_or_confirmation_never_replays(self):
        payload = self.propose()
        request_id = uuid.uuid4().hex
        self.submit("confirm", request_id=request_id, **payload)
        count = len(self.fixture.robot.jobs)
        with self.assertRaisesRegex(WebError, "already received"):
            self.controller.submit("confirm", dict(payload, request_id=request_id))
        replay = self.submit("confirm", **payload)
        self.assertIsNotNone(replay["error"])
        self.assertEqual(len(self.fixture.robot.jobs), count)

    def test_confirm_binds_capture_cup_action_and_token(self):
        for key, value in (("capture_id", "different-capture"), ("cup_id", 2),
                           ("action", "goto"), ("token", "00000000")):
            with self.subTest(field=key):
                payload = self.propose()
                payload[key] = value
                state = self.submit("confirm", **payload)
                self.assertIsNotNone(state["error"])
                self.assertIsNone(state["proposal"])
                self.assertEqual(self.fixture.robot.jobs, [])

    def test_cancel_and_new_scan_invalidate_pending_confirmation(self):
        for operation in ("cancel", "scan"):
            with self.subTest(operation=operation):
                payload = self.propose()
                self.submit(operation)
                final = self.submit("confirm", **payload)
                self.assertIsNotNone(final["error"])
                self.assertIsNone(final["proposal"])
                self.assertEqual(self.fixture.robot.jobs, [])

    def test_expired_proposal_does_not_execute(self):
        payload = self.propose()
        self.fixture.now += 61
        state = self.submit("confirm", **payload)
        self.assertIn("expired", state["error"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_failed_scan_does_not_leave_previous_scan_authorized(self):
        payload = self.propose()
        self.fixture.capture_error = RuntimeError("Camera disconnected")
        state = self.submit("scan")
        self.assertIn("Camera disconnected", state["error"])
        self.assertFalse(state["scan_usable"])
        self.assertIsNone(state["proposal"])
        state = self.submit("confirm", **payload)
        self.assertIsNotNone(state["error"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_failed_request_id_is_still_consumed(self):
        request_id = uuid.uuid4().hex
        self.fixture.capture_error = RuntimeError("Camera failed")
        state = self.submit("scan", request_id=request_id)
        self.assertIsNotNone(state["error"])
        self.fixture.capture_error = None
        with self.assertRaisesRegex(WebError, "already received"):
            self.controller.submit("scan", {"request_id": request_id})
        self.assertEqual(self.fixture.captures, [])

    def test_busy_worker_rejects_another_action_without_queuing_it(self):
        gate = self.fixture.capture_gate = threading.Event()
        self.addCleanup(gate.set)
        self.controller.submit("scan", {"request_id": uuid.uuid4().hex})
        self.assertTrue(self.fixture.capture_entered.wait(timeout=1))
        state = self.controller.state()
        self.assertTrue(state["busy"])
        for operation, fields in (("scan", {}), ("command", {"text": "goto 1"}), ("cancel", {})):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(WebError, "busy"):
                    self.controller.submit(operation, dict(fields, request_id=uuid.uuid4().hex))
        gate.set()
        self.wait_idle()
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_live_motion_stays_busy_and_progress_reports_actual_completed_steps(self):
        self.controller.close()
        self.fixture.robot.dry_run = False
        self.controller = WebController(self.fixture.factory, mode="live")
        self.addCleanup(self.controller.close)
        self.controller.start()
        self.wait_idle()
        # The production save hook publishes snapshots after each controller step.
        self.fixture.session.save = lambda unused: self.controller.publish_session()
        payload = self.propose()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def after_move(count):
            if count == 1:
                entered.set()
                release.wait(timeout=2)
        self.fixture.robot.after_move = after_move
        self.controller.submit("confirm", dict(payload, request_id=uuid.uuid4().hex))
        self.assertTrue(entered.wait(timeout=1))
        state = self.controller.state()
        self.assertTrue(state["busy"])
        self.assertEqual(state["progress"]["completed"], 0)
        self.assertEqual(state["progress"]["step"], "left_ready")
        self.assertIsNone(state["proposal"])
        with self.assertRaisesRegex(WebError, "busy"):
            self.controller.submit("scan", {"request_id": uuid.uuid4().hex})
        release.set()
        final = self.wait_idle()
        self.assertEqual(final["progress"]["completed"], 2)
        self.assertEqual(final["progress"]["state"], "completed")
        self.assertFalse(final["scan_usable"])

    def test_state_is_a_copy_and_does_not_expose_private_inventory_fields(self):
        self.propose()
        state = self.controller.state()
        self.assertNotIn("gateway", state["inventory"])
        self.assertNotIn("images", state["inventory"])
        self.assertNotIn("calibration_sha256", state["inventory"])
        self.assertNotIn("x", state["inventory"]["cups"][0])
        original_token = state["proposal"]["token"]
        state["proposal"]["token"] = "corrupted"
        state["inventory"]["cups"][0]["status"] = "corrupted"
        state["events"].clear()
        fresh = self.controller.state()
        self.assertEqual(fresh["proposal"]["token"], original_token)
        self.assertNotEqual(fresh["inventory"]["cups"][0]["status"], "corrupted")
        self.assertTrue(fresh["events"])

    def test_unregistered_or_old_scan_image_is_not_available(self):
        first = self.scan()
        self.assertEqual(self.controller.image(first), "offline.jpg")
        self.scan()
        self.assertIsNone(self.controller.image(first))
        self.assertIsNone(self.controller.image("../../secret"))

    def test_strict_payloads_reject_unsafe_types_extras_and_unacknowledged_recovery(self):
        cases = [("scan", {"request_id": "short"}), ("scan", {"request_id": "12345678", "extra": 1}),
                 ("recovery", {"request_id": "12345678", "acknowledged": 1}),
                 ("command", {"request_id": "12345678", "text": "  "}),
                 ("command", {"request_id": "12345678", "text": "x" * 2001})]
        for value in (True, 1.0, "1", 0, -1):
            cases.append(("select", {"request_id": "12345678", "capture_id": "scan-1",
                                     "cup_id": value, "action": "hover"}))
        for operation, payload in cases:
            with self.subTest(operation=operation, payload=payload):
                with self.assertRaises(WebError) as caught:
                    self.controller.submit(operation, payload)
                self.assertEqual(caught.exception.status, 422)
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_recovery_acknowledgement_never_moves_and_requires_a_new_scan(self):
        self.scan()
        self.fixture.session.motion_uncertain = True
        self.controller.publish_session()
        state = self.submit("recovery", acknowledged=True)
        self.assertFalse(state["recovery_required"])
        self.assertFalse(state["scan_usable"])
        self.assertEqual(self.fixture.robot.jobs, [])
        self.assertIn("No robot command", state["message"])

    def test_reserved_commands_cannot_home_recover_or_bypass_confirmation(self):
        self.scan()
        for text in ("home", "reset", "confirm abcd1234", "exit"):
            with self.subTest(text=text):
                state = self.submit("command", text=text)
                self.assertIsNotNone(state["error"])
                self.assertIsNone(state["proposal"])
                self.fixture.interpreter.interpret.assert_not_called()
                self.assertEqual(self.fixture.robot.jobs, [])

    def test_language_failure_cancels_an_earlier_proposal_without_fallback_motion(self):
        earlier = self.propose()
        self.fixture.interpreter.interpret.side_effect = ValueError("Local model unavailable")
        state = self.submit("command", text="Please pick cup two")
        self.assertIn("Local model unavailable", state["error"])
        self.assertIsNone(state["proposal"])
        self.submit("confirm", **earlier)
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_disabled_llm_still_allows_direct_proposals_but_not_natural_requests(self):
        self.controller.close()
        self.controller = WebController(self.fixture.factory, llm_enabled=False)
        self.addCleanup(self.controller.close)
        self.controller.start()
        self.wait_idle()
        self.scan()
        state = self.submit("command", text="Please pick cup one")
        self.assertIn("disabled", state["error"])
        self.fixture.interpreter.interpret.assert_not_called()
        state = self.submit("command", text="hover 1")
        self.assertIsNone(state["error"])
        self.assertEqual(state["proposal"]["cup_id"], 1)
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_uncertain_motion_is_exposed_and_blocks_later_selection(self):
        self.controller.close()
        self.fixture.robot.dry_run = False
        self.fixture.robot.error = RobotCommandError("Completion could not be confirmed")
        self.controller = WebController(self.fixture.factory, mode="live")
        self.addCleanup(self.controller.close)
        self.controller.start()
        self.wait_idle()
        payload = self.propose()
        state = self.submit("confirm", **payload)
        self.assertIn("could not be confirmed", state["error"])
        self.assertTrue(state["recovery_required"])
        self.assertEqual(state["progress"]["state"], "interrupted")
        self.assertEqual(state["progress"]["completed"], 0)
        self.assertFalse(state["scan_usable"])
        self.assertIsNone(state["proposal"])
        self.assertEqual(len(self.fixture.robot.jobs), 1)
        fresh_capture = self.scan()
        state = self.submit("select", capture_id=fresh_capture, cup_id=1, action="hover")
        self.assertIsNotNone(state["error"])
        self.assertTrue(state["recovery_required"])
        self.assertEqual(len(self.fixture.robot.jobs), 1)

    def test_startup_failure_or_mode_mismatch_cannot_accept_operations(self):
        def failure(controller):
            raise RuntimeError("Gateway is unavailable")
        for factory, mode in ((failure, "demo"), (self.fixture.factory, "live")):
            with self.subTest(mode=mode, factory=factory):
                controller = WebController(factory, mode=mode)
                self.addCleanup(controller.close)
                controller.start()
                self.wait_idle(controller)
                self.assertFalse(controller.state()["ready"])
                self.assertIsNotNone(controller.state()["error"])
                with self.assertRaises(WebError) as caught:
                    controller.submit("scan", {"request_id": uuid.uuid4().hex})
                self.assertEqual(caught.exception.status, 503)


if __name__ == "__main__":
    unittest.main()
