"""Software-stop races exercised with mocked HTTP and temporary state only."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from cup_commands import CupSession
from cup_inventory import SelectionError
from recovery_journal import RecoveryJournal
from robot_client import RobotClient, RobotCommandError
from test_cup_selection import inventory, RobotDouble
from test_robot_client import job, response, lease_response, done_response
from test_support_grid import temporary_grid


class SoftwareStopTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        self.endpoints = []
        self.client = RobotClient("http://offline.invalid/api/v1", {})
        self.move_hook = None
        self.stop_hook = None
        self.acquire_hook = None
        self.http = patch("robot_client.requests.post", side_effect=self.serve)
        self.post = self.http.start()
        self.addCleanup(self.http.stop)
        self.get_patch = patch("robot_client.requests.get", side_effect=AssertionError("Unexpected HTTP GET"))
        self.get_patch.start()
        self.addCleanup(self.get_patch.stop)

    def serve(self, url, **options):
        endpoint = url.removeprefix("http://offline.invalid/api/v1")
        self.endpoints.append(endpoint)
        if endpoint == "/control/acquire":
            if self.acquire_hook:
                self.acquire_hook()
            return lease_response()
        if endpoint == "/control/renew":
            return lease_response()
        if endpoint == "/control/release":
            return response({"success": True})
        if endpoint == "/robot/rws/move":
            if self.move_hook:
                self.move_hook()
            return done_response()
        if endpoint == "/robot/rapid/stop":
            if self.stop_hook:
                return self.stop_hook()
            return response({"execution_state": "stopped"})
        raise AssertionError(f"Unexpected endpoint: {endpoint}")

    def thread(self, function):
        errors = []
        results = []

        def run():
            try:
                results.append(function())
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return worker, errors, results

    def join(self, worker):
        worker.join(3)
        self.assertFalse(worker.is_alive(), "Mocked operation did not finish")

    def test_idle_stop_acquires_and_releases_lease(self):
        result = self.client.request_stop()
        self.assertTrue(result["stopped"])
        self.assertTrue(self.client.stop_requested)
        self.assertEqual(self.endpoints, ["/control/acquire", "/robot/rapid/stop", "/control/release"])
        with self.assertRaisesRegex(RobotCommandError, "Software stop"):
            self.client.execute_jobs([job()])

    def test_stop_interrupts_inflight_move_without_execution_lock(self):
        moving, stop_sent = threading.Event(), threading.Event()

        def move_wait():
            moving.set()
            self.assertTrue(stop_sent.wait(2))

        self.move_hook = move_wait
        self.stop_hook = lambda: (stop_sent.set(), response({"execution_state": "stopped"}))[1]
        completed = Mock()
        worker, errors, _ = self.thread(lambda: self.client.execute_jobs([job(), job()], after_done=completed))
        self.assertTrue(moving.wait(2))
        self.assertTrue(self.client.request_stop()["stopped"])
        self.join(worker)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RobotCommandError)
        completed.assert_not_called()
        self.assertEqual(self.endpoints.count("/robot/rws/move"), 1)
        self.assertEqual(self.endpoints.count("/control/release"), 1)

    def test_latch_in_precommand_hook_prevents_dispatch_and_keeps_lease_for_stop(self):
        with self.assertRaisesRegex(RobotCommandError, "Software stop"):
            self.client.execute_jobs([job()], before_send=lambda *_: self.client.latch_stop())
        self.assertEqual(self.endpoints, ["/control/acquire"])
        self.assertTrue(self.client.request_stop()["stopped"])
        self.assertEqual(self.endpoints, ["/control/acquire", "/robot/rapid/stop", "/control/release"])

    def test_stop_during_acquisition_never_dispatches_motion(self):
        self.acquire_hook = self.client.latch_stop
        with self.assertRaisesRegex(RobotCommandError, "Software stop"):
            self.client.execute_jobs([job()])
        self.client.request_stop()
        self.assertNotIn("/robot/rws/move", self.endpoints)

    def test_stop_owns_lease_until_its_reply_even_when_move_finishes(self):
        moving, stop_sent, finish_stop = (threading.Event() for _ in range(3))

        def move_wait():
            moving.set()
            self.assertTrue(stop_sent.wait(2))

        def stop_wait():
            stop_sent.set()
            self.assertTrue(finish_stop.wait(2))
            return response({"execution_state": "stopped"})

        self.move_hook, self.stop_hook = move_wait, stop_wait
        movement, errors, _ = self.thread(lambda: self.client.execute_jobs([job()]))
        self.assertTrue(moving.wait(2))
        stopping, stop_errors, _ = self.thread(self.client.request_stop)
        self.assertTrue(stop_sent.wait(2))
        self.assertNotIn("/control/release", self.endpoints)
        with self.assertRaisesRegex(RobotCommandError, "Wait"):
            self.client.clear_stop()
        finish_stop.set()
        self.join(stopping)
        self.join(movement)
        self.assertEqual(stop_errors, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.endpoints.count("/control/release"), 1)

    def test_stop_network_failure_is_not_retried_or_cleared(self):
        self.stop_hook = Mock(side_effect=requests.Timeout("lost response"))
        with self.assertRaises(RobotCommandError):
            self.client.request_stop()
        self.stop_hook.assert_called_once()
        self.assertTrue(self.client.stop_requested)
        self.assertEqual(self.endpoints.count("/control/release"), 1)

    def test_robot_recovery_cannot_clear_latch_before_stop_worker_starts(self):
        self.client.latch_stop()
        with self.assertRaisesRegex(RobotCommandError, "pending software stop"):
            self.client.clear_stop()
        self.assertTrue(self.client.stop_requested)

    def test_stop_requires_explicit_stopped_state(self):
        self.stop_hook = lambda: response({"execution_state": "running"})
        with self.assertRaisesRegex(RobotCommandError, "not confirmed"):
            self.client.request_stop()
        self.assertTrue(self.client.stop_requested)

    def test_dry_run_stop_and_latched_execution_make_no_http(self):
        self.client.dry_run = True
        result = self.client.request_stop()
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["stopped"])
        with self.assertRaises(RobotCommandError):
            self.client.execute_jobs([job()])
        self.client.clear_stop()
        self.client.execute_jobs([job()])
        self.post.assert_not_called()

    def session(self, robot=None, recovery=None, save=None):
        counter = iter(range(10, 100))

        def capture(_purpose):
            sequence = next(counter)
            return inventory(capture=f"scan-{sequence}", sequence=sequence), 99

        session = CupSession(capture, save or Mock(), robot or self.client, "cal-1",
                             clock=lambda: 100, recovery=recovery, placing_grid=temporary_grid(self))
        session.scan()
        return session

    def journal(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return RecoveryJournal(Path(directory.name) / "recovery.json")

    def test_session_stop_failure_still_persists_recovery(self):
        journal = self.journal()
        session = self.session(recovery=journal)
        self.stop_hook = Mock(side_effect=requests.Timeout("lost response"))
        result = session.request_stop()
        self.assertFalse(result["stopped"])
        self.assertIn("unconfirmed", result["message"])
        self.assertTrue(journal.required())
        self.assertEqual(session.status_summary(), {"stop_requested": True, "recovery_required": True,
                                                   "scan_usable": False, "batch": None})

    def test_recovery_is_durable_before_stop_network_request(self):
        journal = self.journal()
        session = self.session(recovery=journal)

        def verify_journal():
            self.assertTrue(journal.required())
            return response({"execution_state": "stopped"})

        self.stop_hook = verify_journal
        self.assertTrue(session.request_stop()["stopped"])

    def test_failed_recovery_write_does_not_prevent_physical_stop(self):
        journal = Mock()
        journal.required.return_value = False
        journal.begin.side_effect = OSError("disk full")
        session = self.session(recovery=journal)
        result = session.request_stop()
        self.assertTrue(result["stopped"])
        self.assertTrue(result["recovery_save_failed"])
        self.assertTrue(session.stop_requested)
        self.assertIn("/robot/rapid/stop", self.endpoints)

    def test_stop_during_scan_does_not_authorize_captured_inventory(self):
        session = self.session()

        def capture(_purpose):
            session.latch_stop()
            return inventory(), 99

        session.capture = capture
        with self.assertRaisesRegex(SelectionError, "Software stop"):
            session.scan()
        self.assertFalse(session.scan_usable)
        self.post.assert_not_called()

    def test_latch_dominates_late_success_assignments(self):
        session = self.session()
        session.latch_stop()
        # Model a worker assigning an outcome computed just before Stop arrived.
        session.motion_uncertain = False
        session.scan_usable = True
        self.assertTrue(session.motion_uncertain)
        self.assertFalse(session.scan_usable)
        with self.assertRaisesRegex(SelectionError, "Software stop"):
            session.preview_selection(action="hover", capture_id="scan-10", cup_id=1)

    def test_reset_rejected_during_scan_and_pending_stop(self):
        session = self.session()
        session._activity_lock.acquire()
        try:
            with self.assertRaisesRegex(SelectionError, "active operation"):
                session.acknowledge_recovery()
        finally:
            session._activity_lock.release()
        session.latch_stop()
        with self.assertRaisesRegex(SelectionError, "stop request"):
            session.acknowledge_recovery()
        session.request_stop()
        session.acknowledge_recovery()
        self.assertFalse(session.stop_requested)
        self.assertFalse(session.motion_uncertain)
        self.assertFalse(session.scan_usable)

    def test_stop_after_final_done_keeps_recovery_and_reserved_destination(self):
        journal = self.journal()
        robot = RobotDouble()
        robot.latch_stop = Mock()
        robot.request_stop = Mock(return_value={"dry_run": False, "stopped": True, "message": "Stopped"})
        session = self.session(robot=robot, recovery=journal)
        robot.after_move = lambda count: session.request_stop() if count == 25 else None
        with self.assertRaisesRegex(SelectionError, "Software stop"):
            session.execute_selection(action="goto", capture_id="scan-10", cup_id=1)
        self.assertTrue(journal.required())
        slot = session.placing_grid.snapshot()["locations"][0]
        self.assertTrue(slot["occupied"])
        self.assertTrue(slot["reserved"])
        self.assertTrue(session.status_summary()["recovery_required"])

    def test_stop_during_final_journal_clear_cannot_erase_lockout(self):
        journal = self.journal()
        robot = RobotDouble()
        robot.request_stop = Mock(return_value={"dry_run": False, "stopped": True, "message": "Stopped"})
        session = self.session(robot=robot, recovery=journal)
        original_clear = journal.clear

        def clear_then_stop():
            original_clear()
            session.latch_stop()

        journal.clear = clear_then_stop
        with self.assertRaisesRegex(SelectionError, "Software stop"):
            session.execute_selection(action="hover", capture_id="scan-10", cup_id=1)
        self.assertTrue(journal.required())
        self.assertTrue(session.stop_requested)
        self.assertTrue(session.motion_uncertain)
        self.assertFalse(session.scan_usable)

    def test_recovery_acknowledgement_rejects_stop_rpc_in_progress(self):
        session = self.session(recovery=self.journal())
        stopping, finish = threading.Event(), threading.Event()

        def stop_wait():
            stopping.set()
            self.assertTrue(finish.wait(2))
            return response({"execution_state": "stopped"})

        self.stop_hook = stop_wait
        worker, errors, _ = self.thread(session.request_stop)
        self.assertTrue(stopping.wait(2))
        with self.assertRaisesRegex(SelectionError, "stop request"):
            session.acknowledge_recovery()
        finish.set()
        self.join(worker)
        self.assertEqual(errors, [])
        session.acknowledge_recovery()
        self.assertFalse(session.stop_requested)

    def test_dry_run_session_stop_never_contacts_gateway(self):
        self.client.dry_run = True
        journal = self.journal()
        session = self.session(recovery=journal)
        result = session.request_stop()
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["stopped"])
        self.assertTrue(session.motion_uncertain)
        self.assertFalse(journal.path.exists())
        session.acknowledge_recovery()
        self.assertFalse(session.stop_requested)
        self.assertFalse(journal.path.exists())
        self.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
