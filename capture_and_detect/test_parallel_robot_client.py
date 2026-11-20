"""Parallel job scheduling with event-gated fake HTTP; no hardware or network."""

import copy
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import threading
import time
import unittest
from unittest.mock import Mock, patch

from handover_plan import handover_jobs
from robot_client import RobotClient, RobotCommandError
from test_gateway_capabilities import schema_fixture


def reply(payload):
    result = Mock()
    result.status_code = 200
    result.headers = {}
    result.json.return_value = payload
    return result


def move(name, arm, *, after=None):
    job = {"name": name, "endpoint": "/robot/rws/move", "body": {
        "arm": arm, "wait": True, "x": 450, "y": 100 if arm == "left" else -200,
        "z": 350, "rx": 180, "ry": 0, "rz": 180, "speed": 100,
        "acceleration": 100, "ramp": 100}}
    if after is not None:
        job["after"] = list(after)
    return job


class QuickRenewClient(RobotClient):
    def _record_lease(self, document, headers):
        super()._record_lease(document, headers)
        self._renew_after = .01


class ParallelRobotClientTests(unittest.TestCase):
    def setUp(self):
        self.started, self.gates, self.returned = {}, {}, {}
        self.prepared = {}
        self.active = {"left": 0, "right": 0}
        self.maximum = {"left": 0, "right": 0}
        self.controls, self.sent, self.completed, self.callback_threads = [], [], [], []
        self.trace = []
        self.responses, self.failures, self.errors = [], {}, []
        self.lock = threading.Lock()
        self.callback_lock = threading.Lock()
        self.renewed, self.released, self.finished = threading.Event(), threading.Event(), threading.Event()
        self.thread = None
        self.extra_before = None
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        for target, options in (
            ("robot_client.requests.post", {"side_effect": self.post}),
            ("robot_client.requests.get", {"side_effect": self.get}),
            ("socket.socket.connect", {"side_effect": AssertionError("Real network forbidden in scheduler tests")}),
        ):
            context = patch(target, **options)
            context.start()
            self.addCleanup(context.stop)
        self.addCleanup(self.unblock)

    def unblock(self):
        for gate in self.gates.values():
            gate.set()
        if self.thread is not None:
            self.thread.join(4)
            self.assertFalse(self.thread.is_alive(), "Executor did not drain its fake requests")

    def response(self, payload):
        value = reply(payload)
        with self.lock:
            self.responses.append(value)
        return value

    def post(self, url, **options):
        endpoint = url.removeprefix("http://offline.invalid/api/v1")
        if endpoint in ("/control/acquire", "/control/renew", "/control/release", "/robot/rapid/stop"):
            with self.lock:
                self.controls.append(endpoint)
                if endpoint == "/control/release":
                    self.assertEqual(self.active, {"left": 0, "right": 0}, "Lease released with HTTP jobs inflight")
                    self.released.set()
            if endpoint == "/robot/rapid/stop":
                return self.response({"execution_state": "stopped"})
            if endpoint == "/control/release":
                return self.response({"success": True, "lease": {"active": False}})
            if endpoint == "/control/renew":
                self.renewed.set()
            return self.response({"success": True, "lease": {"active": True,
                "client_id": "test",
                "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()}})
        self.assertIn(endpoint, {RobotClient.MOVE_ENDPOINT, RobotClient.PATH_ENDPOINT,
                                 RobotClient.HOME_ENDPOINT, RobotClient.GRIPPER_ENDPOINT})
        self.assertNotIn("X-Control-Session", options["headers"])
        arm = options["json"]["arm"]
        with self.lock:
            name = self.prepared[arm]
            self.active[arm] += 1
            self.maximum[arm] = max(self.maximum[arm], self.active[arm])
            self.sent.append(name)
            self.assertEqual(self.active[arm], 1, "Two HTTP jobs dispatched to the same arm")
        self.started[name].set()
        try:
            if not self.gates[name].wait(3):
                raise AssertionError(f"Timed out waiting to finish fake job {name}")
            return self.response(self.failures.get(name, {"accepted": True, "message": "DONE"}))
        finally:
            with self.lock:
                self.active[arm] -= 1
            self.returned[name].set()

    def get(self, url, **options):
        if url.endswith("/openapi.json"):
            result = self.response(schema_fixture(blended_path=True))
            result.content = b"fixture"
            return result
        arm = url.split("/")[-2]
        self.assertIn(arm, ("left", "right"))
        self.assertTrue(url.endswith(f"/robot/arms/{arm}/joints"))
        return self.response({"arm": arm, "joints": [0, -130, 30, 0, 40, 0],
                              "arm_angle": 135 if arm == "left" else -135})

    def before(self, index, job):
        self.assertTrue(self.callback_lock.acquire(blocking=False), "Callbacks ran concurrently")
        try:
            self.callback_threads.append(threading.get_ident())
            self.trace.append(("before", job["name"]))
            self.prepared[job["body"]["arm"]] = job["name"]
            if self.extra_before is not None:
                return self.extra_before(index, job)
        finally:
            self.callback_lock.release()

    def after(self, index, job):
        self.assertTrue(self.callback_lock.acquire(blocking=False), "Callbacks ran concurrently")
        try:
            self.callback_threads.append(threading.get_ident())
            self.trace.append(("after", job["name"]))
            self.completed.append(job["name"])
        finally:
            self.callback_lock.release()

    def launch(self, jobs, *, blocked=(), client_type=RobotClient):
        for job in jobs:
            name = job["name"]
            self.started[name], self.gates[name], self.returned[name] = (threading.Event() for _ in range(3))
            if name not in blocked:
                self.gates[name].set()
        self.client = client_type("http://offline.invalid/api/v1", {})

        def execute():
            try:
                self.client.execute_jobs(jobs, before_send=self.before, after_done=self.after)
            except BaseException as error:
                self.errors.append(error)
            finally:
                self.finished.set()

        self.thread = threading.Thread(target=execute, daemon=True, name="test-job-coordinator")
        self.thread.start()

    def wait_started(self, name):
        self.assertTrue(self.started[name].wait(2), f"Job {name} did not start; errors={self.errors!r}")

    def finish(self, *, error=False):
        self.assertTrue(self.finished.wait(3), "Coordinator did not finish")
        self.thread.join(1)
        self.assertFalse(self.thread.is_alive())
        if error:
            self.assertEqual(len(self.errors), 1)
            self.assertIsInstance(self.errors[0], RobotCommandError)
        else:
            self.assertEqual(self.errors, [])
        self.assertTrue(all(ident == self.thread.ident for ident in self.callback_threads))
        self.assertTrue(all(count <= 1 for count in self.maximum.values()))

    @staticmethod
    def plan(**settings):
        return handover_jobs({"x": 500, "y": 100, "z": 50, "selectable": True},
                             place={"id": 1, "x": 450, "y": -260, "z": 230, "occupied": False},
                             **settings)

    def test_handover_approach_overlaps_but_right_open_waits_for_left(self):
        self.launch(self.plan(), blocked=("left_handover", "right_beside"), client_type=QuickRenewClient)
        self.wait_started("left_handover")
        self.wait_started("right_beside")
        self.assertTrue(self.renewed.wait(1), "Shared lease was not renewed during simultaneous moves")
        self.assertFalse(self.started["right_open"].is_set())
        self.gates["right_beside"].set()
        self.assertTrue(self.returned["right_beside"].wait(1))
        self.assertFalse(self.started["right_open"].wait(.03))
        self.gates["left_handover"].set()
        self.finish()
        self.assertLess(self.completed.index("right_beside"), self.completed.index("left_handover"))
        self.assertLess(self.trace.index(("after", "left_handover")), self.trace.index(("before", "right_open")))
        self.assertEqual(self.controls.count("/control/acquire"), 1)
        self.assertEqual(self.controls.count("/control/release"), 1)

    def test_executor_keeps_lease_and_waits_for_left_park_after_right_clear(self):
        jobs = self.plan()
        right_reported = threading.Event()
        record_completion = self.after

        def after(index, job):
            record_completion(index, job)
            if job["name"] == "right_clear":
                right_reported.set()

        self.after = after
        self.launch(jobs, blocked=("left_park",), client_type=QuickRenewClient)
        self.wait_started("left_park")
        self.wait_started("right_clear")
        self.assertTrue(self.returned["right_clear"].wait(1))
        self.assertTrue(right_reported.wait(1))
        self.assertTrue(self.renewed.wait(1))
        self.assertFalse(self.finished.is_set(), "Caller returned while left parking was still in flight")
        self.assertFalse(self.released.is_set(), "Control lease released before both arms finished")
        self.assertNotIn("left_park", self.completed)

        self.gates["left_park"].set()
        self.finish()
        self.assertEqual(set(self.completed), {job["name"] for job in jobs})
        self.assertEqual(self.completed[-1], "left_park")
        self.assertTrue(self.released.is_set())
        self.assertEqual(self.controls.count("/control/acquire"), 1)
        self.assertEqual(self.controls.count("/control/release"), 1)

    def test_path_completion_is_the_handover_barrier(self):
        def path(name, arm):
            job = move(name, arm, after=[])
            body = job["body"]
            point = {name: body.pop(name) for name in ("x", "y", "z", "rx", "ry", "rz")}
            body["points"] = [{**point, "linear": True, "zone": 5}, {**point, "zone": 0}]
            job["endpoint"] = RobotClient.PATH_ENDPOINT
            return job

        jobs = [path("left_path", "left"), path("right_path", "right"),
                {"name": "right_grip", "endpoint": RobotClient.GRIPPER_ENDPOINT,
                 "body": {"arm": "right", "wait": True, "action": "grip"},
                 "after": ["left_path", "right_path"]}]
        self.launch(jobs, blocked=("left_path", "right_path"))
        self.wait_started("left_path")
        self.wait_started("right_path")
        self.assertEqual(set(self.sent), {"left_path", "right_path"})
        self.gates["right_path"].set()
        self.assertTrue(self.returned["right_path"].wait(1))
        self.assertFalse(self.started["right_grip"].wait(.03))
        self.gates["left_path"].set()
        self.finish()
        self.assertLess(self.trace.index(("after", "left_path")), self.trace.index(("before", "right_grip")))
        self.assertEqual(self.sent.count("left_path"), 1)
        self.assertEqual(self.sent.count("right_path"), 1)

    def test_smooth_separation_overlaps_and_turn_waits_for_both_arms(self):
        self.launch(self.plan(), blocked=("left_clear", "right_lower"))
        self.wait_started("left_clear")
        self.wait_started("right_lower")
        self.assertIn("left_release", self.completed)
        self.assertFalse(self.started["right_retreat"].is_set())
        self.gates["right_lower"].set()
        self.assertTrue(self.returned["right_lower"].wait(1))
        self.assertFalse(self.started["right_retreat"].wait(.03))
        self.gates["left_clear"].set()
        self.finish()
        self.assertLess(self.trace.index(("after", "left_clear")),
                        self.trace.index(("before", "right_retreat")))

    def test_failed_separation_path_blocks_turn_and_placement_without_retry(self):
        self.failures["left_clear"] = {"accepted": False, "message": "ERR move failed"}
        self.launch(self.plan(), blocked=("left_clear", "right_lower"))
        self.wait_started("left_clear")
        self.wait_started("right_lower")
        self.gates["left_clear"].set()
        self.assertTrue(self.returned["left_clear"].wait(1))
        self.assertFalse(self.finished.wait(.03))
        self.gates["right_lower"].set()
        self.finish(error=True)
        self.assertFalse(self.started["right_retreat"].is_set())
        self.assertFalse(self.started["right_place"].is_set())
        self.assertEqual(self.sent.count("left_clear"), 1)

    def test_left_home_inflight_does_not_block_right_placement(self):
        self.launch(self.plan(home_after_cup=True), blocked=("left_home", "right_place"))
        self.wait_started("left_home")
        self.wait_started("right_place")
        self.assertFalse(self.returned["left_home"].is_set())
        self.gates["right_place"].set()
        self.wait_started("right_home")
        self.assertFalse(self.released.is_set())
        self.gates["left_home"].set()
        self.finish()

    def test_failure_prevents_pending_work_and_drains_other_arm_before_release(self):
        jobs = [move("left_first", "left", after=[]), move("right_first", "right", after=[]),
                move("left_later", "left", after=[]), move("right_later", "right", after=["left_first"])]
        self.failures["left_first"] = {"accepted": False, "message": "ERR move failed"}
        self.launch(jobs, blocked=("left_first", "right_first"))
        self.wait_started("left_first")
        self.wait_started("right_first")
        self.gates["left_first"].set()
        self.assertTrue(self.returned["left_first"].wait(1))
        self.assertFalse(self.finished.wait(.03))
        self.assertFalse(self.released.is_set())
        self.gates["right_first"].set()
        self.finish(error=True)
        self.assertEqual(set(self.sent), {"left_first", "right_first"})
        self.assertEqual(self.controls[-1], "/control/release")

    def test_stop_latch_blocks_later_dispatch_while_inflight_requests_unwind(self):
        jobs = [move("left_first", "left", after=[]), move("right_first", "right", after=[]),
                move("left_later", "left", after=[]), move("right_later", "right", after=[])]
        self.launch(jobs, blocked=("left_first", "right_first"))
        self.wait_started("left_first")
        self.wait_started("right_first")
        self.client.latch_stop()
        self.gates["left_first"].set()
        self.gates["right_first"].set()
        self.finish(error=True)
        self.assertEqual(set(self.sent), {"left_first", "right_first"})
        self.client.request_stop()
        self.assertEqual(self.controls.count("/control/acquire"), 1)
        self.assertEqual(self.controls[-2:], ["/robot/rapid/stop", "/control/release"])

    def test_expired_lease_blocks_later_dispatch_and_drains_inflight_jobs(self):
        jobs = [move("left_first", "left", after=[]), move("right_first", "right", after=[]),
                move("left_later", "left", after=[]), move("right_later", "right", after=[])]
        self.launch(jobs, blocked=("left_first", "right_first"))
        self.wait_started("left_first")
        self.wait_started("right_first")
        self.client._lease_deadline = time.monotonic() - 1
        self.gates["left_first"].set()
        self.assertTrue(self.returned["left_first"].wait(1))
        self.assertFalse(self.released.is_set())
        self.gates["right_first"].set()
        self.finish(error=True)
        self.assertIn("lease expired", str(self.errors[0]))
        self.assertEqual(set(self.sent), {"left_first", "right_first"})

    def test_jobs_without_dependencies_keep_the_legacy_serial_order(self):
        self.launch([move("left_first", "left"), move("right_second", "right")], blocked=("left_first",))
        self.wait_started("left_first")
        self.assertFalse(self.started["right_second"].wait(.03))
        self.gates["left_first"].set()
        self.finish()
        self.assertEqual(self.completed, ["left_first", "right_second"])

    def test_invalid_dags_are_rejected_before_any_http(self):
        cases = {
            "duplicate names": [move("a", "left", after=[]), move("a", "right", after=[])],
            "unknown prerequisite": [move("a", "left", after=["missing"])],
            "explicit cycle": [move("a", "left", after=["b"]), move("b", "right", after=["a"])],
            "implicit same-arm cycle": [move("a", "left", after=["b"]), move("b", "left", after=[])],
            "self dependency": [move("a", "left", after=["a"])],
        }
        client = RobotClient("http://offline.invalid/api/v1", {})
        for name, jobs in cases.items():
            with self.subTest(name=name), self.assertRaises(RobotCommandError):
                client.execute_jobs(jobs)
        self.assertEqual(self.controls, [])
        self.assertEqual(self.sent, [])

    def test_dependency_metadata_is_validated_and_copied(self):
        job = move("next", "left", after=["previous"])
        validated = RobotClient._validate_job(job)
        copied = RobotClient._copy_job(validated)
        job["after"].append("external mutation")
        copied["after"].append("callback mutation")
        self.assertEqual(validated["after"], ["previous"])
        for invalid in (None, "previous", ("previous",), [False], [1], [""]):
            bad = copy.deepcopy(job)
            bad["after"] = invalid
            with self.subTest(after=invalid), self.assertRaises(RobotCommandError):
                RobotClient._validate_job(bad)


if __name__ == "__main__":
    unittest.main()
