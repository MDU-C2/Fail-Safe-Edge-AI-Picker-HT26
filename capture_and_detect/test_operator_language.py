"""Offline checks for fresh Start previews and read-only operator commands."""

from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import Mock, patch

from cup_commands import CupSession
from cup_inventory import SelectionError
from language_commands import LanguageController
from local_llm import DisabledIntentClient, LLMError, validate_intent
from robot_client import RobotClient
from test_cup_selection import detection, inventory
from test_support_grid import temporary_grid


class OperatorLanguageTests(unittest.TestCase):
    def setUp(self):
        stream = redirect_stdout(io.StringIO())
        stream.__enter__()
        self.addCleanup(stream.__exit__, None, None, None)
        self.sequence = 0
        self.captures = []
        self.objects = [detection("CUP", 100), detection("CUP BASE", 100),
                        detection("CUP", 200, x=550), detection("CUP BASE", 200, x=550)]

        def capture(purpose):
            self.captures.append(purpose)
            self.sequence += 1
            return inventory(capture=f"{purpose}-{self.sequence}", sequence=self.sequence,
                             objects=self.objects), 99

        self.robot = RobotClient("http://offline.invalid/api/v1", {}, dry_run=True)
        self.session = CupSession(capture, Mock(), self.robot, "cal-1", clock=lambda: 100,
                                  placing_grid=temporary_grid(self))
        self.interpreter = DisabledIntentClient()
        self.language = LanguageController(self.session, self.interpreter, clock=lambda: 100)

    def test_start_scans_then_previews_without_model_or_robot_and_needs_confirmation(self):
        with patch("requests.post") as post, patch("requests.get") as get, \
                patch.object(self.interpreter, "interpret") as model:
            result = self.language.start()
            self.assertEqual(result["kind"], "proposal")
            self.assertEqual(self.captures, ["scan"])
            self.assertEqual(self.language.pending["record"]["action"], "goto_all")
            self.assertEqual(self.language.pending["record"]["cup_ids"], [1, 2])
            model.assert_not_called()
            post.assert_not_called()
            get.assert_not_called()
            result = self.language.confirm(result["token"])
            expected = sum(len(cup["last_verification"]["jobs"])
                           for cup in self.session.inventory["cups"] if cup["id"] in (1, 2))
            self.assertEqual(len(result["jobs"]), expected)
            post.assert_not_called()
            get.assert_not_called()

    def test_start_replaces_previous_proposal_and_binds_new_scan(self):
        first = self.language.start()
        old_capture = self.language.pending["record"]["capture_id"]
        second = self.language.start()
        self.assertNotEqual(first["token"], second["token"])
        self.assertNotEqual(old_capture, self.language.pending["record"]["capture_id"])
        self.assertEqual(self.captures, ["scan", "scan"])
        with self.assertRaises(SelectionError):
            self.language.confirm(first["token"])

    def test_start_does_not_clear_recovery_or_restart_rapid(self):
        self.session.motion_uncertain = True
        with patch("requests.post") as post, self.assertRaisesRegex(SelectionError, "Recovery"):
            self.language.start()
        self.assertEqual(self.captures, [])
        self.assertIsNone(self.language.pending)
        self.assertTrue(self.session.motion_uncertain)
        post.assert_not_called()

    def test_status_help_preserve_pending_and_do_not_query_any_hardware(self):
        self.language.start()
        pending = self.language.pending
        with patch("requests.post") as post, patch("requests.get") as get:
            status = self.language.status()
            help_result = self.language.help()
        self.assertEqual(status["kind"], "status")
        self.assertIn("application status", status["message"].lower())
        self.assertEqual(help_result["kind"], "help")
        self.assertIn("start", help_result["message"])
        self.assertIs(self.language.pending, pending)
        self.assertEqual(self.captures, ["scan"])
        post.assert_not_called()
        get.assert_not_called()

    def test_natural_start_is_bounded_to_same_fresh_preview(self):
        self.language.interpreter = Mock()
        self.language.interpreter.interpret.return_value = {
            "action": "start", "selector": "none", "cup_id": None, "message": "Start picking."}
        result = self.language.propose("Please start picking")
        self.assertEqual(result["kind"], "proposal")
        self.assertEqual(self.captures, ["scan"])
        self.assertEqual(self.language.pending["record"]["action"], "goto_all")

    def test_stop_during_interpretation_prevents_new_movement_proposal(self):
        self.session.scan()
        def interpret(*args):
            self.session.latch_stop()
            return {"action": "goto", "selector": "id", "cup_id": 1, "message": "Pick."}
        self.language.interpreter = Mock(interpret=Mock(side_effect=interpret))
        with self.assertRaises(SelectionError):
            self.language.propose("Pick cup one")
        self.assertIsNone(self.language.pending)
        self.assertTrue(self.session.stop_requested)

    def test_new_actions_require_no_cup_selector_and_model_cannot_control_rapid(self):
        for action in ("start", "status", "help"):
            valid = {"action": action, "selector": "none", "cup_id": None, "message": "Ready."}
            self.assertEqual(validate_intent(valid), valid)
            with self.assertRaises(LLMError):
                validate_intent({**valid, "selector": "id", "cup_id": 1})
        for action in ("stop", "rapid_start", "resume", "reset", "home"):
            with self.assertRaises(LLMError):
                validate_intent({"action": action, "selector": "none", "cup_id": None, "message": "No."})


if __name__ == "__main__":
    unittest.main()
