"""Operator control tests are offline; all state files are temporary."""

from contextlib import redirect_stdout
import copy
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from cup_commands import CupSession
from cup_inventory import SelectionError
from recovery_journal import RecoveryJournal
from robot_client import RobotClient, RobotCommandError
from robot_controls import RobotControls, control_plan, valid_speed
from test_web_controller import WebFixture
from web_controller import WebController, WebError


RUNNING = {"execution_state": "running", "controller_state": "motoron",
           "operation_mode": "AUTO", "collision_state": "INIT", "read_at": "offline"}


class OfflineControlFixture(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        for method in ("get", "post"):
            blocked_http = patch("robot_client.requests." + method,
                                 side_effect=AssertionError("No real HTTP is allowed in operator tests"))
            blocked_http.start()
            self.addCleanup(blocked_http.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.fixture = WebFixture(directory.name, dry_run=False)
        self.session, self.robot = self.fixture.session, self.fixture.robot
        self.robot.read_control_status = Mock(return_value=copy.deepcopy(RUNNING))
        self.robot.control_action = Mock(return_value={"message": "Operator action confirmed.",
                                                      "status": copy.deepcopy(RUNNING)})
        self.robot.request_stop = Mock(return_value={"dry_run": False, "stopped": True,
                                                     "message": "Software stop confirmed."})
        self.recovery = RecoveryJournal(self.directory / "operator_recovery.json")
        self.session.recovery = self.recovery
        self.controls = RobotControls(self.session, clock=lambda: self.fixture.now)

    def gates(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        return entered, release


class RobotControlPlanTests(unittest.TestCase):
    def test_allowlist_builds_only_requested_jobs_and_home_is_capped(self):
        for action in ("home", "open", "grip"):
            for arm in ("left", "right", "both"):
                with self.subTest(action=action, arm=arm):
                    plan = control_plan(action, arm, 1200)
                    expected = ["left", "right"] if arm == "both" else [arm]
                    self.assertEqual([job["body"]["arm"] for job in plan["jobs"]], expected)
                    self.assertTrue(all(job["body"]["wait"] is True for job in plan["jobs"]))
                    if action == "home":
                        self.assertEqual(plan["speed"], 100)
                        self.assertTrue(all(job["endpoint"] == RobotClient.HOME_ENDPOINT
                                            and job["body"]["speed"] == 100 for job in plan["jobs"]))
                    else:
                        self.assertIsNone(plan["speed"])
                        self.assertTrue(all(job["endpoint"] == RobotClient.GRIPPER_ENDPOINT
                                            and job["body"]["action"] == action for job in plan["jobs"]))
                        self.assertTrue(all("speed" not in job["body"] for job in plan["jobs"]))
        self.assertEqual(control_plan("home", "left", 25.5)["jobs"][0]["body"]["speed"], 25.5)

    def test_controller_controls_never_include_home_or_gripper_jobs(self):
        for action in ("rapid_reset", "rapid_start", "collision_ack"):
            with self.subTest(action=action):
                plan = control_plan(action, None, 1200)
                self.assertEqual(plan["jobs"], [])
                self.assertIsNone(plan["speed"])
                with self.assertRaises(SelectionError):
                    control_plan(action, "both", 1200)

    def test_unknown_actions_arms_and_invalid_speed_are_rejected(self):
        for action, arm in (("move", "left"), ("/robot/rws/home", "left"),
                            ("reset", None), ("home", None), ("grip", "all"),
                            ("open", True), (None, "left")):
            with self.subTest(action=action, arm=arm), self.assertRaises(SelectionError):
                control_plan(action, arm, 100)
        for speed in (0, -1, 1501, True, False, None, "100", float("nan"), float("inf")):
            with self.subTest(speed=speed), self.assertRaises(SelectionError):
                valid_speed(speed)
        for speed in (1, 1500, 73.5):
            self.assertEqual(valid_speed(speed), float(speed))


class RobotControlSessionTests(OfflineControlFixture):
    def test_default_and_explicit_speed_profiles_reach_all_automated_home_jobs(self):
        cases = [({}, 250, 200), ({"speed": None}, 250, 200),
                 ({"speed": 400}, 400, 200), ({"speed": 25}, 25, 25),
                 ({"speed": 1200}, 1200, 200)]
        for settings, normal_speed, home_speed in cases:
            with self.subTest(settings=settings):
                self.robot.jobs.clear()
                self.robot.commands.clear()
                self.robot.dry_run = True
                session = CupSession(self.fixture.capture, Mock(), self.robot, "cal-1",
                                     clock=lambda: self.fixture.now,
                                     placing_grid=self.session.placing_grid, **settings)
                self.assertEqual(session.motion_settings()["speed"], normal_speed)
                self.assertEqual(session.motion_settings()["home_speed"], home_speed)
                scan = session.scan()
                jobs = session.execute_selection(action="goto", capture_id=scan["capture"], cup_id=1)
                home_jobs = [job for job in jobs if job["endpoint"].endswith("/home")]
                self.assertEqual([job["name"] for job in home_jobs],
                                 ["right_home_start", "left_home", "right_home"])
                self.assertEqual([job["body"]["speed"] for job in home_jobs], [home_speed] * 3)
                normal_jobs = [job for job in jobs if job["name"] in
                               ("left_ready", "left_home_lateral", "right_place")]
                self.assertEqual(len(normal_jobs), 3)
                self.assertTrue(all(job["body"]["speed"] == normal_speed for job in normal_jobs))
                flip_jobs = [job for job in jobs if job["name"] in ("right_turn_half", "right_retreat")]
                self.assertEqual([job["body"]["speed"] for job in flip_jobs], [min(normal_speed, 100)] * 2)

    def test_applying_speed_caps_automated_homes_without_changing_manual_home_limit(self):
        self.assertEqual(self.session.motion_settings()["home_speed"], 200)
        for speed in (400, 25, 1500):
            with self.subTest(speed=speed):
                self.controls.set_speed(speed)
                self.assertEqual(self.session.motion_settings()["home_speed"], min(speed, 200))
                self.assertEqual(control_plan("home", "both", self.session.speed)["speed"], min(speed, 100))
        self.assertEqual(self.robot.jobs, [])

    def test_preview_never_dispatches_and_requires_one_use_token(self):
        self.controls.propose("open", "left")
        pending = self.controls.snapshot()["proposal"]
        self.assertEqual(len(pending["token"]), 16)
        self.assertEqual(pending["action"], "open")
        self.assertEqual(self.robot.jobs, [])
        self.robot.control_action.assert_not_called()
        self.controls.confirm(pending["token"])
        self.assertEqual(len(self.robot.jobs), 1)
        self.assertIsNone(self.controls.pending)
        with self.assertRaisesRegex(SelectionError, "no longer matches"):
            self.controls.confirm(pending["token"])
        self.assertEqual(len(self.robot.jobs), 1)

    def test_wrong_expired_and_speed_changed_tokens_cannot_execute(self):
        for failure in ("wrong", "expired", "speed"):
            with self.subTest(failure=failure):
                self.controls.propose("home", "both")
                token = self.controls.pending["token"]
                if failure == "wrong":
                    token = "0" * 16 if token != "0" * 16 else "1" * 16
                elif failure == "expired":
                    self.fixture.now += 60
                else:
                    self.session.speed += 1
                with self.assertRaises(SelectionError):
                    self.controls.confirm(token)
                self.assertIsNone(self.controls.pending)
                self.assertEqual(self.robot.jobs, [])

    def test_both_home_runs_left_then_right_at_capped_speed_and_invalidates_scan(self):
        self.session.scan()
        self.session.execute_control("home", "both")
        self.assertEqual([job["name"] for job in self.robot.jobs],
                         ["operator_left_home", "operator_right_home"])
        self.assertEqual([job["body"]["speed"] for job in self.robot.jobs], [100, 100])
        self.assertEqual(self.robot.read_control_status.call_count, 2)
        self.robot.control_action.assert_not_called()
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assertFalse(self.session.scan_usable)
        self.assertFalse(self.session.motion_uncertain)
        self.assertFalse(self.recovery.required())

    def test_first_home_failure_prevents_second_and_persists_across_restart(self):
        self.robot.error = RobotCommandError("left home unconfirmed")
        self.robot.error_at = "operator_left_home"
        with self.assertRaisesRegex(RobotCommandError, "unconfirmed"):
            self.session.execute_control("home", "both")
        self.assertEqual([job["body"]["arm"] for job in self.robot.jobs], ["left"])
        self.assertTrue(self.session.motion_uncertain)
        self.assertTrue(self.recovery.required())
        restarted = CupSession(Mock(), Mock(), self.robot, "cal-1", recovery=self.recovery)
        with self.assertRaisesRegex(SelectionError, "recovery"):
            RobotControls(restarted).propose("home", "right")
        self.assertEqual(len(self.robot.jobs), 1)
        self.robot.control_action.assert_not_called()

    def test_precommand_status_is_checked_for_each_arm_without_automatic_start(self):
        self.robot.read_control_status.side_effect = [RUNNING, {**RUNNING, "execution_state": "stopped"}]
        with self.assertRaisesRegex(SelectionError, "no automatic restart"):
            self.session.execute_control("open", "both")
        self.assertEqual([job["body"]["arm"] for job in self.robot.jobs], ["left"])
        self.robot.control_action.assert_not_called()
        self.assertTrue(self.recovery.required())

    def test_unsafe_controller_states_block_home_without_dispatch(self):
        for update in ({"execution_state": "stopped"}, {"controller_state": "motoroff"},
                       {"operation_mode": "MANR"}, {"collision_state": "TRIGGERED"},
                       {"collision_state": "TRIGGERED_ACK"}, {"collision_state": None},
                       {"collision_state": None, "collision_error": "gateway unreachable"}):
            with self.subTest(update=update):
                self.robot.read_control_status.return_value = {**RUNNING, **update}
                with self.assertRaises(SelectionError):
                    self.session.execute_control("home", "left")
                self.assertEqual(self.robot.jobs, [])
                self.robot.control_action.assert_not_called()
                self.session.acknowledge_recovery()

    def test_controller_action_failure_preserves_recovery_and_never_adds_home(self):
        self.robot.control_action.side_effect = RobotCommandError("controller reply lost")
        with self.assertRaisesRegex(RobotCommandError, "lost"):
            self.session.execute_control("rapid_reset", None)
        self.robot.control_action.assert_called_once_with("rapid_reset")
        self.assertEqual(self.robot.jobs, [])
        self.assertTrue(self.recovery.required())
        self.assertTrue(self.session.motion_uncertain)
        self.session.acknowledge_recovery()
        self.assertEqual(self.robot.jobs, [])
        self.assertEqual(self.robot.control_action.call_count, 1)
        self.assertFalse(self.session.scan_usable)

    def test_controller_actions_dispatch_only_the_explicit_action_and_no_camera(self):
        for action in ("rapid_reset", "rapid_start", "collision_ack"):
            with self.subTest(action=action):
                self.session.execute_control(action, None)
                self.robot.control_action.assert_called_with(action)
                self.assertEqual(self.robot.jobs, [])
                self.assertEqual(self.fixture.captures, [])
                self.assertFalse(self.recovery.required())

    def test_recovery_save_failure_prevents_all_control_dispatch(self):
        with patch.object(self.recovery, "begin", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.session.execute_control("rapid_start", None)
        self.robot.control_action.assert_not_called()
        self.assertEqual(self.robot.jobs, [])

    def test_stop_after_first_home_prevents_next_and_keeps_recovery(self):
        self.robot.after_move = lambda _: self.session.latch_stop()
        with self.assertRaisesRegex(SelectionError, "Software stop"):
            self.session.execute_control("home", "both")
        self.assertEqual([job["body"]["arm"] for job in self.robot.jobs], ["left"])
        self.assertTrue(self.session.stop_requested)
        self.assertTrue(self.recovery.required())
        self.assertFalse(self.session.scan_usable)

    def test_stop_during_recovery_clear_cannot_unlock_completed_control(self):
        real_clear = self.recovery.clear

        def clear_and_stop():
            real_clear()
            self.session.latch_stop()

        with patch.object(self.recovery, "clear", side_effect=clear_and_stop):
            with self.assertRaisesRegex(SelectionError, "Software stop"):
                self.session.execute_control("collision_ack", None)
        self.assertTrue(self.session.motion_uncertain)
        self.assertTrue(self.recovery.required())
        self.robot.control_action.assert_called_once_with("collision_ack")

    def test_speed_changes_future_cup_jobs_but_not_manual_home_cap(self):
        self.robot.dry_run = True
        scan = self.session.scan()
        self.controls.propose("home", "both")
        self.controls.set_speed(320)
        self.assertIsNone(self.controls.pending)
        self.session.execute_selection(action="hover", capture_id=scan["capture"], cup_id=1)
        self.assertEqual([job["body"]["speed"] for job in self.robot.jobs], [320, 320])
        self.assertEqual(control_plan("home", "both", self.session.speed)["speed"], 100)
        self.assertFalse(self.recovery.path.exists())

    def test_active_operation_blocks_manual_controls_and_speed_without_queue(self):
        self.session._activity_lock.acquire()
        try:
            for function in (lambda: self.session.set_speed(250),
                             lambda: self.session.execute_control("home", "left")):
                with self.assertRaisesRegex(SelectionError, "active operation"):
                    function()
        finally:
            self.session._activity_lock.release()
        self.assertEqual(self.session.speed, 250)
        self.assertEqual(self.robot.jobs, [])
        self.robot.control_action.assert_not_called()

    def test_dry_run_real_client_uses_no_http_or_recovery_writes(self):
        self.session.robot = RobotClient("http://offline.invalid/api/v1", {}, dry_run=True)
        for action, arm in (("home", "both"), ("open", "right"), ("grip", "left"),
                            ("rapid_reset", None), ("rapid_start", None), ("collision_ack", None)):
            with self.subTest(action=action):
                self.session.execute_control(action, arm)
        self.assertFalse(self.recovery.path.exists())
        self.assertFalse(self.session.scan_usable)


class WebRobotControlTests(OfflineControlFixture):
    def setUp(self):
        super().setUp()
        self.controller = WebController(self.fixture.factory, mode="live")
        self.addCleanup(self.controller.close)
        self.controller.start()
        self.wait(lambda state: not state["busy"])
        self.assertTrue(self.controller.state()["ready"])
        self.controller.controls.clock = lambda: self.fixture.now

    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = self.controller.state()
            if predicate(state):
                return state
            time.sleep(.003)
        self.fail("Operator worker did not finish within three seconds")

    def request(self, operation, **payload):
        return self.controller.submit(operation, dict(payload, request_id=uuid.uuid4().hex))

    def submit(self, operation, **payload):
        self.assertTrue(self.request(operation, **payload)["accepted"])
        return self.wait(lambda state: not state["busy"])

    def cup_proposal(self):
        capture = self.submit("scan")["inventory"]["capture"]
        state = self.submit("select", capture_id=capture, cup_id=1, action="hover")
        self.assertIsNone(state["error"])
        return state["proposal"]

    def admin_proposal(self, action="home", arm="both"):
        state = self.submit("control_preview", action=action, arm=arm)
        self.assertIsNone(state["error"])
        self.assertEqual(self.robot.jobs, [])
        return state["robot_controls"]["proposal"]

    def test_invalid_payloads_are_rejected_before_worker_or_robot(self):
        invalid = [("speed", {"speed": value}) for value in
                   (True, "50", 0, 1501, float("nan"), float("inf"))]
        invalid += [("control_preview", {"action": "home", "arm": None}),
                    ("control_preview", {"action": "rapid_start", "arm": "both"}),
                    ("control_preview", {"action": "move", "arm": "left"}),
                    ("control_preview", {"action": "home", "arm": "left", "x": 500}),
                    ("control_confirm", {"token": "a" * 16, "acknowledged": False}),
                    ("control_confirm", {"token": "a" * 16, "acknowledged": 1}),
                    ("control_confirm", {"token": "a" * 8, "acknowledged": True}),
                    ("control_confirm", {"token": "a" * 16, "acknowledged": True, "arm": "right"}),
                    ("robot_status", {"arm": "left"})]
        for operation, fields in invalid:
            with self.subTest(operation=operation, fields=fields), self.assertRaises(WebError) as caught:
                self.request(operation, **fields)
            self.assertEqual(caught.exception.status, 422)
        self.assertEqual(self.robot.jobs, [])
        self.robot.control_action.assert_not_called()
        self.robot.read_control_status.assert_not_called()
        self.assertFalse(self.controller.state()["busy"])

    def test_inspection_ack_is_required_and_successful_token_is_consumed(self):
        proposal = self.admin_proposal("open", "left")
        with self.assertRaises(WebError):
            self.request("control_confirm", token=proposal["token"], acknowledged=False)
        self.assertEqual(self.robot.jobs, [])
        final = self.submit("control_confirm", token=proposal["token"], acknowledged=True)
        self.assertIsNone(final["error"])
        self.assertIsNone(final["robot_controls"]["proposal"])
        self.assertEqual([job["body"]["action"] for job in self.robot.jobs], ["open"])
        replay = self.submit("control_confirm", token=proposal["token"], acknowledged=True)
        self.assertIn("no longer matches", replay["error"])
        self.assertEqual(len(self.robot.jobs), 1)

    def test_speed_update_cancels_each_kind_of_proposal(self):
        cup = self.cup_proposal()
        state = self.submit("speed", speed=250)
        self.assertIsNone(state["proposal"])
        self.assertIsNone(self.fixture.language.pending)
        self.assertEqual(state["motion"]["speed"], 250)
        self.assertEqual(state["motion"]["home_speed"], 200)
        admin = self.admin_proposal()
        state = self.submit("speed", speed=75)
        self.assertIsNone(state["robot_controls"]["proposal"])
        self.assertEqual(state["robot_controls"]["home_speed"], 75)
        self.assertEqual(state["motion"]["home_speed"], 75)
        failed = self.submit("control_confirm", token=admin["token"], acknowledged=True)
        self.assertIsNotNone(failed["error"])
        failed = self.submit("confirm", **{key: cup[key] for key in ("token", "capture_id", "cup_id", "action")})
        self.assertIsNotNone(failed["error"])
        self.assertEqual(self.robot.jobs, [])

    def test_cup_and_admin_proposals_are_mutually_exclusive(self):
        cup = self.cup_proposal()
        self.admin_proposal("grip", "right")
        self.assertIsNone(self.controller.state()["proposal"])
        self.assertIsNone(self.fixture.language.pending)
        state = self.submit("select", capture_id=cup["capture_id"], cup_id=2, action="hover")
        self.assertIsNotNone(state["proposal"])
        self.assertIsNone(state["robot_controls"]["proposal"])
        self.assertIsNone(self.controller.controls.pending)
        self.assertEqual(self.robot.jobs, [])

    def test_refresh_status_preserves_both_types_of_pending_preview(self):
        for kind in ("cup", "admin"):
            with self.subTest(kind=kind):
                proposal = self.cup_proposal() if kind == "cup" else self.admin_proposal()
                state = self.submit("robot_status")
                pending = state["proposal"] if kind == "cup" else state["robot_controls"]["proposal"]
                self.assertEqual(pending["token"], proposal["token"])
                self.assertEqual(state["robot_controls"]["status"], RUNNING)
                self.assertIsNone(state["error"])
        self.assertEqual(self.robot.jobs, [])
        self.robot.control_action.assert_not_called()

    def test_status_is_allowed_after_stop_but_controls_and_speed_remain_blocked(self):
        self.admin_proposal()
        stopped = self.submit("stop")
        self.assertTrue(stopped["recovery_required"])
        self.assertIsNone(stopped["robot_controls"]["proposal"])
        state = self.submit("robot_status")
        self.assertEqual(state["robot_controls"]["status"], RUNNING)
        self.assertTrue(state["stop_requested"])
        self.assertTrue(self.recovery.required())
        for operation, fields in (("speed", {"speed": 50}),
                                  ("control_preview", {"action": "home", "arm": "left"})):
            with self.subTest(operation=operation), self.assertRaisesRegex(WebError, "latched"):
                self.request(operation, **fields)
        self.assertEqual(self.robot.jobs, [])
        self.robot.control_action.assert_not_called()

    def test_busy_controller_rejects_admin_status_and_speed_without_queue(self):
        entered, release = self.gates()
        self.fixture.capture_entered, self.fixture.capture_gate = entered, release
        self.request("scan")
        self.assertTrue(entered.wait(1))
        for operation, fields in (("robot_status", {}), ("speed", {"speed": 50}),
                                  ("control_preview", {"action": "home", "arm": "left"})):
            with self.subTest(operation=operation), self.assertRaisesRegex(WebError, "No extra action was queued"):
                self.request(operation, **fields)
        release.set()
        self.wait(lambda state: not state["busy"])
        self.assertEqual(self.session.speed, 250)
        self.robot.read_control_status.assert_not_called()
        self.assertEqual(self.robot.jobs, [])

    def test_stop_while_first_manual_home_waits_prevents_second_home(self):
        proposal = self.admin_proposal()
        entered, release = self.gates()

        def block_first_move(_):
            entered.set()
            self.assertTrue(release.wait(2))

        self.robot.after_move = block_first_move
        self.request("control_confirm", token=proposal["token"], acknowledged=True)
        self.assertTrue(entered.wait(1))
        self.request("stop")
        stopped = self.wait(lambda state: not state["stop_in_flight"])
        self.assertTrue(stopped["busy"])
        self.assertIsNone(stopped["robot_controls"]["proposal"])
        self.robot.request_stop.assert_called_once()
        release.set()
        final = self.wait(lambda state: not state["busy"])
        self.assertTrue(final["recovery_required"])
        self.assertFalse(final["scan_usable"])
        self.assertTrue(self.recovery.required())
        self.assertEqual([job["body"]["arm"] for job in self.robot.jobs], ["left"])

    def test_stop_during_admin_preview_prevents_late_proposal_publication(self):
        self.admin_proposal()
        saved = copy.deepcopy(self.controller.controls.pending)
        self.controller.controls.cancel()
        entered, release = self.gates()
        published = []

        def delayed_proposal(_action, _arm):
            entered.set()
            self.assertTrue(release.wait(2))
            self.controller.controls.pending = saved
            self.controller.publish_session()
            published.append(self.controller.state()["robot_controls"]["proposal"])
            return {"message": "Late proposal"}

        self.controller.controls.propose = delayed_proposal
        self.request("control_preview", action="home", arm="both")
        self.assertTrue(entered.wait(1))
        self.request("stop")
        self.wait(lambda state: not state["stop_in_flight"])
        release.set()
        final = self.wait(lambda state: not state["busy"])
        self.assertEqual(published, [None])
        self.assertIsNone(final["robot_controls"]["proposal"])
        self.assertIsNone(self.controller.controls.pending)
        self.assertEqual(self.robot.jobs, [])
        self.robot.control_action.assert_not_called()

    def test_admin_confirmation_expiration_and_cancellation_never_execute(self):
        proposal = self.admin_proposal("rapid_start", None)
        self.fixture.now += 60
        expired = self.submit("control_confirm", token=proposal["token"], acknowledged=True)
        self.assertIn("expired", expired["error"])
        proposal = self.admin_proposal("rapid_reset", None)
        self.submit("control_cancel")
        cancelled = self.submit("control_confirm", token=proposal["token"], acknowledged=True)
        self.assertIn("no longer matches", cancelled["error"])
        self.robot.control_action.assert_not_called()
        self.assertEqual(self.robot.jobs, [])


if __name__ == "__main__":
    unittest.main()
