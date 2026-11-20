"""Run the terminal's actual arm coordination against an in-memory gateway."""

from collections import defaultdict
import copy
import threading
import unittest
from unittest.mock import Mock, patch

from handover_plan import handover_jobs, pick_tcp_z
from test_v4_terminal import load_v4_terminal


CUP = {"id": 1, "x": 510.0, "y": 80.0, "z": 33.7,
       "rz": 142.5, "aperture": 91.6, "status": "moving"}
PLACE = {"id": 1, "x": 420.0, "y": -310.0, "z": 180.0, "occupied": False}


class PlannedGateway:
    """Match real requests to the shared plan, optionally holding them in flight."""

    def __init__(self, jobs, *, hold=(), fail=None):
        self.jobs = defaultdict(list)
        for job in jobs:
            self.jobs[job["body"]["arm"]].append(job)
        self.lock = threading.Lock()
        self.indices = defaultdict(int)
        self.calls = []
        self.completed = set()
        self.errors = []
        self.started = {job["name"]: threading.Event() for job in jobs}
        self.finished = {job["name"]: threading.Event() for job in jobs}
        self.gates = {name: threading.Event() for name in hold}
        self.fail = fail

    def robot_job(self, arm, path, body):
        with self.lock:
            index = self.indices[arm]
            self.indices[arm] += 1
            if index >= len(self.jobs[arm]):
                self.errors.append(f"Unexpected additional {arm} request: {path}")
                return False
            expected = self.jobs[arm][index]
            name = expected["name"]
            actual = {"arm": arm, **copy.deepcopy(body)}
            self.calls.append((name, path, actual))
            if path != expected["endpoint"].rsplit("/", 1)[-1] or actual != expected["body"]:
                self.errors.append(f"{name}: expected {expected}, got {path} {actual}")
            pending = set(expected.get("after", ())) - self.completed
            if pending:
                self.errors.append(f"{name} started before {sorted(pending)} completed")
            self.started[name].set()
        if name in self.gates and not self.gates[name].wait(5):
            self.errors.append(f"Test did not release {name}")
            return False
        with self.lock:
            if name != self.fail:
                self.completed.add(name)
            self.finished[name].set()
        return name != self.fail

    def release_all(self):
        for gate in self.gates.values():
            gate.set()


