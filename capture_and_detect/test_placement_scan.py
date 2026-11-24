"""Offline scan reconciliation checks use temporary grid and recovery files only."""

import copy
import unittest
from unittest.mock import Mock, patch

from cup_commands import CupSession
from cup_inventory import SelectionError, atomic_json
from placement_records import combine
from recovery_journal import RecoveryJournal
from robot_client import RobotCommandError
from test_cup_selection import RobotDouble, detection, inventory
from test_support_grid import temporary_grid


class PlacementScanTests(unittest.TestCase):
    def setUp(self):
        self.grid = temporary_grid(self)
        self.robot = RobotDouble()
        self.save = Mock()

    def observed_inventory(self, states, *, snapshot=None, frame_states=None, frames=3):
        snapshot = self.grid.snapshot() if snapshot is None else snapshot
        observations = []
        for states_for_frame in (frame_states if frame_states is not None else [states] * frames):
            observations.append([
                {"id": location["id"], "state": states_for_frame[location["id"]],
                 "valid_fraction": 1.0, "polygon_px": [[100, 100], [120, 100], [120, 120], [100, 120]],
                 "reason": "synthetic depth evidence", "table_fraction": 1.0,
                 "visible_fraction": 1.0, "floor_visible_fraction": 1.0,
                 "reason_code": {"clear": "clear", "unknown": "table_depth", "occupied": "raised_depth"}[states_for_frame[location["id"]]],
                 "cup_count": 0, "assigned_cups": [],
                 "evidence": ["raised_depth"] if states_for_frame[location["id"]] == "occupied" else [],
                 "reserved": location.get("reserved", False)}
                for location in snapshot["locations"]])
        objects = []
        for u, x in ((100, 500), (200, 550), (300, 600)):
            objects.extend([detection("CUP", u, x=x), detection("CUP BASE", u, x=x)])
        result = inventory(objects=objects, sequence=12)
        result["placement_observation"] = combine(snapshot, observations, list(range(10, 10 + len(observations))))
        return result

    def session(self, result, *, recovery=None):
        capture = Mock(return_value=(result, 99))
        session = CupSession(capture, self.save, self.robot, "cal-1", clock=lambda: 100,
                             placing_grid=self.grid, recovery=recovery)
        return session, capture

    def occupy(self, *ids):
        value = {"locations": self.grid.snapshot()["locations"], "note": "preserve scan metadata"}
        for location in value["locations"]:
            if location["id"] in ids:
                location["occupied"] = True
        atomic_json(self.grid.path, value)

    def test_three_clear_frames_release_removed_cup_and_save_result_without_motion(self):
        self.occupy(1)
        result = self.observed_inventory({1: "clear", 2: "occupied", 3: "clear"})
        session, capture = self.session(result)
        with patch.object(self.grid, "reconcile_observation", wraps=self.grid.reconcile_observation) as reconcile:
            scanned = session.scan()
        capture.assert_called_once_with("scan")
        reconcile.assert_called_once_with(result["placement_observation"]["grid_snapshot"],
                                          {1: "clear", 2: "occupied", 3: "clear"})
        self.assertEqual([slot["occupied"] for slot in self.grid.snapshot()["locations"]],
                         [False, True, False])
        observation = scanned["placement_observation"]
        self.assertEqual(observation["frame_sequences"], [10, 11, 12])
        self.assertTrue(observation["applied"])
        self.assertEqual(observation["released_ids"], [1])
        self.assertIn("2 available", observation["message"])
        self.save.assert_called_once_with(scanned)
        self.assertTrue(session.scan_usable)
        self.assertEqual(self.robot.jobs, [])

    def test_unknown_visibility_reduces_capacity_without_changing_saved_occupancy(self):
        result = self.observed_inventory({1: "clear", 2: "unknown", 3: "clear"})
        session, _ = self.session(result)
        scanned = session.scan()
        slots = self.grid.snapshot()["locations"]
        self.assertFalse(slots[1]["occupied"])
        self.assertTrue(slots[1]["vision_blocked"])
        self.assertIn("2 available", scanned["placement_observation"]["message"])
        preview = session.preview_batch(capture_id=scanned["capture"])
        self.assertEqual([slot["id"] for slot in preview["placement"]["locations"]], [1, 3])
        self.assertEqual(len(preview["cup_ids"]), 2)
        self.assertEqual(len(preview["excluded_cup_ids"]), 1)
        self.assertEqual(self.robot.jobs, [])

    def test_one_uncertain_frame_prevents_releasing_an_occupied_location(self):
        self.occupy(1)
        clear = {1: "clear", 2: "clear", 3: "clear"}
        result = self.observed_inventory(clear, frame_states=[clear, {**clear, 1: "unknown"}, clear])
        session, _ = self.session(result)
        scanned = session.scan()
        location = self.grid.snapshot()["locations"][0]
        self.assertTrue(location["occupied"])
        self.assertTrue(location["vision_blocked"])
        self.assertEqual(scanned["placement_observation"]["released_ids"], [])

    def test_fresh_clear_scan_releases_stale_reservation_and_occupied_slots(self):
        reservation = self.grid.reserve(self.grid.preview())
        self.occupy(2, 3)
        result = self.observed_inventory({1: "clear", 2: "clear", 3: "clear"})
        session, _ = self.session(result)
        scanned = session.scan()
        self.assertFalse(any(slot["occupied"] or slot.get("reserved", False)
                             for slot in self.grid.snapshot()["locations"]))
        with self.assertRaisesRegex(SelectionError, "changed"):
            self.grid.validate_reservation(reservation)
        self.assertEqual(scanned["placement_observation"]["released_ids"], [1, 2, 3])
        self.assertFalse(any(slot["reserved"] for slot in scanned["placement_observation"]["slots"]))
        self.assertIn("3 available", scanned["placement_observation"]["message"])
        self.assertEqual(self.robot.jobs, [])

    def test_dry_run_scan_previews_observation_without_changing_real_bookkeeping(self):
        self.occupy(1)
        self.robot.dry_run = True
        result = self.observed_inventory({1: "clear", 2: "occupied", 3: "unknown"})
        session, _ = self.session(result)
        before = self.grid.path.read_bytes()
        with patch.object(self.grid, "reconcile_observation") as reconcile:
            scanned = session.scan()
        reconcile.assert_not_called()
        self.assertEqual(self.grid.path.read_bytes(), before)
        self.assertFalse(scanned["placement_observation"]["applied"])
        self.assertEqual(scanned["placement_observation"]["released_ids"], [])
        self.assertIn("Dry-run", scanned["placement_observation"]["message"])
        self.save.assert_called_once_with(scanned)
        self.assertEqual(self.robot.jobs, [])

    def test_recovery_required_scan_reconciles_grid_without_clearing_recovery_journal(self):
        self.grid.reserve(self.grid.preview(3))
        recovery = RecoveryJournal(self.grid.path.with_name("recovery.json"))
        recovery.begin({"action": "interrupted-pick"})
        result = self.observed_inventory({1: "clear", 2: "occupied", 3: "unknown"}, frames=1)
        session, _ = self.session(result, recovery=recovery)
        journal_before = recovery.path.read_bytes()
        scanned = session.scan()
        slots = self.grid.snapshot()["locations"]
        self.assertEqual([slot["occupied"] for slot in slots], [False, True, True])
        self.assertTrue(slots[2]["vision_blocked"])
        self.assertTrue(all("reserved" not in slot and "reservation_id" not in slot for slot in slots))
        self.assertEqual([slot["id"] for slot in self.grid.preview()["locations"]], [1])
        self.assertEqual(recovery.path.read_bytes(), journal_before)
        self.assertTrue(session.motion_uncertain)
        self.assertTrue(scanned["placement_observation"]["applied"])
        self.assertEqual(scanned["placement_observation"]["released_ids"], [1])
        self.assertIn("1 available", scanned["placement_observation"]["message"])
        self.assertIn("recovery still needs acknowledgement", scanned["placement_observation"]["message"])
        with self.assertRaisesRegex(SelectionError, "not confirmed"):
            session.preview_batch(capture_id=scanned["capture"])
        self.assertEqual(self.robot.jobs, [])

    def test_failed_handover_then_scan_uses_observed_cup_presence_without_more_motion(self):
        for state in ("clear", "occupied"):
            with self.subTest(observed_state=state):
                self.grid.release_all(self.grid.snapshot()["fingerprint"])
                result = self.observed_inventory({1: "clear", 2: "clear", 3: "clear"}, frames=1)
                session, capture = self.session(result)
                scanned = session.scan()
                self.robot.error = RobotCommandError("Handover interrupted")
                self.robot.error_at = "right_grip"
                with self.assertRaisesRegex(RobotCommandError, "Handover interrupted"):
                    session.execute_selection(action="goto", capture_id=scanned["capture"], cup_id=1)
                self.assertTrue(self.grid.snapshot()["locations"][0]["reserved"])
                self.assertTrue(session.motion_uncertain)
                capture.assert_called_once_with("scan")  # No automatic capture after failure.
                sent = copy.deepcopy(self.robot.jobs)
                capture.return_value = (self.observed_inventory({1: state, 2: "clear", 3: "clear"}, frames=1), 100)
                session.scan()
                slot = self.grid.snapshot()["locations"][0]
                self.assertEqual(slot["occupied"], state == "occupied")
                self.assertNotIn("reserved", slot)
                self.assertNotIn("reservation_id", slot)
                self.assertEqual(capture.call_count, 2)
                self.assertEqual(self.robot.jobs, sent)
                self.assertTrue(session.motion_uncertain)

    def test_scan_during_active_sequence_cannot_clear_its_reservations(self):
        self.grid.reserve(self.grid.preview())
        session, capture = self.session(self.observed_inventory({1: "clear", 2: "clear", 3: "clear"}))
        before = self.grid.path.read_bytes()
        with session._activity_lock:
            with self.assertRaisesRegex(SelectionError, "active operation"):
                session.scan()
        capture.assert_not_called()
        self.assertEqual(self.grid.path.read_bytes(), before)

    def test_grid_change_during_capture_does_not_reject_scan_or_trigger_recovery(self):
        result = self.observed_inventory({1: "clear", 2: "clear", 3: "clear"})
        session, capture = self.session(result)
        session.inventory = inventory(capture="old-scan")
        session.scan_usable = True

        def grid_changes_during_capture(_):
            self.grid.reserve(self.grid.preview())
            return result, 99

        capture.side_effect = grid_changes_during_capture
        session.scan()
        self.assertTrue(session.scan_usable)
        self.assertFalse(session.motion_uncertain)
        self.assertFalse(session.stop_requested)
        self.assertTrue(all(not slot["occupied"] and not slot.get("reserved", False)
                            for slot in self.grid.snapshot()["locations"]))
        self.assertTrue(result["placement_observation"]["applied"])
        self.save.assert_called_once_with(result)
        self.assertEqual(len(session.preview_batch(capture_id=result["capture"])["cup_ids"]), 3)
        self.assertEqual(self.robot.jobs, [])

    def test_unavailable_placement_check_stays_unauthorized_after_grid_access_recovers(self):
        result = inventory()
        session, capture = self.session(result)
        session.inventory = inventory(capture="previous-authorized-scan")
        session.scan_usable = True
        before = self.grid.path.read_bytes()

        def capture_with_unavailable_grid(_):
            try:
                self.grid.snapshot()
            except SelectionError as error:
                # Same failure result emitted by CaptureService: camera data
                # exists, but no destination geometry could be observed.
                result["placement_observation"] = {
                    "frame_count": 0, "slots": [], "applied": False, "released_ids": [],
                    "message": "Placement camera check unavailable: " + str(error)}
            return result, 99

        capture.side_effect = capture_with_unavailable_grid
        with patch.object(self.grid, "snapshot", side_effect=SelectionError("Temporary grid read failure")):
            with patch.object(self.grid, "reconcile_observation") as reconcile:
                with self.assertRaisesRegex(SelectionError, "Placement area could not be checked"):
                    session.scan()
        reconcile.assert_not_called()
        self.assertFalse(session.scan_usable)
        self.assertEqual(self.grid.path.read_bytes(), before)
        # The file is readable and has capacity again, but that recovery cannot
        # retroactively authorize the failed scan or its stale predecessor.
        self.assertEqual(len(self.grid.preview(3)["locations"]), 3)
        for capture_id in (result["capture"], "previous-authorized-scan"):
            with self.subTest(capture_id=capture_id), self.assertRaisesRegex(SelectionError, "Run scan"):
                session.preview_batch(capture_id=capture_id)
        self.save.assert_not_called()
        self.assertEqual(self.robot.jobs, [])

    def test_stop_during_capture_prevents_reconciliation_and_inventory_save(self):
        self.occupy(1)
        result = self.observed_inventory({1: "clear", 2: "clear", 3: "clear"})
        session, capture = self.session(result)
        session.scan_usable = True
        before = self.grid.path.read_bytes()

        def stop_during_capture(_):
            session.latch_stop()
            return result, 99

        capture.side_effect = stop_during_capture
        with patch.object(self.grid, "reconcile_observation") as reconcile:
            with self.assertRaisesRegex(SelectionError, "Software stop"):
                session.scan()
        reconcile.assert_not_called()
        self.save.assert_not_called()
        self.assertEqual(self.grid.path.read_bytes(), before)
        self.assertFalse(session.scan_usable)
        self.assertTrue(session.motion_uncertain)
        self.assertFalse(result["placement_observation"]["applied"])
        self.assertEqual(self.robot.jobs, [])


if __name__ == "__main__":
    unittest.main()
