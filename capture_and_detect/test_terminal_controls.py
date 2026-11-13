"""Concurrency checks for terminal controls; no network or robot is used."""

import threading
import unittest
from unittest.mock import Mock

from terminal_controller import TerminalController


class TerminalControlTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock(motion_uncertain=False, stop_requested=False)
        self.latched = threading.Event()
        self.stopping = threading.Event()
        self.normal_started = threading.Event()
        self.normal_release = threading.Event()
        self.stop_release = threading.Event()
        self.stop_release.set()
        self.lines = []
        self.output = []
        self.language = Mock()

        def latch():
            self.session.stop_requested = True
            self.session.motion_uncertain = True
            self.latched.set()

        def stop():
            self.stopping.set()
            self.stop_release.wait(3)
            return {"dry_run": False, "stopped": True, "message": "Software stop confirmed"}

        def handle(line):
            self.lines.append(line)
            if line == "goto 2":
                self.normal_started.set()
                self.normal_release.wait(3)
            return True

        self.session.latch_stop.side_effect = latch
        self.session.request_stop.side_effect = stop
        self.controller = TerminalController(self.session, self.language, handle,
                                             output=self.output.append)
        self.addCleanup(self.cleanup_controller)

    def cleanup_controller(self):
        self.normal_release.set()
        self.stop_release.set()
        self.controller.close()

    def start_action(self):
        self.assertTrue(self.controller.submit("goto 2"))
        self.assertTrue(self.normal_started.wait(1))

    def test_stop_latches_immediately_and_uses_independent_worker(self):
        self.start_action()
        self.controller.submit("stop")
        self.assertTrue(self.latched.is_set())
        self.assertTrue(self.stopping.wait(1))
        self.assertFalse(self.normal_release.is_set())
        self.session.latch_stop.assert_called_once_with()
        self.session.request_stop.assert_called_once_with()
        self.assertNotIn("stop", self.lines)

    def test_status_and_help_remain_responsive_while_action_runs(self):
        self.start_action()
        self.controller.submit("status")
        self.controller.submit("help")
        self.assertEqual(self.lines, ["goto 2", "status", "help"])
        self.assertFalse(self.normal_release.is_set())

    def test_busy_commands_are_rejected_and_never_queued(self):
        self.start_action()
        for line in ("scan", "start", "confirm abc12345", "reset", "goto 1", "cancel"):
            self.controller.submit(line)
        self.normal_release.set()
        self.controller.close()
        self.assertEqual(self.lines, ["goto 2"])
        self.assertEqual(sum("nothing was queued" in line for line in self.output), 6)

    def test_stop_in_progress_blocks_actions_and_recovery(self):
        self.stop_release.clear()
        self.controller.submit("stop")
        self.assertTrue(self.stopping.wait(1))
        for line in ("reset", "start", "scan"):
            self.controller.submit(line)
        self.controller.submit("halt")
        self.assertEqual(self.lines, [])
        self.session.request_stop.assert_called_once_with()
        self.assertTrue(any("already running" in line for line in self.output))

    def test_idle_stop_discards_proposal_without_waiting_for_gateway(self):
        self.stop_release.clear()
        self.controller.submit("stop")
        self.assertTrue(self.stopping.wait(1))
        self.language.cancel.assert_called_once_with()
        self.assertFalse(self.stop_release.is_set())

    def test_proposal_is_cancelled_after_busy_interpreter_exits(self):
        finished = threading.Event()
        cancelled = threading.Event()

        def interpret(line):
            self.normal_started.set()
            self.normal_release.wait(3)
            self.language.pending = {"token": "stale"}
            finished.set()
            return True

        def cancel():
            self.assertTrue(finished.is_set(), "cancel must not enter a held language lock")
            self.language.pending = None
            cancelled.set()

        self.controller.handler = interpret
        self.language.cancel.side_effect = cancel
        self.controller.submit("pick cup two")
        self.assertTrue(self.normal_started.wait(1))
        self.controller.submit("stop")
        self.assertTrue(self.stopping.wait(1))
        self.language.cancel.assert_not_called()
        self.normal_release.set()
        self.assertTrue(cancelled.wait(1))
        self.assertIsNone(self.language.pending)

    def test_failed_stop_keeps_latch_and_reports_unconfirmed(self):
        self.session.request_stop.side_effect = RuntimeError("gateway timeout")
        self.controller.submit("stop")
        self.controller.close()
        self.assertTrue(self.session.stop_requested)
        self.assertTrue(self.session.motion_uncertain)
        self.assertTrue(any("unconfirmed" in line and "gateway timeout" in line for line in self.output))
        self.session.acknowledge_recovery.assert_not_called()

    def test_negative_stop_result_does_not_claim_robot_stopped(self):
        self.session.request_stop.side_effect = None
        self.session.request_stop.return_value = {
            "dry_run": False, "stopped": False, "message": "Gateway did not confirm stopping"}
        self.controller.submit("stop")
        self.controller.close()
        self.assertTrue(any("Robot stop is unconfirmed" in line for line in self.output))

    def test_quit_while_busy_requests_stop_without_home_or_resume(self):
        self.start_action()
        self.assertFalse(self.controller.submit("quit"))
        self.assertTrue(self.latched.is_set())
        self.assertTrue(self.stopping.wait(1))
        self.assertEqual(self.lines, ["goto 2"])
        self.session.acknowledge_recovery.assert_not_called()

    def test_idle_quit_and_idle_interrupt_send_no_stop(self):
        self.assertFalse(self.controller.submit("quit"))
        self.controller.close(interrupt=True)
        self.session.latch_stop.assert_not_called()
        self.session.request_stop.assert_not_called()
        self.assertEqual(self.lines, [])

    def test_interrupted_close_requests_stop_then_waits_for_workers(self):
        self.start_action()
        closed = threading.Event()

        def close():
            self.controller.close(interrupt=True)
            closed.set()

        thread = threading.Thread(target=close)
        thread.start()
        try:
            self.assertTrue(self.stopping.wait(1))
            self.assertFalse(closed.is_set())
            self.normal_release.set()
            self.assertTrue(closed.wait(1))
        finally:
            self.normal_release.set()
            thread.join(3)

    def test_stop_still_attempted_if_recovery_persistence_raises(self):
        def latch_with_error():
            self.session.stop_requested = True
            raise OSError("journal is read only")

        self.session.latch_stop.side_effect = latch_with_error
        self.controller.submit("stop")
        self.assertTrue(self.stopping.wait(1))
        self.assertTrue(self.session.stop_requested)
        self.assertTrue(any("journal is read only" in line for line in self.output))


if __name__ == "__main__":
    unittest.main()
