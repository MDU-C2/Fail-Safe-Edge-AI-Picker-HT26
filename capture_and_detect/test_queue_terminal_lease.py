"""Exercise queue-terminal ownership without importing YOLO or contacting hardware."""

import ast
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import threading
import types
import unittest
from unittest.mock import Mock

import requests


SOURCE = Path(__file__).with_name("capture_and_detect_que.py")


class DeferredThread:
    """Run renewal deterministically when a test asks, with no sleeping thread."""

    def __init__(self, *, target, daemon):
        self.target = target
        self.join = Mock()

    def start(self):
        pass


class Reply:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.body = {} if body is None else body
        self.text = "stub response"

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


def load_terminal():
    """Execute the real ownership/workflow classes with in-memory boundaries."""
    clock = types.SimpleNamespace(now=100.0)
    http = types.SimpleNamespace(
        RequestException=requests.RequestException, HTTPError=requests.HTTPError,
        get=Mock(side_effect=AssertionError("Unexpected network access")),
        post=Mock(side_effect=AssertionError("Unexpected network access")),
        delete=Mock(side_effect=AssertionError("Unexpected network access")),
    )
    namespace = {
        "__file__": str(SOURCE), "Path": Path, "contextmanager": contextmanager,
        "datetime": datetime, "requests": http,
        "threading": types.SimpleNamespace(Event=threading.Event, RLock=threading.RLock,
                                          Thread=DeferredThread),
        "time": types.SimpleNamespace(monotonic=lambda: clock.now,
                                      perf_counter=lambda: clock.now, sleep=Mock()),
        "depth_average": types.SimpleNamespace(capture_averaged=Mock()),
        "log": Mock(), "print_objects": Mock(), "find_cups": Mock(),
        "load_places": Mock(), "save_places": Mock(), "handover_jobs": Mock(),
    }
    wanted = {"Gateway", "RobotError", "Picker", "Handover", "plan_to_queue_steps"}
    nodes = [node for node in ast.parse(SOURCE.read_text()).body
             if isinstance(node, ast.Assign)
             or isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in wanted]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return types.SimpleNamespace(**namespace), namespace, clock


