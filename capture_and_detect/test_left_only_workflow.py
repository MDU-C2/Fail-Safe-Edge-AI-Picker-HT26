"""Left-arm replacement uses real session checks and synthetic camera/robot data."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from cup_commands import CupSession
from cup_inventory import SelectionError
from language_commands import LanguageController
from local_llm import DisabledIntentClient
from robot_client import RobotClient
from test_cup_selection import RobotDouble, detection, inventory


class LeftOnlyWorkflowTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def session(self, *, dry_run=False, robot=None, grid=None, left_only=True, cups=1):
        objects = []
        for index in range(cups):
            objects.extend([detection("CUP", 100 + index * 100, x=500 + index * 50),
                            detection("CUP BASE", 100 + index * 100, x=500 + index * 50)])
        sequence = 10

        def capture(purpose):
            nonlocal sequence
            sequence += 1
            return inventory(capture=f"{purpose}-{sequence}", sequence=sequence, objects=objects), 99

        camera = Mock(side_effect=capture)
        robot = robot if robot is not None else RobotDouble()
        robot.dry_run = dry_run
        session = CupSession(camera, Mock(), robot, "cal-1", clock=lambda: 100,
                             placing_grid=grid, left_only=left_only)
        session.scan()
        return session, camera, robot

    def assert_left_replacement(self, jobs):
        self.assertTrue(jobs)
        self.assertEqual({job["body"]["arm"] for job in jobs}, {"left"})
        names = [job["name"] for job in jobs]
        self.assertLess(names.index("grip_cup"), names.index("replace_cup"))
        self.assertLess(names.index("replace_cup"), names.index("left_release"))
        self.assertEqual(names[-1], "left_home")
        descend = next(job["body"] for job in jobs if job["name"] == "descend_to_cup")
        replace = next(job["body"] for job in jobs if job["name"] == "replace_cup")
        self.assertEqual([descend[axis] for axis in "xyz"], [replace[axis] for axis in "xyz"])

    def test_fake_live_replaces_and_homes_without_grid_or_right_arm(self):
        grid = Mock()
        session, camera, robot = self.session(grid=grid)
        capture_id = session.inventory["capture"]
        self.assertEqual(session.preview_selection(action="goto", capture_id=capture_id, cup_id=1),
                         {"cup_ids": [1], "placement": None})
        jobs = session.execute_selection(action="goto", capture_id=capture_id, cup_id=1)
        self.assert_left_replacement(jobs)
        self.assertEqual(robot.jobs, jobs)
        self.assertEqual(grid.mock_calls, [])
        self.assertEqual([call.args[0] for call in camera.call_args_list], ["scan", "verification"])
        cup = session.inventory["cups"][0]
        self.assertEqual(cup["status"], "replaced")
        self.assertEqual(cup["execution"]["state"], "completed")
        self.assertIsNone(cup["execution"]["placement"])
        self.assertFalse(session.motion_uncertain)
        self.assertFalse(session.scan_usable)

    def test_dry_run_previews_replacement_without_http_or_completion_status(self):
        robot = RobotClient("http://offline.invalid/api/v1", {}, dry_run=True)
        session, _, _ = self.session(dry_run=True, robot=robot)
        with patch("requests.sessions.Session.request", side_effect=AssertionError("unexpected HTTP")) as request:
            jobs = session.execute_selection(action="goto", capture_id=session.inventory["capture"], cup_id=1)
        request.assert_not_called()
        self.assert_left_replacement(jobs)
        cup = session.inventory["cups"][0]
        self.assertNotIn(cup["status"], {"placed", "replaced"})
        self.assertNotIn("execution", cup)
        self.assertIn("dry-run", cup["last_action"])
        self.assertTrue(session.scan_usable)

    def test_batch_has_no_destinations_and_checks_each_cup_again(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                session, camera, robot = self.session(dry_run=dry_run, cups=2)
                capture_id = session.inventory["capture"]
                preview = session.preview_batch(capture_id=capture_id)
                self.assertEqual(preview, {"cup_ids": [1, 2], "placement": None, "excluded_cup_ids": []})
                jobs = session.execute_batch(capture_id=capture_id, cup_ids=preview["cup_ids"], placement=None)
                self.assertEqual(sum(job["name"] == "replace_cup" for job in jobs), 2)
                self.assertEqual({job["body"]["arm"] for job in jobs}, {"left"})
                self.assertEqual([call.args[0] for call in camera.call_args_list],
                                 ["scan", "verification", "verification"])
                self.assertEqual(session.batch_progress["completed_cups"], [1, 2])
                self.assertEqual(session.batch_progress["state"], "previewed" if dry_run else "completed")
                if not dry_run:
                    self.assertEqual([cup["status"] for cup in session.inventory["cups"]], ["replaced", "replaced"])
                self.assertEqual(robot.jobs, jobs)

    def test_injected_destinations_never_reserve_or_execute(self):
        grid = Mock()
        session, camera, robot = self.session(grid=grid)
        for method, options in ((session.execute_selection, {"action": "goto", "cup_id": 1}),
                                (session.execute_batch, {"cup_ids": [1]})):
            with self.subTest(method=method.__name__), self.assertRaisesRegex(SelectionError, "do not use placement"):
                method(capture_id=session.inventory["capture"], placement={"locations": []}, **options)
        self.assertEqual(grid.mock_calls, [])
        self.assertEqual(robot.jobs, [])
        camera.assert_called_once_with("scan")

    def test_default_handover_still_requires_a_placement_grid(self):
        robot = RobotDouble()
        session = CupSession(Mock(return_value=(inventory(), 99)), Mock(), robot, "cal-1", clock=lambda: 100)
        session.scan()
        self.assertFalse(session.left_only)
        for method, options in ((session.preview_selection, {"action": "goto", "cup_id": 1}),
                                (session.preview_batch, {})):
            with self.subTest(method=method.__name__), self.assertRaisesRegex(SelectionError, "No placement grid"):
                method(capture_id=session.inventory["capture"], **options)
        self.assertEqual(robot.jobs, [])

    def test_confirmation_describes_replacement_without_handover_or_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            session, _, robot = self.session(dry_run=True)
            language = LanguageController(session, DisabledIntentClient(), audit_dir=Path(folder), clock=lambda: 100)
            proposal = language.propose_selection(action="goto", capture_id=session.inventory["capture"], cup_id=1)
            record = language.pending["record"]
            self.assertIsNone(record["handover"])
            self.assertIsNone(record["placement"])
            self.assertIn("replace", record["description"])
            self.assertIn("left arm home", proposal["message"])
            self.assertNotIn("hand to the right", proposal["message"])
            self.assertEqual(robot.jobs, [])
            language.confirm(proposal["token"])
            self.assert_left_replacement(robot.jobs)


if __name__ == "__main__":
    unittest.main()
