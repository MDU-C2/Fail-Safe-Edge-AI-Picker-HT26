"""Saved-scan placement evidence remains active without recapturing between cups."""

from contextlib import redirect_stdout
import copy
import io
import json
import unittest
from unittest.mock import Mock

from cup_commands import CupSession
from cup_inventory import SelectionError
from recovery_journal import RecoveryJournal
from test_cup_selection import TRANSFER_JOB_COUNT, RobotDouble, inventory
from test_support_grid import temporary_grid


class PlacementVerificationTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        self.grid = temporary_grid(self)
        self.robot = RobotDouble()
        self.recovery = RecoveryJournal(self.grid.path.with_name("test_recovery.json"))

    @staticmethod
    def observed(state="clear", *, sequence=10, slot_id=1, include=True):
        result = inventory(capture=f"capture-{sequence}", sequence=sequence)
        if include:
            result["placement_observation"] = {
                "frame_count": 1, "applied": False,
                "slots": [{"id": slot_id, "state": state, "reserved": False,
                           "reason": "synthetic camera evidence", "valid_fraction": 1.0,
                           "reason_code": {"clear": "clear", "unknown": "table_depth", "occupied": "raised_depth"}[state],
                           "cup_count": 0, "assigned_cups": [],
                           "evidence": ["raised_depth"] if state == "occupied" else [],
                           "table_fraction": 1.0, "visible_fraction": 1.0, "floor_visible_fraction": 1.0,
                           "polygon_px": [[100, 100], [120, 100], [120, 120], [100, 120]]}],
            }
        return result

    @classmethod
    def recheck_observed(cls, state="clear", *, sequence=11, reason_code=None):
        result = cls.observed(state, sequence=sequence)
        if reason_code is not None:
            result["placement_observation"]["slots"][0]["reason_code"] = reason_code
        return result

    def session(self, *, first=None, unused=None, scan=None, retries=(), home_after_cup=False):
        scan = self.observed() if scan is None else scan
        if "placement_observation" in scan:
            observation = scan["placement_observation"]
            observation["grid_snapshot"] = self.grid.snapshot()
            prototype = observation["slots"][0]
            observation["slots"] = [dict(copy.deepcopy(prototype), id=place["id"])
                                    for place in observation["grid_snapshot"]["locations"]]
            observation.update(frame_count=1, frame_sequences=[10], released_ids=[])
        # Extra values deliberately remain unread; this helper also supports
        # legacy callers while proving their verification policy is inactive.
        observations = [(scan, 99)] + [(item, 99) for item in (first, unused, *retries) if item is not None]
        self.capture = Mock(side_effect=observations)
        self.save = Mock()
        session = CupSession(self.capture, self.save, self.robot, "cal-1", clock=lambda: 100,
                             placing_grid=self.grid, recovery=self.recovery, home_after_cup=home_after_cup)
        session.scan()
        return session

    @staticmethod
    def execute(session, *, action="goto"):
        return session.execute_selection(action=action, capture_id=session.inventory["capture"], cup_id=1)

    def test_clear_saved_destination_completes_without_any_further_camera_request(self):
        session = self.session(first=self.observed("occupied", sequence=11))
        jobs = self.execute(session)
        self.assertEqual(len(jobs), TRANSFER_JOB_COUNT)
        self.capture.assert_called_once_with("scan")
        audit = session.inventory["cups"][0]["last_verification"]
        self.assertEqual(audit["source"], "saved_scan")
        self.assertEqual(audit["placement_visibility"]["id"], 1)
        self.assertEqual(audit["placement_checks"][0]["capture"], "capture-10")
        self.assertFalse(self.recovery.required())
        self.assertTrue(session.scan_usable)

    def test_nonclear_scan_still_blocks_all_placements_without_polling(self):
        for state in ("occupied", "unknown"):
            with self.subTest(state=state):
                self.grid = temporary_grid(self)
                session = self.session(scan=self.observed(state))
                before = self.grid.path.read_bytes()
                with self.assertRaises(SelectionError):
                    self.execute(session)
                self.assertEqual(self.robot.jobs, [])
                self.assertEqual(self.grid.path.read_bytes(), before)
                self.capture.assert_called_once_with("scan")
                self.assertFalse(self.recovery.required())

    def test_missing_duplicate_or_malformed_saved_destination_blocks_before_motion(self):
        for change in ("missing", "duplicate", "polygon", "reference", "nonfinite", "evidence", "metrics"):
            with self.subTest(change=change):
                self.grid = temporary_grid(self)
                session = self.session()
                observation = session.inventory["placement_observation"]
                slot = observation["slots"][0]
                if change == "missing": observation["slots"].pop(0)
                elif change == "duplicate": observation["slots"].append(copy.deepcopy(slot))
                elif change == "polygon": slot["polygon_px"] = [[1, 1], [1, 1], [1, 1]]
                elif change == "reference": observation["table_reference"] = []
                elif change == "nonfinite": slot["polygon_px"][0][0] = float("nan")
                elif change == "evidence": slot["evidence"] = ["raised_depth"]
                elif change == "metrics": slot["valid_fraction"] = 0.4
                with self.assertRaises(SelectionError): self.execute(session)
                self.assertEqual(self.robot.jobs, [])
                self.capture.assert_called_once_with("scan")
                self.assertFalse(any(slot.get("reserved") for slot in self.grid.snapshot()["locations"]))

    def test_edited_destination_geometry_requires_new_scan_even_with_new_preview(self):
        for field in ("x", "y", "z", "cell_size_mm"):
            with self.subTest(field=field):
                self.grid = temporary_grid(self)
                session = self.session()
                value = json.loads(self.grid.path.read_text())
                if field == "cell_size_mm": value[field] = 90
                else: value["locations"][0][field] += 1
                self.grid.path.write_text(json.dumps(value))
                with self.assertRaisesRegex(SelectionError, "geometry changed"):
                    self.execute(session)
                self.assertEqual(self.robot.jobs, [])
                self.capture.assert_called_once_with("scan")

    def test_clear_dry_run_preserves_grid_and_records_the_saved_slot(self):
        self.robot.dry_run = True
        session = self.session()
        before = self.grid.path.read_bytes()
        self.execute(session)
        self.assertEqual(self.grid.path.read_bytes(), before)
        self.assertEqual(session.inventory["cups"][0]["last_verification"]["placement_visibility"]["id"], 1)
        self.capture.assert_called_once_with("scan")
        self.assertFalse(self.recovery.required())

    def test_hover_does_not_require_clear_placement_destination(self):
        session = self.session(scan=self.observed("occupied"))
        jobs = self.execute(session, action="hover")
        self.assertEqual(len(jobs), 2)
        self.capture.assert_called_once_with("scan")

    def test_other_nonclear_locations_do_not_block_selected_clear_destination(self):
        session = self.session()
        session.inventory["placement_observation"]["slots"][1] = self.observed("unknown", slot_id=2)["placement_observation"]["slots"][0]
        self.assertEqual(len(self.execute(session)), TRANSFER_JOB_COUNT)
        self.capture.assert_called_once_with("scan")

    def test_saved_named_identity_check_receives_original_snapshot_without_age_limit(self):
        session = self.session()
        received = []
        def identity(snapshot, cup):
            received.append((snapshot["capture"], cup["id"]))
            session.clock = lambda: 100000
            return {"same": True}
        session.execute_selection(action="goto", capture_id="capture-10", cup_id=1, identity_check=identity)
        self.assertEqual(received, [("capture-10", 1)])
        self.capture.assert_called_once_with("scan")
        self.assertEqual(self.robot.jobs[-1]["name"], "right_clear")

    def test_stop_during_identity_check_still_prevents_dispatch(self):
        session = self.session()
        def identity(snapshot, cup): session.latch_stop()
        with self.assertRaisesRegex(SelectionError, "stop"):
            session.execute_selection(action="goto", capture_id="capture-10", cup_id=1, identity_check=identity)
        self.assertEqual(self.robot.jobs, [])
        self.capture.assert_called_once_with("scan")


if __name__ == "__main__":
    unittest.main()
