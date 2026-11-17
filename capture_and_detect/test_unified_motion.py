"""Latest cup geometry and guarded return paths; all controller calls are mocked."""

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import unittest
from unittest.mock import Mock, patch

import requests

from cup_inventory import SelectionError, hover_moves
from handover_plan import handover_jobs, left_only_jobs, pick_position_problem
from robot_client import RobotClient, RobotCommandError


def reply(payload, status=200):
    response = Mock()
    response.status_code = status
    response.headers = {}
    response.json.return_value = payload
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(str(status))
    return response


class UnifiedMotionTests(unittest.TestCase):
    def setUp(self):
        self.cup = {"x": 500, "y": 100, "z": 50, "selectable": True,
                    "rz": 135, "aperture": 95, "has_handle": True}
        self.place = {"id": 1, "x": 420, "y": -200, "z": 230, "occupied": False}

    def transfer(self, **kwargs):
        return handover_jobs(self.cup, place=self.place, **kwargs)

    def test_handle_yaw_is_used_until_lift_then_fixed_handover_orientation(self):
        jobs = {job["name"]: job["body"] for job in self.transfer()}
        cup_points = [*jobs["approach_cup"]["points"], jobs["left_handover"]["points"][0]]
        self.assertEqual(len(cup_points), 3)
        for point in cup_points:
            self.assertEqual(tuple(point[axis] for axis in ("rx", "ry", "rz")), (180, 0, 135))
        self.assertEqual(jobs["left_handover"]["points"][-1]["rz"], 180)
        self.assertEqual(jobs["left_ready"]["points"][0]["rz"], 180)
        self.assertEqual(jobs["left_open"], {"arm": "left", "wait": True, "action": "move", "position": 10})

    def test_unknown_aperture_opens_fully_and_known_apertures_are_clamped(self):
        for aperture, expected in ((None, None), (-1, 0), (0, 0), (40, 0), (75, 0), (95, 10), (125, 25), (145, 25)):
            with self.subTest(aperture=aperture):
                cup = dict(self.cup, aperture=aperture)
                for jobs in (left_only_jobs(cup), handover_jobs(cup, place=self.place)):
                    body = next(job["body"] for job in jobs if job["name"] == "left_open")
                    if expected is None:
                        self.assertEqual(body, {"arm": "left", "wait": True, "action": "open"})
                    else:
                        self.assertEqual(body["action"], "move")
                        self.assertEqual(body["position"], expected)

    def test_invalid_yaw_or_aperture_never_produces_a_motion_plan(self):
        for field, values in (("rz", (None, True, "135", float("nan"), 10 ** 1000)),
                              ("aperture", (True, "95", float("inf"), 10 ** 1000))):
            for value in values:
                with self.subTest(field=field, value=value):
                    cup = dict(self.cup, **{field: value})
                    for planner in (lambda: handover_jobs(cup, place=self.place), lambda: left_only_jobs(cup)):
                        with self.assertRaises(SelectionError):
                            planner()

    def test_old_ambiguity_metadata_does_not_block_v4_plans(self):
        cup = dict(self.cup, grip_geometry_error="Two handles are equally close")
        self.assertIsNone(pick_position_problem(cup))
        self.assertEqual(handover_jobs(cup, place=self.place), self.transfer())
        self.assertEqual(left_only_jobs(cup), left_only_jobs(self.cup))
        self.assertEqual(len(hover_moves(cup)), 2)


    def test_transfer_return_uses_known_clearances_without_lowering_left_arm(self):
        jobs = self.transfer(home_after_cup=True)
        names = [job["name"] for job in jobs]
        by_name = {job["name"]: job for job in jobs}
        self.assertNotIn("left_ready_return", names)
        self.assertNotIn("left_home_raise", names)
        self.assertNotIn("right_home_raise", names)
        self.assertEqual(tuple(by_name["left_home_lateral"]["body"][a] for a in "xyz"), (350, 400, 690))
        self.assertEqual(tuple(by_name["right_clear"]["body"]["points"][0][a] for a in "xyz"),
                         (420, -200, 310))
        for before, after in (("right_retreat", "left_close_return"),
                              ("left_close_return", "left_home_lateral"),
                              ("right_clear", "right_home")):
            self.assertIn(before, by_name[after]["after"])
        high = handover_jobs(self.cup, place=dict(self.place, z=620), home_after_cup=True)
        self.assertNotIn("right_home_raise", [job["name"] for job in high])

    def test_left_only_replaces_the_same_cup_then_returns_with_same_yaw(self):
        jobs = left_only_jobs(self.cup, tag_offset=(-115, 25, 40), speed=50, home_after_cup=True)
        names = [job["name"] for job in jobs]
        self.assertTrue(all(job["body"]["arm"] == "left" for job in jobs))
        self.assertEqual(names[-4:], ["left_close_return", "left_home_raise", "left_home_lateral", "left_home"])
        by_name = {job["name"]: job["body"] for job in jobs}
        self.assertEqual(by_name["replace_cup"], by_name["descend_to_cup"])
        self.assertEqual(tuple(by_name["left_home_raise"][a] for a in "xyz"), (385, 125, 600))
        self.assertEqual(tuple(by_name["left_home_lateral"][a] for a in "xyz"), (385, 400, 600))
        self.assertLess(names.index("replace_cup"), names.index("left_release"))
        self.assertLess(names.index("left_release"), names.index("left_clear"))
        for body in by_name.values():
            if "rz" in body and body is not by_name["left_ready"]:
                self.assertEqual(body["rz"], 135)
            if "speed" in body:
                self.assertLessEqual(body["speed"], 50)
        # A return from y>=400 needs no movement back toward that line.
        self.assertNotIn("left_home_lateral", [job["name"] for job in
                         left_only_jobs(dict(self.cup, y=420), home_after_cup=True)])

    def test_client_accepts_every_planned_job_in_both_modes(self):
        for jobs in (self.transfer(), left_only_jobs(self.cup)):
            for job in jobs:
                self.assertEqual(RobotClient._validate_job(job), job)

    def test_failed_clearance_is_not_retried_or_followed_by_home(self):
        client = RobotClient("http://offline.invalid/api/v1", {})
        sent = []

        def post(url, **kwargs):
            if url.endswith("/control/acquire"):
                return reply({"success": True, "lease": {"active": True, "client_id": "operator",
                    "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()}})
            if url.endswith("/control/release"):
                return reply({"success": True, "lease": {"active": False}})
            sent.append((url, kwargs["json"]))
            if url.endswith("/move") and kwargs["json"].get("z") == 600:
                raise requests.Timeout("clearance outcome is unknown")
            return reply({"accepted": True, "message": "DONE"})

        with patch("robot_client.requests.post", side_effect=post), patch("robot_client.requests.get") as get:
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RobotCommandError, "unknown"):
                client.execute_jobs(left_only_jobs(self.cup, home_after_cup=True))
            get.assert_not_called()
        self.assertEqual(sum(body.get("z") == 600 for _, body in sent), 1)
        self.assertFalse(any(url.endswith("/home") for url, _ in sent))


class GripperTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.client = RobotClient("http://offline.invalid/api/v1", {})

    def test_position_is_read_only_and_response_is_closed(self):
        response = reply({"arm": "left", "position": 12.5})
        with patch("robot_client.requests.get", return_value=response) as get, patch("robot_client.requests.post") as post:
            self.assertEqual(self.client.read_gripper_position("left"), 12.5)
            get.assert_called_once()
            post.assert_not_called()
        response.close.assert_called_once()

    def test_missing_invalid_or_failed_telemetry_is_unknown_and_never_retried(self):
        cases = [requests.Timeout("offline"), reply({}, 404), reply(None),
                 reply({"arm": "right", "position": 12}), reply({"arm": "left", "position": True}),
                 reply({"arm": "left", "position": -1}), reply({"arm": "left", "position": 26}),
                 reply({"arm": "left", "position": float("nan")}),
                 reply({"arm": "left", "position": 10 ** 1000})]
        for response in cases:
            with self.subTest(response=response):
                with patch("robot_client.requests.get", side_effect=[response]) as get:
                    self.assertIsNone(self.client.read_gripper_position("left"))
                    get.assert_called_once()
                if not isinstance(response, Exception):
                    response.close.assert_called_once()

    def test_dry_run_has_no_http_and_stop_during_read_still_blocks(self):
        with patch("robot_client.requests.get") as get:
            self.client.dry_run = True
            self.assertIsNone(self.client.read_gripper_position("left"))
            get.assert_not_called()
        self.client.dry_run = False
        response = reply({"arm": "left", "position": 10})
        def stopped(*args, **kwargs):
            self.client.latch_stop()
            return response
        with patch("robot_client.requests.get", side_effect=stopped):
            with self.assertRaisesRegex(RobotCommandError, "Software stop"):
                self.client.read_gripper_position("left")
        response.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
