"""Offline placement/batch integration checks; no real grid or hardware is used."""

from contextlib import redirect_stdout
import copy
import io
import json
import unittest
from unittest.mock import Mock

from cup_commands import CupSession
from cup_inventory import SelectionError
from robot_client import RobotCommandError
from recovery_journal import RecoveryJournal
from test_cup_selection import TRANSFER_JOB_COUNT, RobotDouble, detection, inventory
from test_support_grid import temporary_grid


class BatchSelectionTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        self.grid = temporary_grid(self)
        self.robot = RobotDouble()
        self.sequence = 0
        self.capture_count = 0
        self.capture_trace = []
        self.transform = None
        self.session = CupSession(self.capture, Mock(), self.robot, "cal-1", clock=lambda: 100,
                                  placing_grid=self.grid)

    def capture(self, kind):
        self.sequence += 1
        self.capture_count += 1
        self.capture_trace.append((kind, len(self.robot.jobs)))
        objects = []
        # y spread > 100 mm apart: no cup has a neighbour (pick_order.py), so confidence decides.
        for u, x, y in ((100, 500, -100), (200, 550, 100), (300, 600, 300)):
            pair = [detection("CUP", u, x=x), detection("CUP BASE", u, x=x)]
            for item in pair:
                item["robot_y_mm"] = y
            objects.extend(pair)
        objects.append(detection("CUP", 400, x=650))  # Missing base: never selected.
        if self.transform is not None:
            objects = self.transform(kind, objects)
        result = inventory(capture=f"{kind}-{self.sequence}", sequence=self.sequence, objects=objects)
        confidence_by_pixel = {100: 0.7, 200: 0.95, 300: 0.85, 400: 0.99}
        for cup in result["cups"]:
            cup["confidence"] = confidence_by_pixel.get(cup["pixel"]["u"], 0.99)
        return result, self.session.clock()

    def scan(self):
        return self.session.scan()["capture"]

    def execute(self, capture_id, preview):
        return self.session.execute_batch(capture_id=capture_id, cup_ids=preview["cup_ids"],
                                          placement=preview["placement"])

    def locations(self):
        return self.grid.snapshot()["locations"]

    def test_batch_preview_picks_fewest_neighbours_first_then_confidence(self):
        def in_a_row(_kind, objects):              # x 500, 550, 600 at one y: 50 mm apart
            for item in objects:
                item["robot_y_mm"] = 100
            return objects
        self.transform = in_a_row
        preview = self.session.preview_batch(capture_id=self.scan())
        # Cup 2 (0.95, the most certain) is in the middle with 2 neighbours, so it goes last;
        # the two end cups have 1 neighbour each and keep confidence order (3: 0.85, 1: 0.7).
        self.assertEqual(preview["cup_ids"], [3, 1, 2])
        self.assertEqual(self.robot.jobs, [])

    def test_batch_preview_confidence_order_filters_blocked_cups_without_motion_or_mutation(self):
        capture_id = self.scan()
        original = self.grid.path.read_bytes()
        preview = self.session.preview_batch(capture_id=capture_id)
        self.assertEqual(preview["cup_ids"], [2, 3, 1])
        self.assertEqual([place["id"] for place in preview["placement"]["locations"]], [1, 2, 3])
        self.assertEqual(self.grid.path.read_bytes(), original)
        self.assertEqual(self.robot.jobs, [])
        self.assertEqual(self.capture_count, 1)

    def test_batch_executes_only_snapshot_cups_with_distinct_places_without_recapture(self):
        capture_id = self.scan()
        preview = self.session.preview_batch(capture_id=capture_id)

        def add_new_leftmost(kind, objects):
            return objects + [detection("CUP", 20, x=420), detection("CUP BASE", 20, x=420)]

        self.transform = add_new_leftmost
        jobs = self.execute(capture_id, preview)
        self.assertEqual(len(jobs), 3 * TRANSFER_JOB_COUNT)
        self.assertEqual(len(self.robot.jobs), 3 * TRANSFER_JOB_COUNT)
        self.assertEqual([job["body"]["points"][-1]["x"] for job in self.robot.jobs if job["name"] == "approach_cup"],
                         [550, 600, 500])
        self.assertEqual([job["body"]["points"][-1]["x"] for job in self.robot.jobs if job["name"] == "right_place"],
                         [420, 500, 580])
        self.assertEqual(self.capture_count, 1)
        self.assertEqual(self.capture_trace,
                         [("scan", 0)])
        self.assertEqual(self.session.inventory["batch_execution"]["completed_cups"], [2, 3, 1])
        self.assertEqual(self.session.inventory["batch_execution"]["state"], "completed")
        self.assertTrue(all(cup["status"] == "placed" for cup in self.session.inventory["cups"][:3]))
        self.assertTrue(self.session.scan_usable)
        self.assertFalse(self.session.motion_uncertain)
        for place in self.locations():
            self.assertTrue(place["occupied"])
            self.assertFalse(place.get("reserved", False))

    def test_batch_dry_run_never_changes_grid_or_calls_live_robot(self):
        self.robot.dry_run = True
        capture_id = self.scan()
        original = self.grid.path.read_bytes()
        preview = self.session.preview_batch(capture_id=capture_id)
        jobs = self.execute(capture_id, preview)
        self.assertEqual(len(jobs), 3 * TRANSFER_JOB_COUNT)
        self.assertEqual(self.grid.path.read_bytes(), original)
        self.assertFalse(self.session.motion_uncertain)

    def test_failed_dry_run_invalidates_scan_without_reserving_destinations(self):
        self.robot.dry_run = True
        capture_id = self.scan()
        self.robot.execute_jobs = Mock(side_effect=RobotCommandError("Preview execution failed"))
        with self.assertRaisesRegex(RobotCommandError, "Preview execution failed"):
            self.session.execute_selection(action="goto", capture_id=capture_id, cup_id=1)
        self.assertFalse(self.session.scan_usable)
        self.assertFalse(self.session.motion_uncertain)
        self.assertEqual(self.robot.jobs, [])
        self.assertFalse(any(place["occupied"] for place in self.locations()))
        self.assertEqual(self.capture_count, 1)

    def test_new_scan_clears_previous_batch_progress(self):
        self.robot.dry_run = True
        capture_id = self.scan()
        self.execute(capture_id, self.session.preview_batch(capture_id=capture_id))
        self.assertIsNotNone(self.session.batch_progress)
        self.scan()
        self.assertIsNone(self.session.batch_progress)
        self.assertNotIn("batch_execution", self.session.inventory)

    def test_batch_uses_only_free_capacity_in_confidence_order_and_reports_excluded_ids(self):
        changed = json.loads(self.grid.path.read_text())
        changed["locations"][0]["occupied"] = True
        self.grid.path.write_text(json.dumps(changed))
        capture_id = self.scan()
        preview = self.session.preview_batch(capture_id=capture_id)
        self.assertEqual(preview["cup_ids"], [2, 3])
        self.assertEqual(preview["excluded_cup_ids"], [1])
        self.assertEqual([place["id"] for place in preview["placement"]["locations"]], [2, 3])
        self.assertEqual(len(self.execute(capture_id, preview)), 2 * TRANSFER_JOB_COUNT)
        self.assertEqual([job["body"]["points"][-1]["x"] for job in self.robot.jobs if job["name"] == "approach_cup"],
                         [550, 600])

    def test_full_grid_prevents_pick_and_batch_before_capture_or_motion(self):
        changed = json.loads(self.grid.path.read_text())
        for place in changed["locations"]:
            place["occupied"] = True
        self.grid.path.write_text(json.dumps(changed))
        capture_id = self.scan()
        for action in ("single", "batch"):
            with self.subTest(action=action), self.assertRaisesRegex(SelectionError, "free|occupied|full"):
                if action == "single":
                    self.session.preview_selection(action="goto", capture_id=capture_id, cup_id=1)
                else:
                    self.session.preview_batch(capture_id=capture_id)
        self.assertEqual(self.capture_count, 1)
        self.assertEqual(self.robot.jobs, [])

    def test_changed_grid_after_preview_prevents_every_command(self):
        capture_id = self.scan()
        preview = self.session.preview_batch(capture_id=capture_id)
        changed = json.loads(self.grid.path.read_text())
        changed["locations"][0]["x"] += 1
        self.grid.path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(SelectionError, "changed"):
            self.execute(capture_id, preview)
        self.assertEqual(self.robot.jobs, [])
        self.assertFalse(any(place.get("reserved") for place in self.locations()))

    def test_grid_metadata_change_after_ready_does_not_interrupt_confirmed_batch(self):
        capture_id = self.scan()
        preview = self.session.preview_batch(capture_id=capture_id)

        def mutate_after_ready(count):
            if count < TRANSFER_JOB_COUNT and self.robot.jobs[-1]["name"] == "approach_cup":
                changed = json.loads(self.grid.path.read_text())
                changed["locations"][0]["x"] += 1
                self.grid.path.write_text(json.dumps(changed))
        self.robot.after_move = mutate_after_ready
        self.execute(capture_id, preview)
        self.assertEqual(len(self.robot.jobs), 3 * TRANSFER_JOB_COUNT)
        self.assertEqual([job["body"]["points"][-1]["x"] for job in self.robot.jobs
                          if job["name"] == "right_place"], [420, 500, 580])
        self.assertEqual(self.locations()[0]["x"], 421)
        self.assertTrue(all(place["occupied"] for place in self.locations()))
        self.assertFalse(any(place.get("reserved", False) for place in self.locations()))
        self.assertFalse(self.session.motion_uncertain)
        self.assertTrue(self.session.scan_usable)
        self.assertEqual(self.capture_trace, [("scan", 0)])

    def test_scan_grid_update_during_batch_does_not_stop_or_revalidate_movements(self):
        journal = RecoveryJournal(self.grid.path.with_name("recovery.json"))
        self.session.recovery = journal
        self.robot.stop = Mock()
        capture_id = self.scan()
        preview = self.session.preview_batch(capture_id=capture_id)
        self.grid.validate_reservation = Mock(side_effect=AssertionError("Unexpected grid revalidation"))
        observed = []

        def update_grid_after_first_placement(count):
            if count < TRANSFER_JOB_COUNT and self.robot.jobs[-1]["name"] == "right_place":
                observed.append(self.grid.reconcile_observation(
                    self.grid.snapshot(), {1: "occupied", 2: "clear", 3: "clear"}))

        self.robot.after_move = update_grid_after_first_placement
        self.execute(capture_id, preview)

        self.assertEqual(len(observed), 1)
        self.assertEqual(len(self.robot.jobs), 3 * TRANSFER_JOB_COUNT)
        self.assertEqual([job["body"]["points"][-1]["x"] for job in self.robot.jobs
                          if job["name"] == "right_place"], [420, 500, 580])
        self.assertEqual(self.locations(), observed[0]["locations"])
        self.assertEqual([place["occupied"] for place in self.locations()], [True, False, False])
        self.assertFalse(any(place.get("reserved", False) for place in self.locations()))
        self.grid.validate_reservation.assert_not_called()
        self.robot.stop.assert_not_called()
        self.assertEqual(self.capture_trace, [("scan", 0)])
        self.assertEqual(self.session.batch_progress["completed_cups"], [2, 3, 1])
        self.assertEqual(self.session.batch_progress["state"], "completed")
        self.assertTrue(self.session.scan_usable)
        self.assertFalse(self.session.motion_uncertain)
        self.assertFalse(self.session.stop_requested)
        self.assertFalse(journal.required())

    def test_invalid_source_coordinates_cannot_reserve_grid_or_move(self):
        capture_id = self.scan()
        preview = self.session.preview_batch(capture_id=capture_id)
        before = self.grid.path.read_bytes()
        self.session.inventory["cups"][1]["x"] = float("nan")
        with self.assertRaises(SelectionError):
            self.execute(capture_id, preview)
        self.assertEqual(self.robot.jobs, [])
        self.assertEqual(self.grid.path.read_bytes(), before)
        self.assertEqual(self.capture_trace, [("scan", 0)])

    def test_robot_failure_keeps_all_unfinished_places_reserved_and_stops_batch(self):
        self.robot.error = RobotCommandError("right grip was not confirmed")
        self.robot.error_at = "right_grip"
        capture_id = self.scan()
        preview = self.session.preview_batch(capture_id=capture_id)
        with self.assertRaisesRegex(RobotCommandError, "not confirmed"):
            self.execute(capture_id, preview)
        self.assertEqual(self.robot.jobs[-1]["name"], "right_grip")
        self.assertEqual(sum(job["name"] == "grip_cup" for job in self.robot.jobs), 1)
        self.assertFalse(any(job["endpoint"].endswith("/home") for job in self.robot.jobs))
        self.assertNotIn("left_release", [job["name"] for job in self.robot.jobs])
        self.assertTrue(self.session.motion_uncertain)
        for place in self.locations():
            self.assertTrue(place["occupied"])
            self.assertTrue(place["reserved"])

    def test_second_cup_robot_failure_keeps_completed_slot_and_remaining_reservations(self):
        capture_id = self.scan()
        preview = self.session.preview_batch(capture_id=capture_id)
        def fail_second_receive(_):
            if len(self.robot.jobs) > TRANSFER_JOB_COUNT and self.robot.jobs[-1]["name"] == "right_take":
                raise RobotCommandError("Second cup receive unconfirmed")
        self.robot.after_move = fail_second_receive
        with self.assertRaisesRegex(RobotCommandError, "Second cup receive unconfirmed"):
            self.execute(capture_id, preview)
        self.assertGreater(len(self.robot.jobs), TRANSFER_JOB_COUNT)
        self.assertLess(len(self.robot.jobs), 2 * TRANSFER_JOB_COUNT)
        self.assertEqual(self.session.inventory["batch_execution"]["completed_cups"], [2])
        self.assertEqual([job["body"]["points"][-1]["x"] for job in self.robot.jobs if job["name"] == "approach_cup"], [550, 600])
        locations = self.locations()
        self.assertTrue(locations[0]["occupied"])
        self.assertFalse(locations[0].get("reserved", False))
        self.assertTrue(all(place["occupied"] and place["reserved"] for place in locations[1:]))
        self.assertFalse(self.session.scan_usable)
        self.assertTrue(self.session.motion_uncertain)
        self.assertEqual(self.capture_trace, [("scan", 0)])

    def test_all_cups_reuse_saved_frame_without_requesting_new_sequences(self):
        capture_id = self.scan()
        preview = self.session.preview_batch(capture_id=capture_id)
        frame = copy.deepcopy(self.session.inventory["frame"])
        self.session.capture = Mock(side_effect=AssertionError("Batch requested a new frame"))
        self.execute(capture_id, preview)
        self.session.capture.assert_not_called()
        self.assertEqual(len(self.robot.jobs), 3 * TRANSFER_JOB_COUNT)
        for cup in self.session.inventory["cups"][:3]:
            self.assertEqual(cup["last_verification"]["frame"], frame)
            self.assertEqual(cup["last_verification"]["source"], "saved_scan")

    def test_confirmed_cup_list_cannot_be_reordered_or_expanded(self):
        capture_id = self.scan()
        preview = self.session.preview_batch(capture_id=capture_id)
        for ids in ([1, 2, 3], [2, 3, 1, 4], [2, 3, 1, 1], [True, 3, 1], [2, 3, True], [2.0, 3, 1]):
            with self.subTest(ids=ids), self.assertRaises(SelectionError):
                self.session.execute_batch(capture_id=capture_id, cup_ids=ids,
                                           placement=preview["placement"])
        self.assertEqual(self.robot.jobs, [])
        self.assertFalse(any(place["occupied"] for place in self.locations()))

    def test_recovery_journal_stays_active_between_cups_and_clears_after_whole_batch(self):
        journal = RecoveryJournal(self.grid.path.with_name("recovery.json"))
        self.session.recovery = journal
        observed = []
        self.robot.after_move = lambda count: observed.append(journal.required())
        capture_id = self.scan()
        preview = self.session.preview_batch(capture_id=capture_id)
        self.execute(capture_id, preview)
        self.assertEqual(len(observed), 3 * TRANSFER_JOB_COUNT)
        self.assertTrue(all(observed))
        self.assertFalse(journal.required())

    def test_partial_batch_failure_remains_recovery_required_after_restart(self):
        journal = RecoveryJournal(self.grid.path.with_name("recovery.json"))
        self.session.recovery = journal
        capture_id = self.scan()
        preview = self.session.preview_batch(capture_id=capture_id)
        def fail_second_pick(_):
            if len(self.robot.jobs) > TRANSFER_JOB_COUNT and self.robot.jobs[-1]["name"] == "grip_cup":
                raise RobotCommandError("Second cup grip unconfirmed")
        self.robot.after_move = fail_second_pick
        with self.assertRaisesRegex(RobotCommandError, "Second cup grip unconfirmed"):
            self.execute(capture_id, preview)
        restarted = CupSession(self.capture, Mock(), RobotDouble(), "cal-1", clock=lambda: 100,
                               placing_grid=self.grid, recovery=RecoveryJournal(journal.path))
        self.assertTrue(restarted.motion_uncertain)
        self.assertTrue(journal.required())
        self.assertEqual(self.session.batch_progress["completed_cups"], [2])
        self.assertEqual(self.capture_count, 1)

    def test_missing_grid_blocks_pick_and_batch_but_allows_hover(self):
        self.session.placing_grid = None
        capture_id = self.scan()
        with self.assertRaisesRegex(SelectionError, "grid"):
            self.session.preview_selection(action="goto", capture_id=capture_id, cup_id=1)
        with self.assertRaisesRegex(SelectionError, "grid"):
            self.session.preview_batch(capture_id=capture_id)
        preview = self.session.preview_selection(action="hover", capture_id=capture_id, cup_id=1)
        self.assertIsNone(preview["placement"])
        self.assertEqual(preview["cup_ids"], [1])
        self.assertEqual(self.robot.jobs, [])

    def test_selected_place_is_reserved_before_first_command_and_complete_only_after_clearance(self):
        seen = []

        def inspect_grid(count):
            seen.append((self.robot.jobs[-1]["name"], copy.deepcopy(self.locations())))

        self.robot.after_move = inspect_grid
        capture_id = self.scan()
        preview = self.session.preview_selection(action="goto", capture_id=capture_id, cup_id=1)
        self.session.execute_selection(action="goto", capture_id=capture_id, cup_id=1,
                                       placement=preview["placement"])
        self.assertEqual(len(seen), TRANSFER_JOB_COUNT)
        self.assertEqual(seen[-1][0], "right_clear")
        self.assertTrue(all(places[0]["occupied"] and places[0]["reserved"] for _, places in seen))
        self.assertTrue(self.locations()[0]["occupied"])
        self.assertFalse(self.locations()[0].get("reserved", False))


if __name__ == "__main__":
    unittest.main()
