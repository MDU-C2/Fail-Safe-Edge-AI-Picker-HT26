"""Concurrent progress/recovery checks with a simulated robot and saved test grid."""

from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from cup_commands import CupSession
from cup_inventory import SelectionError
from recovery_journal import RecoveryJournal
from robot_client import RobotCommandError
from test_cup_selection import inventory
from test_support_grid import grid_fixture
from web_controller import WebController


class ConcurrentRobotDouble:
    dry_run = False

    def __init__(self, *, fail=False, return_early=False):
        self.fail = fail
        self.return_early = return_early
        self.jobs = []
        self.observe = lambda _stage: None

    def execute_jobs(self, jobs, before_send=None, after_done=None):
        def dispatch(index):
            job = copy.deepcopy(jobs[index])
            replacement = before_send(index, job)
            job = replacement if replacement is not None else job
            self.jobs.append(copy.deepcopy(job))
            return job

        parallel_names = ("left_handover", "right_via")
        for index, job in enumerate(jobs):
            if job["name"] == parallel_names[1]:
                continue
            if job["name"] != parallel_names[0]:
                dispatched = dispatch(index)
                after_done(index, dispatched)
                continue
            right_index = next(i for i, candidate in enumerate(jobs)
                               if candidate["name"] == parallel_names[1])
            left, right = dispatch(index), dispatch(right_index)
            self.observe("both_pending")
            after_done(right_index, right)
            self.observe("right_done")
            if self.fail:
                raise RobotCommandError("Left handover outcome was not confirmed")
            if self.return_early:
                return
            after_done(index, left)
            self.observe("both_done")
        self.observe("before_return")


class ConcurrentCupProgressTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.journal = RecoveryJournal(self.root / "recovery.json")
        self.robot = ConcurrentRobotDouble()
        self.saved = []
        self.observed = {}
        self.controller = WebController(lambda _: None, mode="live")
        self.addCleanup(self.controller.close)

        def save(value):
            self.saved.append(copy.deepcopy(value))
            self.controller.publish_session(self.session)

        self.session = CupSession(
            Mock(side_effect=[(inventory(), 99),
                              (inventory(capture="verification", sequence=11), 99)]),
            save, self.robot, "cal-1", clock=lambda: 100,
            recovery=self.journal, placing_grid=grid_fixture(self.root))
        self.controller.session = self.session
        self.robot.observe = self.observe
        self.session.scan()

    def observe(self, stage):
        self.observed[stage] = {
            "cup": copy.deepcopy(self.session.inventory["cups"][0]),
            "progress": self.controller.state()["progress"],
            "grid": self.session.placing_grid.snapshot(),
            "journal": json.loads(self.journal.path.read_text()),
        }

    def execute(self):
        return self.session.execute_selection(action="goto", capture_id="scan-1", cup_id=1)

    def test_reverse_completion_keeps_other_arm_pending_and_actual_confirmation_order(self):
        jobs = self.execute()
        both = self.observed["both_pending"]
        self.assertEqual(both["cup"]["execution"]["pending_steps"], ["left_handover", "right_via"])
        self.assertEqual(set(both["cup"]["execution"]["pending_jobs"]), {"left_handover", "right_via"})
        self.assertEqual(both["progress"]["step"], "left_handover + right_via")
        self.assertEqual(both["progress"]["pending_steps"], ["left_handover", "right_via"])
        right = self.observed["right_done"]
        execution = right["cup"]["execution"]
        self.assertEqual(execution["pending_steps"], ["left_handover"])
        self.assertEqual(execution["pending_step"], "left_handover")
        self.assertEqual(execution["pending_job"]["name"], "left_handover")
        self.assertEqual(right["progress"]["completed"], both["progress"]["completed"] + 1)
        self.assertEqual(right["progress"]["step"], "left_handover")
        self.assertEqual(right["journal"]["context"]["execution"], execution)
        names = [job["name"] for job in self.session.inventory["cups"][0]["execution"]["completed_steps"]]
        self.assertLess(names.index("right_via"), names.index("left_handover"))
        self.assertEqual(set(names), {job["name"] for job in jobs})

    def test_placement_and_recovery_remain_reserved_until_executor_returns(self):
        self.execute()
        pending = self.observed["before_return"]
        self.assertEqual(pending["cup"]["status"], "command_pending")
        self.assertEqual(pending["cup"]["execution"]["pending_steps"], [])
        self.assertTrue(pending["grid"]["locations"][0]["reserved"])
        self.assertTrue(pending["journal"]["recovery_required"])
        self.assertEqual(self.session.inventory["cups"][0]["status"], "placed")
        self.assertFalse(self.session.placing_grid.snapshot()["locations"][0].get("reserved", False))
        self.assertFalse(self.journal.required())

    def test_one_failed_arm_preserves_completed_sibling_and_pending_reservation(self):
        self.robot.fail = True
        with self.assertRaisesRegex(RobotCommandError, "Left handover"):
            self.execute()
        cup = self.session.inventory["cups"][0]
        self.assertEqual(cup["status"], "movement_unconfirmed")
        execution = cup["execution"]
        self.assertEqual(execution["state"], "interrupted")
        self.assertEqual(execution["pending_steps"], ["left_handover"])
        self.assertEqual(execution["completed_steps"][-1]["name"], "right_via")
        self.assertEqual(json.loads(self.journal.path.read_text())["context"]["execution"], execution)
        self.assertTrue(self.session.placing_grid.snapshot()["locations"][0]["reserved"])
        self.assertTrue(self.session.motion_uncertain)
        self.assertFalse(self.session.scan_usable)
        self.assertTrue(self.journal.required())
        self.assertEqual(self.controller.state()["progress"]["pending_steps"], ["left_handover"])
        self.assertNotIn("right_beside", [job["name"] for job in self.robot.jobs])

    def test_premature_executor_return_cannot_complete_placement(self):
        self.robot.return_early = True
        with self.assertRaisesRegex(SelectionError, "Not every robot step"):
            self.execute()
        self.assertEqual(self.session.inventory["cups"][0]["status"], "movement_unconfirmed")
        self.assertTrue(self.session.placing_grid.snapshot()["locations"][0]["reserved"])
        self.assertTrue(self.journal.required())

    def test_later_stop_journal_keeps_pending_and_completed_arm_records(self):
        self.robot.fail = True
        with self.assertRaises(RobotCommandError):
            self.execute()
        execution = copy.deepcopy(self.session.inventory["cups"][0]["execution"])
        self.robot.request_stop = Mock(return_value={"stopped": True})
        result = self.session.request_stop()
        self.assertTrue(result["stopped"])
        context = json.loads(self.journal.path.read_text())["context"]
        self.assertEqual(context["action"], "software_stop")
        self.assertEqual(context["executions"]["1"], execution)
        self.assertTrue(self.journal.required())
        self.assertTrue(self.session.placing_grid.snapshot()["locations"][0]["reserved"])

    def test_stop_keeps_both_unconfirmed_steps_and_blocks_completion(self):
        def observe(stage):
            self.observe(stage)
            if stage == "both_pending":
                self.session.latch_stop()
        self.robot.observe = observe
        with self.assertRaisesRegex(SelectionError, "Software stop requested"):
            self.execute()
        execution = self.session.inventory["cups"][0]["execution"]
        self.assertEqual(execution["pending_steps"], ["left_handover", "right_via"])
        self.assertTrue(self.session.placing_grid.snapshot()["locations"][0]["reserved"])
        self.assertTrue(self.session.motion_uncertain)
        self.assertTrue(self.journal.required())

    def test_batch_progress_counts_confirmed_steps_and_displays_both_current_arms(self):
        first = self.session.inventory["cups"][0]
        first["execution"] = {"state": "completed", "total_steps": 3, "completed_steps": [{}, {}, {}]}
        second = copy.deepcopy(first)
        second["id"] = 2
        second["execution"] = {"state": "running", "total_steps": 4, "completed_steps": [{}, {}],
                               "pending_steps": ["left_home", "right_place"]}
        self.session.inventory["cups"].append(second)
        self.session.batch_progress = {"cup_ids": [1, 2], "current_cup_id": 2,
                                       "completed_cups": [1], "state": "running"}
        self.controller.publish_session()
        progress = self.controller.state()["progress"]
        self.assertEqual((progress["completed"], progress["total"]), (5, 7))
        self.assertEqual(progress["step"], "left_home + right_place")
        self.assertEqual(progress["pending_steps"], ["left_home", "right_place"])

    def test_saved_legacy_single_pending_step_still_displays(self):
        self.session.inventory["cups"][0]["execution"] = {
            "state": "interrupted", "total_steps": 3, "completed_steps": [{}],
            "pending_step": "right_home"}
        self.controller.publish_session()
        progress = self.controller.state()["progress"]
        self.assertEqual(progress["step"], "right_home")
        self.assertEqual(progress["pending_steps"], ["right_home"])
        self.assertEqual(progress["completed"], 1)


if __name__ == "__main__":
    unittest.main()