class QueueTerminalLeaseTests(unittest.TestCase):
    def setUp(self):
        self.module, self.namespace, self.clock = load_terminal()
        self.gateway = self.module.Gateway("http://gateway.invalid", "test-placeholder")
        self.calls = []

        def post(path, timeout, body=None):
            self.calls.append(path)
            if path == "/robot/rws/queue":
                return Reply(202, {"state": "running"})
            return Reply(200, {"lease": {"client_id": "primary"},
                               "accepted": True, "message": "DONE"})

        self.post = post
        self.gateway.post = Mock(side_effect=post)

    def renew_once(self):
        self.gateway._renew_stop.wait = Mock(side_effect=[False, True])
        self.gateway._renew_thread.target()

    def picker(self, *, capture_failure=None):
        cups = [{"id": number, "status": "waiting", "x": 400, "y": 100,
                 "z": 40, "diameter": 70} for number in (1, 2)]
        places = {"locations": [dict(id=number, x=350, y=-260, z=230, occupied=False)
                                for number in (1, 2)]}
        self.module.find_cups.return_value = cups
        self.module.load_places.return_value = places
        trace = []

        def capture(*args):
            self.gateway.check_control()
            self.assertNotIn("/control/release", self.calls)
            trace.append("capture")
            if capture_failure:
                raise capture_failure
            return "rgb", "depth", ["depth"]

        self.module.depth_average.capture_averaged.side_effect = capture
        detector = Mock(K={})
        detector.analyse.return_value = ([], "annotated")
        store = Mock(folder="unused")
        left = Mock(name="left_arm", gateway=self.gateway, offset=(0, 0, 0))
        right = Mock(name="right_arm", gateway=self.gateway)

        def home(arm):
            self.gateway.check_control()
            self.assertNotIn("/control/release", self.calls)
            trace.append(arm + " home")

        left.home.side_effect = lambda: home("left")
        right.home.side_effect = lambda: home("right")
        handover = self.module.Handover(left, right, threading.Event())
        # The exact movement geometry belongs to handover_plan's own tests.
        self.module.handover_jobs.return_value = [
            {"name": "left_open", "endpoint": "/robot/rws/gripper",
             "body": {"arm": "left", "action": "open", "wait": True}, "after": []},
            {"name": "right_open", "endpoint": "/robot/rws/gripper",
             "body": {"arm": "right", "action": "open", "wait": True}, "after": []},
        ]
        self.module.requests.get.return_value = Reply(body={"state": "done", "steps": []})
        self.module.requests.get.side_effect = None
        picker = self.module.Picker(self.gateway, detector, handover, store)
        return picker, cups, places, trace

    def test_primary_holds_one_lease_from_capture_through_both_cups_and_home(self):
        picker, cups, places, trace = self.picker()
        self.assertEqual(picker.capture_once(), cups)
        self.assertEqual(self.calls, ["/control/acquire", "/robot/rws/queue",
                                     "/robot/rws/queue", "/control/release"])
        self.assertEqual(trace, ["capture", "left home", "right home"])
        self.assertEqual([cup["status"] for cup in cups], ["placed", "placed"])
        self.assertTrue(all(place["occupied"] for place in places["locations"]))
        self.assertFalse(self.gateway.holding)
        self.assertEqual(self.gateway._control_depth, 0)
        self.assertIsNone(self.gateway._renew_thread)

    def test_nested_scopes_do_not_acquire_release_or_set_standby_holding(self):
        with self.gateway.control():
            self.assertFalse(self.gateway.holding)
            thread = self.gateway._renew_thread
            with self.gateway.control():
                with self.gateway.control():
                    self.assertIs(self.gateway._renew_thread, thread)
                    self.assertEqual(self.calls, ["/control/acquire"])
            self.assertEqual(self.calls, ["/control/acquire"])
        self.assertEqual(self.calls, ["/control/acquire", "/control/release"])
        thread.join.assert_called_once_with(timeout=6)

    def test_standby_reuses_persistent_lease_across_capture_and_final_home(self):
        self.assertTrue(self.gateway.try_acquire()[0])
        self.gateway.hold()
        renewal = self.gateway._renew_thread
        picker, _, _, _ = self.picker()
        picker.capture_once()
        self.assertTrue(self.gateway.holding)
        self.assertEqual(self.gateway._control_depth, 0)
        self.assertIs(self.gateway._renew_thread, renewal)
        self.assertEqual(self.calls, ["/control/acquire", "/robot/rws/queue", "/robot/rws/queue"])
        self.gateway.drop()
        self.assertEqual(self.calls[-1], "/control/release")
        self.assertFalse(self.gateway.holding)

    def test_capture_failure_releases_scoped_lease_without_commands(self):
        picker, _, _, _ = self.picker(capture_failure=OSError("camera disconnected"))
        with self.assertRaisesRegex(OSError, "camera disconnected"):
            picker.capture_once()
        self.assertEqual(self.calls, ["/control/acquire", "/control/release"])
        self.assertEqual(self.gateway._control_depth, 0)

    def test_failed_renewal_latches_and_blocks_all_new_mutations(self):
        for failure in (Reply(409), requests.Timeout("renew timed out")):
            for standby in (False, True):
                with self.subTest(failure=type(failure).__name__, standby=standby):
                    self.setUp()
                    if standby:
                        self.gateway.try_acquire()
                        self.gateway.hold()
                    with self.gateway.control():
                        def post(path, timeout, body=None):
                            if path == "/control/renew":
                                self.calls.append(path)
                                if isinstance(failure, Exception):
                                    raise failure
                                return failure
                            return self.post(path, timeout, body)

                        self.gateway.post.side_effect = post
                        self.renew_once()
                        self.assertTrue(self.gateway.lost.is_set())
                        calls = list(self.calls)
                        self.assertFalse(self.gateway.queue_submit([])[0])
                        with self.assertRaises(self.module.RobotError):
                            self.gateway.robot_job("left", "home", {})
                        self.assertIn("no new commands", self.gateway.queue_clear())
                        with self.assertRaises(self.module.RobotError):
                            with self.gateway.control():
                                self.fail("A lost nested scope must not execute")
                        self.assertEqual(self.calls, calls)
                    if standby:
                        self.gateway.drop()
                    self.assertNotIn("/control/release", self.calls)
                    self.module.requests.delete.assert_not_called()

    def test_expired_local_lease_blocks_submission_while_renewal_is_unconfirmed(self):
        with self.gateway.control():
            self.clock.now += 5.0
            self.assertFalse(self.gateway.queue_submit([])[0])
            self.assertTrue(self.gateway.lost.is_set())
        self.assertEqual(self.calls, ["/control/acquire"])

    def test_successful_renewal_extends_from_request_start_not_delayed_reply(self):
        with self.gateway.control():
            self.clock.now = 103.0

            def delayed_post(path, timeout, body=None):
                reply = self.post(path, timeout, body)
                if path == "/control/renew":
                    self.clock.now = 104.0
                return reply

            self.gateway.post.side_effect = delayed_post
            self.renew_once()
            self.assertEqual(self.gateway._lease_deadline, 108.0)
            self.assertTrue(self.gateway.queue_submit([])[0])
        self.assertEqual(self.calls, ["/control/acquire", "/control/renew",
                                     "/robot/rws/queue", "/control/release"])

    def test_late_successful_renewal_cannot_resume_a_lost_owner(self):
        with self.gateway.control():
            deadline = self.gateway._lease_deadline

            def late_post(path, timeout, body=None):
                reply = self.post(path, timeout, body)
                if path == "/control/renew":
                    self.clock.now = deadline
                    with self.assertRaises(self.module.RobotError):
                        self.gateway.check_control()
                return reply

            self.gateway.post.side_effect = late_post
            self.renew_once()
            self.assertTrue(self.gateway.lost.is_set())
            self.assertEqual(self.gateway._lease_deadline, deadline)
            self.assertFalse(self.gateway.queue_submit([])[0])
        self.assertEqual(self.calls, ["/control/acquire", "/control/renew"])

    def test_lost_lease_does_not_cancel_accepted_cup_or_submit_next_cup_or_home(self):
        picker, cups, places, trace = self.picker()
        statuses = iter(("running", "done"))

        def status(*args, **kwargs):
            self.gateway.lost.set()
            return Reply(body={"state": next(statuses), "steps": []})

        self.module.requests.get.side_effect = status
        picker.capture_once()
        self.assertEqual(self.calls, ["/control/acquire", "/robot/rws/queue"])
        self.assertEqual(self.module.requests.get.call_count, 2)
        self.module.requests.delete.assert_not_called()
        self.assertEqual(trace, ["capture"])
        self.assertEqual([cup["status"] for cup in cups], ["placed", "waiting"])
        self.assertTrue(places["locations"][0]["occupied"])
        self.assertFalse(places["locations"][1]["occupied"])

    def test_loss_during_detection_prevents_first_queue(self):
        picker, cups, _, trace = self.picker()

        def detect(*args):
            self.gateway.lost.set()
            return [], "annotated"

        picker.detector.analyse.side_effect = detect
        picker.capture_once()
        self.assertEqual(self.calls, ["/control/acquire"])
        self.assertEqual([cup["status"] for cup in cups], ["waiting", "waiting"])
        self.assertEqual(trace, ["capture"])

    def test_uncertain_queue_submission_is_not_retried_or_followed_by_home(self):
        picker, cups, _, trace = self.picker()

        def post(path, timeout, body=None):
            if path == "/robot/rws/queue":
                self.calls.append(path)
                raise requests.Timeout("response lost after submission")
            return self.post(path, timeout, body)

        self.gateway.post.side_effect = post
        picker.capture_once()
        self.assertEqual(self.calls, ["/control/acquire", "/robot/rws/queue", "/control/release"])
        self.assertEqual([cup["status"] for cup in cups], ["failed", "waiting"])
        self.assertEqual(trace, ["capture"])
        self.module.requests.get.assert_not_called()
        self.module.requests.delete.assert_not_called()

    def test_standby_expiry_before_capture_returns_to_waiting_without_commands(self):
        self.gateway.try_acquire()
        self.gateway.hold()
        picker, _, _, _ = self.picker()
        self.clock.now += 5.0
        self.assertTrue(picker.auto_run(5))
        self.assertTrue(self.gateway.lost.is_set())
        self.module.depth_average.capture_averaged.assert_not_called()
        self.assertEqual(self.calls, ["/control/acquire"])
        self.gateway.drop()


if __name__ == "__main__":
    unittest.main()
