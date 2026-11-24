"""Read-only selected-device telemetry and strict height-setting forwarding."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from device_agent import DeviceAgent
from device_dashboard import DeviceDashboard
from device_service import PickerHTTPServer
from local_device import LocalDeviceClient
from test_device_handoff import AgentClient, FakeController, FakeFactory, FakeStore
from web_controller import WebError


class PoseController(FakeController):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.snapshot["instance_id"] = uuid.uuid4().hex
        self.pose_reads = 0

    def robot_pose(self):
        self.pose_reads += 1
        return {"source": "live", "arms": {"left": {"joints": [1] * 6,
                "arm_angle": 20, "gripper": 3, "t": 123}, "right": None}, "now": 124}


class PoseClient(AgentClient):
    def request(self, path, body=None, **kwargs):
        if path == "/v1/robot-pose":
            self.calls.append((path, body))
            return self.agent.robot_pose()
        return super().request(path, body, **kwargs)


class SelectedPoseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.agents = {name: DeviceAgent(name, FakeFactory(), FakeStore(root / name),
                      controller_class=PoseController) for name in ("mac", "pi", "lynx")}
        self.clients = {name: PoseClient(agent) for name, agent in self.agents.items()}
        self.clients["mac"] = LocalDeviceClient(self.agents["mac"])
        self.dashboard = DeviceDashboard(self.clients, root / "owner.json", initial_device="mac")
        self.addCleanup(self.dashboard.close)
        self.dashboard._resume()
        for device in self.clients:
            self.dashboard._read(device)
        network = patch("requests.Session.request", side_effect=AssertionError("No external I/O"))
        network.start()
        self.addCleanup(network.stop)

    def test_local_pose_keeps_child_clock_and_never_submits_or_acquires(self):
        controller = self.agents["mac"].controller
        before = copy.deepcopy(self.agents["mac"].owner)
        state = self.dashboard.state()
        pose = self.dashboard.robot_pose()
        self.assertEqual(pose["clock_id"], state["pose_clock_id"])
        self.assertNotEqual(pose["clock_id"], state["instance_id"])
        self.assertEqual(pose["device_epoch"], state["device_epoch"])
        self.assertEqual(pose["device_id"], "mac")
        self.assertEqual(pose["now"], 124)
        self.assertEqual(pose["arms"]["left"]["t"], 123)
        self.assertEqual(controller.calls, [])
        self.assertEqual(before, self.agents["mac"].owner)
        for _ in range(10):
            self.dashboard.robot_pose()
        self.assertEqual(controller.pose_reads, 1)

    def test_selected_remote_uses_its_own_clock_and_old_device_closes(self):
        old = self.agents["mac"].controller
        self.dashboard.robot_pose()
        self.dashboard._switch("pi")
        state = self.dashboard.state()
        pose = self.dashboard.robot_pose()
        self.assertTrue(old.closed)
        self.assertEqual(pose["device_id"], "pi")
        self.assertEqual(pose["clock_id"], state["pose_clock_id"])
        self.assertNotEqual(pose["clock_id"], old.snapshot["instance_id"])
        self.assertEqual(self.agents["pi"].controller.calls, [])
        self.assertEqual(self.clients["pi"].count("/v1/robot-pose"), 1)

    def test_old_remote_without_feature_does_not_get_unknown_endpoint(self):
        self.dashboard._switch("pi")
        self.dashboard._cache["pi"]["health"]["features"].pop("robot_pose")
        self.assertEqual(self.dashboard.robot_pose()["source"], "unavailable")
        self.assertEqual(self.clients["pi"].count("/v1/robot-pose"), 0)

    def test_slow_pose_does_not_cross_device_epoch_or_block_other_viewers(self):
        self.dashboard._switch("pi")
        client = self.clients["pi"]
        request = client.request
        entered, release = threading.Event(), threading.Event()

        def blocked(path, *args, **kwargs):
            if path == "/v1/robot-pose":
                entered.set()
                release.wait(2)
            return request(path, *args, **kwargs)

        client.request = blocked
        result = []
        thread = threading.Thread(target=lambda: result.append(self.dashboard.robot_pose()))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.dashboard.robot_pose()["source"], "unavailable")
            with self.dashboard._lock:
                self.dashboard._epoch = uuid.uuid4().hex
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]["source"], "unavailable")
        self.assertEqual(result[0]["device_epoch"], self.dashboard._epoch)
        self.assertEqual(self.clients["pi"].count("/v1/robot-pose"), 1)

    def test_controller_restart_during_pose_is_discarded(self):
        self.dashboard._switch("pi")
        original = self.clients["pi"].request

        def replaced(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == "/v1/robot-pose":
                self.dashboard._cache["pi"]["state"]["instance_id"] = "new-controller"
            return result

        self.clients["pi"].request = replaced
        self.assertEqual(self.dashboard.robot_pose()["source"], "unavailable")

    def test_height_only_forwards_actual_booleans_under_current_ownership(self):
        agent = self.agents["mac"]
        context = copy.deepcopy(self.dashboard.record["context"])
        for value in (0, 1, "true", None, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(WebError, "boolean"):
                agent.submit(dict(context, operation="pick_height",
                    payload={"camera_z": value, "request_id": uuid.uuid4().hex}))
        self.assertEqual(agent.controller.calls, [])
        for value in (True, False):
            payload = {"camera_z": value, "request_id": uuid.uuid4().hex}
            self.clients["mac"].request("/v1/command", dict(context, operation="pick_height", payload=payload))
            self.assertEqual(agent.controller.calls[-1], ("pick_height", payload))

    def test_http_pose_requires_device_auth_and_never_dispatches_motion(self):
        agent = self.agents["mac"]
        server = PickerHTTPServer(("127.0.0.1", 0), agent, "test-device-token")
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            connection.request("GET", "/v1/robot-pose")
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            connection.close()
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            connection.request("GET", "/v1/robot-pose", headers={"Authorization": "Bearer test-device-token"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            pose = json.loads(response.read())
            self.assertEqual(pose["clock_id"], agent.controller.state()["instance_id"])
            connection.close()
            self.assertEqual(agent.controller.calls, [])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)


if __name__ == "__main__":
    unittest.main()
