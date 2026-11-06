"""Offline integration tests for cup inventory, selection, and terminal commands."""

from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from capture_and_detect_custom import handle_terminal
from cup_commands import CupSession
from cup_inventory import SelectionError, atomic_json, build_inventory, selected_cup
from robot_client import RobotClient, RobotCommandError
from recovery_journal import RecoveryJournal


def detection(kind, u, *, x=500, z=50, detection_id=None):
    radius = 15 if kind == "CUP" else 3
    return {"class": kind, "detection_id": detection_id, "confidence": 0.9,
            "x_px": u, "y_px": 100, "bbox_xyxy": [u - radius, 80, u + radius, 120],
            "robot_x_mm": x, "robot_y_mm": 100, "robot_z_mm": z,
            "x_mm": 10, "y_mm": 20, "z_mm": 800 if z is not None else None}


def inventory(*, capture="scan-1", sequence=10, objects=None, calibration="cal-1", tag_offset=(0, 0, 0)):
    if objects is None:
        objects = [detection("CUP", 100), detection("CUP BASE", 100, detection_id=1)]
    return build_inventory(objects, capture_id=capture,
                           frame={"sequence": sequence, "timestamp": "2001-01-01T00:00:00Z",
                                  "observed_at": "2026-10-16T12:00:00Z", "received_monotonic": 99},
                           calibration_id=calibration, gateway="http://offline.invalid/api/v1",
                           images={"annotated": "offline.jpg"}, model={"weights": "fixture"}, tag_offset=tag_offset)


class RobotDouble:
    dry_run = False

    def __init__(self, error=None, after_move=None, error_at=None):
        self.commands = []
        self.jobs = []
        self.error = error
        self.error_at = error_at
        self.after_move = after_move

    def execute_jobs(self, jobs, before_send=None, after_done=None):
        for index, job in enumerate(copy.deepcopy(jobs)):
            replacement = before_send(index, job) if before_send else None
            job = replacement if replacement is not None else job
            self.jobs.append(job)
            self.commands.append(job["body"])
            if self.error and (self.error_at is None or self.error_at == job["name"]):
                raise self.error
            if self.after_move:
                self.after_move(len(self.commands))
            if after_done:
                after_done(index, job)


