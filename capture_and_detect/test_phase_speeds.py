"""Phase settings change future speeds only; all tests use offline robot doubles."""
import copy
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from cup_inventory import SelectionError
from handover_plan import (effective_phase_speeds, handover_jobs, left_only_jobs,
                           normalize_phase_speeds)
from robot_assistant import RobotAssistant
from test_web_controller import WebFixture
from web_controller import WebController, WebError


class PhasePlanTests(unittest.TestCase):
    def setUp(self):
        self.cup = {"x": 500, "y": 100, "z": 50, "selectable": True, "blocked_reason": None}
        self.place = {"id": 1, "x": 420, "y": -200, "z": 230, "occupied": False}
        self.profile = {"pick_lift": 180, "handover": 220, "flip": 200, "placement": 140}

    def plan(self, **values):
        return handover_jobs(self.cup, place=self.place, **values)

    def test_default_profile_keeps_ui_phase_limits_including_mixed_speed_paths(self):
        for speed in (1, 50, 250, 500, 1500):
            jobs = self.plan(speed=speed)
            self.assertEqual(jobs, self.plan(speed=speed, phase_speeds=normalize_phase_speeds()))
            expected = {
                "approach_cup": (speed, [speed, speed, min(speed, 100)]),
                "left_handover": (min(speed, 100), [min(speed, 100)] * 2),
                "right_via": (speed, [speed]),
                "right_beside": (speed, [speed]),
                "right_take": (speed, [min(speed, 150), speed]),
                "left_clear": (min(speed, 100), [min(speed, 100)]),
                "left_park": (speed, [speed]),
                "right_lower": (speed, [speed]),
                "right_retreat": (min(speed, 100), [min(speed, 100)] * 2),
                "right_place": (speed, [speed]),
                "right_clear": (speed, [speed]),
            }
            for job in jobs:
                if not job["endpoint"].endswith("/path"):
                    continue
                with self.subTest(speed=speed, name=job["name"]):
                    body_speed, point_speeds = expected[job["name"]]
                    self.assertEqual(job["body"]["speed"], body_speed)
                    self.assertEqual([point["speed"] for point in job["body"]["points"]], point_speeds)

    def test_selected_phase_speeds_leave_all_geometry_grippers_and_dependencies_unchanged(self):
        original = self.plan()
        changed = self.plan(phase_speeds=self.profile)
        expected = {
            "approach_cup": (250, [250, 250, 180]),
            "left_handover": (180, [180, 180]),
            "left_clear": (180, [180]),
            "right_via": (220, [220]),
            "right_beside": (220, [220]),
            "right_take": (220, [150, 220]),
            "right_lower": (220, [220]),
            "right_retreat": (200, [200, 200]),
            "right_place": (140, [140]),
            "right_clear": (140, [140]),
        }
        self.assertEqual(len(original), len(changed))
        for before, after in zip(original, changed):
            with self.subTest(name=after["name"]):
                if after["name"] in expected:
                    body_speed, point_speeds = expected[after["name"]]
                    self.assertEqual(after["body"]["speed"], body_speed)
                    self.assertEqual([point["speed"] for point in after["body"]["points"]], point_speeds)
                    after["body"]["speed"] = before["body"]["speed"]
                    for initial, point in zip(before["body"]["points"], after["body"]["points"]):
                        point["speed"] = initial["speed"]
                self.assertEqual(after, before)

    def test_overall_home_caps_and_phase_speeds_ignore_legacy_alignment(self):
        fast = dict.fromkeys(self.profile, 1500)
        for cap in (7, 75, 300):
            jobs = {job["name"]: job for job in self.plan(speed=cap, phase_speeds=fast,
                                                        handover_offset=(1, -1, 2),
                                                        home_after_cup=True)}
            for job in jobs.values():
                if "speed" in job["body"]:
                    self.assertLessEqual(job["body"]["speed"], cap)
                for point in job["body"].get("points", []):
                    self.assertLessEqual(point["speed"], cap)
                if job["endpoint"].endswith("/home"):
                    self.assertLessEqual(job["body"]["speed"], 200)
            for name in ("right_via", "right_beside", "right_take", "right_lower"):
                body = jobs[name]["body"]
                self.assertEqual((body["speed"], body["acceleration"], body["ramp"]), (cap, 60, 40))
            self.assertEqual([point["speed"] for point in jobs["right_take"]["body"]["points"]],
                             [min(cap, 150), cap])
            self.assertEqual(jobs["right_retreat"]["body"]["acceleration"], 30)
            self.assertEqual([point["speed"] for point in jobs["right_retreat"]["body"]["points"]], [cap, cap])
        slow_alignment = dict(self.profile, handover=10)
        jobs = self.plan(phase_speeds=slow_alignment, handover_offset=(1, 0, 0))
        take = next(j["body"] for j in jobs if j["name"] == "right_take")
        self.assertEqual(take["speed"], 10)
        self.assertEqual([point["speed"] for point in take["points"]], [10, 10])

    def test_left_only_plan_keeps_original_defaults_and_applies_pick_and_replace_caps(self):
        default = left_only_jobs(self.cup)
        explicit = left_only_jobs(self.cup, phase_speeds=normalize_phase_speeds())
        self.assertEqual(default, explicit)
        jobs = {j["name"]: j for j in left_only_jobs(self.cup, phase_speeds=self.profile)}
        self.assertEqual(jobs["descend_to_cup"]["body"]["speed"], 180)
        self.assertEqual(jobs["lift_cup"]["body"]["speed"], 180)
        self.assertEqual(jobs["replace_cup"]["body"]["speed"], 140)
        self.assertEqual(jobs["left_clear"]["body"]["speed"], 140)

    def test_invalid_profiles_never_produce_a_plan(self):
        invalid = [[], {}, dict(self.profile, other=100)]
        for phase in self.profile:
            for value in (True, False, "100", 0, -1, 1500.1, float("nan"), float("inf"), 10 ** 1000):
                invalid.append(dict(self.profile, **{phase: value}))
        invalid.extend((dict(self.profile, pick_lift=None), dict(self.profile, flip=None)))
        for value in invalid:
            with self.subTest(profile=str(value)[:80]), self.assertRaises(SelectionError):
                self.plan(phase_speeds=value)

    def test_requested_profile_is_copied_and_follow_overall_remains_dynamic(self):
        settings = normalize_phase_speeds()
        before = copy.deepcopy(settings)
        self.assertEqual(effective_phase_speeds(400, settings),
                         {"pick_lift": 100, "handover": 400, "flip": 100, "placement": 400})
        self.assertEqual(effective_phase_speeds(50, settings), dict.fromkeys(settings, 50))
        self.assertEqual(settings, before)
        settings["flip"] = 800
        self.assertEqual(normalize_phase_speeds()["flip"], 100)


class PhaseControllerTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = WebFixture(temporary.name)
        self.controller = WebController(self.fixture.factory, mode="dry_run", llm_enabled=False)
        self.addCleanup(self.controller.close)
        self.controller.start()
        self.wait_idle()
        self.profile = {"pick_lift": 180, "handover": 220, "flip": 200, "placement": 140}
        network = patch("requests.Session.request", side_effect=AssertionError("No network in phase tests"))
        network.start()
        self.addCleanup(network.stop)

    def wait_idle(self):
        deadline = time.monotonic() + 3
        while self.controller.state()["busy"]:
            if time.monotonic() > deadline:
                self.fail("Offline controller did not finish")
            time.sleep(.005)
        return self.controller.state()

    def submit(self, operation, **payload):
        self.controller.submit(operation, dict(payload, request_id=uuid.uuid4().hex))
        return self.wait_idle()

    def preview(self, batch=False):
        capture = self.submit("scan")["inventory"]["capture"]
        if batch:
            return self.submit("batch", capture_id=capture)["proposal"]
        return self.submit("select", capture_id=capture, cup_id=1, action="goto")["proposal"]

    def test_settings_update_never_moves_and_next_batch_snapshot_matches_dispatched_jobs(self):
        state = self.submit("phase_speeds", **self.profile)
        self.assertIsNone(state["error"])
        self.assertTrue(state["motion"]["phase_speeds_supported"])
        self.assertEqual(state["motion"]["phase_speed_settings"], self.profile)
        self.assertEqual(state["motion"]["phase_speeds"], self.profile)
        self.assertEqual(self.fixture.robot.jobs, [])
        self.assertEqual(self.fixture.captures, [])
        proposal = self.preview(batch=True)
        self.assertEqual(proposal["phase_speeds"], self.profile)
        self.assertEqual(proposal["flip_speed"], 200)
        payload = {key: proposal[key] for key in ("token", "capture_id", "cup_id", "action")}
        state = self.submit("confirm", **payload)
        self.assertIsNone(state["error"])
        turns = [job for job in self.fixture.robot.jobs if job["name"] == "right_retreat"]
        self.assertEqual(len(turns), 2)
        for job in turns:
            self.assertEqual(job["endpoint"], "/robot/rws/path")
            self.assertEqual(job["body"]["speed"], 200)
            self.assertEqual([point["speed"] for point in job["body"]["points"]], [200, 200])
        for cup in self.fixture.session.inventory["cups"][:2]:
            self.assertEqual(cup["last_verification"]["motion_settings"]["phase_speeds"], self.profile)

    def test_change_cancels_web_preview_and_direct_stale_motion_binding_blocks_without_capture(self):
        proposal = self.preview()
        self.submit("phase_speeds", **self.profile)
        self.assertIsNone(self.controller.state()["proposal"])
        self.assertIsNone(self.fixture.language.pending)
        self.assertEqual(self.fixture.robot.jobs, [])
        self.assertEqual(self.fixture.captures, ["scan"])
        proposal = self.fixture.language.propose_selection(action="goto", capture_id=proposal["capture_id"], cup_id=1)
        self.fixture.session.set_phase_speeds(dict(self.profile, flip=190))
        with self.assertRaisesRegex(SelectionError, "settings changed"):
            self.fixture.language.confirm(proposal["token"])
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_invalid_http_payload_preserves_pending_preview_and_profile(self):
        proposal = self.preview()
        before = copy.deepcopy(self.fixture.session.phase_speeds)
        for change in ({"flip": True}, {"placement": 0}, {"pick_lift": None}, {"extra": 1}):
            with self.subTest(change=change), self.assertRaises(WebError) as error:
                self.controller.submit("phase_speeds", dict(self.profile, request_id=uuid.uuid4().hex, **change))
            self.assertEqual(error.exception.status, 422)
        self.assertEqual(self.controller.state()["proposal"]["token"], proposal["token"])
        self.assertEqual(self.fixture.session.phase_speeds, before)

    def test_execution_lock_recovery_and_stop_reject_profile_changes(self):
        session = self.fixture.session
        before = copy.deepcopy(session.phase_speeds)
        session._activity_lock.acquire()
        try:
            with self.assertRaisesRegex(SelectionError, "active operation"):
                session.set_phase_speeds(self.profile)
        finally:
            session._activity_lock.release()
        session.motion_uncertain = True
        with self.assertRaisesRegex(SelectionError, "recovery"):
            session.set_phase_speeds(self.profile)
        session.motion_uncertain = False
        session.latch_stop()
        with self.assertRaises(SelectionError):
            session.set_phase_speeds(self.profile)
        self.assertEqual(session.phase_speeds, before)
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_lower_overall_cap_updates_effective_values_without_erasing_selected_profile(self):
        self.submit("phase_speeds", **self.profile)
        state = self.submit("speed", speed=75)
        self.assertEqual(state["motion"]["phase_speeds"], dict.fromkeys(self.profile, 75))
        self.assertEqual(state["motion"]["phase_speed_settings"], self.profile)
        self.assertEqual(state["motion"]["home_speed"], 75)
        self.assertEqual(state["motion"]["flip_speed"], 75)
        state = self.submit("speed", speed=300)
        self.assertEqual(state["motion"]["phase_speeds"], self.profile)
        self.assertEqual(state["motion"]["home_speed"], 200)
        scene = RobotAssistant._scene(state)
        self.assertEqual(scene["motion_settings"]["phase_speeds"], self.profile)
        self.assertIn("read-only", scene["capabilities"])


if __name__ == "__main__":
    unittest.main()
