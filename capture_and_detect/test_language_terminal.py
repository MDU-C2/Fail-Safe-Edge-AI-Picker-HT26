"""Offline terminal routing and model-only startup checks."""

from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import Mock, patch

import capture_and_detect_custom as app
from cup_inventory import SelectionError
from cup_commands import CupSession
from language_commands import LanguageController
from local_llm import DisabledIntentClient, LLMError
from test_cup_selection import RobotDouble, detection, inventory
from test_support_grid import temporary_grid


class LanguageTerminalTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        stream = redirect_stdout(self.output)
        stream.__enter__()
        self.addCleanup(stream.__exit__, None, None, None)
        self.session = Mock()
        self.language = Mock()
        self.language.propose.return_value = {"kind": "proposal", "message": "Confirm cup 2"}
        self.language.help.return_value = {"kind": "help", "message": "Command help"}
        self.language.start.return_value = {"kind": "proposal", "message": "Confirm a fresh batch"}
        self.language.status.return_value = {"kind": "status", "message": "Application status"}
        self.session.request_stop.return_value = {"dry_run": False, "stopped": True,
                                                  "message": "Software stop confirmed"}

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

    def test_explicit_pick_all_builds_confirmation_without_interpreting_or_moving(self):
        self.session.inventory = {"capture": "scan-1"}
        self.language.propose_batch.return_value = {"kind": "proposal", "message": "Confirm batch"}
        for line in ("goto all", "pick all", "pick all available cups"):
            with self.subTest(line=line):
                self.language.reset_mock()
                self.assertTrue(app.handle_input(self.session, line, self.language))
                self.language.propose_batch.assert_called_once_with(capture_id="scan-1")
                self.language.propose.assert_not_called()
                self.session.execute_selection.assert_not_called()
                self.session.execute_batch.assert_not_called()

    def test_pick_all_and_confirmation_work_with_disabled_llm(self):
        objects = [detection("CUP", 100), detection("CUP BASE", 100),
                   detection("CUP", 200, x=550), detection("CUP BASE", 200, x=550)]
        capture = Mock(side_effect=[(inventory(capture=f"frame-{i}", sequence=10 + i, objects=objects), 99)
                                    for i in range(3)])
        robot = RobotDouble()
        robot.dry_run = True
        session = CupSession(capture, Mock(), robot, "cal-1", clock=lambda: 100,
                             placing_grid=temporary_grid(self))
        interpreter = DisabledIntentClient()
        language = LanguageController(session, interpreter, clock=lambda: 100)
        with patch.object(interpreter, "interpret", wraps=interpreter.interpret) as interpret, \
             patch("requests.post") as post, patch("requests.get") as get:
            app.handle_input(session, "scan", language)
            app.handle_input(session, "goto all", language)
            self.assertEqual(robot.jobs, [])
            self.assertEqual(language.pending["record"]["cup_ids"], [1, 2])
            app.handle_input(session, "confirm " + language.pending["token"], language)
            self.assertEqual(len(robot.jobs), 48)
            self.assertIsNone(language.pending)
            interpret.assert_not_called()
            post.assert_not_called()
            get.assert_not_called()

    def test_explicit_stop_aliases_bypass_interpreter_and_request_real_stop(self):
        for line in ("stop", "halt", "stop robot", "stop the robot"):
            with self.subTest(line=line):
                self.session.reset_mock()
                self.language.reset_mock()
                app.handle_input(self.session, line, self.language)
                self.session.latch_stop.assert_called_once()
                self.session.request_stop.assert_called_once()
                self.language.cancel.assert_called_once()
                self.language.propose.assert_not_called()
        self.assertIn("Software stop confirmed", self.output.getvalue())
        self.session.execute_selection.assert_not_called()

    def test_start_status_and_help_bypass_interpreter_and_do_not_directly_move(self):
        for line, method in (("start", "start"), ("start picking", "start"),
                             ("status", "status"), ("help", "help")):
            with self.subTest(line=line):
                self.language.reset_mock()
                app.handle_input(self.session, line, self.language)
                getattr(self.language, method).assert_called_once_with()
                self.language.propose.assert_not_called()
                self.session.execute_selection.assert_not_called()
                self.session.execute_batch.assert_not_called()

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
