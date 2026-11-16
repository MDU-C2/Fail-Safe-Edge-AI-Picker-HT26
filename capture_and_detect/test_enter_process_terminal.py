"""Enter-to-process terminal checks using synthetic frames and robot doubles."""

from contextlib import ExitStack, redirect_stdout
import io
import json
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import capture_and_detect as app
from cup_commands import CupSession
from cup_inventory import SelectionError
from recovery_journal import RecoveryJournal
from robot_client import RobotCommandError
from terminal_controller import TerminalController
from test_cup_selection import RobotDouble, TRANSFER_JOB_COUNT, detection, inventory
from test_support_grid import temporary_grid


class EnterProcessTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        stream = redirect_stdout(self.output)
        stream.__enter__()
        self.addCleanup(stream.__exit__, None, None, None)
        self.grid = temporary_grid(self)
        self.robot = RobotDouble()
        self.captures = []
        self.session = CupSession(self.capture, Mock(), self.robot, "cal-1", clock=lambda: 100,
                                  placing_grid=self.grid)

    def capture(self, purpose):
        self.captures.append((purpose, len(self.robot.jobs)))
        objects = [detection("CUP", 100), detection("CUP BASE", 100),
                   detection("CUP", 200, x=550), detection("CUP BASE", 200, x=550)]
        return inventory(capture=f"frame-{len(self.captures)}", sequence=10 + len(self.captures),
                         objects=objects), 99

    def test_enter_uses_fresh_scan_then_checked_batch_with_no_second_confirmation(self):
        self.session.inventory = inventory(capture="old-scan")
        self.session.scan_usable = True
        with patch.object(self.session, "execute_batch", wraps=self.session.execute_batch) as execute:
            self.assertTrue(app.handle_process_input(self.session, ""))
        self.assertEqual(self.captures, [("scan", 0), ("verification", 0),
                                        ("verification", TRANSFER_JOB_COUNT)])
        self.assertEqual(len(self.robot.jobs), 2 * TRANSFER_JOB_COUNT)
        self.assertEqual(execute.call_args.kwargs["capture_id"], "frame-1")
        self.assertEqual(execute.call_args.kwargs["cup_ids"], [1, 2])
        self.assertEqual(execute.call_args.kwargs["expected_motion"], self.session.motion_settings())
        self.assertEqual(self.session.inventory["batch_execution"]["completed_cups"], [1, 2])
        self.assertFalse(self.session.scan_usable)

    def test_each_enter_captures_again_even_after_a_dry_run(self):
        self.robot.dry_run = True
        original_grid = self.grid.path.read_bytes()
        app.handle_process_input(self.session, "")
        app.handle_process_input(self.session, "   ")
        self.assertEqual([purpose for purpose, _ in self.captures],
                         ["scan", "verification", "verification"] * 2)
        self.assertEqual(self.session.inventory["capture"], "frame-4")
        self.assertEqual(self.grid.path.read_bytes(), original_grid)
        self.assertIn("no robot commands sent", self.output.getvalue())

    def test_quit_has_no_capture_or_motion(self):
        for line in ("q", " Q ", "quit", "exit"):
            self.assertFalse(app.handle_process_input(self.session, line))
        terminal = TerminalController(self.session, None,
                                      lambda line: app.handle_process_input(self.session, line))
        try:
            self.assertFalse(terminal.submit("q"))
        finally:
            terminal.close()
        self.assertEqual(self.captures, [])
        self.assertEqual(self.robot.jobs, [])
        self.assertFalse(self.session.stop_requested)

    def test_recovery_or_stop_blocks_enter_before_capture(self):
        self.session.motion_uncertain = True
        with self.assertRaisesRegex(SelectionError, "Recovery"):
            app.handle_process_input(self.session, "")
        self.session.motion_uncertain = False
        self.session.latch_stop()
        with self.assertRaisesRegex(SelectionError, "Recovery"):
            app.handle_process_input(self.session, "")
        self.assertEqual(self.captures, [])
        self.assertEqual(self.robot.jobs, [])

    def test_prompt_names_reset_while_recovery_or_stop_is_required(self):
        ready_prompt = "Press Enter to process cups (q + Enter to quit): "
        recovery_prompt = "Recovery required — inspect robot, then type reset + Enter (q to quit): "
        self.assertEqual(app.terminal_prompt(self.session), ready_prompt)
        self.session.motion_uncertain = True
        self.assertEqual(app.terminal_prompt(self.session), recovery_prompt)
        self.assertEqual(app.terminal_prompt(self.session, command_mode=True), "cups> ")
        self.session.motion_uncertain = False
        self.session.latch_stop()
        self.assertEqual(app.terminal_prompt(self.session), recovery_prompt)
        self.assertEqual(app.terminal_prompt(self.session, command_mode=True), "cups> ")
        self.assertEqual(self.captures, [])
        self.assertEqual(self.robot.jobs, [])

    def recovery_session(self, *, dry_run=False):
        journal = RecoveryJournal(self.grid.path.parent / "terminal-recovery.json")
        journal.begin({"action": "unfinished_test_sequence"})
        self.robot.dry_run = dry_run
        session = CupSession(self.capture, Mock(), self.robot, "cal-1", clock=lambda: 100,
                             recovery=journal, placing_grid=self.grid)
        return session, journal

    def test_live_reset_restores_ready_prompt_and_persists_across_restart(self):
        session, journal = self.recovery_session()
        self.assertIn("type reset", app.terminal_prompt(session))
        with self.assertRaisesRegex(SelectionError, "Enter without typing reset"):
            app.handle_process_input(session, "")
        app.handle_process_input(session, "reset")
        self.assertFalse(journal.required())
        self.assertFalse(session.motion_uncertain)
        self.assertEqual(app.terminal_prompt(session), "Press Enter to process cups (q + Enter to quit): ")
        restarted = CupSession(self.capture, Mock(), self.robot, "cal-1", recovery=RecoveryJournal(journal.path))
        self.assertFalse(restarted.motion_uncertain)
        self.assertEqual(app.terminal_prompt(restarted), app.terminal_prompt(session))
        self.assertEqual(self.captures, [])
        self.assertEqual(self.robot.jobs, [])

    def test_dry_run_reset_does_not_clear_live_recovery_record(self):
        session, journal = self.recovery_session(dry_run=True)
        original = journal.path.read_bytes()
        app.handle_process_input(session, "reset")
        self.assertFalse(session.motion_uncertain)
        self.assertIn("Press Enter to process", app.terminal_prompt(session))
        self.assertEqual(journal.path.read_bytes(), original)
        restarted = CupSession(self.capture, Mock(), RobotDouble(), "cal-1", recovery=RecoveryJournal(journal.path))
        self.assertTrue(restarted.motion_uncertain)
        self.assertIn("type reset", app.terminal_prompt(restarted))
        self.assertEqual(self.captures, [])
        self.assertEqual(self.robot.jobs, [])

    def test_external_journal_clear_does_not_unlock_running_terminal(self):
        session, journal = self.recovery_session()
        RecoveryJournal(journal.path).clear()
        self.assertFalse(journal.required())
        self.assertTrue(session.motion_uncertain)
        self.assertIn("type reset", app.terminal_prompt(session))
        with self.assertRaisesRegex(SelectionError, "type reset and press Enter"):
            app.handle_process_input(session, "")
        self.assertEqual(self.captures, [])
        self.assertEqual(self.robot.jobs, [])

    def test_reset_and_status_do_not_start_a_cycle(self):
        self.session.motion_uncertain = True
        for line in ("status", "help", "reset"):
            self.assertTrue(app.handle_process_input(self.session, line))
        self.assertFalse(self.session.motion_uncertain)
        self.assertFalse(self.session.scan_usable)
        self.assertEqual(self.captures, [])
        self.assertEqual(self.robot.jobs, [])

    def test_only_enter_starts_default_cycle(self):
        for line in ("scan", "goto all", "start", "anything", "q extra"):
            with self.subTest(line=line), self.assertRaisesRegex(SelectionError, "--commands"):
                app.handle_process_input(self.session, line)
        self.assertEqual(self.captures, [])
        self.assertEqual(self.robot.jobs, [])

    def test_changed_motion_settings_after_preview_cannot_move(self):
        preview = self.session.preview_batch

        def change_settings(**kwargs):
            result = preview(**kwargs)
            self.session.speed += 1
            return result

        with patch.object(self.session, "preview_batch", side_effect=change_settings):
            with self.assertRaisesRegex(SelectionError, "Movement settings changed"):
                app.handle_process_input(self.session, "")
        self.assertEqual(self.captures, [("scan", 0)])
        self.assertEqual(self.robot.jobs, [])

    def test_changed_grid_after_preview_cannot_move(self):
        preview = self.session.preview_batch

        def change_grid(**kwargs):
            result = preview(**kwargs)
            changed = json.loads(self.grid.path.read_text())
            changed["locations"][0]["x"] += 1
            self.grid.path.write_text(json.dumps(changed))
            return result

        with patch.object(self.session, "preview_batch", side_effect=change_grid):
            with self.assertRaisesRegex(SelectionError, "changed"):
                app.handle_process_input(self.session, "")
        self.assertEqual(self.robot.jobs, [])

    def test_failed_motion_is_not_retried_by_next_enter(self):
        self.robot.error = RobotCommandError("grip not confirmed")
        self.robot.error_at = "right_grip"
        with self.assertRaisesRegex(RobotCommandError, "not confirmed"):
            app.handle_process_input(self.session, "")
        jobs_before, captures_before = len(self.robot.jobs), len(self.captures)
        with self.assertRaisesRegex(SelectionError, "Recovery"):
            app.handle_process_input(self.session, "")
        self.assertEqual(len(self.robot.jobs), jobs_before)
        self.assertEqual(len(self.captures), captures_before)
        self.assertTrue(self.session.motion_uncertain)

    def test_busy_enter_is_not_queued_and_quit_stops_active_cycle(self):
        entered, release = threading.Event(), threading.Event()

        def capture(purpose):
            entered.set()
            self.assertTrue(release.wait(3))
            return self.capture(purpose)

        self.session.capture = capture
        self.session.request_stop = Mock(return_value={"stopped": True, "message": "Stopped"})
        messages = []
        terminal = TerminalController(self.session, None,
                                      lambda line: app.handle_process_input(self.session, line),
                                      output=messages.append)
        try:
            self.assertTrue(terminal.submit(""))
            self.assertTrue(entered.wait(1))
            self.assertTrue(terminal.submit(""))
            self.assertFalse(terminal.submit("q"))
            self.assertTrue(self.session.stop_requested)
        finally:
            release.set()
            terminal.close()
        self.session.request_stop.assert_called_once_with()
        self.assertEqual(self.captures, [("scan", 0)])
        self.assertEqual(self.robot.jobs, [])
        self.assertTrue(any("nothing was queued" in message for message in messages))

    def test_main_defaults_to_enter_mode_and_explicit_flags_keep_command_shell(self):
        for flags, command_mode, recovery_required in (([], False, False), (["--commands"], True, False),
                (["--llm"], True, False), ([], False, True), (["--commands"], True, True)):
            with self.subTest(flags=flags, recovery_required=recovery_required), ExitStack() as stack:
                self.session.motion_uncertain = recovery_required
                recovery_path = self.grid.path.parent / "robot_recovery.json"
                stack.enter_context(patch.object(app, "RecoveryJournal", return_value=SimpleNamespace(path=recovery_path)))
                for name in ("load_token", "CameraClient", "CaptureService",
                             "LanguageController", "OllamaIntentClient"):
                    stack.enter_context(patch.object(app, name))
                stack.enter_context(patch.object(app, "Calibration", return_value=SimpleNamespace(
                    digest="cal-1", offset=(0, 0, 0))))
                stack.enter_context(patch.object(app, "CaptureStore", return_value=SimpleNamespace(
                    folder=self.grid.path.parent, save=Mock())))
                stack.enter_context(patch.object(app, "CupSession", return_value=self.session))
                stack.enter_context(patch("device_agent.ExecutionLock"))
                stack.enter_context(patch("named_cups.NamedCupStore"))
                stack.enter_context(patch("cup_visual_matcher.OllamaCupMatcher"))
                stack.enter_context(patch("inference_integration.create_detector", return_value=(Mock(), "fake")))
                stack.enter_context(patch("inference_integration.create_robot"))
                process = stack.enter_context(patch.object(app, "handle_process_input", return_value=True))
                command = stack.enter_context(patch.object(app, "handle_input", return_value=True))
                read = stack.enter_context(patch("builtins.input", side_effect=["", "q"]))

                def controller(session, language, handler, output):
                    return SimpleNamespace(submit=lambda line: False if line == "q" else handler(line),
                                           close=Mock())

                stack.enter_context(patch.object(app, "TerminalController", side_effect=controller))
                self.assertEqual(app.main(flags), 0)
                self.assertEqual(command.call_count, int(command_mode))
                self.assertEqual(process.call_count, int(not command_mode))
                self.assertEqual(read.call_args.args[0], "cups> " if command_mode else
                                 "Recovery required — inspect robot, then type reset + Enter (q to quit): "
                                 if recovery_required else "Press Enter to process cups (q + Enter to quit): ")
                if recovery_required:
                    self.assertIn(f"Recovery record: {recovery_path}", self.output.getvalue())
        self.assertIn("without another confirmation", self.output.getvalue())
        self.assertEqual(self.captures, [])
        self.assertEqual(self.robot.jobs, [])

    def test_web_alias_passes_browser_arguments_without_terminal_startup(self):
        browser_main = Mock(return_value=17)
        with patch.dict(sys.modules, {"web_app": SimpleNamespace(main=browser_main)}), \
             patch.object(app, "load_token") as token:
            self.assertEqual(app.main(["--web", "--demo", "--port", "8088"]), 17)
        browser_main.assert_called_once_with(["--demo", "--port", "8088"])
        token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
