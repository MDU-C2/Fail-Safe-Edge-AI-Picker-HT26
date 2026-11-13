"""Fresh destination checks use synthetic observations and a robot double only."""

from contextlib import redirect_stdout
import copy
import io
import unittest
from unittest.mock import Mock, patch

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
                           "polygon_px": [[100, 100], [120, 100], [120, 120], [100, 120]]}],
            }
        return result

    @classmethod
    def recheck_observed(cls, state="clear", *, sequence=11, reason_code=None):
        result = cls.observed(state, sequence=sequence)
        slot = result["placement_observation"]["slots"][0]
        slot.update(reason_code=reason_code or ("clear" if state == "clear" else "table_depth"),
                    cup_count=0, evidence=[], assigned_cups=[],
                    table_fraction=1.0, visible_fraction=1.0, floor_visible_fraction=1.0)
        return result

    def session(self, *, first=None, unused=None, scan=None, retries=()):
        scan = self.observed() if scan is None else scan
        if "placement_observation" in scan:
            observation = scan["placement_observation"]
            observation["grid_snapshot"] = self.grid.snapshot()
            prototype = observation["slots"][0]
            observation["slots"] = [dict(copy.deepcopy(prototype), id=place["id"])
                                    for place in observation["grid_snapshot"]["locations"]]
            observation.update(frame_count=3, frame_sequences=[8, 9, 10], released_ids=[])
        first = self.observed(sequence=11) if first is None else first
        observations = [(scan, 99), (first, 99)]
        if unused is not None:
            observations.append((unused, 99))
        observations.extend((fresh, 99) for fresh in retries)
        self.capture = Mock(side_effect=observations)
        self.save = Mock()
        session = CupSession(self.capture, self.save, self.robot, "cal-1", clock=lambda: 100,
                             placing_grid=self.grid, recovery=self.recovery)
        session.scan()
        return session

    @staticmethod
    def execute(session, *, action="goto"):
        return session.execute_selection(action=action, capture_id=session.inventory["capture"], cup_id=1)

    def test_occupied_or_unknown_first_check_blocks_every_command_and_reservation(self):
        for state in ("occupied", "unknown"):
            with self.subTest(state=state):
                session = self.session(first=self.observed(state, sequence=11))
                before = self.grid.path.read_bytes()
                with patch.object(self.grid, "reconcile_observation") as reconcile:
                    with self.assertRaisesRegex(SelectionError, f"location 1 is {state}"):
                        self.execute(session)
                reconcile.assert_not_called()
                self.assertEqual(self.robot.jobs, [])
                self.assertEqual(self.grid.path.read_bytes(), before)
                self.assertFalse(self.recovery.required())
                self.assertFalse(session.motion_uncertain)
                self.assertFalse(session.scan_usable)
                self.assertEqual(self.capture.call_count, 2)

    def test_missing_fresh_observation_blocks_when_scan_used_placement_vision(self):
        session = self.session(first=self.observed(sequence=11, include=False))
        before = self.grid.path.read_bytes()
        with self.assertRaisesRegex(SelectionError, "location 1 is unknown"):
            self.execute(session)
        self.assertEqual(self.robot.jobs, [])
        self.assertEqual(self.grid.path.read_bytes(), before)

    def test_missing_or_ambiguous_destination_slot_cannot_pass_verification(self):
        for kind in ("different_id", "duplicate", "missing_slots", "bool_id"):
            with self.subTest(kind=kind):
                fresh = self.observed(sequence=11)
                if kind == "different_id":
                    fresh["placement_observation"]["slots"][0]["id"] = 2
                elif kind == "duplicate":
                    fresh["placement_observation"]["slots"] *= 2
                elif kind == "missing_slots":
                    fresh["placement_observation"].pop("slots")
                else:
                    fresh["placement_observation"]["slots"][0]["id"] = True
                session = self.session(first=fresh)
                with self.assertRaisesRegex(SelectionError, "location 1 is unknown"):
                    self.execute(session)
                self.assertEqual(self.robot.jobs, [])
                self.assertFalse(any(slot["occupied"] for slot in self.grid.snapshot()["locations"]))

    def test_fresh_placement_evidence_enables_veto_even_if_scan_was_legacy(self):
        session = self.session(scan=self.observed(include=False),
                               first=self.observed("occupied", sequence=11))
        with self.assertRaisesRegex(SelectionError, "location 1 is occupied"):
            self.execute(session)
        self.assertEqual(self.robot.jobs, [])

    def test_clear_dry_run_preserves_grid_and_records_exact_checked_slot(self):
        self.robot.dry_run = True
        fresh = self.observed(sequence=11)
        checked = copy.deepcopy(fresh["placement_observation"]["slots"][0])
        session = self.session(first=fresh)
        before = self.grid.path.read_bytes()
        with patch.object(self.grid, "reconcile_observation") as reconcile:
            jobs = self.execute(session)
        reconcile.assert_not_called()
        self.assertEqual(len(jobs), TRANSFER_JOB_COUNT)
        self.assertEqual(self.grid.path.read_bytes(), before)
        self.assertFalse(self.recovery.required())
        audit = session.inventory["cups"][0]["last_verification"]
        self.assertEqual(audit["placement_visibility"], checked)
        fresh["placement_observation"]["slots"][0]["state"] = "occupied"
        self.assertEqual(audit["placement_visibility"]["state"], "clear")
        self.assertEqual(self.capture.call_count, 2)

    def test_second_placement_frame_is_not_requested_after_ready(self):
        session = self.session(unused=self.observed("occupied", sequence=12))
        with patch.object(self.grid, "reconcile_observation") as reconcile:
            jobs = self.execute(session)
        reconcile.assert_not_called()
        self.assertEqual(len(jobs), TRANSFER_JOB_COUNT)
        self.assertEqual(len(self.robot.jobs), TRANSFER_JOB_COUNT)
        destination = self.grid.snapshot()["locations"][0]
        self.assertTrue(destination["occupied"])
        self.assertFalse(destination.get("reserved", False))
        self.assertFalse(self.recovery.required())
        self.assertFalse(session.motion_uncertain)
        self.assertFalse(session.scan_usable)
        self.assertEqual(self.capture.call_count, 2)
        audit = session.inventory["cups"][0]["last_verification"]
        self.assertEqual(audit["capture"], "capture-11")
        self.assertEqual(audit["placement_visibility"]["state"], "clear")

    def test_initial_check_expiring_after_ready_blocks_approach_and_keeps_reservation(self):
        now = [100]
        session = self.session()
        session.clock = lambda: now[0]
        self.robot.after_move = lambda count: now.__setitem__(0, 130) if self.robot.jobs[-1]["name"] == "left_ready" else None
        with self.assertRaisesRegex(SelectionError, "old; motion blocked"):
            self.execute(session)
        self.assertEqual([job["name"] for job in self.robot.jobs],
                         ["right_close_travel", "right_home_start", "left_close_travel", "left_ready"])
        self.assertTrue(self.grid.snapshot()["locations"][0]["reserved"])
        self.assertTrue(self.recovery.required())
        self.assertTrue(session.motion_uncertain)
        self.assertEqual(self.capture.call_count, 2)

    def test_single_clear_check_completes_with_reserved_slot_evidence_allowed(self):
        first = self.observed(sequence=11)
        first["placement_observation"]["slots"][0]["reserved"] = True
        session = self.session(first=first)
        with patch.object(self.grid, "reconcile_observation") as reconcile:
            jobs = self.execute(session)
        reconcile.assert_not_called()
        self.assertEqual(len(jobs), TRANSFER_JOB_COUNT)
        self.assertEqual(len(self.robot.jobs), TRANSFER_JOB_COUNT)
        destination = self.grid.snapshot()["locations"][0]
        self.assertTrue(destination["occupied"])
        self.assertFalse(destination.get("reserved", False))
        self.assertFalse(self.recovery.required())
        self.assertFalse(session.motion_uncertain)
        audit = session.inventory["cups"][0]["last_verification"]
        self.assertEqual(audit["capture"], "capture-11")
        self.assertTrue(audit["placement_visibility"]["reserved"])
        self.assertEqual(self.capture.call_count, 2)

    def test_hover_does_not_require_a_clear_placement_destination(self):
        session = self.session(first=self.observed("occupied", sequence=11))
        before = self.grid.path.read_bytes()
        jobs = self.execute(session, action="hover")
        self.assertEqual([job["name"] for job in jobs], ["left_ready", "approach_cup"])
        self.assertEqual(self.grid.path.read_bytes(), before)
        self.assertNotIn("placement_visibility", session.inventory["cups"][0]["last_verification"])
        self.assertEqual(self.capture.call_count, 2)

    def test_other_blocked_locations_do_not_block_the_clear_selected_destination(self):
        first = self.observed(sequence=11)
        slots = first["placement_observation"]["slots"]
        slots.extend([dict(copy.deepcopy(slots[0]), id=2, state="occupied"),
                      dict(copy.deepcopy(slots[0]), id=3, state="unknown")])
        session = self.session(first=first)
        with patch.object(self.grid, "reconcile_observation") as reconcile:
            jobs = self.execute(session)
        reconcile.assert_not_called()
        self.assertEqual(len(jobs), TRANSFER_JOB_COUNT)
        self.assertEqual(len(self.robot.jobs), TRANSFER_JOB_COUNT)
        checked = session.inventory["cups"][0]["last_verification"]["placement_visibility"]
        self.assertEqual((checked["id"], checked["state"]), (1, "clear"))
        self.assertFalse(session.motion_uncertain)

    def test_transient_depth_unknown_requires_three_clear_frames_before_any_command(self):
        for reason_code in ("table_depth", "invalid_depth"):
            with self.subTest(reason_code=reason_code):
                self.robot = RobotDouble()
                session = self.session(first=self.recheck_observed("unknown", reason_code=reason_code))
                before = self.grid.path.read_bytes()
                frames = [self.recheck_observed("unknown", reason_code=reason_code)]
                frames += [self.recheck_observed(sequence=seq) for seq in (12, 13, 14)]
                remaining = iter(frames)

                def capture(kind):
                    self.assertEqual(kind, "verification")
                    self.assertEqual(self.robot.jobs, [])
                    self.assertEqual(self.grid.path.read_bytes(), before)
                    return next(remaining), 99

                self.capture.side_effect = capture
                identity = Mock(return_value={"name": "requested cup"})
                session.execute_selection(action="goto", capture_id=session.inventory["capture"],
                                          cup_id=1, identity_check=identity)
                self.assertEqual(len(self.robot.jobs), TRANSFER_JOB_COUNT)
                identity.assert_called_once_with(frames[-1], frames[-1]["cups"][0])
                self.assertEqual(self.capture.call_count, 5)  # Scan plus exactly four checks.
                audit = session.inventory["cups"][0]["last_verification"]
                self.assertEqual(audit["state"], "verified")
                self.assertEqual(audit["stationary_recheck"], {"required_clear": 3, "clear_count": 3})
                self.assertEqual([check["capture"] for check in audit["placement_checks"]],
                                 [f"capture-{seq}" for seq in (11, 12, 13, 14)])
                self.assertEqual([check["state"] for check in audit["placement_checks"]],
                                 ["unknown", "clear", "clear", "clear"])
                self.assertEqual(audit["placement_checks"][0]["reason_code"], reason_code)
                self.assertEqual(audit["capture"], "capture-14")
                # Restore only this temporary test grid for the next subcase.
                self.grid.path.write_bytes(before)

    def test_recheck_stops_on_first_nonclear_without_polling_until_clear(self):
        for state in ("unknown", "occupied"):
            with self.subTest(state=state):
                failed = self.recheck_observed(state, sequence=12)
                failed["placement_observation"]["slots"][0]["reason"] = "specific camera explanation"
                session = self.session(first=self.recheck_observed("unknown"), retries=[
                    failed, self.recheck_observed(sequence=13), self.recheck_observed(sequence=14)])
                before = self.grid.path.read_bytes()
                with self.assertRaisesRegex(SelectionError, "specific camera explanation"):
                    self.execute(session)
                self.assertEqual(self.capture.call_count, 3)
                self.assertEqual(self.robot.jobs, [])
                self.assertEqual(self.grid.path.read_bytes(), before)
                self.assertFalse(self.recovery.required())
                self.assertFalse(session.motion_uncertain)
                audit = session.inventory["cups"][0]["last_verification"]
                self.assertEqual(audit["state"], "failed")
                self.assertEqual(len(audit["placement_checks"]), 2)
                self.assertEqual(audit["placement_checks"][-1]["state"], state)
                self.assertIn("specific camera explanation", audit["error"])
                self.assertIs(self.save.call_args.args[0], session.inventory)

    def test_only_structured_unobstructed_depth_unknowns_are_eligible(self):
        changes = [
            {"reason_code": "occluded"}, {"reason_code": "outside_view"},
            {"reason_code": None}, {"cup_count": 1}, {"cup_count": False},
            {"evidence": ["raised_depth"]}, {"evidence": None},
            {"assigned_cups": [{"object_indices": [1]}]},
            {"visible_fraction": 0.49}, {"floor_visible_fraction": 0.49},
            {"visible_fraction": float("nan")}, {"floor_visible_fraction": float("inf")},
            {"visible_fraction": True}, {"floor_visible_fraction": 1.01},
            {"valid_fraction": float("nan")}, {"table_fraction": 1.1},
            {"table_fraction": None}, {"assigned_cups": None},
        ]
        for change in changes:
            with self.subTest(change=change):
                first = self.recheck_observed("unknown")
                first["placement_observation"]["slots"][0].update(change)
                session = self.session(first=first)
                before = self.grid.path.read_bytes()
                with self.assertRaises(SelectionError):
                    self.execute(session)
                self.assertEqual(self.capture.call_count, 2)
                self.assertEqual(self.robot.jobs, [])
                self.assertEqual(self.grid.path.read_bytes(), before)

    def test_recheck_revalidates_sequence_calibration_offset_target_and_grip(self):
        for change in ("sequence", "bool_sequence", "calibration", "offset", "target", "yaw", "opening"):
            with self.subTest(change=change):
                fresh = self.recheck_observed(sequence=12)
                if change == "sequence":
                    fresh["frame"]["sequence"] = 11
                elif change == "bool_sequence":
                    fresh["frame"]["sequence"] = True
                elif change == "calibration":
                    fresh["calibration_sha256"] = "different"
                elif change == "offset":
                    fresh["tag_offset_mm"] = [1, 0, 0]
                elif change == "target":
                    fresh["cups"][0]["x"] += 30
                elif change == "yaw":
                    fresh["cups"][0]["rz"] = 190
                else:
                    fresh["cups"][0]["aperture"] = 100
                session = self.session(first=self.recheck_observed("unknown"), retries=[fresh])
                before = self.grid.path.read_bytes()
                with self.assertRaises(SelectionError):
                    self.execute(session)
                self.assertEqual(self.capture.call_count, 3)
                self.assertEqual(self.robot.jobs, [])
                self.assertEqual(self.grid.path.read_bytes(), before)
                self.assertFalse(self.recovery.required())
                self.assertEqual(session.inventory["cups"][0]["last_verification"]["state"], "failed")

    def test_recheck_rejects_changed_or_malformed_destination_and_reference(self):
        for change in ("polygon", "missing_polygon", "nan_polygon", "duplicate", "bool_id",
                       "missing_observation", "reference", "width", "cup_evidence", "occluded_clear"):
            with self.subTest(change=change):
                fresh = self.recheck_observed(sequence=12)
                observation = fresh["placement_observation"]
                slot = observation["slots"][0]
                if change == "polygon":
                    slot["polygon_px"][0][0] += 1
                elif change == "missing_polygon":
                    slot.pop("polygon_px")
                elif change == "nan_polygon":
                    slot["polygon_px"][0][0] = float("nan")
                elif change == "duplicate":
                    observation["slots"].append(copy.deepcopy(slot))
                elif change == "bool_id":
                    slot["id"] = True
                elif change == "missing_observation":
                    fresh.pop("placement_observation")
                elif change == "reference":
                    observation["table_reference"] = {"file_sha256": "changed"}
                elif change == "width":
                    observation["cell_size_mm"] = 90
                elif change == "cup_evidence":
                    slot["evidence"] = ["cup"]
                else:
                    slot["floor_visible_fraction"] = 0.4
                session = self.session(first=self.recheck_observed("unknown"), retries=[fresh])
                before = self.grid.path.read_bytes()
                with self.assertRaises(SelectionError):
                    self.execute(session)
                self.assertEqual(self.robot.jobs, [])
                self.assertEqual(self.grid.path.read_bytes(), before)
                self.assertEqual(self.capture.call_count, 3)

    def test_initial_unknown_requires_geometry_and_reference_matching_original_scan(self):
        first = self.recheck_observed("unknown")
        first["placement_observation"]["table_reference"] = {"file_sha256": "new-reference"}
        session = self.session(first=first)
        with self.assertRaisesRegex(SelectionError, "reference changed since the scan"):
            self.execute(session)
        self.assertEqual(self.capture.call_count, 2)
        self.assertEqual(self.robot.jobs, [])

    def test_inconsistent_clear_metrics_cannot_finish_recheck(self):
        for field, value in (("valid_fraction", 0.994), ("table_fraction", 0.994),
                             ("valid_fraction", float("inf")), ("table_fraction", True),
                             ("valid_fraction", None), ("table_fraction", 1.01),
                             ("assigned_cups", None)):
            with self.subTest(field=field, value=value):
                fresh = self.recheck_observed(sequence=12)
                fresh["placement_observation"]["slots"][0][field] = value
                session = self.session(first=self.recheck_observed("unknown"), retries=[fresh])
                before = self.grid.path.read_bytes()
                with self.assertRaises(SelectionError):
                    self.execute(session)
                self.assertEqual(self.robot.jobs, [])
                self.assertEqual(self.grid.path.read_bytes(), before)
                self.assertEqual(self.capture.call_count, 3)

    def test_recheck_age_and_stop_are_checked_before_any_new_capture_or_command(self):
        for failure in ("old_frame", "stop_after_capture", "stop_after_save"):
            with self.subTest(failure=failure):
                session = self.session(first=self.recheck_observed("unknown"))
                before = self.grid.path.read_bytes()
                frames = iter([self.recheck_observed("unknown"), self.recheck_observed(sequence=12)])

                def capture(_kind):
                    fresh = next(frames)
                    if fresh["frame"]["sequence"] == 12 and failure == "stop_after_capture":
                        session.latch_stop()
                    return fresh, 70 if fresh["frame"]["sequence"] == 12 and failure == "old_frame" else 99

                self.capture.side_effect = capture
                if failure == "stop_after_save":
                    self.save.side_effect = lambda _: session.latch_stop()
                with self.assertRaisesRegex(SelectionError, "old; motion blocked|Software stop"):
                    self.execute(session)
                self.assertEqual(self.robot.jobs, [])
                self.assertEqual(self.grid.path.read_bytes(), before)
                self.assertEqual(self.capture.call_count, 2 if failure == "stop_after_save" else 3)
                self.assertEqual(session.inventory["cups"][0]["last_verification"]["state"], "failed")

    def test_final_named_check_expiring_image_blocks_without_more_captures(self):
        session = self.session(first=self.recheck_observed("unknown"),
                               retries=[self.recheck_observed(sequence=seq) for seq in (12, 13, 14)])
        now = [100]
        session.clock = lambda: now[0]
        identity = Mock(side_effect=lambda *_: now.__setitem__(0, 130))
        before = self.grid.path.read_bytes()
        with self.assertRaisesRegex(SelectionError, "old; motion blocked"):
            session.execute_selection(action="goto", capture_id=session.inventory["capture"],
                                      cup_id=1, identity_check=identity)
        identity.assert_called_once()
        self.assertEqual(self.capture.call_count, 5)
        self.assertEqual(self.robot.jobs, [])
        self.assertEqual(self.grid.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
