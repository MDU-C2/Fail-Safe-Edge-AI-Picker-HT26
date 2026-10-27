"""Offline terminal routing and model-only startup checks."""

from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import Mock, patch

import capture_and_detect_custom as app
from cup_inventory import SelectionError
from local_llm import LLMError


class LanguageTerminalTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        stream = redirect_stdout(self.output)
        stream.__enter__()
        self.addCleanup(stream.__exit__, None, None, None)
        self.session = Mock()
        self.language = Mock()
        self.language.propose.return_value = {"kind": "proposal", "message": "Confirm cup 2"}

    def test_natural_request_only_goes_to_interpreter_until_confirmation(self):
        self.assertTrue(app.handle_input(self.session, "pick cup two", self.language))
        self.language.propose.assert_called_once_with("pick cup two")
        self.session.execute_selection.assert_not_called()
        self.language.confirm.assert_not_called()
        self.language.confirm.return_value = {"kind": "result", "message": "Preview complete"}
        self.assertTrue(app.handle_input(self.session, "confirm abc12345", self.language))
        self.language.confirm.assert_called_once_with("abc12345")

    def test_exact_operator_commands_remain_separate_and_invalidate_pending_proposals(self):
        for line in ("scan", "goto 2", "hover 2", "reset", "q", ""):
            with self.subTest(line=line), patch.object(app, "handle_terminal", return_value=True) as terminal:
                self.language.reset_mock()
                app.handle_input(self.session, line, self.language)
                self.language.cancel.assert_called_once()
                terminal.assert_called_once_with(self.session, line)
                self.language.propose.assert_not_called()

    def test_read_only_operator_commands_leave_proposal_available_for_review(self):
        for line in ("list", "show 2", "help"):
            with self.subTest(line=line), patch.object(app, "handle_terminal", return_value=True):
                self.language.reset_mock()
                app.handle_input(self.session, line, self.language)
                self.language.cancel.assert_not_called()
                self.language.propose.assert_not_called()

    def test_stop_only_cancels_pending_request_without_claiming_hardware_stop(self):
        app.handle_input(self.session, "stop", self.language)
        self.language.cancel.assert_called_once()
        self.assertIn("does not stop", self.output.getvalue())
        self.session.execute_selection.assert_not_called()

    def test_reserved_recovery_or_malformed_confirmation_never_reaches_llm(self):
        for line in ("home", "home both", "reset all", "confirm", "confirm code extra"):
            with self.subTest(line=line):
                with self.assertRaises(SelectionError):
                    app.handle_input(self.session, line, self.language)
        self.language.propose.assert_not_called()
        self.language.confirm.assert_not_called()
        self.session.execute_selection.assert_not_called()

    def test_language_disabled_keeps_original_terminal_behavior(self):
        with patch.object(app, "handle_terminal", return_value=False) as terminal:
            self.assertFalse(app.handle_input(self.session, "quit"))
            terminal.assert_called_once_with(self.session, "quit")

    def test_model_only_check_never_loads_camera_robot_token_or_yolo(self):
        with patch.object(app, "OllamaIntentClient") as client, \
             patch.object(app, "load_token") as token, patch.object(app, "load_model") as yolo, \
             patch.object(app, "CameraClient") as camera, patch.object(app, "RobotClient") as robot:
            client.return_value.interpret.return_value = {
                "action": "hover", "selector": "id", "cup_id": 2, "message": "hover cup two"}
            self.assertEqual(app.main(["--check-llm"]), 0)
            for unused in (token, yolo, camera, robot):
                unused.assert_not_called()
            self.assertIn("No camera or robot connection", self.output.getvalue())

    def test_model_check_reports_runtime_failure_or_misinterpretation(self):
        with patch.object(app, "OllamaIntentClient") as client, patch.object(app, "load_token") as token:
            client.return_value.interpret.side_effect = LLMError("not running")
            self.assertEqual(app.main(["--check-llm"]), 1)
            client.return_value.interpret.side_effect = None
            client.return_value.interpret.return_value = {
                "action": "goto", "selector": "id", "cup_id": 2, "message": "wrong action"}
            self.assertEqual(app.main(["--check-llm"]), 1)
            token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
