"""3D view telemetry: read-only joint/gripper reads and the /api/robot-pose boundary."""

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch

import requests

from robot_client import RobotClient
from robot_telemetry import RobotTelemetry
from test_web_controller import WebFixture
from web_controller import WebController

HERE = Path(__file__).resolve().parent
WEB_DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))


class FakeGateway:
    """Counts connections and concurrent requests like the C++ gateway would see them."""

    def __init__(self, delay=0.02):
        self.delay = delay
        self.lock = threading.Lock()
        self.in_flight = self.max_in_flight = 0
        self.sessions = self.open_sessions = 0
        self.calls = []          # (start, end, endpoint, kwargs)
        self.fail = None         # exception to raise, or None
        self.replies = {
            "/robot/arms/left/joints": {"arm": "left", "joints": [0, -130, 30, 0, 40, 0], "arm_angle": 135},
            "/robot/arms/right/joints": {"arm": "right", "joints": [1, 2, 3, 4, 5, 6], "arm_angle": -135},
            "/robot/arms/left/gripper": {"arm": "left", "position": 12.5},
            "/robot/arms/right/gripper": {"arm": "right", "position": 0},
        }

    def session(self):
        gateway = self

        class Session:
            closed = False

            def get(self, url, **kwargs):
                assert not self.closed
                endpoint = url.removeprefix("http://gateway:8000/api/v1")
                with gateway.lock:
                    gateway.in_flight += 1
                    gateway.max_in_flight = max(gateway.max_in_flight, gateway.in_flight)
                start = time.monotonic()
                try:
                    time.sleep(gateway.delay)
                    if gateway.fail is not None:
                        raise gateway.fail
                    response = Mock()
                    response.status_code = 200
                    response.json.return_value = gateway.replies[endpoint]
                    response.raise_for_status.return_value = None
                    return response
                finally:
                    with gateway.lock:
                        gateway.in_flight -= 1
                        gateway.calls.append((start, time.monotonic(), endpoint, kwargs))

            def close(self):
                if not self.closed:
                    self.closed = True
                    with gateway.lock:
                        gateway.open_sessions -= 1

        with self.lock:
            self.sessions += 1
            self.open_sessions += 1
        return Session()


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class RobotTelemetryTests(unittest.TestCase):
    def test_slow_reads_never_starve_either_arms_joints(self):
        now, calls = [0.0], []
        gateway = FakeGateway(delay=0)
        session = gateway.session()
        original_get = session.get

        def get(url, **kwargs):
            calls.append(url.rsplit("/", 2)[-2:])
            now[0] += 1.2  # With the duty gap this exceeds GRIPPER_PERIOD.
            return original_get(url, **kwargs)

        session.get = get
        telemetry = RobotTelemetry("http://gateway:8000/api/v1", {},
                                   session_factory=lambda: session, clock=lambda: now[0])
        telemetry._wanted = lambda: len(calls) < 9
        telemetry._sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
        telemetry._run()
        self.assertEqual(calls, [["left", "joints"], ["right", "joints"], ["left", "gripper"],
                                 ["left", "joints"], ["right", "joints"], ["right", "gripper"],
                                 ["left", "joints"], ["right", "joints"], ["left", "gripper"]])
        self.assertEqual(gateway.open_sessions, 0)
        self.assertIsNotNone(telemetry._arms["left"])
        self.assertIsNotNone(telemetry._arms["right"])

    def make(self, gateway, **overrides):
        telemetry = RobotTelemetry("http://gateway:8000/api/v1/", {"Authorization": "Bearer t"},
                                   session_factory=gateway.session)
        for name, value in {"DEMAND_WINDOW": 1.0, "GRIPPER_PERIOD": 0.2, **overrides}.items():
            setattr(telemetry, name, value)
        self.addCleanup(telemetry.close)
        return telemetry

    def watch(self, telemetry, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            telemetry.snapshot()
            time.sleep(0.05)

    def test_one_connection_one_request_in_flight_and_only_gets(self):
        gateway = FakeGateway()
        telemetry = self.make(gateway)
        with patch("robot_telemetry.requests.post") as post:
            self.watch(telemetry, 0.8)
        post.assert_not_called()
        snapshot = telemetry.snapshot()
        self.assertEqual(snapshot["arms"]["left"]["joints"], [0, -130, 30, 0, 40, 0])
        self.assertEqual(snapshot["arms"]["left"]["gripper"], 12.5)
        self.assertEqual(snapshot["arms"]["right"]["arm_angle"], -135)
        self.assertLessEqual(snapshot["arms"]["right"]["t"], snapshot["now"])
        self.assertEqual(gateway.max_in_flight, 1)
        self.assertEqual(gateway.sessions, 1)
        for _, _, _, kwargs in gateway.calls:
            self.assertEqual(kwargs["headers"], {"Authorization": "Bearer t"})
            self.assertEqual(kwargs["timeout"], RobotTelemetry.REQUEST_TIMEOUT)
            self.assertFalse(kwargs["allow_redirects"])

    def test_rws_lock_is_left_free_at_least_half_the_time(self):
        gateway = FakeGateway(delay=0.06)
        telemetry = self.make(gateway, MIN_GAP=0.01)
        self.watch(telemetry, 1.0)
        calls = sorted(gateway.calls)
        self.assertGreater(len(calls), 4)
        for (start, end, _, _), (next_start, _, _, _) in zip(calls, calls[1:]):
            self.assertGreaterEqual(next_start - end, (end - start) * 0.95)

    def test_error_drops_the_connection_and_waits_past_the_gateway_timeout(self):
        self.assertGreater(RobotTelemetry.ERROR_BACKOFF[0], 10.0)  # C++ gateway rws_timeout
        gateway = FakeGateway()
        gateway.fail = requests.ConnectionError("503 Too many connections")
        telemetry = self.make(gateway, ERROR_BACKOFF=(0.5, 1.0))
        self.watch(telemetry, 0.3)
        self.assertEqual(len(gateway.calls), 1)          # no retry during the backoff
        self.assertEqual(gateway.open_sessions, 0)       # connection dropped
        self.assertIn("ConnectionError", telemetry.snapshot()["error"])
        gateway.fail = None
        self.watch(telemetry, 0.6)
        self.assertGreater(len(gateway.calls), 1)
        self.assertEqual(gateway.sessions, 2)
        self.assertIsNone(telemetry.snapshot()["error"])
        self.assertEqual(gateway.max_in_flight, 1)

    def test_invalid_replies_are_never_shown(self):
        gateway = FakeGateway()
        for bad in ({"arm": "right", "joints": [1, 2, 3], "arm_angle": 0},
                    {"arm": "left", "joints": [1, 2, 3, 4, 5, 6], "arm_angle": 0},
                    {"arm": "right", "joints": [1, 2, 3, 4, 5, float("nan")], "arm_angle": 0},
                    {"arm": "right", "joints": [1, 2, 3, 4, 5, True], "arm_angle": 0},
                    {"arm": "right", "joints": [1, 2, 3, 4, 5, 6]}):
            gateway.replies["/robot/arms/right/joints"] = bad
            telemetry = self.make(gateway, ERROR_BACKOFF=(0.05, 0.1))
            self.watch(telemetry, 0.4)
            self.assertIsNone(telemetry.snapshot()["arms"]["right"], bad)
            telemetry.close()

    def test_out_of_range_gripper_is_unknown_but_joints_stay(self):
        gateway = FakeGateway()
        gateway.replies["/robot/arms/left/gripper"] = {"arm": "left", "position": 40}
        telemetry = self.make(gateway)
        self.watch(telemetry, 0.6)
        left = telemetry.snapshot()["arms"]["left"]
        self.assertIsNone(left["gripper"])
        self.assertEqual(left["arm_angle"], 135)

    def test_stops_and_frees_the_connection_when_nobody_watches(self):
        gateway = FakeGateway()
        telemetry = self.make(gateway, DEMAND_WINDOW=0.2)
        telemetry.snapshot()
        self.assertTrue(wait_until(lambda: gateway.calls))
        self.assertTrue(wait_until(lambda: not telemetry._thread.is_alive()))
        self.assertEqual(gateway.open_sessions, 0)
        count = len(gateway.calls)
        time.sleep(0.3)
        self.assertEqual(len(gateway.calls), count)
        telemetry.snapshot()   # a viewer comes back
        self.assertTrue(wait_until(lambda: len(gateway.calls) > count))

    def test_close_stops_the_reader(self):
        gateway = FakeGateway()
        telemetry = self.make(gateway)
        telemetry.snapshot()
        self.assertTrue(wait_until(lambda: gateway.calls))
        telemetry.close()
        self.assertFalse(telemetry._thread.is_alive())
        self.assertEqual(gateway.open_sessions, 0)
        with self.assertRaises(RuntimeError):
            telemetry.snapshot()


class ControllerRobotPoseTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = WebFixture(Path(directory.name) / "audit")
        self.controller = WebController(self.fixture.factory)
        self.controller.start()
        self.addCleanup(self.controller.close)
        for _ in range(300):
            if self.controller.session is not None:
                break
            threading.Event().wait(0.01)
        self.robot = self.controller.session.robot

    def test_live_pose_comes_from_one_shared_reader(self):
        self.robot.dry_run = False
        self.robot.api, self.robot.headers = "http://gateway:8000/api/v1", {"Authorization": "Bearer t"}
        with patch("robot_telemetry.RobotTelemetry") as Telemetry:
            Telemetry.return_value.api = "http://gateway:8000/api/v1"
            Telemetry.return_value.snapshot.return_value = {"arms": {"left": None, "right": None}, "now": 1.0, "error": None}
            first, second = self.controller.robot_pose(), self.controller.robot_pose()
        self.assertEqual(first["source"], "live")
        self.assertEqual(second["now"], 1.0)
        Telemetry.assert_called_once_with("http://gateway:8000/api/v1", {"Authorization": "Bearer t"})
        self.controller.close()
        Telemetry.return_value.close.assert_called_once()
        self.assertEqual(self.controller.robot_pose()["source"], "unavailable")

    def test_robot_without_gateway_details_is_unavailable(self):
        self.robot.dry_run = False
        self.robot.api = None
        self.assertEqual(self.controller.robot_pose()["source"], "unavailable")

    def test_dry_run_shows_home_pose_marked_simulated(self):
        self.robot.dry_run = True
        pose = self.controller.robot_pose()
        self.assertEqual(pose["source"], "simulated")
        self.assertEqual(pose["arms"]["left"]["arm_angle"], 135)
        self.assertEqual(pose["arms"]["right"]["joints"], [0, -130, 30, 0, 40, 0])


class CupMotionStateTests(unittest.TestCase):
    """The 3D view moves cups from the step names and when each step was first seen."""

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

    def wait_idle(self):
        deadline = time.monotonic() + 5
        while self.controller.state()["busy"]:
            self.assertLess(time.monotonic(), deadline, "controller stayed busy")
            time.sleep(0.005)
        return self.controller.state()

    def submit(self, operation, **payload):
        payload.setdefault("request_id", uuid.uuid4().hex)
        self.assertEqual(self.controller.submit(operation, payload), {"accepted": True})
        return self.wait_idle()

    def public_cup(self, cup_id):
        return next(cup for cup in self.controller.state()["inventory"]["cups"] if cup["id"] == cup_id)

    def run_steps(self, *names, state="running"):
        cup = next(cup for cup in self.controller.session.inventory["cups"] if cup["id"] == 1)
        execution = cup.setdefault("execution", {"state": state, "completed_steps": [], "total_steps": 18})
        execution["state"] = state
        execution["completed_steps"].extend({"name": name, "body": {}} for name in names)
        self.controller.publish_session()
        return self.public_cup(1)["motion"]

    def test_cup_in_motion_publishes_finished_steps_with_stable_times(self):
        state = self.submit("scan")
        self.assertTrue(all(cup.get("motion") is None for cup in state["inventory"]["cups"]))
        self.assertTrue(all("robot_mm" in cup for cup in state["inventory"]["cups"]))
        started = time.monotonic()
        first = self.run_steps("right_open_start", "left_open", "approach_cup", "grip_cup")
        self.assertEqual(first["state"], "running")
        self.assertEqual(first["completed_steps"], ["right_open_start", "left_open", "approach_cup", "grip_cup"])
        self.assertTrue(all(started <= value <= time.monotonic() for value in first["done_at"].values()))
        time.sleep(0.02)
        second = self.run_steps("right_grip", "left_release", state="interrupted")
        self.assertEqual(second["state"], "interrupted")
        for step, value in first["done_at"].items():
            self.assertEqual(second["done_at"][step], value)     # first-seen times never move
        self.assertGreater(second["done_at"]["right_grip"], first["done_at"]["grip_cup"])
        self.assertIsNone(self.public_cup(2)["motion"])

    def test_public_motion_carries_the_gripping_offset(self):
        self.controller.session.tag_offset = (-115.0, 4.0, 40.0)
        self.controller.publish_session()
        self.assertEqual(self.controller.state()["motion"]["tag_offset_mm"], [-115.0, 4.0, 40.0])

    def test_a_new_scan_starts_fresh_step_times(self):
        self.submit("scan")
        before = self.run_steps("grip_cup")["done_at"]["grip_cup"]
        time.sleep(0.02)
        self.controller.session.inventory = dict(self.controller.session.inventory, capture="another-scan")
        after = self.run_steps()["done_at"]["grip_cup"]
        self.assertGreater(after, before)


@unittest.skipUnless(WEB_DEPENDENCIES, "Install requirements-web.txt to run local HTTP tests")
class RobotPoseHttpTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from web_app import create_app
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = WebFixture(Path(directory.name) / "audit")
        self.controller = WebController(self.fixture.factory)
        self.client = TestClient(create_app(self.controller), base_url="http://127.0.0.1:8080")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_robot_pose_is_a_get_and_cannot_be_posted(self):
        self.controller.robot_pose = Mock(return_value={"source": "live", "arms": {"left": None, "right": None}})
        self.assertEqual(self.client.get("/api/robot-pose").json()["source"], "live")
        posted = self.client.post("/api/robot-pose", headers={"Origin": "http://127.0.0.1:8080"})
        self.assertEqual(posted.status_code, 403)   # no CSRF token: rejected before any handler

    def test_controller_without_telemetry_reports_unavailable(self):
        self.controller.robot_pose = None
        self.assertEqual(self.client.get("/api/robot-pose").json(),
                         {"source": "unavailable", "arms": {"left": None, "right": None}})

    def test_only_listed_3d_assets_are_served(self):
        for asset, kind in (("viewer3d.js", "text/javascript"), ("pose_interpolation.js", "text/javascript"),
                            ("cup_holding.js", "text/javascript"),
                            ("three-bundle.min.js", "text/javascript"),
                            ("yumi_model.json", "application/json"), ("yumi_model.bin", "application/octet-stream")):
            response = self.client.get(f"/static/3d/{asset}")
            self.assertEqual(response.status_code, 200, asset)
            self.assertTrue(response.headers["content-type"].startswith(kind), asset)
        for asset in ("../web_app.py", "..%2Fweb_app.py", "other.js", "viewer3d.JS"):
            self.assertEqual(self.client.get(f"/static/3d/{asset}").status_code, 404, asset)


class ModelAssetTests(unittest.TestCase):
    """The shipped model must match what the viewer expects."""

    def test_manifest_parts_fit_inside_the_binary(self):
        manifest = json.loads((HERE / "web_static/3d/yumi_model.json").read_text())
        size = (HERE / "web_static/3d/yumi_model.bin").stat().st_size
        self.assertEqual(manifest["chain"], ["rax_1", "rax_2", "eax_a", "rax_3", "rax_4", "rax_5", "rax_6"])
        for arm in ("left", "right"):
            self.assertEqual(len(manifest["arms"][arm]["joints"]), 7)
            self.assertEqual(len(manifest["arms"][arm]["links"]), 7)
            for link in manifest["arms"][arm]["links"]:
                self.assertIn(link, manifest["parts"])
        self.assertIn("body", manifest["parts"])
        for arm in ("left", "right"):
            gripper = manifest["grippers"][arm]
            self.assertIn(gripper["body"], manifest["parts"])
            self.assertGreater(gripper["cup_grip_centre_m"], 0.1)
            self.assertEqual(len(gripper["fingers"]), 2)
            for finger in gripper["fingers"]:
                self.assertIn(finger["part"], manifest["parts"])
                self.assertEqual(len(finger["matrix"]), 16)
                self.assertAlmostEqual(sum(value * value for value in finger["axis"]), 1.0)
        for name, part in manifest["parts"].items():
            width = 2 if part["index_type"] == "uint16" else 4
            self.assertLessEqual(part["positions"] + part["vertices"] * 12, size, name)
            self.assertLessEqual(part["normals"] + part["vertices"] * 3, size, name)
            self.assertLessEqual(part["indices"] + part["index_count"] * width, size, name)
            self.assertEqual(part["positions"] % 4, 0, name)
            self.assertEqual(part["indices"] % width, 0, name)


if __name__ == "__main__":
    unittest.main()