class TerminalSmoothHandoverTests(unittest.TestCase):
    def setUp(self):
        self.terminal = load_v4_terminal()
        self.addCleanup(patch.stopall)
        # Most fixtures compare the fixed-height plan explicitly; individual
        # camera-height cases override this independently of launcher defaults.
        patch.object(self.terminal, "USE_CAMERA_Z", False).start()
        patch.object(self.terminal, "log").start()
        patch.object(self.terminal.requests, "get",
                     side_effect=AssertionError("Unexpected network request")).start()
        patch.object(self.terminal.requests, "post",
                     side_effect=AssertionError("Unexpected network request")).start()

    def handover(self, gateway, *, offset=(0, 0, 0)):
        abort = threading.Event()
        left = self.terminal.Arm(gateway, "left", abort, offset)
        right = self.terminal.Arm(gateway, "right", abort)
        left.grip_level = Mock(return_value=6.2)
        right.grip_level = Mock(return_value=3.1)
        return self.terminal.Handover(left, right, abort)

    def background_run(self, handover, cup):
        outcome = {}

        def run():
            try:
                outcome["result"] = handover.run(cup, place=PLACE)
            except BaseException as error:
                outcome["error"] = error

        worker = threading.Thread(target=run, name="terminal-controller-test", daemon=True)
        worker.start()

        def cleanup():
            handover.abort.set()
            handover.left.gateway.release_all()
            worker.join(2)

        self.addCleanup(cleanup)
        return worker, outcome

    def assert_started(self, gateway, name):
        self.assertTrue(gateway.started[name].wait(2), f"{name} did not start: {gateway.errors}")

    def assert_finished(self, gateway, name):
        self.assertTrue(gateway.finished[name].wait(2), f"{name} did not finish: {gateway.errors}")

    def test_terminal_sends_the_same_smooth_plan_as_ui_and_records_grip_levels(self):
        cases = [
            ((0, 0, 0), False, CUP),
            ((11.2, -8.4, 50), True, {**CUP, "z": 45.6, "rz": 201.2}),
            ((-7, 12, -30), True, {**CUP, "z": -20, "aperture": None}),
        ]
        for offset, camera_z, values in cases:
            with self.subTest(offset=offset, camera_z=camera_z, cup=values):
                cup = copy.deepcopy(values)
                jobs = handover_jobs(cup, place=PLACE, tag_offset=offset, camera_z=camera_z)
                gateway = PlannedGateway(jobs)
                handover = self.handover(gateway, offset=offset)
                with patch.object(self.terminal, "USE_CAMERA_Z", camera_z):
                    self.assertTrue(handover.run(cup, place=PLACE))

                self.assertEqual(gateway.errors, [])
                self.assertEqual(gateway.completed, {job["name"] for job in jobs})
                self.assertEqual(len(gateway.calls), len(jobs))
                self.assertEqual({path for _, path, _ in gateway.calls}, {"path", "gripper"})
                self.assertTrue(all(body["wait"] is True for _, _, body in gateway.calls))
                self.assertEqual(cup["pick_z"], pick_tcp_z(cup, camera_z=camera_z))
                self.assertEqual(cup["grip_left_mm"], 6.2)
                self.assertEqual(cup["grip_right_mm"], 3.1)
                handover.left.grip_level.assert_called_once_with()
                handover.right.grip_level.assert_called_once_with()
                self.assertEqual(handover.left.last, ((350, 400, 690), (180, 0, 180)))
                self.assertEqual(handover.right.last, ((420.0, -310.0, 260.0), (180, 0, 0)))

    def test_repeated_cups_never_home_or_add_legacy_moves(self):
        cup = copy.deepcopy(CUP)
        jobs = handover_jobs(cup, place=PLACE)
        gateway = PlannedGateway(jobs)
        handover = self.handover(gateway)
        for index in range(3):
            with self.subTest(cup=index):
                gateway = PlannedGateway(jobs)
                handover.left.gateway = handover.right.gateway = gateway
                self.assertTrue(handover.run(cup, place=PLACE))
                self.assertEqual(gateway.errors, [])
                self.assertEqual(len(gateway.calls), len(jobs))
                self.assertNotIn("home", [path for _, path, _ in gateway.calls])
                self.assertNotIn("move", [path for _, path, _ in gateway.calls])

    def test_two_arms_overlap_only_after_the_required_handover_barriers(self):
        cup = copy.deepcopy(CUP)
        jobs = handover_jobs(cup, place=PLACE)
        gateway = PlannedGateway(jobs, hold=("approach_cup", "right_via", "left_handover",
                                             "right_beside", "right_grip", "left_clear", "right_lower"))
        handover = self.handover(gateway)
        worker, outcome = self.background_run(handover, cup)

        self.assert_started(gateway, "approach_cup")
        self.assert_started(gateway, "right_via")
        gateway.gates["approach_cup"].set()
        self.assert_finished(gateway, "grip_cup")
        self.assertFalse(gateway.started["left_handover"].is_set())
        self.assertFalse(gateway.started["right_beside"].is_set())

        gateway.gates["right_via"].set()
        self.assert_started(gateway, "left_handover")
        self.assert_started(gateway, "right_beside")
        gateway.gates["right_beside"].set()
        self.assert_finished(gateway, "right_beside")
        self.assertFalse(gateway.started["right_open"].is_set())

        gateway.gates["left_handover"].set()
        self.assert_started(gateway, "right_grip")
        self.assertFalse(gateway.started["left_release"].is_set())
        gateway.gates["right_grip"].set()
        self.assert_started(gateway, "left_clear")
        self.assert_started(gateway, "right_lower")
        gateway.gates["right_lower"].set()
        self.assert_finished(gateway, "right_lower")
        self.assertFalse(gateway.started["right_retreat"].is_set())

        gateway.gates["left_clear"].set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, {"result": True})
        self.assertEqual(gateway.errors, [])

    def test_failed_path_aborts_dependents_and_drains_the_other_arm_without_fallback(self):
        cup = copy.deepcopy(CUP)
        gateway = PlannedGateway(handover_jobs(cup, place=PLACE),
                                 hold=("left_handover", "right_beside"), fail="left_handover")
        handover = self.handover(gateway)
        worker, outcome = self.background_run(handover, cup)
        self.assert_started(gateway, "left_handover")
        self.assert_started(gateway, "right_beside")
        gateway.gates["left_handover"].set()
        self.assertTrue(handover.abort.wait(2))
        self.assertTrue(worker.is_alive(), "Lease-owning caller must wait for the in-flight right arm")
        self.assertFalse(gateway.started["right_open"].is_set())
        gateway.gates["right_beside"].set()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, {"result": False})
        self.assertEqual(gateway.errors, [])
        self.assertEqual([name for name, _, _ in gateway.calls].count("left_handover"), 1)
        self.assertFalse(gateway.started["left_release"].is_set())
        self.assertFalse(gateway.started["right_take"].is_set())
        self.assertFalse(gateway.started["right_retreat"].is_set())
        self.assertNotIn("move", [path for _, path, _ in gateway.calls])
        self.assertNotIn("home", [path for _, path, _ in gateway.calls])
        self.assertEqual(cup["grip_left_mm"], 6.2)
        self.assertIsNone(cup["grip_right_mm"])

    def test_unknown_optional_gripper_telemetry_does_not_change_the_motion(self):
        cup = copy.deepcopy(CUP)
        gateway = PlannedGateway(handover_jobs(cup, place=PLACE))
        handover = self.handover(gateway)
        handover.left.grip_level.return_value = None
        handover.right.grip_level.return_value = None
        self.assertTrue(handover.run(cup, place=PLACE))
        self.assertEqual(gateway.errors, [])
        self.assertIsNone(cup["grip_left_mm"])
        self.assertIsNone(cup["grip_right_mm"])

    def test_ctrl_c_waits_for_both_in_flight_requests_before_releasing_the_caller(self):
        cup = copy.deepcopy(CUP)
        gateway = PlannedGateway(handover_jobs(cup, place=PLACE),
                                 hold=("left_handover", "right_beside"))
        handover = self.handover(gateway)
        interrupted = threading.Event()
        real_event = threading.Event

        class InterruptControllerOnce(real_event):
            def wait(event, timeout=None):
                if (threading.current_thread().name == "terminal-controller-test"
                        and timeout is not None and not interrupted.is_set()):
                    # Raise at the coordination wait only after both actual
                    # gateway calls are in flight; never interrupt a worker.
                    if (gateway.started["left_handover"].wait(2)
                            and gateway.started["right_beside"].wait(2)):
                        interrupted.set()
                        raise KeyboardInterrupt
                return super().wait(timeout)

        with patch.object(self.terminal.threading, "Event", InterruptControllerOnce):
            worker, outcome = self.background_run(handover, cup)
            self.assertTrue(handover.abort.wait(3), "Controller did not handle the injected Ctrl+C")
            self.assertTrue(interrupted.is_set())
            self.assertTrue(worker.is_alive())
            self.assertEqual(outcome, {})
            gateway.gates["left_handover"].set()
            self.assert_finished(gateway, "left_handover")
            self.assertTrue(worker.is_alive(), "The right arm is still in flight")
            gateway.gates["right_beside"].set()
            worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertIsInstance(outcome.get("error"), KeyboardInterrupt)
        self.assertEqual(gateway.errors, [])
        self.assertFalse(gateway.started["right_open"].is_set())
        self.assertFalse(gateway.started["left_release"].is_set())

    def test_gateway_requires_accepted_and_exact_done_without_retrying(self):
        gateway = self.terminal.Gateway("http://offline.invalid:8000", "test-token")
        cases = [
            (200, {"accepted": True, "message": "DONE"}, True),
            (200, {"accepted": False, "message": "DONE"}, False),
            (200, {"message": "DONE"}, False),
            (200, {"accepted": 1, "message": "DONE"}, False),
            (200, {"accepted": True, "message": "QUEUED"}, False),
            (200, {"accepted": True, "message": "done"}, False),
            (409, {"accepted": True, "message": "DONE"}, False),
            (504, {}, False),
        ]
        body = {"wait": True, "points": [{"z": 334}]}
        for status, result, expected in cases:
            with self.subTest(status=status, result=result):
                response = Mock(status_code=status, text="offline response")
                response.json.return_value = result
                with patch.object(gateway, "post", return_value=response) as post:
                    self.assertIs(gateway.robot_job("left", "path", body), expected)
                    post.assert_called_once_with("/robot/rws/path", timeout=self.terminal.JOB_TIMEOUT,
                                                 body={"arm": "left", **body})

        with patch.object(gateway, "post", side_effect=self.terminal.requests.Timeout("offline")) as post:
            self.assertFalse(gateway.robot_job("left", "path", body))
            self.assertEqual(post.call_count, 1)

    def test_path_height_exception_only_applies_to_final_linear_pick_or_place(self):
        jobs = {job["name"]: job for job in handover_jobs(CUP, place=PLACE)}
        for name in ("approach_cup", "right_place"):
            with self.subTest(allowed=name):
                job = copy.deepcopy(jobs[name])
                job["body"]["points"][-1]["z"] = 180
                gateway = Mock()
                gateway.robot_job.return_value = True
                arm = self.terminal.Arm(gateway, job["body"]["arm"], threading.Event())
                arm.run_plan_job(job)
                gateway.robot_job.assert_called_once()

        invalid = []
        transit = copy.deepcopy(jobs["approach_cup"])
        transit["body"]["points"][0]["z"] = 180
        invalid.append(transit)
        joint_pick = copy.deepcopy(jobs["approach_cup"])
        joint_pick["body"]["points"][-1].update(z=180, linear=False)
        invalid.append(joint_pick)
        high_place = copy.deepcopy(jobs["right_place"])
        high_place["body"]["points"][-1]["z"] = 701
        invalid.append(high_place)
        low_clear = copy.deepcopy(jobs["right_clear"])
        low_clear["body"]["points"][-1]["z"] = 180
        invalid.append(low_clear)
        for job in invalid:
            with self.subTest(refused=job):
                gateway = Mock()
                arm = self.terminal.Arm(gateway, job["body"]["arm"], threading.Event())
                with self.assertRaises(self.terminal.RobotError):
                    arm.run_plan_job(job)
                gateway.robot_job.assert_not_called()
                self.assertIsNone(arm.last)

    def test_left_only_keeps_original_individual_moves_and_travel_height(self):
        cup = copy.deepcopy(CUP)
        gateway = Mock()
        gateway.robot_job.return_value = True
        handover = self.handover(gateway, offset=(10, -20, 80))
        self.assertTrue(handover.run(cup, left_only=True))

        def move(x, y, z, rz, speed=250):
            return ("left", "move", {"x": x, "y": y, "z": z, "rx": 180, "ry": 0, "rz": rz,
                                      "speed": speed, "acceleration": 100, "ramp": 100})

        self.assertEqual([call.args for call in gateway.robot_job.call_args_list], [
            ("left", "gripper", {"action": "grip"}),
            move(400, 100, 450, 180),
            move(520.0, 60.0, 409, 142.5),
            ("left", "gripper", {"action": "move", "position": 8.3}),
            move(520.0, 60.0, 229, 142.5, 100),
            ("left", "gripper", {"action": "grip"}),
            move(520.0, 60.0, 409, 142.5, 100),
            move(520.0, 60.0, 229, 142.5),
            ("left", "gripper", {"action": "open"}),
            move(520.0, 60.0, 409, 142.5),
        ])
        self.assertEqual(cup["pick_z"], 70)
        self.assertEqual(cup["grip_left_mm"], 6.2)
        self.assertIsNone(cup["grip_right_mm"])
        handover.right.grip_level.assert_not_called()


if __name__ == "__main__":
    unittest.main()
