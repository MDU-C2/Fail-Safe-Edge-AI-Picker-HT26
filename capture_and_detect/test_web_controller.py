"""Offline tests for web proposals and serialized robot access; no hardware is used."""

from contextlib import redirect_stdout
import copy
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock

from cup_commands import CupSession
from cup_inventory import SelectionError
from language_commands import LanguageController
from robot_client import RobotCommandError
from robot_controls import RobotControls
from test_cup_selection import RobotDouble, detection, inventory
from test_support_grid import grid_fixture
from web_controller import WebController, WebError


class WebFixture:
    """Use the real selection pipeline with an entirely in-memory camera and robot."""

    def __init__(self, directory, *, dry_run=True, camera_z=False, home_after_cup=False, left_only=False):
        self.now = 100.0
        self.sequence = 10
        self.captures = []
        self.capture_gate = None
        self.capture_entered = threading.Event()
        self.capture_error = None
        self.robot = RobotDouble()
        self.robot.dry_run = dry_run
        self.robot.operator_capabilities = {
            action: {"supported": True, "reason": None, "protocol": "offline_fixture"}
            for action in ("home", "open", "grip", "robot_status", "stop", "rapid_reset",
                           "rapid_start", "collision_ack", "collision_status")}
        self.interpreter = Mock()
        self.interpreter.interpret.return_value = {
            "action": "hover", "selector": "id", "cup_id": 1, "message": "Hover above cup 1."}
        self.session = CupSession(self.capture, Mock(), self.robot, "cal-1", clock=lambda: self.now,
                                  placing_grid=grid_fixture(directory), camera_z=camera_z,
                                  home_after_cup=home_after_cup, left_only=left_only)
        self.language = LanguageController(self.session, self.interpreter,
                                           audit_dir=Path(directory), clock=lambda: self.now)

    def capture(self, kind):
        self.capture_entered.set()
        if self.capture_gate is not None:
            if not self.capture_gate.wait(timeout=3):
                raise RuntimeError("Test camera gate timed out")
        if self.capture_error is not None:
            raise self.capture_error
        self.sequence += 1
        self.captures.append(kind)
        result = inventory(capture=f"{kind}-{self.sequence}", sequence=self.sequence, objects=[
            detection("CUP", 100), detection("CUP BASE", 100),
            detection("CUP", 200, x=550), detection("CUP BASE", 200, x=550),
            detection("CUP", 300, x=600),
        ])
        return result, self.now

    def factory(self, controller):
        self.controller = controller
        return self.session, self.language


class DirectWebSelectionTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = WebFixture(directory.name)
        self.session = self.fixture.session
        self.language = self.fixture.language
        self.session.scan()

    def propose(self, **changes):
        values = {"action": "hover", "capture_id": self.session.inventory["capture"], "cup_id": 1}
        values.update(changes)
        return self.language.propose_selection(**values)

    def test_speed_control_reports_actual_caps_without_new_session_attributes(self):
        controls = RobotControls(self.session)
        for speed, automatic, manual in ((250, 200, 100), (25, 25, 25), (1500, 200, 100)):
            with self.subTest(speed=speed):
                result = controls.set_speed(speed)
                self.assertIn(f"automatic home uses {automatic} mm/s", result["message"])
                self.assertIn(f"manual home is capped at {manual} mm/s", result["message"])
                self.assertEqual(self.session.speed, speed)
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_direct_selection_requires_confirmation_and_skips_model(self):
        result = self.propose()
        self.assertEqual(result["kind"], "proposal")
        self.assertEqual(self.fixture.robot.commands, [])
        self.assertEqual(self.fixture.captures, ["scan"])
        self.fixture.interpreter.interpret.assert_not_called()
        confirmed = self.language.confirm(result["token"])
        self.assertEqual(confirmed["action"], "hover")
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assertEqual(len(self.fixture.robot.jobs), 2)
        self.fixture.interpreter.interpret.assert_not_called()

    def test_stale_capture_unknown_blocked_and_invalid_ids_never_propose(self):
        invalid = [{"capture_id": "old-image"}, {"cup_id": 99}, {"cup_id": 3},
                   {"cup_id": True}, {"cup_id": 1.0}, {"cup_id": "1"},
                   {"action": "home"}, {"action": "grip"}, {"action": "reset"}]
        for changes in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.propose(**changes)
                self.assertIsNone(self.language.pending)
                self.assertEqual(self.fixture.robot.commands, [])

    def test_direct_proposal_obeys_expiration_and_scan_fingerprint(self):
        first = self.propose()
        self.fixture.now += 61
        with self.assertRaisesRegex(SelectionError, "expired"):
            self.language.confirm(first["token"])
        second = self.propose()
        self.session.inventory["cups"][0]["status"] = "changed"
        with self.assertRaisesRegex(SelectionError, "changed"):
            self.language.confirm(second["token"])
        self.assertEqual(self.fixture.robot.commands, [])

    def test_failed_new_selection_cancels_previous_proposal(self):
        first = self.propose()
        with self.assertRaises(ValueError):
            self.propose(cup_id=99)
        with self.assertRaisesRegex(SelectionError, "No pending"):
            self.language.confirm(first["token"])
        self.assertEqual(self.fixture.robot.commands, [])


class WebControllerTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = WebFixture(directory.name)
        self.controller = WebController(self.fixture.factory)
        self.addCleanup(self.controller.close)
        self.controller.start()
        self.wait_idle()
        self.assertTrue(self.controller.state()["ready"])

    def wait_idle(self, controller=None):
        controller = controller or self.controller
        deadline = time.monotonic() + 3
        while controller.state()["busy"]:
            if time.monotonic() > deadline:
                self.fail("Controller worker did not complete within 3 seconds")
            time.sleep(0.005)
        return controller.state()

    def submit(self, operation, **payload):
        payload.setdefault("request_id", uuid.uuid4().hex)
        self.assertEqual(self.controller.submit(operation, payload), {"accepted": True})
        return self.wait_idle()

    def scan(self):
        state = self.submit("scan")
        self.assertIsNone(state["error"])
        self.assertTrue(state["scan_usable"])
        return state["inventory"]["capture"]

    def propose(self, *, action="hover", cup_id=1):
        capture = self.scan()
        state = self.submit("select", capture_id=capture, cup_id=cup_id, action=action)
        self.assertIsNone(state["error"])
        self.assertIsNotNone(state["proposal"])
        return {key: state["proposal"][key] for key in ("token", "capture_id", "cup_id", "action")}

    def test_button_selection_and_exact_terminal_commands_only_propose(self):
        capture = self.scan()
        for operation, payload in (("select", {"capture_id": capture, "cup_id": 1, "action": "hover"}),
                                   ("command", {"text": "goto 2"}),
                                   ("command", {"text": "hover 1"})):
            with self.subTest(operation=operation, payload=payload):
                state = self.submit(operation, **payload)
                self.assertIsNone(state["error"])
                self.assertIsNotNone(state["proposal"])
                self.assertEqual(self.fixture.robot.jobs, [])
                self.fixture.interpreter.interpret.assert_not_called()

    def test_natural_request_proposes_then_confirmation_uses_real_pipeline(self):
        self.scan()
        state = self.submit("command", text="Please hover above the first cup")
        self.assertEqual(state["proposal"]["cup_id"], 1)
        self.assertEqual(state["proposal"]["acceleration"], 100)
        self.assertEqual(state["motion"]["acceleration"], 60)
        self.assertEqual(self.fixture.robot.jobs, [])
        self.fixture.interpreter.interpret.assert_called_once()
        payload = {key: state["proposal"][key] for key in ("token", "capture_id", "cup_id", "action")}
        final = self.submit("confirm", **payload)
        self.assertIsNone(final["error"])
        self.assertIsNone(final["proposal"])
        self.assertIn("no robot commands", final["message"])
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assertEqual([job["name"] for job in self.fixture.robot.jobs], ["left_ready", "approach_cup"])
        self.assertTrue(all(job["body"]["acceleration"] == 100 for job in self.fixture.robot.jobs))

    def test_single_pick_proposal_reports_the_selected_workflow_acceleration(self):
        for left_only, acceleration in ((False, 60), (True, 100)):
            with self.subTest(left_only=left_only), tempfile.TemporaryDirectory() as directory:
                fixture = WebFixture(directory, left_only=left_only)
                controller = WebController(fixture.factory)
                try:
                    controller.start()
                    self.wait_idle(controller)
                    fixture.session.scan()
                    fixture.language.propose_selection(action="goto", cup_id=1,
                        capture_id=fixture.session.inventory["capture"])
                    controller.publish_session()
                    self.assertEqual(controller.state()["proposal"]["acceleration"], acceleration)
                    self.assertEqual(fixture.robot.jobs, [])
                finally:
                    controller.close()

    def test_batch_button_previews_exact_ids_slots_and_speeds_before_one_confirmation(self):
        capture = self.scan()
        state = self.submit("batch", capture_id=capture)
        self.assertIsNone(state["error"])
        proposal = state["proposal"]
        self.assertEqual(proposal["action"], "goto_all")
        self.assertIsNone(proposal["cup_id"])
        self.assertEqual(proposal["cup_ids"], [1, 2])
        self.assertEqual([place["id"] for place in proposal["placements"]], [1, 2])
        self.assertEqual(proposal["speed"], 250)
        self.assertEqual(proposal["home_speed"], 200)
        self.assertEqual(proposal["acceleration"], 60)
        self.assertIs(proposal["home_after_cup"], False)
        self.assertIs(state["motion"]["home_after_cup"], False)
        self.assertEqual(state["motion"]["handover_plan"], "smooth_dual_arm_v1")
        self.assertEqual(state["motion"]["acceleration"], 60)
        self.assertEqual(state["motion"]["ramp"], 40)
        self.assertEqual(state["motion"]["carry_max_acceleration"], 1.5)
        self.assertIn("clearance poses", proposal["description"])
        self.assertIs(proposal["camera_z"], False)
        self.assertEqual(proposal["flip_speed"], 100)
        self.assertEqual(self.fixture.robot.jobs, [])
        self.fixture.interpreter.interpret.assert_not_called()
        payload = {key: proposal[key] for key in ("token", "capture_id", "cup_id", "action")}
        state = self.submit("confirm", **payload)
        self.assertIsNone(state["error"])
        expected = sum(len(cup["last_verification"]["jobs"])
                       for cup in self.fixture.session.inventory["cups"] if cup["id"] in proposal["cup_ids"])
        self.assertEqual(len(self.fixture.robot.jobs), expected)
        self.assertIsNone(state["proposal"])
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assertFalse(any(place["occupied"] for place in self.fixture.session.placing_grid.snapshot()["locations"]))

    def test_explicit_home_mode_is_exposed_in_proposal_and_progress_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = WebFixture(directory, home_after_cup=True)
            controller = WebController(fixture.factory)
            try:
                controller.start()
                self.wait_idle(controller)
                controller.submit("scan", {"request_id": uuid.uuid4().hex})
                state = self.wait_idle(controller)
                controller.submit("select", {"request_id": uuid.uuid4().hex,
                                  "capture_id": state["inventory"]["capture"], "cup_id": 1, "action": "goto"})
                state = self.wait_idle(controller)
                proposal = state["proposal"]
                self.assertIsNone(state["error"])
                self.assertIs(state["motion"]["home_after_cup"], True)
                self.assertIs(proposal["home_after_cup"], True)
                self.assertIn("return both arms home", proposal["description"])
                controller._prepare_progress(fixture.language.pending["record"])
                expected = controller._execution_totals[1]
                payload = {key: proposal[key] for key in ("token", "capture_id", "cup_id", "action")}
                controller.submit("confirm", {"request_id": uuid.uuid4().hex, **payload})
                state = self.wait_idle(controller)
                self.assertIsNone(state["error"])
                self.assertEqual(expected, len(fixture.robot.jobs))
                self.assertEqual(fixture.robot.jobs[-1]["name"], "right_home")
            finally:
                controller.close()

    def test_camera_pick_mode_is_visible_in_public_motion_and_pick_proposal(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = WebFixture(directory, camera_z=True)
            controller = WebController(fixture.factory)
            try:
                controller.start()
                self.wait_idle(controller)
                self.assertIs(controller.state()["motion"]["camera_z"], True)
                controller.submit("scan", {"request_id": uuid.uuid4().hex})
                state = self.wait_idle(controller)
                controller.submit("select", {"request_id": uuid.uuid4().hex,
                                  "capture_id": state["inventory"]["capture"], "cup_id": 1, "action": "goto"})
                state = self.wait_idle(controller)
                self.assertIsNone(state["error"])
                self.assertIs(state["proposal"]["camera_z"], True)
                self.assertIn("base plus 20 mm", state["proposal"]["description"])
                self.assertIn("40 mm minimum", state["proposal"]["description"])
                self.assertEqual(fixture.robot.jobs, [])
            finally:
                controller.close()

    def test_batch_command_without_model_only_proposes_and_cancel_sends_no_commands(self):
        self.scan()
        for text in ("goto all", "pick all available cups"):
            with self.subTest(text=text):
                state = self.submit("command", text=text)
                self.assertIsNone(state["error"])
                self.assertEqual(state["proposal"]["action"], "goto_all")
                self.assertEqual(state["proposal"]["cup_ids"], [1, 2])
                self.assertEqual(self.fixture.robot.jobs, [])
                self.fixture.interpreter.interpret.assert_not_called()
        state = self.submit("cancel")
        self.assertIsNone(state["proposal"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_batch_confirmation_cannot_change_action_or_inject_a_single_cup(self):
        for changes in ({"action": "goto"}, {"cup_id": 1}, {"action": "goto", "cup_id": 1}):
            with self.subTest(changes=changes):
                capture = self.scan()
                proposal = self.submit("batch", capture_id=capture)["proposal"]
                payload = {key: proposal[key] for key in ("token", "capture_id", "cup_id", "action")}
                payload.update(changes)
                if len(changes) == 1:
                    with self.assertRaises(WebError) as error:
                        self.controller.submit("confirm", dict(payload, request_id=uuid.uuid4().hex))
                    self.assertEqual(error.exception.status, 422)
                else:
                    state = self.submit("confirm", **payload)
                    self.assertIsNotNone(state["error"])
                    self.assertIsNone(state["proposal"])
                self.assertEqual(self.fixture.robot.jobs, [])

    def test_duplicate_request_id_or_confirmation_never_replays(self):
        payload = self.propose()
        request_id = uuid.uuid4().hex
        self.submit("confirm", request_id=request_id, **payload)
        count = len(self.fixture.robot.jobs)
        with self.assertRaisesRegex(WebError, "already received"):
            self.controller.submit("confirm", dict(payload, request_id=request_id))
        replay = self.submit("confirm", **payload)
        self.assertIsNotNone(replay["error"])
        self.assertEqual(len(self.fixture.robot.jobs), count)

    def test_confirm_binds_capture_cup_action_and_token(self):
        for key, value in (("capture_id", "different-capture"), ("cup_id", 2),
                           ("action", "goto"), ("token", "00000000")):
            with self.subTest(field=key):
                payload = self.propose()
                payload[key] = value
                state = self.submit("confirm", **payload)
                self.assertIsNotNone(state["error"])
                self.assertIsNone(state["proposal"])
                self.assertEqual(self.fixture.robot.jobs, [])

    def test_cancel_and_new_scan_invalidate_pending_confirmation(self):
        for operation in ("cancel", "scan"):
            with self.subTest(operation=operation):
                payload = self.propose()
                self.submit(operation)
                final = self.submit("confirm", **payload)
                self.assertIsNotNone(final["error"])
                self.assertIsNone(final["proposal"])
                self.assertEqual(self.fixture.robot.jobs, [])

    def test_expired_proposal_does_not_execute(self):
        payload = self.propose()
        self.fixture.now += 61
        state = self.submit("confirm", **payload)
        self.assertIn("expired", state["error"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_failed_scan_does_not_leave_previous_scan_authorized(self):
        payload = self.propose()
        self.fixture.capture_error = RuntimeError("Camera disconnected")
        state = self.submit("scan")
        self.assertIn("Camera disconnected", state["error"])
        self.assertFalse(state["scan_usable"])
        self.assertIsNone(state["proposal"])
        state = self.submit("confirm", **payload)
        self.assertIsNotNone(state["error"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_failed_request_id_is_still_consumed(self):
        request_id = uuid.uuid4().hex
        self.fixture.capture_error = RuntimeError("Camera failed")
        state = self.submit("scan", request_id=request_id)
        self.assertIsNotNone(state["error"])
        self.fixture.capture_error = None
        with self.assertRaisesRegex(WebError, "already received"):
            self.controller.submit("scan", {"request_id": request_id})
        self.assertEqual(self.fixture.captures, [])

    def test_busy_worker_rejects_another_action_without_queuing_it(self):
        gate = self.fixture.capture_gate = threading.Event()
        self.addCleanup(gate.set)
        self.controller.submit("scan", {"request_id": uuid.uuid4().hex})
        self.assertTrue(self.fixture.capture_entered.wait(timeout=1))
        state = self.controller.state()
        self.assertTrue(state["busy"])
        for operation, fields in (("scan", {}), ("command", {"text": "goto 1"}), ("cancel", {})):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(WebError, "busy"):
                    self.controller.submit(operation, dict(fields, request_id=uuid.uuid4().hex))
        gate.set()
        self.wait_idle()
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_live_motion_stays_busy_and_progress_reports_actual_completed_steps(self):
        self.controller.close()
        self.fixture.robot.dry_run = False
        self.controller = WebController(self.fixture.factory, mode="live")
        self.addCleanup(self.controller.close)
        self.controller.start()
        self.wait_idle()
        # The production save hook publishes snapshots after each controller step.
        self.fixture.session.save = lambda unused: self.controller.publish_session()
        payload = self.propose()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def after_move(count):
            if count == 1:
                entered.set()
                release.wait(timeout=2)
        self.fixture.robot.after_move = after_move
        self.controller.submit("confirm", dict(payload, request_id=uuid.uuid4().hex))
        self.assertTrue(entered.wait(timeout=1))
        state = self.controller.state()
        self.assertTrue(state["busy"])
        self.assertEqual(state["progress"]["completed"], 0)
        self.assertEqual(state["progress"]["step"], "left_ready")
        self.assertIsNone(state["proposal"])
        with self.assertRaisesRegex(WebError, "busy"):
            self.controller.submit("scan", {"request_id": uuid.uuid4().hex})
        release.set()
        final = self.wait_idle()
        self.assertEqual(final["progress"]["completed"], 2)
        self.assertEqual(final["progress"]["state"], "completed")
        self.assertTrue(final["scan_usable"])
        self.assertEqual(self.fixture.captures, ["scan"])

    def test_successive_selected_cups_share_one_snapshot_and_completed_cup_is_disabled(self):
        self.controller.close()
        self.fixture.robot.dry_run = False
        self.controller = WebController(self.fixture.factory, mode="live")
        self.addCleanup(self.controller.close)
        self.controller.start()
        self.wait_idle()
        capture = self.scan()
        self.fixture.capture_error = AssertionError("No new camera observation is allowed during picking")
        for cup_id in (1, 2):
            proposal = self.submit("select", capture_id=capture, cup_id=cup_id, action="goto")["proposal"]
            self.assertIsNotNone(proposal)
            state = self.submit("confirm", **{key: proposal[key] for key in ("token", "capture_id", "cup_id", "action")})
            self.assertIsNone(state["error"])
            self.assertTrue(state["scan_usable"])
            self.assertEqual(state["inventory"]["capture"], capture)
            completed = next(cup for cup in state["inventory"]["cups"] if cup["id"] == cup_id)
            self.assertFalse(completed["selectable"])
            self.assertFalse(completed["pick_available"])
            if cup_id == 1:
                self.assertTrue(next(cup for cup in state["inventory"]["cups"] if cup["id"] == 2)["pick_available"])
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assertIn("already", self.submit("select", capture_id=capture, cup_id=1, action="goto")["error"])

    def test_receiving_alignment_route_is_no_longer_available(self):
        with self.assertRaises(WebError):
            self.submit("handover_offset", x=1, y=2, z=3)
        self.assertEqual(self.fixture.session.handover_offset, (0, 0, 0))

    def test_live_batch_progress_counts_all_planned_cups_and_confirmed_steps(self):
        self.controller.close()
        self.fixture.robot.dry_run = False
        self.controller = WebController(self.fixture.factory, mode="live")
        self.addCleanup(self.controller.close)
        self.controller.start()
        self.wait_idle()
        self.fixture.session.save = lambda unused: self.controller.publish_session()
        progress = []
        self.fixture.robot.after_move = lambda count: progress.append(self.controller.state()["progress"])
        capture = self.scan()
        proposal = self.submit("batch", capture_id=capture)["proposal"]
        payload = {key: proposal[key] for key in ("token", "capture_id", "cup_id", "action")}
        state = self.submit("confirm", **payload)
        self.assertIsNone(state["error"])
        expected = sum(len(cup["last_verification"]["jobs"])
                       for cup in self.fixture.session.inventory["cups"] if cup["id"] in proposal["cup_ids"])
        self.assertEqual(len(progress), expected)
        self.assertTrue(all(item["total"] == expected for item in progress))
        self.assertEqual(progress[0]["completed"], 0)
        self.assertEqual(state["progress"]["completed"], expected)
        self.assertEqual(state["progress"]["total"], expected)
        self.assertEqual(state["progress"]["state"], "completed")
        self.assertEqual(state["batch"]["completed_cups"], [1, 2])
        self.assertEqual(state["batch"]["current"], 2)
        self.assertEqual(state["batch"]["total"], 2)
        self.assertEqual(self.fixture.captures, ["scan"])

    def test_state_is_a_copy_and_does_not_expose_private_inventory_fields(self):
        self.propose()
        state = self.controller.state()
        self.assertNotIn("gateway", state["inventory"])
        self.assertNotIn("images", state["inventory"])
        self.assertNotIn("calibration_sha256", state["inventory"])
        self.assertNotIn("x", state["inventory"]["cups"][0])
        original_token = state["proposal"]["token"]
        state["proposal"]["token"] = "corrupted"
        state["inventory"]["cups"][0]["status"] = "corrupted"
        state["events"].clear()
        fresh = self.controller.state()
        self.assertEqual(fresh["proposal"]["token"], original_token)
        self.assertNotEqual(fresh["inventory"]["cups"][0]["status"], "corrupted")
        self.assertTrue(fresh["events"])

    def test_dynamic_batch_publishes_updated_totals_even_without_historical_cup_rows(self):
        capture = self.scan()
        session = self.fixture.session
        session.between_cup_check = True
        self.fixture.robot.dry_run = False
        cup = copy.deepcopy(session.inventory["cups"][0])
        cup.update(id=4, execution={"state": "running", "total_steps": 7,
                                    "completed_steps": ["open", "approach"]})
        session.inventory["cups"] = [cup]
        session.batch_progress = {"cup_ids": [1, 4], "completed_cups": [1], "pending_cups": [4],
                                  "current_cup_id": 4, "state": "running", "revision": 2,
                                  "planned_steps": {1: 5, 4: 7}}
        self.controller._execution_capture = capture
        self.controller._execution_totals = {1: 30, 2: 30}
        self.controller.publish_session()
        state = self.controller.state()
        self.assertTrue(state["motion"]["dynamic_batch"])
        self.assertEqual((state["progress"]["completed"], state["progress"]["total"]), (7, 12))
        self.assertEqual(state["batch"]["pending_cups"], [4])
        self.assertEqual(state["batch"]["revision"], 2)
        # A later observation removes the unfinished cup and adds a different one.
        cup.update(id=5)
        cup.pop("execution")
        session.batch_progress.update(cup_ids=[1, 5], pending_cups=[5], current_cup_id=5,
                                      planned_steps={"1": 5, "5": 9}, revision=3)
        self.controller.publish_session()
        state = self.controller.state()
        self.assertEqual((state["progress"]["completed"], state["progress"]["total"]), (5, 14))
        self.assertEqual(state["batch"]["pending_cups"], [5])
        session.batch_progress.update(completed_cups=[1, 5], pending_cups=[], state="completed",
                                      completion_reason="workspace_empty")
        self.controller.publish_session()
        state = self.controller.state()
        self.assertEqual((state["progress"]["completed"], state["progress"]["total"]), (14, 14))
        self.assertEqual(state["batch"]["completion_reason"], "workspace_empty")

    def test_dynamic_batch_proposal_is_explicit_but_single_selection_stays_single(self):
        self.fixture.session.between_cup_check = True
        capture = self.scan()
        proposal = self.submit("batch", capture_id=capture)["proposal"]
        self.assertTrue(proposal["dynamic_batch"])
        self.assertIn("include new pickable cups", proposal["description"])
        self.submit("cancel")
        proposal = self.submit("select", capture_id=capture, cup_id=2, action="goto")["proposal"]
        self.assertFalse(proposal["dynamic_batch"])
        self.assertEqual(proposal["cup_ids"], [2])
        self.assertNotIn("include new pickable cups", proposal["description"])

    def test_configured_right_park_is_reviewed_and_counted_for_every_cup(self):
        pose = {"x": 400, "y": -200, "z": 300, "rx": 0, "ry": 180, "rz": 0}
        self.fixture.session._right_park_pose = pose
        capture = self.scan()
        proposal = self.submit("batch", capture_id=capture)["proposal"]
        self.assertEqual(proposal["right_park_pose"], pose)
        self.assertEqual(self.controller.state()["motion"]["right_park_pose"], pose)
        self.assertIn("move the right arm to its camera-clear waiting position", proposal["description"])
        state = self.submit("confirm", **{key: proposal[key] for key in ("token", "capture_id", "cup_id", "action")})
        self.assertIsNone(state["error"])
        self.assertEqual(sum(job["name"] == "right_park" for job in self.fixture.robot.jobs), 2)
        self.assertEqual(state["progress"]["total"], len(self.fixture.robot.jobs))
        self.assertEqual(state["progress"]["completed"], len(self.fixture.robot.jobs))

    def test_grid_refresh_policy_is_forwarded_in_review_and_keeps_single_pick_scope(self):
        session = self.fixture.session
        session.between_cup_check = True
        for enabled in (False, True):
            with self.subTest(grid_refresh=enabled):
                session.between_cup_grid_check = enabled
                capture = self.scan()
                state = self.submit("batch", capture_id=capture)
                proposal = state["proposal"]
                self.assertEqual(state["motion"]["between_cup_grid_check"], enabled)
                self.assertEqual(state["motion"]["continuous_batch"], enabled)
                self.assertTrue(proposal["between_cup_check"])
                self.assertEqual(proposal["between_cup_grid_check"], enabled)
                self.assertEqual(proposal["continuous_batch"], enabled)
                if enabled:
                    self.assertIn("Camera-cleared locations become available again", proposal["description"])
                    self.assertIn("wait and refresh automatically", proposal["description"])
                    self.assertIn("send both arms Home once", proposal["description"])
                    self.assertIn("active until Stop", proposal["description"])
                    self.assertNotIn("evidence stays from the manual scan", proposal["description"])
                else:
                    self.assertIn("refreshes source cups only", proposal["description"])
                    self.assertNotIn("wait and refresh automatically", proposal["description"])
                self.submit("cancel")
                proposal = self.submit("select", capture_id=capture, cup_id=2, action="goto")["proposal"]
                self.assertEqual(proposal["cup_ids"], [2])
                self.assertFalse(proposal["dynamic_batch"])
                self.assertFalse(proposal["continuous_batch"])
                self.assertEqual(proposal["between_cup_grid_check"], enabled)
                self.assertNotIn("wait and refresh automatically", proposal["description"])
                self.assertNotIn("send both arms Home once", proposal["description"])
                self.submit("cancel")

    def test_waiting_for_grid_publishes_queued_cups_separately_from_dispatchable_work(self):
        self.scan()
        session = self.fixture.session
        session.between_cup_check = session.between_cup_grid_check = True
        session.batch_progress = {"state": "waiting_for_grid", "cup_ids": [1, 2],
                                  "completed_cups": [1], "pending_cups": [], "waiting_cup_ids": [2],
                                  "current_cup_id": None, "planned_steps": {1: 5},
                                  "completion_reason": "grid_full", "revision": 2}
        self.controller.publish_session()
        state = self.controller.state()
        self.assertEqual(state["batch"]["state"], "waiting_for_grid")
        self.assertEqual(state["batch"]["waiting_cup_ids"], [2])
        self.assertEqual(state["batch"]["pending_cups"], [])
        self.assertTrue(state["batch"]["between_cup_grid_check"])
        self.assertEqual(state["progress"]["step"], "waiting_for_grid")
        self.assertIsNone(state["batch"]["cup_id"])
        session.inventory["workspace_check"] = {"state": "capturing", "grid_checked": False}
        self.controller.publish_session()
        self.assertEqual(self.controller.state()["batch"]["state"], "waiting_for_grid")
        for phase in ("homing_when_empty", "waiting_for_cups"):
            session.inventory.pop("workspace_check", None)
            session.batch_progress.update(state=phase, waiting_cup_ids=[], arms_home=phase == "waiting_for_cups",
                home_cycles=1, idle_home={"state": "completed", "completed_steps": ["home_right", "home_left"],
                                         "pending_job": None, "total_steps": 2})
            self.controller.publish_session()
            state = self.controller.state()
            self.assertEqual(state["batch"]["state"], phase)
            self.assertEqual(state["progress"]["step"], phase)
            self.assertTrue(state["batch"]["continuous_batch"])
            self.assertEqual(state["batch"]["arms_home"], phase == "waiting_for_cups")
            self.assertEqual(state["batch"]["home_cycles"], 1)
            self.assertEqual(state["batch"]["idle_home"]["completed_steps"], ["home_right", "home_left"])

    def test_close_releases_stationary_batch_wait_before_joining_worker_without_robot_commands(self):
        entered, finish = threading.Event(), threading.Event()
        self.fixture.session.finish_batch_when_parked = Mock(side_effect=finish.set)
        def wait_for_finish():
            entered.set()
            return finish.wait(timeout=2)
        waiting = self.controller._worker.submit(wait_for_finish)
        self.assertTrue(entered.wait(timeout=1))
        self.controller.close()
        self.assertTrue(waiting.result())
        self.fixture.session.finish_batch_when_parked.assert_called_once_with()
        self.assertEqual(self.fixture.robot.jobs, [])
        self.controller.close()
        self.fixture.session.finish_batch_when_parked.assert_called_once_with()

    def test_unregistered_or_old_scan_image_is_not_available(self):
        first = self.scan()
        self.assertEqual(self.controller.image(first), "offline.jpg")
        self.scan()
        self.assertIsNone(self.controller.image(first))
        self.assertIsNone(self.controller.image("../../secret"))

    def test_strict_payloads_reject_unsafe_types_extras_and_unacknowledged_recovery(self):
        cases = [("scan", {"request_id": "short"}), ("scan", {"request_id": "12345678", "extra": 1}),
                 ("recovery", {"request_id": "12345678", "acknowledged": 1}),
                 ("command", {"request_id": "12345678", "text": "  "}),
                 ("command", {"request_id": "12345678", "text": "x" * 2001})]
        for value in (True, 1.0, "1", 0, -1):
            cases.append(("select", {"request_id": "12345678", "capture_id": "scan-1",
                                     "cup_id": value, "action": "hover"}))
        for operation, payload in cases:
            with self.subTest(operation=operation, payload=payload):
                with self.assertRaises(WebError) as caught:
                    self.controller.submit(operation, payload)
                self.assertEqual(caught.exception.status, 422)
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_recovery_acknowledgement_never_moves_and_requires_a_new_scan(self):
        self.scan()
        self.fixture.session.motion_uncertain = True
        self.controller.publish_session()
        state = self.submit("recovery", acknowledged=True)
        self.assertFalse(state["recovery_required"])
        self.assertFalse(state["scan_usable"])
        self.assertEqual(self.fixture.robot.jobs, [])
        self.assertIn("No robot movement", state["message"])

    def test_reserved_commands_cannot_home_recover_or_bypass_confirmation(self):
        self.scan()
        for text in ("home", "reset", "confirm abcd1234", "exit"):
            with self.subTest(text=text):
                state = self.submit("command", text=text)
                self.assertIsNotNone(state["error"])
                self.assertIsNone(state["proposal"])
                self.fixture.interpreter.interpret.assert_not_called()
                self.assertEqual(self.fixture.robot.jobs, [])

    def test_language_failure_cancels_an_earlier_proposal_without_fallback_motion(self):
        earlier = self.propose()
        self.fixture.interpreter.interpret.side_effect = ValueError("Local model unavailable")
        state = self.submit("command", text="Please pick cup two")
        self.assertIn("Local model unavailable", state["error"])
        self.assertIsNone(state["proposal"])
        self.submit("confirm", **earlier)
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_disabled_llm_still_allows_direct_proposals_but_not_natural_requests(self):
        self.controller.close()
        self.controller = WebController(self.fixture.factory, llm_enabled=False)
        self.addCleanup(self.controller.close)
        self.controller.start()
        self.wait_idle()
        self.scan()
        state = self.submit("command", text="Please pick cup one")
        self.assertIn("disabled", state["error"])
        self.fixture.interpreter.interpret.assert_not_called()
        state = self.submit("command", text="hover 1")
        self.assertIsNone(state["error"])
        self.assertEqual(state["proposal"]["cup_id"], 1)
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_uncertain_motion_is_exposed_and_blocks_later_selection(self):
        self.controller.close()
        self.fixture.robot.dry_run = False
        self.fixture.robot.error = RobotCommandError("Completion could not be confirmed")
        self.controller = WebController(self.fixture.factory, mode="live")
        self.addCleanup(self.controller.close)
        self.controller.start()
        self.wait_idle()
        payload = self.propose()
        state = self.submit("confirm", **payload)
        self.assertIn("could not be confirmed", state["error"])
        self.assertTrue(state["recovery_required"])
        self.assertEqual(state["progress"]["state"], "interrupted")
        self.assertEqual(state["progress"]["completed"], 0)
        self.assertFalse(state["scan_usable"])
        self.assertIsNone(state["proposal"])
        self.assertEqual(len(self.fixture.robot.jobs), 1)
        self.assertEqual(self.fixture.captures, ["scan"])
        fresh_capture = self.scan()
        state = self.submit("select", capture_id=fresh_capture, cup_id=1, action="hover")
        self.assertIsNotNone(state["error"])
        self.assertTrue(state["recovery_required"])
        self.assertEqual(len(self.fixture.robot.jobs), 1)

    def test_startup_failure_or_mode_mismatch_cannot_accept_operations(self):
        def failure(controller):
            raise RuntimeError("Gateway is unavailable")
        for factory, mode in ((failure, "demo"), (self.fixture.factory, "live")):
            with self.subTest(mode=mode, factory=factory):
                controller = WebController(factory, mode=mode)
                self.addCleanup(controller.close)
                controller.start()
                self.wait_idle(controller)
                self.assertFalse(controller.state()["ready"])
                self.assertIsNotNone(controller.state()["error"])
                with self.assertRaises(WebError) as caught:
                    controller.submit("scan", {"request_id": uuid.uuid4().hex})
                self.assertEqual(caught.exception.status, 503)


if __name__ == "__main__":
    unittest.main()