class CupSelectionTests(unittest.TestCase):
    def setUp(self):
        self.output = redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def session(self, *, fresh=None, started=99, robot=None, save=None, recovery=None):
        original = inventory()
        if fresh is None:
            fresh = inventory(capture="verification-1", sequence=11)
        after_ready = copy.deepcopy(fresh)
        after_ready["capture"] = "verification-after-ready"
        after_ready["frame"]["sequence"] += 1
        capture = Mock(side_effect=[(original, 90), (fresh, started), (after_ready, started)])
        robot = robot if robot is not None else RobotDouble()
        save = save if save is not None else Mock()
        session = CupSession(capture, save, robot, "cal-1", clock=lambda: 100,
                             max_result_age=10, recovery=recovery)
        return session, capture, robot, save

    def select(self, session, **changes):
        request = {"action": "hover", "capture_id": "scan-1", "cup_id": 1}
        request.update(changes)
        return session.execute_selection(**request)

    def test_inventory_keeps_all_cups_and_orphan_bases_with_left_to_right_ids(self):
        objects = [detection("CUP BASE", 400), detection("CUP", 300),
                   detection("CUP BASE", 300, z=None), detection("CUP", 200),
                   detection("CUP", 100), detection("CUP BASE", 100)]
        result = inventory(objects=objects)
        self.assertEqual([cup["id"] for cup in result["cups"]], [1, 2, 3, 4])
        self.assertEqual([cup["pixel"]["u"] for cup in result["cups"]], [100, 200, 300, 400])
        self.assertEqual([cup["selectable"] for cup in result["cups"]], [True, False, False, False])
        for cup, reason in zip(result["cups"][1:], ("no CUP BASE", "missing or invalid", "no surrounding CUP")):
            self.assertIn(reason, cup["blocked_reason"])
        self.assertNotIn("received_monotonic", result["frame"])
        self.assertIsNone(result["cups"][2]["z"])

    def test_multiple_bases_and_overlapping_cups_are_not_selectable(self):
        cases = [[detection("CUP", 100), detection("CUP BASE", 98), detection("CUP BASE", 102)],
                 [detection("CUP", 95), detection("CUP", 105), detection("CUP BASE", 100)]]
        for objects, reason in zip(cases, ("multiple", "overlapping")):
            with self.subTest(reason=reason):
                result = inventory(objects=objects)
                self.assertTrue(all(not cup["selectable"] for cup in result["cups"]))
                self.assertTrue(all(reason in cup["blocked_reason"] for cup in result["cups"]))

    def test_wrong_capture_unknown_and_invalid_ids_cannot_execute(self):
        cases = [{"capture_id": "old-scan"}, {"cup_id": 99}, {"cup_id": 0},
                 {"cup_id": -1}, {"cup_id": True}, {"cup_id": "1"}, {"action": "pick"}]
        for changes in cases:
            with self.subTest(changes=changes):
                session, capture, robot, _ = self.session()
                session.scan()
                with self.assertRaises(SelectionError):
                    self.select(session, **changes)
                self.assertEqual(capture.call_count, 1)
                self.assertEqual(robot.commands, [])

    def test_invalid_coordinates_are_blocked(self):
        for x, z in [(float("nan"), 50), (float("inf"), 50), (None, 50), (900, 50), (500, 350)]:
            with self.subTest(x=x, z=z):
                result = inventory(objects=[detection("CUP", 100), detection("CUP BASE", 100, x=x, z=z)])
                with self.assertRaises(SelectionError):
                    selected_cup(result, "scan-1", 1)

    def test_terminal_scan_list_show_help_never_execute(self):
        for command in ("", "scan", "capture"):
            with self.subTest(command=command):
                session, capture, robot, _ = self.session()
                self.assertTrue(handle_terminal(session, command))
                for read in ("list", "show 1", "help"):
                    self.assertTrue(handle_terminal(session, read))
                self.assertFalse(handle_terminal(session, "quit"))
                capture.assert_called_once_with("scan")
                self.assertEqual(robot.commands, [])

    def test_malformed_terminal_commands_never_execute(self):
        for line in ("goto", "goto two", "goto 1 2", "goto 1; HOME", "GOTO,500,100,200",
                     "pick 1", "scan extra", "goto -1", "goto 0", 'goto "unterminated'):
            with self.subTest(line=line):
                session, capture, robot, _ = self.session()
                session.scan()
                with self.assertRaises(ValueError):
                    handle_terminal(session, line)
                self.assertEqual(capture.call_count, 1)
                self.assertEqual(robot.commands, [])

    def test_hover_reobserves_around_ready_and_uses_fresh_rws_coordinates_then_invalidates(self):
        fresh = inventory(capture="verify", sequence=11,
                          objects=[detection("CUP", 102), detection("CUP BASE", 102, x=505)])
        session, capture, robot, save = self.session(fresh=fresh)
        handle_terminal(session, "scan")
        handle_terminal(session, "hover 1")
        self.assertEqual([(c["x"], c["y"], c["z"]) for c in robot.commands], [(400, 100, 450), (505, 100, 409)])
        self.assertTrue(all(c["arm"] == "left" and c["wait"] is True for c in robot.commands))
        self.assertEqual(capture.call_args_list[-1].args, ("verification",))
        self.assertEqual(capture.call_count, 3)
        self.assertEqual(session.inventory["cups"][0]["status"], "at_hover")
        self.assertFalse(session.scan_usable)
        self.assertEqual(save.call_count, 6)
        with self.assertRaises(SelectionError):
            self.select(session)
        self.assertEqual(len(robot.commands), 2)

    def test_dry_run_with_real_robot_client_makes_no_http_requests(self):
        robot = RobotClient("http://offline.invalid/api/v1", {}, dry_run=True)
        session, _, _, _ = self.session(robot=robot)
        with patch("requests.post") as post, patch("requests.get") as get:
            session.scan()
            moves = [job["body"] for job in self.select(session)]
            self.assertEqual([(c["x"], c["y"], c["z"]) for c in moves], [(400, 100, 450), (500, 100, 409)])
            post.assert_not_called()
            get.assert_not_called()
        self.assertTrue(session.scan_usable)

    def test_bad_verification_never_sends_any_command(self):
        baseline = inventory(capture="verify", sequence=11)
        cases = []
        moved = copy.deepcopy(baseline)
        moved["cups"][0]["x"] += 100
        cases.append(("moved", moved, 99))
        missing = copy.deepcopy(baseline)
        missing["cups"] = []
        cases.append(("missing", missing, 99))
        ambiguous = copy.deepcopy(baseline)
        ambiguous["cups"].append(dict(ambiguous["cups"][0], id=2))
        cases.append(("ambiguous", ambiguous, 99))
        changed = copy.deepcopy(baseline)
        changed["calibration_sha256"] = "different"
        cases.append(("calibration", changed, 99))
        frozen = copy.deepcopy(baseline)
        frozen["frame"]["sequence"] = 10
        cases.extend([("frozen", frozen, 99), ("old", baseline, 89), ("future clock", baseline, 101)])
        for name, fresh, started in cases:
            with self.subTest(name=name):
                session, _, robot, _ = self.session(fresh=fresh, started=started)
                session.scan()
                with self.assertRaises(SelectionError):
                    self.select(session)
                self.assertEqual(robot.commands, [])
                self.assertFalse(session.scan_usable)

    def test_failed_scan_invalidates_previous_authorization(self):
        session, capture, robot, _ = self.session()
        session.scan()
        capture.side_effect = RuntimeError("camera failed")
        with self.assertRaises(RuntimeError):
            session.scan()
        with self.assertRaises(SelectionError):
            self.select(session)
        self.assertFalse(session.scan_usable)
        self.assertEqual(robot.commands, [])

    def test_timeout_locks_motion_even_after_new_scan_until_reset(self):
        session, capture, robot, _ = self.session(robot=RobotDouble(RobotCommandError("timeout")))
        session.scan()
        with self.assertRaises(RobotCommandError):
            self.select(session)
        self.assertTrue(session.motion_uncertain)
        self.assertEqual(session.inventory["cups"][0]["status"], "movement_unconfirmed")
        capture.side_effect = [(inventory(capture="scan-2", sequence=12), 99)]
        session.scan()
        with self.assertRaises(SelectionError):
            self.select(session, capture_id="scan-2")
        handle_terminal(session, "reset")
        self.assertFalse(session.motion_uncertain)
        self.assertFalse(session.scan_usable)
        self.assertEqual(len(robot.commands), 1)

    def test_failed_save_prevents_dispatch(self):
        session, _, robot, save = self.session(save=Mock(side_effect=OSError("disk full")))
        session.scan()
        with self.assertRaises(OSError):
            self.select(session)
        save.assert_called_once()
        self.assertEqual(robot.commands, [])
        self.assertFalse(session.scan_usable)

    def test_current_offset_is_applied_once_in_final_move(self):
        session, capture, robot, _ = self.session()
        session.tag_offset = (-115, 0, 40)
        capture.side_effect = [(inventory(tag_offset=session.tag_offset), 90),
                               (inventory(capture="v1", sequence=11, tag_offset=session.tag_offset), 99),
                               (inventory(capture="v2", sequence=12, tag_offset=session.tag_offset), 99)]
        session.scan()
        self.select(session)
        self.assertEqual((robot.commands[1]["x"], robot.commands[1]["y"], robot.commands[1]["z"]), (385, 100, 409))
        self.assertEqual(session.inventory["cups"][0]["x"], 500)
        self.assertEqual(session.inventory["cups"][0]["last_verification"]["jobs"][1]["endpoint"], "/robot/rws/move")

    def test_slow_ready_move_gets_new_observation_before_hover(self):
        now = [100]
        robot = RobotDouble(after_move=lambda count: now.__setitem__(0, 130) if count == 1 else None)
        session, capture, _, _ = self.session(robot=robot)
        session.clock = lambda: now[0]
        changed = inventory(capture="after-ready", sequence=12,
                            objects=[detection("CUP", 102), detection("CUP BASE", 102, x=505)])
        capture.side_effect = [(inventory(), 90), (inventory(capture="v1", sequence=11), 99), (changed, 129)]
        session.scan()
        moves = [job["body"] for job in self.select(session)]
        self.assertEqual(len(robot.commands), 2)
        self.assertEqual(robot.commands[1]["x"], 505)
        self.assertEqual(moves[1]["x"], 505)
        self.assertFalse(session.motion_uncertain)

    def test_cup_disappears_after_ready_blocks_hover_and_requires_recovery(self):
        session, capture, robot, _ = self.session()
        capture.side_effect = [(inventory(), 90), (inventory(capture="v1", sequence=11), 99),
                               (inventory(capture="v2", sequence=12, objects=[]), 99)]
        session.scan()
        with self.assertRaises(SelectionError):
            self.select(session)
        self.assertEqual(len(robot.commands), 1)
        self.assertEqual(robot.commands[0]["x"], 400)
        self.assertTrue(session.motion_uncertain)
        self.assertFalse(session.scan_usable)

    def test_post_ready_save_failure_blocks_hover(self):
        session, _, robot, save = self.session(save=Mock(side_effect=[None, None, None, OSError("disk full"), None]))
        session.scan()
        with self.assertRaises(OSError):
            self.select(session)
        self.assertEqual(len(robot.commands), 1)
        self.assertTrue(session.motion_uncertain)

    def test_atomic_failures_keep_previous_complete_json_and_remove_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cups.json"
            original = {"capture": "original", "cups": []}
            atomic_json(path, original)
            with self.assertRaises(ValueError):
                atomic_json(path, {"capture": "broken", "x": float("nan")})
            self.assertEqual(json.loads(path.read_text()), original)
            with patch("cup_inventory.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    atomic_json(path, {"capture": "replacement"})
            self.assertEqual(json.loads(path.read_text()), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_goto_runs_selected_cup_through_handover_and_homes_with_progress_saved(self):
        session, capture, robot, save = self.session()
        session.tag_offset = (-115, 0, 40)

        def scene(capture_id, sequence, selected_x, extra=False):
            objects = [detection("CUP", 100), detection("CUP BASE", 100, detection_id=1),
                       detection("CUP", 200, x=selected_x),
                       detection("CUP BASE", 200, x=selected_x, detection_id=2)]
            if extra:
                objects += [detection("CUP", 30, x=450),
                            detection("CUP BASE", 30, x=450, detection_id=3)]
            return inventory(capture=capture_id, sequence=sequence, objects=objects,
                             tag_offset=session.tag_offset)

        capture.side_effect = [(scene("scan-1", 10, 600), 90),
                               (scene("v1", 11, 605, extra=True), 99),
                               (scene("v2", 12, 608, extra=True), 99)]
        handle_terminal(session, "scan")
        handle_terminal(session, "goto 2")
        jobs = {job["name"]: job for job in robot.jobs}
        for name, height in (("approach_cup", 409), ("descend_to_cup", 234), ("lift_cup", 409)):
            body = jobs[name]["body"]
            self.assertEqual((body["x"], body["y"], body["z"]), (493, 100, height))
        self.assertEqual(robot.jobs[-1]["name"], "right_home")
        self.assertIn("left_home", jobs)
        self.assertEqual(capture.call_count, 3)
        selected = session.inventory["cups"][1]
        self.assertEqual(selected["x"], 600)  # Original observation is retained.
        self.assertEqual(selected["last_verification"]["matched_cup_id"], 3)
        self.assertEqual(selected["status"], "placed")
        self.assertEqual(selected["execution"]["completed_steps"], robot.jobs)
        self.assertEqual(selected["execution"]["state"], "completed")
        self.assertFalse(session.scan_usable)
        self.assertFalse(session.motion_uncertain)
        self.assertGreater(save.call_count, len(jobs))

    def test_full_handover_dry_run_never_contacts_robot_and_prints_all_actions(self):
        robot = RobotClient("http://offline.invalid/api/v1", {}, dry_run=True)
        session, capture, _, _ = self.session(robot=robot)
        with patch("requests.post") as post, patch("requests.get") as get:
            session.scan()
            jobs = self.select(session, action="goto")
            self.assertEqual({job["endpoint"] for job in jobs},
                             {"/robot/rws/move", "/robot/rws/home", "/robot/rws/gripper"})
            post.assert_not_called()
            get.assert_not_called()
        self.assertEqual(capture.call_count, 2)
        self.assertTrue(session.scan_usable)

    def test_full_pick_blocks_out_of_workspace_or_invalid_table_height_before_motion(self):
        for y, z in ((-627, -55), (100, -55), (100, 180)):
            with self.subTest(y=y, z=z):
                session, capture, robot, _ = self.session()
                result = inventory()
                result["cups"][0].update(y=y, z=z)
                capture.side_effect = [(result, 90)]
                session.scan()
                with self.assertRaises(SelectionError):
                    self.select(session, action="goto")
                self.assertEqual(robot.jobs, [])
                self.assertEqual(capture.call_count, 1)

    def test_failed_right_grip_prevents_left_release_and_any_automatic_home(self):
        robot = RobotDouble(RobotCommandError("right grip failed"), error_at="right_grip")
        session, capture, _, _ = self.session(robot=robot)
        session.scan()
        with self.assertRaises(RobotCommandError):
            self.select(session, action="goto")
        names = [job["name"] for job in robot.jobs]
        self.assertEqual(names[-1], "right_grip")
        self.assertNotIn("left_release", names)
        self.assertNotIn("left_home", names)
        self.assertNotIn("right_home", names)
        record = session.inventory["cups"][0]
        self.assertEqual(record["status"], "movement_unconfirmed")
        self.assertEqual(record["execution"]["pending_step"], "right_grip")
        self.assertEqual(record["execution"]["last_completed_step"], "right_take")
        self.assertTrue(session.motion_uncertain)
        capture.side_effect = [(inventory(capture="new", sequence=13), 99)]
        session.scan()
        with self.assertRaises(SelectionError):
            self.select(session, action="goto", capture_id="new")

    def test_slow_initial_home_is_followed_by_fresh_camera_check_before_approach(self):
        now = [100]
        robot = RobotDouble(after_move=lambda count: now.__setitem__(0, 130) if count == 1 else None)
        session, capture, _, _ = self.session(robot=robot)
        session.clock = lambda: now[0]
        capture.side_effect = [(inventory(), 90), (inventory(capture="v1", sequence=11), 99),
                               (inventory(capture="v2", sequence=12), 129)]
        session.scan()
        self.select(session, action="goto")
        self.assertEqual(session.inventory["cups"][0]["status"], "placed")
        self.assertEqual(capture.call_count, 3)

    def test_age_limit_stops_before_descent_but_not_during_transfer_after_grip(self):
        for delayed_step, should_fail in (("approach_cup", True), ("descend_to_cup", True), ("grip_cup", False)):
            with self.subTest(delayed_step=delayed_step):
                now = [100]
                robot = RobotDouble()
                robot.after_move = lambda count: now.__setitem__(0, 130) if robot.jobs[-1]["name"] == delayed_step else None
                session, _, _, _ = self.session(robot=robot)
                session.clock = lambda: now[0]
                session.scan()
                if should_fail:
                    with self.assertRaisesRegex(SelectionError, "old; motion blocked"):
                        self.select(session, action="goto")
                    self.assertEqual(robot.jobs[-1]["name"], delayed_step)
                    self.assertTrue(session.motion_uncertain)
                else:
                    self.select(session, action="goto")
                    self.assertEqual(robot.jobs[-1]["name"], "right_home")
                    self.assertFalse(session.motion_uncertain)

    def test_failure_to_save_progress_after_gripping_does_not_release_or_home(self):
        robot = RobotDouble()
        def persist(result):
            execution = result["cups"][0].get("execution", {})
            if execution.get("last_completed_step") == "grip_cup":
                raise OSError("disk full after grip")
        session, _, _, _ = self.session(robot=robot, save=Mock(side_effect=persist))
        session.scan()
        with self.assertRaisesRegex(OSError, "disk full after grip"):
            self.select(session, action="goto")
        self.assertEqual(robot.jobs[-1]["name"], "grip_cup")
        self.assertTrue(session.motion_uncertain)

    def test_failed_transfer_remains_locked_across_restart_and_scan_until_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recovery.json"
            robot = RobotDouble(RobotCommandError("lost connection"), error_at="right_grip")
            session, _, _, _ = self.session(robot=robot, recovery=RecoveryJournal(path))
            session.scan()
            with self.assertRaises(RobotCommandError):
                self.select(session, action="goto")
            restarted, capture, new_robot, _ = self.session(recovery=RecoveryJournal(path))
            self.assertTrue(restarted.motion_uncertain)
            restarted.scan()
            with self.assertRaisesRegex(SelectionError, "Previous movement"):
                self.select(restarted, action="goto")
            self.assertEqual(new_robot.jobs, [])
            self.assertEqual(capture.call_count, 1)
            self.assertTrue(RecoveryJournal(path).required())
            handle_terminal(restarted, "reset")
            self.assertFalse(RecoveryJournal(path).required())
            self.assertFalse(restarted.scan_usable)
            with self.assertRaisesRegex(SelectionError, "Run scan"):
                self.select(restarted, action="goto")

    def test_recovery_journal_is_active_during_live_jobs_and_clears_only_on_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = RecoveryJournal(Path(directory) / "recovery.json")
            states = []
            robot = RobotDouble(after_move=lambda count: states.append(journal.required()))
            session, _, _, _ = self.session(robot=robot, recovery=journal)
            session.scan()
            self.select(session, action="goto")
            self.assertTrue(states)
            self.assertTrue(all(states))
            self.assertFalse(journal.required())

    def test_failed_recovery_journal_write_prevents_first_live_command(self):
        journal = Mock()
        journal.required.return_value = False
        journal.begin.side_effect = OSError("cannot persist recovery")
        session, _, robot, _ = self.session(recovery=journal)
        session.scan()
        with self.assertRaisesRegex(OSError, "cannot persist"):
            self.select(session, action="goto")
        self.assertEqual(robot.jobs, [])
        journal.clear.assert_not_called()

    def test_dry_run_does_not_create_live_recovery_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recovery.json"
            session, _, _, _ = self.session(recovery=RecoveryJournal(path),
                robot=RobotClient("http://offline.invalid/api/v1", {}, dry_run=True))
            session.scan()
            self.select(session, action="goto")
            self.assertFalse(path.exists())

    def test_full_selection_uses_real_client_with_mocked_gateway_and_home_reads(self):
        def response(body):
            reply = Mock(status_code=200, headers={})
            reply.json.return_value = body
            return reply

        posted = []
        def post(url, **options):
            posted.append((url, options.get("json")))
            if url.endswith("/control/acquire"):
                return response({"success": True, "lease": {"active": True}})
            if url.endswith("/control/release"):
                return response({"success": True})
            return response({"accepted": True, "message": "DONE"})

        def get(url, **options):
            arm = "right" if "/right/" in url else "left"
            return response({"arm": arm, "joints": [0, -130, 30, 0, 40, 0],
                             "arm_angle": -135 if arm == "right" else 135})

        session, _, _, _ = self.session(robot=RobotClient("http://offline.invalid/api/v1", {}))
        with patch("requests.post", side_effect=post), patch("requests.get", side_effect=get) as read:
            session.scan()
            jobs = self.select(session, action="goto")
        robot_calls = [(url.split("/api/v1")[1], body) for url, body in posted if "/robot/" in url]
        self.assertEqual(robot_calls, [(job["endpoint"], job["body"]) for job in jobs])
        self.assertEqual(read.call_count, 3)
        self.assertTrue(posted[0][0].endswith("/control/acquire"))
        self.assertTrue(posted[-1][0].endswith("/control/release"))
        self.assertEqual(session.inventory["cups"][0]["status"], "placed")


if __name__ == "__main__":
    unittest.main()
