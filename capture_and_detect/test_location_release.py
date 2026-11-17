"""Offline regression coverage for explicit placement release; no device access."""

from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from cup_inventory import SelectionError
from recovery_journal import RecoveryJournal
from test_web_controller import WebFixture
from web_controller import WebController, WebError


class LocationReleaseFixture(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        for method in ("get", "post"):
            forbidden = patch("robot_client.requests." + method,
                              side_effect=AssertionError("Location release must not contact hardware"))
            forbidden.start()
            self.addCleanup(forbidden.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.fixture = WebFixture(directory.name, dry_run=False)
        self.session = self.fixture.session
        self.robot = self.fixture.robot
        self.grid = self.session.placing_grid
        self.recovery = RecoveryJournal(self.directory / "release_recovery.json")
        self.session.recovery = self.recovery
        self.session.capture = Mock(wraps=self.fixture.capture)
        self.robot.read_control_status = Mock(side_effect=AssertionError("No controller status read is needed"))
        self.robot.control_action = Mock(side_effect=AssertionError("Release must not send controller actions"))
        self.robot.request_stop = Mock(side_effect=AssertionError("Release must not request a robot stop"))

    def mark_locations(self):
        value = json.loads(self.grid.path.read_text())
        value["locations"][0]["occupied"] = True
        value["locations"][1].update(occupied=True, reserved=True, reservation_id="old-sequence")
        value["locations"][2]["vision_blocked"] = True
        self.grid.path.write_text(json.dumps(value))
        return self.grid.snapshot()

    def assert_no_device_calls(self, *, captures=0):
        self.assertEqual(self.session.capture.call_count, captures)
        self.assertEqual(self.robot.jobs, [])
        self.assertEqual(self.robot.commands, [])
        self.robot.read_control_status.assert_not_called()
        self.robot.control_action.assert_not_called()
        self.robot.request_stop.assert_not_called()
        self.fixture.interpreter.interpret.assert_not_called()

    def assert_locations_released(self, before):
        after = self.grid.snapshot()
        for old, new in zip(before["locations"], after["locations"]):
            self.assertEqual({key: old[key] for key in ("id", "x", "y", "z")},
                             {key: new[key] for key in ("id", "x", "y", "z")})
            self.assertIs(new["occupied"], False)
            self.assertFalse(new.get("reserved", False))
            self.assertFalse(new.get("vision_blocked", False))
            self.assertNotIn("reservation_id", new)
        self.assertEqual(len(after["locations"]), len(before["locations"]))
        return after


class SessionLocationReleaseTests(LocationReleaseFixture):
    def release(self, **changes):
        arguments = {"fingerprint": self.grid.snapshot()["fingerprint"], "acknowledged": True}
        arguments.update(changes)
        return self.session.release_locations(**arguments)

    def test_release_persists_all_slots_preserves_coordinates_and_invalidates_scan(self):
        self.session.scan()
        self.session.batch_progress = {"state": "completed"}
        before = self.mark_locations()
        result = self.release()
        self.assertEqual(result["count"], 3)
        self.assertIsInstance(result["message"], str)
        self.assert_locations_released(before)
        self.assertFalse(self.session.scan_usable)
        self.assertIsNone(self.session.batch_progress)
        with self.assertRaisesRegex(SelectionError, "Run scan"):
            self.session.preview_selection(action="goto", capture_id=self.session.inventory["capture"], cup_id=1)
        self.assert_no_device_calls(captures=1)

    def test_release_requires_an_explicit_boolean_acknowledgement(self):
        self.mark_locations()
        before = self.grid.path.read_bytes()
        for invalid in (False, None, 1, "true", "yes", [], {}):
            with self.subTest(acknowledged=invalid), self.assertRaises((SelectionError, ValueError)):
                self.release(acknowledged=invalid)
            self.assertEqual(self.grid.path.read_bytes(), before)
        with self.assertRaises(TypeError):
            self.session.release_locations(fingerprint=self.grid.snapshot()["fingerprint"])
        self.assert_no_device_calls()

    def test_stale_grid_fingerprint_does_not_overwrite_new_state(self):
        self.session.scan()
        stale = self.grid.snapshot()["fingerprint"]
        self.mark_locations()
        before = self.grid.path.read_bytes()
        with self.assertRaisesRegex(SelectionError, "changed"):
            self.release(fingerprint=stale)
        self.assertEqual(self.grid.path.read_bytes(), before)
        self.assertFalse(self.session.scan_usable)
        self.assert_no_device_calls(captures=1)

    def test_active_operation_and_pending_stop_reject_release_without_writes(self):
        self.mark_locations()
        before = self.grid.path.read_bytes()
        self.session._activity_lock.acquire()
        try:
            with self.assertRaises(SelectionError):
                self.release()
        finally:
            self.session._activity_lock.release()
        self.session._stop_pending.set()
        try:
            with self.assertRaises(SelectionError):
                self.release()
        finally:
            self.session._stop_pending.clear()
        self.assertEqual(self.grid.path.read_bytes(), before)
        self.assert_no_device_calls()

    def test_release_keeps_motion_uncertainty_stop_and_recovery_journal(self):
        before = self.mark_locations()
        self.recovery.begin({"action": "interrupted_pick", "cup_id": 1})
        journal = self.recovery.path.read_bytes()
        self.session.motion_uncertain = True
        self.session.latch_stop()
        self.session._stop_pending.clear()  # The independent stop worker has finished.
        self.release()
        self.assert_locations_released(before)
        self.assertTrue(self.session.motion_uncertain)
        self.assertTrue(self.session.stop_requested)
        self.assertTrue(self.recovery.required())
        self.assertEqual(self.recovery.path.read_bytes(), journal)
        self.assertFalse(self.session.scan_usable)
        self.assert_no_device_calls()

    def test_absent_grid_and_storage_failure_never_enable_motion(self):
        self.session.scan()
        self.session.placing_grid = None
        with self.assertRaises(SelectionError):
            self.release()
        self.session.placing_grid = self.grid
        before = self.grid.path.read_bytes()
        with patch.object(self.grid, "release_all", side_effect=OSError("Disk unavailable")):
            with self.assertRaises(OSError):
                self.release()
        self.assertEqual(self.grid.path.read_bytes(), before)
        self.assertFalse(self.session.scan_usable)
        self.assert_no_device_calls(captures=1)


class WebLocationReleaseTests(LocationReleaseFixture):
    def setUp(self):
        super().setUp()
        self.controller = WebController(self.fixture.factory, mode="live")
        self.addCleanup(self.controller.close)
        self.controller.start()
        self.wait_idle()
        self.assertTrue(self.controller.state()["ready"])

    def wait_idle(self):
        deadline = time.monotonic() + 3
        while self.controller.state()["busy"]:
            if time.monotonic() > deadline:
                self.fail("Location release worker did not finish in three seconds")
            time.sleep(.003)
        return self.controller.state()

    def submit(self, operation, **payload):
        payload.setdefault("request_id", uuid.uuid4().hex)
        self.assertEqual(self.controller.submit(operation, payload), {"accepted": True})
        return self.wait_idle()

    def release(self, **changes):
        payload = {"fingerprint": self.grid.snapshot()["fingerprint"], "acknowledged": True}
        payload.update(changes)
        return self.submit("release_locations", **payload)

    def cup_proposal(self):
        capture = self.submit("scan")["inventory"]["capture"]
        state = self.submit("select", capture_id=capture, cup_id=1, action="hover")
        self.assertIsNotNone(state["proposal"])
        return copy.deepcopy(state["proposal"])

    def test_public_grid_fingerprint_binds_release_and_changes_after_write(self):
        before = self.mark_locations()
        self.controller.publish_session()
        self.assertEqual(self.controller.state()["placement_grid"]["fingerprint"], before["fingerprint"])
        state = self.release(fingerprint=before["fingerprint"])
        self.assertIsNone(state["error"])
        after = self.assert_locations_released(before)
        self.assertEqual(state["placement_grid"]["fingerprint"], after["fingerprint"])
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])
        self.assertFalse(state["scan_usable"])
        self.assert_no_device_calls()

    def test_payload_requires_exact_fields_valid_fingerprint_and_boolean_ack(self):
        good = {"request_id": uuid.uuid4().hex, "fingerprint": self.grid.snapshot()["fingerprint"],
                "acknowledged": True}
        invalid = []
        for key in ("fingerprint", "acknowledged"):
            invalid.append({name: value for name, value in good.items() if name != key})
        invalid.extend(dict(good, acknowledged=value) for value in (False, None, 1, "true"))
        invalid.extend(dict(good, fingerprint=value) for value in (None, 1, "x" * 64, "a" * 63))
        invalid.append(dict(good, force=True))
        before = self.grid.path.read_bytes()
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(WebError) as caught:
                self.controller.submit("release_locations", payload)
            self.assertEqual(caught.exception.status, 422)
        self.assertEqual(self.grid.path.read_bytes(), before)
        self.assertFalse(self.controller.state()["busy"])
        self.assert_no_device_calls()

    def test_release_cancels_cup_and_admin_previews_and_requires_new_scan(self):
        cup = self.cup_proposal()
        state = self.release()
        self.assertIsNone(state["error"])
        self.assertIsNone(state["proposal"])
        self.assertIsNone(self.fixture.language.pending)
        self.assertFalse(state["scan_usable"])
        stale = self.submit("confirm", **{key: cup[key] for key in ("token", "capture_id", "cup_id", "action")})
        self.assertIsNotNone(stale["error"])
        admin = self.submit("control_preview", action="home", arm="both")["robot_controls"]["proposal"]
        self.assertIsNotNone(admin)
        state = self.release()
        self.assertIsNone(state["robot_controls"]["proposal"])
        self.assertIsNone(self.controller.controls.pending)
        stale = self.submit("control_confirm", token=admin["token"], acknowledged=True)
        self.assertIsNotNone(stale["error"])
        self.assert_no_device_calls(captures=1)

    def test_stale_fingerprint_and_storage_error_discard_pending_authorization(self):
        for cause in ("stale", "storage"):
            with self.subTest(cause=cause):
                self.cup_proposal()
                fingerprint = self.grid.snapshot()["fingerprint"]
                before = self.grid.path.read_bytes()
                if cause == "stale":
                    self.mark_locations()
                    before = self.grid.path.read_bytes()
                    state = self.release(fingerprint=fingerprint)
                else:
                    with patch.object(self.grid, "release_all", side_effect=OSError("Disk unavailable")):
                        state = self.release(fingerprint=fingerprint)
                self.assertIsNotNone(state["error"])
                self.assertIsNone(state["proposal"])
                self.assertIsNone(state["robot_controls"]["proposal"])
                self.assertIsNone(self.fixture.language.pending)
                self.assertFalse(state["scan_usable"])
                self.assertEqual(self.grid.path.read_bytes(), before)
        self.assert_no_device_calls(captures=2)

    def test_replayed_request_id_is_not_executed_again(self):
        request_id = uuid.uuid4().hex
        self.release(request_id=request_id)
        before = self.mark_locations()
        saved = self.grid.path.read_bytes()
        with self.assertRaisesRegex(WebError, "already received"):
            self.release(request_id=request_id, fingerprint=before["fingerprint"])
        self.assertEqual(self.grid.path.read_bytes(), saved)
        self.assert_no_device_calls()

    def test_busy_camera_operation_rejects_release_without_queuing(self):
        before = self.mark_locations()
        saved = self.grid.path.read_bytes()
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.fixture.capture_gate = gate
        self.controller.submit("scan", {"request_id": uuid.uuid4().hex})
        self.assertTrue(self.fixture.capture_entered.wait(1))
        with self.assertRaisesRegex(WebError, "No extra action was queued"):
            self.controller.submit("release_locations", {"request_id": uuid.uuid4().hex,
                                   "fingerprint": before["fingerprint"], "acknowledged": True})
        gate.set()
        self.wait_idle()
        self.assertEqual(self.grid.path.read_bytes(), saved)
        self.assert_no_device_calls(captures=1)

    def test_release_after_completed_stop_preserves_every_recovery_guard(self):
        before = self.mark_locations()
        self.recovery.begin({"action": "software_stop", "stopped": True})
        journal = self.recovery.path.read_bytes()
        self.session.latch_stop()
        self.session._stop_pending.clear()
        self.controller.publish_session()
        state = self.release()
        self.assertIsNone(state["error"])
        self.assert_locations_released(before)
        self.assertTrue(state["stop_requested"])
        self.assertTrue(state["recovery_required"])
        self.assertFalse(state["scan_usable"])
        self.assertTrue(self.session.motion_uncertain)
        self.assertEqual(self.recovery.path.read_bytes(), journal)
        self.assert_no_device_calls()


if __name__ == "__main__":
    unittest.main()
