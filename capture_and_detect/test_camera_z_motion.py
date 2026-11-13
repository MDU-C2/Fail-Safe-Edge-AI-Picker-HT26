"""Offline planner/session checks for the opt-in camera-derived pick height."""

from contextlib import redirect_stdout
import copy
import io
import unittest
from unittest.mock import Mock, patch

from cup_commands import CupSession
from cup_inventory import SelectionError, build_inventory
from handover_plan import handover_jobs, left_only_jobs
from test_support_grid import temporary_grid


def scene(z, *, capture="scan-1", sequence=10, tag_offset=(0, 0, 0), diameter=None):
    objects = []
    for kind, radius in (("CUP", 15), ("CUP BASE", 3)):
        obj = {"class": kind, "detection_id": len(objects), "confidence": 0.9,
               "x_px": 100, "y_px": 100,
               "bbox_xyxy": [100 - radius, 80, 100 + radius, 120],
               "robot_x_mm": 500, "robot_y_mm": 100, "robot_z_mm": z,
               "x_mm": 10, "y_mm": 20, "z_mm": 800}
        if kind == "CUP BASE" and diameter is not None:
            obj["diameter_mm"] = diameter
        objects.append(obj)
    return build_inventory(
        objects, capture_id=capture,
        frame={"sequence": sequence, "timestamp": "2001-01-01T00:00:00Z",
               "observed_at": "2026-11-13T12:00:00Z", "received_monotonic": 99},
        calibration_id="cal-1", gateway="http://offline.invalid/api/v1",
        images={"annotated": "offline.jpg"}, model={"weights": "fixture"},
        tag_offset=tag_offset,
    )


class RobotDouble:
    """Record jobs without importing a robot client or making any requests."""

    dry_run = False

    def __init__(self):
        self.jobs = []

    def execute_jobs(self, jobs, before_send=None, after_done=None):
        for index, job in enumerate(copy.deepcopy(jobs)):
            replacement = before_send(index, job) if before_send else None
            job = replacement if replacement is not None else job
            self.jobs.append(job)
            if after_done:
                after_done(index, job)


def bodies(jobs):
    return {job["name"]: job["body"] for job in jobs}


