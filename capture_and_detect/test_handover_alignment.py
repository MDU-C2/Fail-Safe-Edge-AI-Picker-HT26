"""Offline receiving-pose teaching checks; no camera, model or robot connections."""

from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from cup_commands import CupSession
from cup_inventory import SelectionError
from handover_plan import handover_jobs, handover_settings, normalize_handover_offset
from test_web_controller import WebFixture
from web_controller import WebController, WebError


NEAR_RECEIVER = {"right_beside", "right_below", "right_take", "right_lower"}


class AlignmentPlanTests(unittest.TestCase):
    def setUp(self):
        self.cup = {"x": 500, "y": 100, "z": 50, "selectable": True, "blocked_reason": None}
        self.place = {"id": 1, "x": 420, "y": -200, "z": 230, "occupied": False}

    def plan(self, **settings):
        return handover_jobs(self.cup, place=self.place, **settings)

    def test_zero_offset_preserves_complete_existing_plan(self):
        for speed in (1, 25, 100, 1200):
            with self.subTest(speed=speed):
                self.assertEqual(self.plan(speed=speed), self.plan(speed=speed, handover_offset=[0, -0.0, 0]))
                take = next(job["body"] for job in self.plan(speed=speed) if job["name"] == "right_take")
                self.assertEqual(tuple(take[axis] for axis in "xyz"), (330, -115, 316))
                self.assertEqual((take["speed"], take["acceleration"], take["ramp"]), (speed, 100, 100))

    def test_offset_contract_rejects_non_numeric_or_outside_teaching_envelope(self):
        for value in (None, {}, "1,2,3", (), (1, 2), (1, 2, 3, 4),
                      (True, 0, 0), (0, False, 0), (0, 0, "1"),
                      (float("nan"), 0, 0), (0, float("inf"), 0),
                      (0, 0, -float("inf")), (20.001, 0, 0),
                      (0, -20.001, 0), (0, 0, 10 ** 1000)):
            with self.subTest(value=value), self.assertRaises(SelectionError):
                normalize_handover_offset(value)
        value = [-20, 0, 20]
        normalized = normalize_handover_offset(value)
        self.assertEqual(normalized, (-20.0, 0.0, 20.0))
        self.assertIsInstance(normalized, tuple)
        value[0] = 0
        self.assertEqual(normalized[0], -20)

    def test_offsets_translate_only_receiving_waypoints_and_derived_turn_midpoint(self):
        baseline = {job["name"]: job for job in self.plan()}
        for offset in ((2, 0, 0), (0, -3, 0), (0, 0, 4), (-20, 20, -20)):
            with self.subTest(offset=offset):
                jobs = self.plan(handover_offset=offset)
                self.assertEqual([job["name"] for job in jobs], list(baseline))
                for job in jobs:
                    original = baseline[job["name"]]
                    if job["name"] in NEAR_RECEIVER | {"right_turn_half"}:
                        factor = .5 if job["name"] == "right_turn_half" else 1
                        for axis, delta in zip("xyz", offset):
                            self.assertEqual(job["body"][axis], original["body"][axis] + factor * delta)
                        for field in ("arm", "wait", "rx", "ry", "rz"):
                            self.assertEqual(job["body"][field], original["body"][field])
                    else:
                        # Includes left-arm poses, via, placing, home and all
                        # gripper jobs: no early release or finger setting.
                        self.assertEqual(job, original)

    def test_adjusted_near_moves_use_teaching_caps_and_respect_lower_selected_speed(self):
        for speed in (1, 12.5, 25, 1200):
            with self.subTest(speed=speed):
                baseline = {job["name"]: job for job in self.plan(speed=speed)}
                for job in self.plan(speed=speed, handover_offset=(1, 0, 0)):
                    body = job["body"]
                    if job["name"] in NEAR_RECEIVER:
                        self.assertEqual((body["speed"], body["acceleration"], body["ramp"]),
                                         (min(speed, 25), 20, 10))
                    elif "speed" in body:
                        self.assertEqual(tuple(body[field] for field in ("speed", "acceleration", "ramp")),
                                         tuple(baseline[job["name"]]["body"][field]
                                               for field in ("speed", "acceleration", "ramp")))

    def test_receiving_offset_is_independent_of_camera_calibration_offset(self):
        original = {job["name"]: job for job in self.plan(handover_offset=(2, -3, 4))}
        adjusted = self.plan(handover_offset=(2, -3, 4), tag_offset=(-115, 25, 40))
        for job in adjusted:
            if job["name"] in ("approach_cup", "descend_to_cup", "lift_cup"):
                self.assertEqual((job["body"]["x"], job["body"]["y"]), (385, 125))
            else:
                self.assertEqual(job, original[job["name"]])

    def test_preview_exposes_flange_target_and_effective_approach_speed(self):
        self.assertEqual(handover_settings((2, -3, 4), speed=100), {
            "offset_mm": {"x": 2, "y": -3, "z": 4},
            "receive_mm": {"x": 332, "y": -118, "z": 320},
            "approach_speed": 25, "adjusted": True,
        })
        self.assertFalse(handover_settings()["adjusted"])
        self.assertEqual(handover_settings((1, 0, 0), speed=7)["approach_speed"], 7)


