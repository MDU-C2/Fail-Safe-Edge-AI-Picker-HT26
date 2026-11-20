"""Legacy verification settings now share V4's saved-scan execution policy."""

import copy
import threading
import unittest
from unittest.mock import Mock

from cup_commands import CupSession
from cup_inventory import SelectionError
from handover_plan import handover_jobs
from test_cup_selection import RobotDouble, detection, inventory, TRANSFER_JOB_COUNT
import test_placement_verification as fixtures


class MultiFrameVerificationTests(unittest.TestCase):
    setUp = fixtures.PlacementVerificationTests.setUp
    observed = staticmethod(fixtures.PlacementVerificationTests.observed)
    recheck_observed = classmethod(fixtures.PlacementVerificationTests.recheck_observed.__func__)
    execute = staticmethod(fixtures.PlacementVerificationTests.execute)

    def session(self, *, scan=None, home_after_cup=False, verification_frames=3, strict=False,
                max_age=15.0):
        scan = self.recheck_observed(sequence=10) if scan is None else scan
        observation = scan["placement_observation"]
        observation["grid_snapshot"] = self.grid.snapshot()
        observation["slots"] = [dict(copy.deepcopy(observation["slots"][0]), id=place["id"])
                                for place in observation["grid_snapshot"]["locations"]]
        observation.update(frame_count=1, frame_sequences=[10], released_ids=[])
        self.capture = Mock(side_effect=[(scan, 99.0)])
        self.save = Mock()
        session = CupSession(self.capture, self.save, self.robot, "cal-1", clock=lambda: 100.0,
                             max_result_age=max_age, placing_grid=self.grid, recovery=self.recovery,
                             home_after_cup=home_after_cup, verification_frames=verification_frames,
                             strict_single_verification=strict)
        session.scan()
        return session

    def frames(self, states):
        """Saved evidence fixtures retained for the Lynx compatibility tests."""
        frames = []
        for sequence, state in enumerate(states, 11):
            kind = "clear" if state == "clear" else "occupied" if state in ("raised_depth", "cup", "detected_object") else "unknown"
            frame = self.recheck_observed(kind, sequence=sequence, reason_code=state)
            slot = frame["placement_observation"]["slots"][0]
            if state in ("raised_depth", "cup", "detected_object"):
                slot["evidence"] = [state]
            if state == "cup":
                slot["cup_count"] = 1
                slot["assigned_cups"] = [{"object_indices": [0]}]
            if state == "occluded":
                slot.update(visible_fraction=0.2, floor_visible_fraction=0.3)
            if state == "invalid_depth":
                slot["valid_fraction"] = 0.7
            if state == "table_depth":
                slot["table_fraction"] = 0.8
            frames.append(frame)
        return frames

    def test_constructor_keeps_legacy_option_shape_without_enabling_recapture(self):
        for value in (0, 2, 4, True, 3.0, "3", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CupSession(Mock(), Mock(), self.robot, "cal-1", verification_frames=value)
        for value in (1, 3):
            session = CupSession(Mock(), Mock(), self.robot, "cal-1", verification_frames=value)
            self.assertEqual(session.verification_frames, value)

    def test_legacy_three_frame_setting_uses_only_the_original_scan(self):
        session = self.session()
        self.capture.side_effect = AssertionError("Execution must not capture")
        jobs = self.execute(session)
        self.assertEqual(len(jobs), TRANSFER_JOB_COUNT)
        self.capture.assert_called_once_with("scan")
        audit = session.inventory["cups"][0]["last_verification"]
        self.assertEqual(audit["source"], "saved_scan")
        self.assertEqual([check["capture"] for check in audit["placement_checks"]], ["capture-10"])
        self.assertNotIn("multi_frame", audit)
        self.assertNotIn("prefetch", audit)

    def test_saved_nonclear_destination_blocks_without_retry_or_motion(self):
        for state in ("table_depth", "invalid_depth", "occluded", "raised_depth", "cup", "detected_object"):
            with self.subTest(state=state):
                fixtures.PlacementVerificationTests.setUp(self)
                session = self.session()
                session.inventory["placement_observation"]["slots"][0] = self.frames([state])[0]["placement_observation"]["slots"][0]
                before = self.grid.path.read_bytes()
                with self.assertRaises(SelectionError):
                    self.execute(session)
                self.capture.assert_called_once_with("scan")
                self.assertEqual(self.robot.jobs, [])
                self.assertEqual(self.grid.path.read_bytes(), before)
                self.assertFalse(self.recovery.required())

    def test_malformed_saved_destination_evidence_is_not_accepted(self):
        for field, value in (("polygon_px", []), ("valid_fraction", float("nan")),
                              ("cup_count", False), ("evidence", ["unexpected"]),
                              ("reason_code", "unexpected")):
            with self.subTest(field=field):
                fixtures.PlacementVerificationTests.setUp(self)
                session = self.session()
                session.inventory["placement_observation"]["slots"][0][field] = value
                with self.assertRaises(SelectionError):
                    self.execute(session)
                self.assertEqual(self.robot.jobs, [])
                self.capture.assert_called_once_with("scan")

    def test_old_scan_and_slow_commands_have_no_camera_age_deadline(self):
        session = self.session(max_age=0.1)
        now = [100000.0]
        session.clock = lambda: now[0]
        self.robot.after_move = lambda _: now.__setitem__(0, now[0] + 10000)
        self.execute(session)
        self.assertEqual(self.robot.jobs[-1]["name"], "right_clear")
        self.assertFalse(session.motion_uncertain)
        self.capture.assert_called_once_with("scan")

    def batch_session(self, *, home_after_cup=False):
        scan = self.recheck_observed(sequence=10)
        scan["cups"] = inventory(objects=[detection("CUP", 100), detection("CUP BASE", 100),
                                         detection("CUP", 250, x=560), detection("CUP BASE", 250, x=560)])["cups"]
        return self.session(scan=scan, home_after_cup=home_after_cup)

    def test_batch_has_no_verification_or_background_prefetch_even_with_optional_home(self):
        for home in (False, True):
            with self.subTest(home=home):
                fixtures.PlacementVerificationTests.setUp(self)
                session = self.batch_session(home_after_cup=home)
                count = len(handover_jobs(session.inventory["cups"][0],
                            place=self.grid.snapshot()["locations"][0], home_after_cup=home))
                jobs = session.execute_batch(capture_id=session.inventory["capture"])
                self.assertEqual(len(jobs), 2 * count)
                self.assertEqual(session.batch_progress["completed_cups"], [1, 2])
                self.capture.assert_called_once_with("scan")
                self.assertFalse(any(thread.name == "cup-camera-prefetch" for thread in threading.enumerate()))
                self.assertTrue(session.scan_usable)
                self.assertFalse(session.motion_uncertain)

    def test_stop_during_motion_prevents_later_steps_and_next_cup(self):
        session = self.batch_session()
        self.robot.after_move = lambda _: session.latch_stop() if self.robot.jobs[-1]["name"] == "approach_cup" else None
        with self.assertRaisesRegex(SelectionError, "Software stop"):
            session.execute_batch(capture_id=session.inventory["capture"])
        self.assertEqual(self.robot.jobs[-1]["name"], "approach_cup")
        self.assertEqual(session.batch_progress["completed_cups"], [])
        self.assertTrue(session.motion_uncertain)
        self.assertTrue(self.recovery.required())
        self.capture.assert_called_once_with("scan")

    def test_failed_move_is_not_retried_and_preserves_reserved_destinations(self):
        self.robot = RobotDouble(error=RuntimeError("unconfirmed move"), error_at="right_take")
        session = self.batch_session()
        with self.assertRaisesRegex(RuntimeError, "unconfirmed move"):
            session.execute_batch(capture_id=session.inventory["capture"])
        self.assertEqual([job["name"] for job in self.robot.jobs].count("right_take"), 1)
        self.assertNotIn("left_release", [job["name"] for job in self.robot.jobs])
        self.assertTrue(session.motion_uncertain)
        self.assertTrue(self.recovery.required())
        self.assertTrue(all(slot.get("reserved") for slot in self.grid.snapshot()["locations"][:2]))
        self.capture.assert_called_once_with("scan")


if __name__ == "__main__":
    unittest.main()
