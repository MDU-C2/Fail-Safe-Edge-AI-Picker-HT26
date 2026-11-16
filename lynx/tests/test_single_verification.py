"""Strict one-observation picking checks; saved fixtures and robot doubles only."""

import copy
import threading
import unittest
from unittest.mock import Mock, patch

import lynx  # Make the existing sibling-import picking modules available.
from cup_commands import CupSession
from cup_inventory import SelectionError
from test_cup_selection import TRANSFER_JOB_COUNT, RobotDouble, detection, inventory
import test_multiframe_verification as multi_fixtures
import test_placement_verification as fixtures


class SingleVerificationTests(unittest.TestCase):
    setUp = fixtures.PlacementVerificationTests.setUp
    observed = staticmethod(fixtures.PlacementVerificationTests.observed)
    recheck_observed = classmethod(fixtures.PlacementVerificationTests.recheck_observed.__func__)
    frames = multi_fixtures.MultiFrameVerificationTests.frames
    execute = staticmethod(fixtures.PlacementVerificationTests.execute)

    def session(self, *, first=None, started=99.0, strict=True, max_age=60.0, scan=None):
        scan = self.recheck_observed(sequence=10) if scan is None else scan
        observation = scan["placement_observation"]
        observation["grid_snapshot"] = self.grid.snapshot()
        observation["slots"] = [dict(copy.deepcopy(observation["slots"][0]), id=place["id"])
                                for place in self.grid.snapshot()["locations"]]
        observation.update(frame_count=1, frame_sequences=[10], released_ids=[])
        first = self.recheck_observed(sequence=11) if first is None else first
        self.capture = Mock(side_effect=[(scan, 99.0), (first, started)])
        self.save = Mock()
        session = CupSession(self.capture, self.save, self.robot, "cal-1", clock=lambda: 100.0,
                             max_result_age=max_age, placing_grid=self.grid, recovery=self.recovery,
                             verification_frames=1, strict_single_verification=strict)
        session.scan()
        return session

    def test_constructor_keeps_legacy_default_and_rejects_incompatible_policy(self):
        legacy = CupSession(Mock(), Mock(), self.robot, "cal-1")
        self.assertEqual(legacy.max_result_age, 15.0)
        self.assertEqual(legacy.verification_frames, 1)
        self.assertFalse(legacy.strict_single_verification)
        for value in (None, 0, 1, "true"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CupSession(Mock(), Mock(), self.robot, "cal-1", strict_single_verification=value)
        with self.assertRaises(ValueError):
            CupSession(Mock(), Mock(), self.robot, "cal-1", verification_frames=3,
                       strict_single_verification=True)

    def test_31_8_second_observation_passes_60_second_limit_with_one_capture(self):
        session = self.session(started=68.2)
        jobs = self.execute(session)
        self.assertEqual(len(jobs), TRANSFER_JOB_COUNT)
        self.assertEqual(self.capture.call_count, 2)  # One scan, one verification.
        checks = session.inventory["cups"][0]["last_verification"]["placement_checks"]
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["capture"], "capture-11")
        self.assertFalse(session.motion_uncertain)

    def test_exact_60_second_boundary_is_accepted(self):
        self.execute(self.session(started=40.0))
        self.assertEqual(len(self.robot.jobs), TRANSFER_JOB_COUNT)
        self.assertEqual(self.capture.call_count, 2)

    def test_expired_future_or_invalid_capture_time_blocks_without_retry(self):
        for started in (39.9, 100.1, None, True, "99", float("nan"), float("inf")):
            with self.subTest(started=started):
                session = self.session(started=started)
                before = self.grid.path.read_bytes()
                with self.assertRaises(SelectionError):
                    self.execute(session)
                self.assertEqual(self.capture.call_count, 2)
                self.assertEqual(self.robot.jobs, [])
                self.assertEqual(self.grid.path.read_bytes(), before)
                self.assertFalse(session.scan_usable)

    def test_legacy_15_second_limit_still_rejects_31_8_seconds(self):
        session = self.session(started=68.2, strict=False, max_age=15.0)
        with self.assertRaisesRegex(SelectionError, "31.8s old"):
            self.execute(session)
        self.assertEqual(self.robot.jobs, [])
        self.assertEqual(self.capture.call_count, 2)

    def test_every_nonclear_destination_blocks_after_one_check(self):
        for state in ("table_depth", "invalid_depth", "occluded", "raised_depth", "cup", "detected_object"):
            with self.subTest(state=state):
                first = self.frames([state])[0]
                session = self.session(first=first)
                before = self.grid.path.read_bytes()
                with self.assertRaises(SelectionError):
                    self.execute(session)
                self.assertEqual(self.capture.call_count, 2)
                self.assertEqual(self.robot.jobs, [])
                self.assertEqual(self.grid.path.read_bytes(), before)
                self.assertFalse(session.motion_uncertain)
                self.assertFalse(session.scan_usable)

    def test_single_clear_preserves_source_identity_geometry_and_evidence_guards(self):
        cases = ("sequence", "calibration", "offset_bool", "source", "model", "confidence_setting",
                 "capture_id", "polygon", "reference", "metrics", "false_clear", "cup_schema", "target", "grip")
        for kind in cases:
            with self.subTest(kind=kind):
                first = self.recheck_observed(sequence=11)
                slot = first["placement_observation"]["slots"][0]
                if kind == "sequence": first["frame"]["sequence"] = 10
                elif kind == "calibration": first["calibration_sha256"] = "changed"
                elif kind == "offset_bool": first["tag_offset_mm"] = [False, False, False]
                elif kind == "source": first["gateway"] = "http://changed.invalid"
                elif kind == "model": first["model"]["weights"] = "changed"
                elif kind == "confidence_setting": first["model"]["confidence_threshold"] = 0.25
                elif kind == "capture_id": first["capture"] = "capture-10"
                elif kind == "polygon": slot["polygon_px"][0][0] += 1
                elif kind == "reference": first["placement_observation"]["table_reference"] = {"file_sha256": "changed"}
                elif kind == "metrics": slot["valid_fraction"] = float("nan")
                elif kind == "false_clear": slot["evidence"] = ["raised_depth"]
                elif kind == "cup_schema": first["cups"][0]["selectable"] = 1
                elif kind == "target": first["cups"][0]["x"] += 40
                else: first["cups"][0]["rz"] = 190
                session = self.session(first=first)
                before = self.grid.path.read_bytes()
                with self.assertRaises(SelectionError):
                    self.execute(session)
                self.assertEqual(self.capture.call_count, 2)
                self.assertEqual(self.robot.jobs, [])
                self.assertEqual(self.grid.path.read_bytes(), before)

    def test_failed_single_observation_audit_is_saved_before_returning(self):
        session = self.session(first=self.frames(["invalid_depth"])[0])
        self.save.reset_mock()
        with self.assertRaises(SelectionError):
            self.execute(session)
        self.save.assert_called_once_with(session.inventory)
        audit = session.inventory["cups"][0]["last_verification"]
        self.assertEqual(audit["state"], "failed")
        self.assertEqual(len(audit["placement_checks"]), 1)
        self.assertEqual(audit["placement_checks"][0]["reason_code"], "invalid_depth")
        self.assertTrue(audit["error"])

    def test_age_expiring_after_descent_blocks_grip_without_recapture(self):
        now = [100.0]
        session = self.session(started=100.0)
        session.clock = lambda: now[0]
        self.robot.after_move = lambda _: now.__setitem__(0, 160.1) if self.robot.jobs[-1]["name"] == "descend_to_cup" else None
        with self.assertRaisesRegex(SelectionError, "60.1s old"):
            self.execute(session)
        self.assertEqual(self.robot.jobs[-1]["name"], "descend_to_cup")
        self.assertNotIn("grip_cup", [job["name"] for job in self.robot.jobs])
        self.assertEqual(self.capture.call_count, 2)
        self.assertTrue(session.motion_uncertain)
        self.assertTrue(self.recovery.required())

    def test_slow_persistence_counts_toward_age_before_first_dispatch(self):
        now = [100.0]
        session = self.session(started=100.0)
        session.clock = lambda: now[0]
        def save(observation):
            if observation["cups"][0].get("execution", {}).get("state") == "pending":
                now[0] = 160.1
        self.save.side_effect = save
        with self.assertRaisesRegex(SelectionError, "60.1s old"):
            self.execute(session)
        self.assertEqual(self.robot.jobs, [])
        self.assertEqual(self.capture.call_count, 2)
        self.assertTrue(session.motion_uncertain)

    def test_age_after_grip_does_not_interrupt_the_transfer(self):
        now = [100.0]
        session = self.session(started=100.0)
        session.clock = lambda: now[0]
        self.robot.after_move = lambda _: now.__setitem__(0, 160.1) if self.robot.jobs[-1]["name"] == "grip_cup" else None
        self.execute(session)
        self.assertEqual(self.robot.jobs[-1]["name"], "right_home")
        self.assertEqual(self.capture.call_count, 2)
        self.assertFalse(session.motion_uncertain)

    def test_stop_during_only_verification_blocks_every_job(self):
        session = self.session()
        def capture(_):
            session.latch_stop()
            return self.recheck_observed(sequence=11), 99.0
        self.capture.side_effect = capture
        with self.assertRaisesRegex(SelectionError, "Software stop"):
            self.execute(session)
        self.assertEqual(self.capture.call_count, 2)
        self.assertEqual(self.robot.jobs, [])
        self.assertTrue(session.motion_uncertain)
        self.assertFalse(session.scan_usable)

    def batch_session(self, *, prior_home_capture=False):
        cups = inventory(objects=[detection("CUP", 100), detection("CUP BASE", 100),
                                  detection("CUP", 250, x=560), detection("CUP BASE", 250, x=560)])["cups"]
        scan = self.recheck_observed(sequence=10)
        scan["cups"] = copy.deepcopy(cups)
        session = self.session(scan=scan)
        now, calls = [100.0], []
        session.clock = lambda: now[0]
        coordinator = threading.get_ident()
        def capture(purpose):
            self.assertEqual(threading.get_ident(), coordinator)
            sequence = 11 + len(calls)
            calls.append((purpose, len(self.robot.jobs), now[0]))
            fresh = self.recheck_observed(sequence=sequence)
            fresh["cups"] = copy.deepcopy(cups)
            slot = fresh["placement_observation"]["slots"][0]
            fresh["placement_observation"]["slots"] = [dict(copy.deepcopy(slot), id=place["id"])
                                                       for place in self.grid.snapshot()["locations"]]
            started = 109.0 if prior_home_capture and sequence == 12 else now[0]
            return fresh, started
        self.capture.side_effect = capture
        def after_move(_):
            if self.robot.jobs[-1]["name"] == "right_home":
                now[0] = 110.0
        self.robot.after_move = after_move
        return session, calls

    def test_two_cup_batch_verifies_once_per_cup_after_home_without_prefetch(self):
        session, calls = self.batch_session()
        with patch("cup_commands._VerificationPrefetch", side_effect=AssertionError("No prefetch in strict single mode")):
            jobs = session.execute_batch(capture_id=session.inventory["capture"])
        self.assertEqual(len(jobs), 2 * TRANSFER_JOB_COUNT)
        self.assertEqual(calls, [("verification", 0, 100.0), ("verification", TRANSFER_JOB_COUNT, 110.0)])
        self.assertEqual(self.capture.call_count, 3)  # One scan, one check per cup.
        self.assertEqual(session.batch_progress["completed_cups"], [1, 2])
        self.assertFalse(session.motion_uncertain)

    def test_next_batch_cup_rejects_capture_started_before_previous_home(self):
        session, calls = self.batch_session(prior_home_capture=True)
        with patch("cup_commands._VerificationPrefetch", side_effect=AssertionError("No prefetch in strict single mode")):
            with self.assertRaisesRegex(SelectionError, "after both arms.*home"):
                session.execute_batch(capture_id=session.inventory["capture"])
        self.assertEqual(len(self.robot.jobs), TRANSFER_JOB_COUNT)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.capture.call_count, 3)
        self.assertTrue(session.motion_uncertain)


if __name__ == "__main__":
    unittest.main()