class AlignmentFixture(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        http = patch("requests.sessions.Session.request", side_effect=AssertionError("HTTP forbidden in alignment tests"))
        http.start()
        self.addCleanup(http.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.fixture = WebFixture(directory.name)
        self.session = self.fixture.session
        self.language = self.fixture.language
        self.robot = self.fixture.robot
        self.session.scan()

    def propose(self, batch=False):
        capture = self.session.inventory["capture"]
        if batch:
            return self.language.propose_batch(capture_id=capture)
        return self.language.propose_selection(action="goto", capture_id=capture, cup_id=1)


class AlignmentSessionTests(AlignmentFixture):
    def test_setting_is_local_invalidates_scan_and_batch_and_never_commands_robot(self):
        original = copy.deepcopy(self.session.inventory)
        self.session.batch_progress = {"state": "previewed"}
        self.session.set_handover_offset([2, -3, 0])
        self.assertFalse(self.session.scan_usable)
        self.assertIsNone(self.session.batch_progress)
        self.assertEqual(self.session.inventory, original)
        self.assertEqual(self.session.motion_settings()["handover"]["receive_mm"], {"x": 332, "y": -118, "z": 316})
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assertEqual(self.robot.jobs, [])

    def test_invalid_setting_preserves_current_settings_and_scan(self):
        original = self.session.motion_settings()
        with self.assertRaises(SelectionError):
            self.session.set_handover_offset((21, 0, 0))
        self.assertEqual(self.session.motion_settings(), original)
        self.assertTrue(self.session.scan_usable)
        self.assertEqual(self.robot.jobs, [])

    def test_busy_operation_refuses_setting_change(self):
        self.session._activity_lock.acquire()
        try:
            with self.assertRaises(SelectionError):
                self.session.set_handover_offset((1, 0, 0))
        finally:
            self.session._activity_lock.release()
        self.assertFalse(self.session.motion_settings()["handover"]["adjusted"])

    def test_recovery_or_stop_refuses_setting_change(self):
        self.session.motion_uncertain = True
        with self.assertRaises(SelectionError):
            self.session.set_handover_offset((1, 0, 0))
        self.session.motion_uncertain = False
        self.session.latch_stop()
        with self.assertRaises(SelectionError):
            self.session.set_handover_offset((1, 0, 0))
        self.assertFalse(self.session.motion_settings()["handover"]["adjusted"])
        self.assertEqual(self.robot.jobs, [])

    def test_constructor_validates_offset_and_settings_snapshot_has_no_mutable_alias(self):
        session = CupSession(Mock(), Mock(), self.robot, "cal-1", handover_offset=[2, -3, 4])
        snapshot = session.motion_settings()
        self.assertEqual(snapshot["handover"]["receive_mm"], {"x": 332, "y": -118, "z": 320})
        snapshot["handover"]["offset_mm"]["x"] = 19
        self.assertEqual(session.motion_settings()["handover"]["offset_mm"]["x"], 2)
        with self.assertRaises(SelectionError):
            CupSession(Mock(), Mock(), self.robot, "cal-1", handover_offset=[True, 0, 0])

    def test_proposal_audit_binds_settings_and_confirmation_executes_adjusted_plan(self):
        self.session.set_handover_offset((2, -3, 4))
        self.session.scan()
        proposal = self.propose()
        expected = self.session.motion_settings()
        record = self.language.pending["record"]
        self.assertEqual(record["motion_settings"], expected)
        self.assertEqual(record["handover"], expected["handover"])
        path, = self.directory.glob(f"language_{proposal['token']}*.json")
        self.assertEqual(json.loads(path.read_text())["motion_settings"], expected)
        result = self.language.confirm(proposal["token"])
        take = next(job["body"] for job in result["jobs"] if job["name"] == "right_take")
        self.assertEqual(tuple(take[axis] for axis in "xyz"), (332, -118, 320))
        self.assertEqual(take["speed"], 25)

    def test_changed_settings_consume_existing_proposal_without_camera_or_motion(self):
        proposal = self.propose()
        self.session.set_handover_offset((2, 0, 0))
        with self.assertRaises(SelectionError):
            self.language.confirm(proposal["token"])
        self.assertIsNone(self.language.pending)
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assertEqual(self.robot.jobs, [])

    def test_lower_speed_caps_home_and_invalidates_existing_proposal(self):
        self.assertEqual(self.session.speed, 250)
        self.assertEqual(self.session.motion_settings()["home_speed"], 200)
        proposal = self.propose()
        self.assertEqual(self.language.pending["record"]["home_speed"], 200)
        self.assertEqual(self.language.pending["record"]["motion_settings"]["home_speed"], 200)
        # A lower selected speed caps home and makes the prior preview stale.
        self.session.set_speed(75)
        self.assertEqual(self.session.speed, 75)
        self.assertEqual(self.session.motion_settings()["home_speed"], 75)
        with self.assertRaises(SelectionError):
            self.language.confirm(proposal["token"])
        self.assertIsNone(self.language.pending)
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assertEqual(self.robot.jobs, [])

    def test_settings_changed_during_model_interpretation_cannot_create_proposal(self):
        def interpret(_text, _scene):
            self.session.set_handover_offset((2, 0, 0))
            return {"action": "goto", "selector": "id", "cup_id": 1, "message": "Pick cup one."}

        self.fixture.interpreter.interpret.side_effect = interpret
        with self.assertRaises(SelectionError):
            self.language.propose("Pick cup one")
        self.assertIsNone(self.language.pending)
        self.assertEqual(self.robot.jobs, [])

    def test_settings_rechecked_inside_execution_lock_after_confirmation_race(self):
        for batch in (False, True):
            with self.subTest(batch=batch):
                proposal = self.propose(batch=batch)
                name = "execute_batch" if batch else "execute_selection"
                execute = getattr(self.session, name)

                def change_before_lock(**kwargs):
                    self.session.set_speed(self.session.speed - 1)
                    return execute(**kwargs)

                with patch.object(self.session, name, side_effect=change_before_lock):
                    with self.assertRaises(SelectionError):
                        self.language.confirm(proposal["token"])
                self.assertEqual(self.fixture.captures, ["scan"])
                self.assertEqual(self.robot.jobs, [])


class AlignmentWebTests(AlignmentFixture):
    def setUp(self):
        super().setUp()
        self.controller = WebController(self.fixture.factory)
        self.addCleanup(self.controller.close)
        self.controller.start()
        self.wait()

    def wait(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = self.controller.state()
            if not state["busy"]:
                return state
            time.sleep(.003)
        self.fail("Alignment worker did not finish")

    def submit(self, operation, **payload):
        response = self.controller.submit(operation, dict(payload, request_id=uuid.uuid4().hex))
        self.assertTrue(response["accepted"])
        return self.wait()

    def test_web_setting_cancels_proposal_and_publishes_effective_pose_without_motion(self):
        state = self.submit("select", action="goto", capture_id=self.session.inventory["capture"], cup_id=1)
        self.assertIsNotNone(state["proposal"])
        state = self.submit("handover_offset", x=2, y=-3, z=4)
        self.assertIsNone(state["proposal"])
        self.assertFalse(state["scan_usable"])
        self.assertEqual(state["motion"]["handover"]["receive_mm"], {"x": 332, "y": -118, "z": 320})
        self.assertEqual(self.robot.jobs, [])
        self.assertEqual(self.fixture.captures, ["scan"])

    def test_web_rejects_extra_fields_non_numeric_values_and_outside_offsets(self):
        for payload in ({"x": 21, "y": 0, "z": 0}, {"x": True, "y": 0, "z": 0},
                        {"x": 0, "y": "2", "z": 0}, {"x": 0, "y": 0},
                        {"x": 0, "y": 0, "z": 0, "endpoint": "/robot/rws/move"}):
            with self.subTest(payload=payload), self.assertRaises(WebError):
                self.controller.submit("handover_offset", dict(payload, request_id=uuid.uuid4().hex))
        self.assertEqual(self.robot.jobs, [])
        self.assertFalse(self.session.motion_settings()["handover"]["adjusted"])


if __name__ == "__main__":
    unittest.main()
