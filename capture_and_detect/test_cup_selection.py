"""Offline integration tests for cup inventory, selection, and terminal commands."""

from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from picker_workflow import handle_terminal
from cup_commands import CupSession
from cup_inventory import SelectionError, atomic_json, build_inventory, selected_cup
from handover_plan import handover_jobs
from robot_client import RobotClient, RobotCommandError
from recovery_journal import RecoveryJournal
from test_support_grid import temporary_grid


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


# Workflow fixtures count complete transfers; planner tests independently check
# the exact jobs, geometry and dependencies.
TRANSFER_JOB_COUNT = len(handover_jobs(inventory()["cups"][0],
                                     place={"id": 1, "x": 420, "y": -200, "z": 230, "occupied": False}))


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

    def session(self, *, fresh=None, started=99, robot=None, save=None, recovery=None, home_after_cup=False):
        original = inventory()
        if fresh is None:
            fresh = inventory(capture="verification-1", sequence=11)
        capture = Mock(side_effect=[(original, 90), (fresh, started)])
        robot = robot if robot is not None else RobotDouble()
        save = save if save is not None else Mock()
        session = CupSession(capture, save, robot, "cal-1", clock=lambda: 100,
                             max_result_age=10, recovery=recovery, placing_grid=temporary_grid(self),
                             home_after_cup=home_after_cup)
        return session, capture, robot, save

    def select(self, session, **changes):
        request = {"action": "hover", "capture_id": "scan-1", "cup_id": 1}
        request.update(changes)
        return session.execute_selection(**request)

    def test_inventory_keeps_supported_bases_with_left_to_right_ui_ids(self):
        objects = [detection("CUP BASE", 400), detection("CUP", 300),
                   detection("CUP BASE", 300, z=None), detection("CUP", 200),
                   detection("CUP", 100), detection("CUP BASE", 100)]
        result = inventory(objects=objects)
        self.assertEqual([cup["id"] for cup in result["cups"]], [1, 2])
        self.assertEqual([cup["pixel"]["u"] for cup in result["cups"]], [100, 300])
        self.assertEqual([cup["selectable"] for cup in result["cups"]], [True, False])
        self.assertNotIn("received_monotonic", result["frame"])
        self.assertIsNone(result["cups"][1]["z"])

    def test_multiple_bases_and_overlapping_cups_follow_v4_candidates(self):
        cases = [([detection("CUP", 100), detection("CUP BASE", 98), detection("CUP BASE", 102)], 2),
                 ([detection("CUP", 95), detection("CUP", 105), detection("CUP BASE", 100)], 1)]
        for objects, count in cases:
            with self.subTest(count=count):
                result = inventory(objects=objects)
                self.assertEqual(len(result["cups"]), count)
                self.assertTrue(all(cup["selectable"] for cup in result["cups"]))

    def test_scanned_handle_rotation_and_opening_reach_the_pick_jobs(self):
        base = dict(detection("CUP BASE", 100), diameter_mm=50)
        handle = detection("CUP HANDLE", 120, x=520)
        original = inventory(objects=[detection("CUP", 100), base, handle])
        session, capture, robot, _ = self.session()
        capture.side_effect = [(original, 90)]
        session.scan()
        jobs = self.select(session, action="goto")
        bodies = {job["name"]: job["body"] for job in jobs}
        self.assertEqual(bodies["left_open"]["position"], 22.5)
        pick_points = bodies["approach_cup"]["points"][1:] + bodies["left_handover"]["points"][:1]
        self.assertEqual([point["rz"] for point in pick_points], [90, 90, 90])
        self.assertEqual(bodies["left_handover"]["points"][-1]["rz"], 180)
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(robot.jobs, jobs)
        geometry = original["cups"][0]["last_verification"]["grip_geometry"]
        self.assertEqual(geometry, {"diameter": 50, "aperture": 120, "rz": 90,
                                    "has_handle": True, "pick_tcp_z_mm": 70})

    def test_later_camera_geometry_is_not_requested_or_substituted(self):
        fresh = inventory(capture="unused", sequence=11)
        fresh["cups"][0].update(rz=90, has_handle=True, diameter=50, aperture=90)
        session, capture, robot, _ = self.session(fresh=fresh)
        session.scan()
        expected = copy.deepcopy(session.inventory["cups"][0])
        jobs = self.select(session, action="goto")
        descent = next(job["body"]["points"][-1] for job in jobs if job["name"] == "approach_cup")
        self.assertEqual(descent["rz"], expected["rz"])
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(robot.jobs, jobs)

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
        for x, z in [(float("nan"), 50), (float("inf"), 50), (None, 50), (900, 50), (500, None)]:
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

    def test_hover_uses_saved_coordinates_and_keeps_scan_available(self):
        fresh = inventory(capture="unused", sequence=11,
                          objects=[detection("CUP", 102), detection("CUP BASE", 102, x=505)])
        session, capture, robot, _ = self.session(fresh=fresh)
        handle_terminal(session, "scan")
        handle_terminal(session, "hover 1")
        self.assertEqual([(c["x"], c["y"], c["z"]) for c in robot.commands], [(400, 100, 450), (500, 100, 409)])
        capture.assert_called_once_with("scan")
        self.assertEqual(session.inventory["cups"][0]["status"], "at_hover")
        self.assertTrue(session.scan_usable)
        self.assertTrue(session.inventory["cups"][0]["selectable"])

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

    def test_no_verification_or_calibration_consistency_read_before_pick(self):
        session, capture, robot, _ = self.session()
        session.scan()
        capture.side_effect = AssertionError("Unexpected camera request")
        session.calibration_id = "different-runtime-id"
        self.select(session, action="goto")
        self.assertEqual(len(robot.jobs), TRANSFER_JOB_COUNT)
        self.assertEqual(capture.call_count, 1)

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
        save.assert_called()
        self.assertEqual(robot.commands, [])
        self.assertFalse(session.scan_usable)

    def test_current_offset_is_applied_once_in_final_move(self):
        session, capture, robot, _ = self.session()
        session.tag_offset = (-115, 0, 40)
        capture.side_effect = [(inventory(tag_offset=session.tag_offset), 90),
                               (inventory(capture="v1", sequence=11, tag_offset=session.tag_offset), 99)]
        session.scan()
        self.select(session)
        self.assertEqual((robot.commands[1]["x"], robot.commands[1]["y"], robot.commands[1]["z"]), (385, 100, 409))
        self.assertEqual(session.inventory["cups"][0]["x"], 500)
        self.assertEqual(session.inventory["cups"][0]["last_verification"]["jobs"][1]["endpoint"], "/robot/rws/move")

    def test_slow_ready_does_not_expire_saved_scan(self):
        now = [100]
        robot = RobotDouble(after_move=lambda count: now.__setitem__(0, 10000))
        session, capture, _, _ = self.session(robot=robot)
        session.clock = lambda: now[0]
        session.scan()
        self.select(session)
        self.assertEqual([job["name"] for job in robot.jobs], ["left_ready", "approach_cup"])
        self.assertEqual(capture.call_count, 1)
        self.assertFalse(session.motion_uncertain)
        self.assertTrue(session.scan_usable)

    def test_second_camera_frame_is_unused_after_ready(self):
        session, capture, robot, _ = self.session()
        capture.side_effect = [(inventory(), 90), (inventory(capture="v1", sequence=11), 99),
                               (inventory(capture="v2", sequence=12, objects=[]), 99)]
        session.scan()
        self.select(session)
        self.assertEqual([job["name"] for job in robot.jobs], ["left_ready", "approach_cup"])
        self.assertEqual(robot.commands[1]["x"], 500)
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(session.inventory["cups"][0]["last_verification"]["capture"], "scan-1")
        self.assertFalse(session.motion_uncertain)
        self.assertTrue(session.scan_usable)

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

    def test_goto_runs_selected_cup_through_handover_and_clearance_with_progress_saved(self):
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
        pick_points = (jobs["approach_cup"]["body"]["points"][1:] +
                       jobs["left_handover"]["body"]["points"][:1])
        self.assertEqual([(p["x"], p["y"], p["z"]) for p in pick_points],
                         [(485, 100, 334), (485, 100, 229), (485, 100, 334)])
        self.assertEqual(robot.jobs[-1]["name"], "right_clear")
        self.assertIn("left_clear", jobs)
        self.assertFalse(any(job["endpoint"].endswith("/home") for job in robot.jobs))
        self.assertEqual(capture.call_count, 1)
        selected = session.inventory["cups"][1]
        self.assertEqual(selected["x"], 600)  # Original observation is retained.
        self.assertEqual(selected["last_verification"]["matched_cup_id"], 2)
        self.assertEqual(selected["last_verification"]["capture"], "scan-1")
        self.assertEqual(selected["status"], "placed")
        self.assertEqual(selected["execution"]["completed_steps"], robot.jobs)
        self.assertEqual(selected["execution"]["state"], "completed")
        self.assertTrue(session.scan_usable)
        self.assertFalse(session.motion_uncertain)
        self.assertGreater(save.call_count, len(jobs))

    def test_full_handover_dry_run_never_contacts_robot_and_prints_all_actions(self):
        robot = RobotClient("http://offline.invalid/api/v1", {}, dry_run=True)
        session, capture, _, _ = self.session(robot=robot)
        with patch("requests.post") as post, patch("requests.get") as get:
            session.scan()
            jobs = self.select(session, action="goto")
            self.assertEqual({job["endpoint"] for job in jobs},
                             {"/robot/rws/path", "/robot/rws/gripper"})
            post.assert_not_called()
            get.assert_not_called()
        self.assertEqual(capture.call_count, 1)
        self.assertTrue(session.scan_usable)

    def test_pick_keeps_v4_source_xy_bounds_but_no_extra_camera_height_gate(self):
        for y, z, blocked in ((-627, -55, True), (100, -55, False), (100, 180, False)):
            with self.subTest(y=y, z=z):
                session, capture, robot, _ = self.session()
                result = inventory(objects=[detection("CUP", 100), dict(detection("CUP BASE", 100, z=z), robot_y_mm=y)])
                capture.side_effect = [(result, 90)]
                session.scan()
                if blocked:
                    with self.assertRaises(SelectionError):
                        self.select(session, action="goto")
                    self.assertEqual(robot.jobs, [])
                else:
                    self.assertEqual(len(self.select(session, action="goto")), TRANSFER_JOB_COUNT)
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

    def test_opt_in_home_does_not_expire_the_snapshot(self):
        now = [100]
        robot = RobotDouble(after_move=lambda count: now.__setitem__(0, 10000))
        session, capture, _, _ = self.session(robot=robot, home_after_cup=True)
        session.clock = lambda: now[0]
        session.scan()
        self.select(session, action="goto")
        self.assertEqual(capture.call_count, 1)
        self.assertTrue(any(job["endpoint"].endswith("/home") for job in robot.jobs))
        self.assertFalse(session.motion_uncertain)
        self.assertFalse(session.placing_grid.snapshot()["locations"][0].get("reserved", False))

    def test_elapsed_time_does_not_interrupt_any_transfer_phase(self):
        for delayed_step in ("approach_cup", "left_handover", "grip_cup"):
            with self.subTest(delayed_step=delayed_step):
                now = [100]
                robot = RobotDouble()
                robot.after_move = lambda count: now.__setitem__(0, 10000) if robot.jobs[-1]["name"] == delayed_step else None
                session, capture, _, _ = self.session(robot=robot)
                session.clock = lambda: now[0]
                session.scan()
                self.select(session, action="goto")
                self.assertEqual(robot.jobs[-1]["name"], "right_clear")
                self.assertFalse(session.motion_uncertain)
                self.assertEqual(capture.call_count, 1)

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

    def test_failed_final_recovery_clear_keeps_hover_locked_until_recovery(self):
        journal = Mock()
        journal.required.return_value = False
        journal.clear.side_effect = OSError("cannot clear recovery")
        session, _, robot, _ = self.session(recovery=journal)
        session.scan()
        with self.assertRaisesRegex(OSError, "cannot clear recovery"):
            self.select(session, action="hover")
        self.assertEqual(len(robot.jobs), 2)
        self.assertTrue(session.motion_uncertain)
        self.assertFalse(session.scan_usable)
        journal.clear.assert_called_once()

    def test_dry_run_does_not_create_live_recovery_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recovery.json"
            session, _, _, _ = self.session(recovery=RecoveryJournal(path),
                robot=RobotClient("http://offline.invalid/api/v1", {}, dry_run=True))
            session.scan()
            self.select(session, action="goto")
            self.assertFalse(path.exists())

    def test_default_selection_uses_real_client_without_home_requests_or_home_reads(self):
        self.check_full_selection(home_after_cup=False)

    def test_opt_in_home_uses_real_client_with_verified_home_reads(self):
        self.check_full_selection(home_after_cup=True)

    def check_full_selection(self, *, home_after_cup):
        from test_gateway_capabilities import schema_fixture

        def response(body):
            reply = Mock(status_code=200, headers={}, content=b"fixture")
            reply.json.return_value = body
            return reply

        posted, requests_in_order = [], []
        def post(url, **options):
            requests_in_order.append(url)
            posted.append((url, options.get("json")))
            if url.endswith("/control/acquire"):
                return response({"success": True, "lease": {"active": True}})
            if url.endswith("/control/release"):
                return response({"success": True})
            return response({"accepted": True, "message": "DONE"})

        def get(url, **options):
            requests_in_order.append(url)
            if url.endswith("/openapi.json"):
                return response(schema_fixture(blended_path=True))
            arm = "right" if "/right/" in url else "left"
            if url.endswith("/gripper"):
                return response({"arm": arm, "position": 8.5})
            return response({"arm": arm, "joints": [0, -130, 30, 0, 40, 0],
                             "arm_angle": -135 if arm == "right" else 135})

        session, _, _, _ = self.session(robot=RobotClient("http://offline.invalid/api/v1", {}),
                                       home_after_cup=home_after_cup)
        with patch("requests.post", side_effect=post), patch("requests.get", side_effect=get) as read:
            session.scan()
            jobs = self.select(session, action="goto")
        robot_calls = [(url.split("/api/v1")[1], body) for url, body in posted if "/robot/" in url]
        self.assertEqual(len(robot_calls), len(jobs))
        expected_by_arm = {arm: [job for job in jobs if job["body"]["arm"] == arm]
                           for arm in ("left", "right")}
        dispatched = []
        for endpoint, body in robot_calls:
            # Both arms can overlap; each arm must preserve every exact payload
            # in its own order, including repeated home/gripper commands.
            expected = expected_by_arm[body["arm"]].pop(0)
            self.assertEqual((endpoint, body), (expected["endpoint"], expected["body"]))
            dispatched.append(expected["name"])
        self.assertTrue(all(not remaining for remaining in expected_by_arm.values()))
        positions = {name: index for index, name in enumerate(dispatched)}
        for job in jobs:
            for predecessor in job.get("after", []):
                self.assertLess(positions[predecessor], positions[job["name"]])
        homes = 3 if home_after_cup else 0
        self.assertEqual(sum(url.endswith("/home") for url, _ in posted), homes)
        self.assertEqual(read.call_count, 2 + homes)
        self.assertEqual(sum(call.args[0].endswith("/joints") for call in read.call_args_list), homes)
        self.assertEqual(sum(call.args[0].endswith("/gripper") for call in read.call_args_list), 2)
        self.assertFalse(any(url.endswith("/openapi.json") for url in requests_in_order))
        self.assertTrue(requests_in_order[0].endswith("/control/acquire"))
        self.assertTrue(posted[0][0].endswith("/control/acquire"))
        self.assertTrue(posted[-1][0].endswith("/control/release"))
        self.assertEqual(sum(url.endswith("/control/acquire") for url, _ in posted), 1)
        self.assertEqual(sum(url.endswith("/control/release") for url, _ in posted), 1)
        self.assertEqual(session.inventory["cups"][0]["status"], "placed")
        self.assertEqual(session.inventory["cups"][0]["grip_left_mm"], 8.5)
        self.assertEqual(session.inventory["cups"][0]["grip_right_mm"], 8.5)


if __name__ == "__main__":
    unittest.main()