class CameraZPlanTests(unittest.TestCase):
    def setUp(self):
        self.cup = {"x": 500, "y": 100, "z": 80, "selectable": True,
                    "blocked_reason": None}
        self.place = {"id": 1, "x": 420, "y": -200, "z": 230, "occupied": False}

    def plan(self, *, left_only=False, cup=None, **settings):
        cup = self.cup if cup is None else cup
        if left_only:
            return left_only_jobs(cup, **settings)
        return handover_jobs(cup, place=self.place, **settings)

    def test_default_remains_fixed_70_mm_and_explicit_false_is_identical(self):
        for left_only in (False, True):
            for z in (-30, 20, 80, 150):
                with self.subTest(left_only=left_only, z=z):
                    cup = dict(self.cup, z=z)
                    jobs = self.plan(left_only=left_only, cup=cup)
                    self.assertEqual(jobs, self.plan(left_only=left_only, cup=cup, camera_z=False))
                    self.assertEqual(bodies(jobs)["descend_to_cup"]["z"], 229)
                    if left_only:
                        self.assertEqual(bodies(jobs)["replace_cup"]["z"], 229)

    def test_camera_height_rounding_floor_and_high_cup_apply_to_pick_and_replace(self):
        for left_only in (False, True):
            for detected, tcp in ((-30, 40), (19.9, 40), (20, 40),
                                  (20.06, 40.1), (63.26, 83.3), (150, 170)):
                with self.subTest(left_only=left_only, detected=detected):
                    result = bodies(self.plan(left_only=left_only,
                                              cup=dict(self.cup, z=detected), camera_z=True))
                    self.assertAlmostEqual(result["descend_to_cup"]["z"], tcp + 159)
                    if left_only:
                        self.assertEqual(result["replace_cup"]["z"], result["descend_to_cup"]["z"])

    def test_travel_is_250_tcp_and_409_flange_in_both_modes(self):
        for left_only in (False, True):
            for camera_z in (False, True):
                with self.subTest(left_only=left_only, camera_z=camera_z):
                    result = bodies(self.plan(left_only=left_only, camera_z=camera_z))
                    for name in ("approach_cup", "lift_cup"):
                        self.assertEqual(result[name]["z"], 409)
                    if left_only:
                        self.assertEqual(result["left_clear"]["z"], 409)
                    self.assertEqual(scene(80)["hover_motion"]["travel_tcp_z_mm"], 250)

    def test_camera_mode_changes_only_descent_and_same_height_replacement(self):
        for left_only in (False, True):
            with self.subTest(left_only=left_only):
                original = self.plan(left_only=left_only)
                changed = self.plan(left_only=left_only, camera_z=True)
                self.assertEqual([job["name"] for job in original], [job["name"] for job in changed])
                changed_names = {"descend_to_cup", "replace_cup"}
                for baseline, actual in zip(original, changed):
                    expected = copy.deepcopy(baseline)
                    if expected["name"] in changed_names:
                        expected["body"]["z"] = 259
                    self.assertEqual(actual, expected)
                names = [job["name"] for job in changed]
                self.assertIn("left_home", names)
                if left_only:
                    self.assertEqual(names[-1], "left_home")
                    self.assertTrue(all(job["body"]["arm"] == "left" for job in changed))
                else:
                    self.assertIn("right_home_start", names)
                    self.assertEqual(names[-1], "right_home")

    def test_camera_height_does_not_apply_tag_offset_z_twice_or_mutate_cup(self):
        original = copy.deepcopy(self.cup)
        for left_only in (False, True):
            for offset_z in (-1000, 0, 20, 40, 1000):
                with self.subTest(left_only=left_only, offset_z=offset_z):
                    result = bodies(self.plan(left_only=left_only, camera_z=True,
                                              tag_offset=(-115, 25, offset_z)))
                    for name in ("approach_cup", "descend_to_cup", "lift_cup"):
                        self.assertEqual((result[name]["x"], result[name]["y"]), (385, 125))
                    self.assertEqual(result["descend_to_cup"]["z"], 259)
        self.assertEqual(self.cup, original)

    def test_camera_option_requires_a_real_boolean(self):
        for left_only in (False, True):
            for value in (None, 0, 1, "false", "true", [], {}):
                with self.subTest(left_only=left_only, value=value), self.assertRaises(ValueError):
                    self.plan(left_only=left_only, camera_z=value)

    def test_invalid_or_implausible_height_never_returns_a_camera_plan(self):
        for left_only in (False, True):
            for value in (None, True, "50", float("nan"), float("inf"),
                          -float("inf"), 10 ** 1000, -30.1, 150.1):
                with self.subTest(left_only=left_only, value=value), self.assertRaises(SelectionError):
                    self.plan(left_only=left_only, cup=dict(self.cup, z=value), camera_z=True)

    def test_grip_margin_is_70_mm_without_changing_measured_diameter(self):
        cup = scene(80, diameter=50)["cups"][0]
        self.assertEqual(cup["diameter"], 50)
        self.assertEqual(cup["aperture"], 120)
        result = bodies(self.plan(cup=cup, camera_z=True))
        self.assertEqual(result["left_open"]["position"], 22.5)


class CameraZSessionTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def session(self, *, scanned=70, fresh=70, **settings):
        offset = settings.get("tag_offset", (0, 0, 0))
        capture = Mock(side_effect=[(scene(scanned, tag_offset=offset), 90),
                                   (scene(fresh, capture="verify-1", sequence=11,
                                          tag_offset=offset), 99)])
        robot = RobotDouble()
        save = Mock()
        grid = temporary_grid(self)
        session = CupSession(capture, save, robot, "cal-1", clock=lambda: 100,
                             max_result_age=10, placing_grid=grid, **settings)
        return session, capture, robot, grid

    def execute(self, session, **settings):
        return session.execute_selection(action="goto", capture_id="scan-1", cup_id=1, **settings)

    def test_default_setting_is_false_and_camera_setting_is_read_only(self):
        default, _, _, _ = self.session()
        self.assertIs(default.camera_z, False)
        self.assertIs(default.motion_settings()["camera_z"], False)
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                session, _, _, _ = self.session(camera_z=enabled)
                self.assertIs(session.camera_z, enabled)
                self.assertIs(session.motion_settings()["camera_z"], enabled)
                with self.assertRaises(AttributeError):
                    session.camera_z = not enabled
                self.assertIs(session.camera_z, enabled)
                self.assertIs(session.motion_settings()["camera_z"], enabled)

    def test_session_rejects_non_boolean_camera_setting_before_capture(self):
        for value in (None, 0, 1, "false", "true", [], {}):
            capture, robot = Mock(), RobotDouble()
            with self.subTest(value=value), self.assertRaises(ValueError):
                CupSession(capture, Mock(), robot, "cal-1", camera_z=value)
            capture.assert_not_called()
            self.assertEqual(robot.jobs, [])

    def test_single_and_batch_previews_propagate_camera_mode_without_movement(self):
        for left_only in (False, True):
            with self.subTest(left_only=left_only):
                session, capture, robot, grid = self.session(camera_z=True, left_only=left_only)
                session.scan()
                before = grid.snapshot()
                planner = left_only_jobs if left_only else handover_jobs
                name = "cup_commands.left_only_jobs" if left_only else "cup_commands.handover_jobs"
                with patch(name, wraps=planner) as planned:
                    session.preview_selection(action="goto", capture_id="scan-1", cup_id=1)
                    session.preview_batch(capture_id="scan-1")
                self.assertEqual(planned.call_count, 2)
                self.assertTrue(all(call.kwargs["camera_z"] is True for call in planned.call_args_list))
                self.assertEqual(capture.call_count, 1)
                self.assertEqual(robot.jobs, [])
                self.assertEqual(grid.snapshot(), before)

    def test_fresh_height_changes_over_five_mm_block_before_reservation_or_motion(self):
        for left_only in (False, True):
            for fresh in (64.9, 75.1):
                with self.subTest(left_only=left_only, fresh=fresh):
                    session, capture, robot, grid = self.session(
                        scanned=70, fresh=fresh, camera_z=True, left_only=left_only)
                    session.scan()
                    before = grid.snapshot()
                    with patch.object(grid, "reserve", wraps=grid.reserve) as reserve:
                        with self.assertRaisesRegex(SelectionError, "pick height changed"):
                            self.execute(session)
                        reserve.assert_not_called()
                    self.assertEqual(capture.call_count, 2)
                    self.assertEqual(robot.jobs, [])
                    self.assertEqual(grid.snapshot(), before)
                    self.assertFalse(session.scan_usable)

    def test_fresh_height_changes_up_to_five_mm_reach_jobs_and_audit(self):
        for left_only in (False, True):
            for fresh in (65, 70, 75):
                with self.subTest(left_only=left_only, fresh=fresh):
                    session, capture, robot, _ = self.session(
                        scanned=70, fresh=fresh, camera_z=True, left_only=left_only)
                    session.scan()
                    jobs = self.execute(session)
                    self.assertEqual(robot.jobs, jobs)
                    result = bodies(jobs)
                    self.assertEqual(result["descend_to_cup"]["z"], fresh + 20 + 159)
                    if left_only:
                        self.assertEqual(result["replace_cup"]["z"], result["descend_to_cup"]["z"])
                    audit = session.inventory["cups"][0]["last_verification"]
                    self.assertEqual(audit["grip_geometry"]["pick_tcp_z_mm"], fresh + 20)
                    self.assertIs(audit["motion_settings"]["camera_z"], True)
                    self.assertEqual(capture.call_count, 2)

    def test_height_guard_compares_planned_descent_after_floor(self):
        session, _, robot, _ = self.session(scanned=10, fresh=19, camera_z=True)
        session.scan()
        self.execute(session)
        self.assertEqual(bodies(robot.jobs)["descend_to_cup"]["z"], 199)
        audit = session.inventory["cups"][0]["last_verification"]
        self.assertEqual(audit["grip_geometry"]["pick_tcp_z_mm"], 40)

    def test_fractional_five_mm_boundary_uses_the_planners_tenth_mm_resolution(self):
        for fresh, accepted in ((71.1, True), (81.1, True), (71.0, False), (81.2, False)):
            with self.subTest(fresh=fresh):
                session, _, robot, grid = self.session(scanned=76.1, fresh=fresh, camera_z=True)
                session.scan()
                before = grid.snapshot()
                if accepted:
                    jobs = self.execute(session)
                    self.assertEqual(robot.jobs, jobs)
                    self.assertAlmostEqual(bodies(jobs)["descend_to_cup"]["z"], fresh + 20 + 159)
                else:
                    with self.assertRaisesRegex(SelectionError, "pick height changed"):
                        self.execute(session)
                    self.assertEqual(robot.jobs, [])
                    self.assertEqual(grid.snapshot(), before)

    def test_fixed_mode_keeps_70_mm_descent_when_observed_height_changes(self):
        session, _, robot, _ = self.session(scanned=70, fresh=80)
        session.scan()
        self.execute(session)
        self.assertEqual(bodies(robot.jobs)["descend_to_cup"]["z"], 229)
        audit = session.inventory["cups"][0]["last_verification"]
        self.assertEqual(audit["grip_geometry"]["pick_tcp_z_mm"], 70)
        self.assertIs(audit["motion_settings"]["camera_z"], False)

    def test_confirmation_with_different_camera_setting_blocks_before_fresh_capture(self):
        session, capture, robot, grid = self.session(camera_z=True)
        session.scan()
        settings = session.motion_settings()
        settings["camera_z"] = False
        before = grid.snapshot()
        with self.assertRaisesRegex(SelectionError, "settings changed"):
            self.execute(session, expected_motion=settings)
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(robot.jobs, [])
        self.assertEqual(grid.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
