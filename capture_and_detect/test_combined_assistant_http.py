"""Combined Mac/Pi/Lynx HTTP contract with synthetic cameras and robot doubles."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from assistant_api import AssistantSettings
from cup_commands import CupSession
from device_agent import DeviceAgent, HandoffStore
from device_dashboard import DeviceDashboard
from language_commands import LanguageController
from local_device import LocalDeviceClient
from robot_assistant import RobotAssistant
from test_device_handoff import AgentClient
from test_named_cup_flow import NamedCupFixture, visual_result
from web_app import create_app


class CombinedAssistantHTTPTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.fixtures, self.agents = {}, {}
        clients = {}
        for name in ("mac", "pi", "lynx"):
            fixture = NamedCupFixture(self.root / name, dry_run=True)
            self.fixtures[name] = fixture
            calibration = fixture.root / "calibration.json"
            reference = fixture.root / "placing-grid.table-reference.json"
            calibration.write_text('{"offline_calibration":true}')
            reference.write_text('{"offline_table_reference":true}')
            store = HandoffStore(fixture.root, "offline-robot", runtime_paths={
                "output": fixture.image_root, "placing_grid": fixture.grid.path,
                "calibration": calibration, "table_reference": reference})

            def factory(controller, fixture=fixture, store=store):
                # Reconstruct a clean controller on each activation while the
                # real named library and placement records remain on disk.
                session = CupSession(fixture.capture, Mock(), fixture.robot, "cal-1",
                    clock=lambda: fixture.now, placing_grid=fixture.grid,
                    recovery=store.recovery, max_result_age=30)
                language = LanguageController(session, fixture.interpreter,
                    clock=lambda: fixture.now, audit_dir=fixture.root / "audit",
                    named_cups=fixture.store)
                fixture.session, fixture.language = session, language
                return session, language

            agent = DeviceAgent(name, factory, store, live=False)
            self.agents[name] = agent
            self.addCleanup(agent.close)
            clients[name] = LocalDeviceClient(agent) if name == "mac" else AgentClient(agent)
        self.dashboard = DeviceDashboard(clients, self.root / "dashboard.json", initial_device="mac",
                                         mode="dry_run", poll_interval=.02)
        self.addCleanup(self.dashboard.close)
        self.provider = Mock()
        self.provider.interpret.return_value = dict(message="The white cup is cup 1.", action="select",
            cup_ids=[1], groups=[], cup_name=None, saved_cup_id=None)
        self.provider.match.return_value = visual_result(1)
        self.settings = AssistantSettings(self.root / "assistant.json")
        self.settings.update({"api_key": "sk-synthetic-combined-test"})
        self.assistant = RobotAssistant(self.dashboard, settings=self.settings, client=self.provider)
        self.client = TestClient(create_app(self.dashboard, assistant=self.assistant,
                                image_root=self.root / "dashboard-cache"),
                                base_url="http://127.0.0.1:8080")
        network = patch("requests.Session.request", side_effect=AssertionError("External network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.wait_idle("mac")

    def state(self):
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def wait_idle(self, device=None):
        deadline = time.monotonic() + 5
        while True:
            state = self.state()
            if (state["ready"] and not state["busy"] and not state["devices"]["switching"]
                    and (device is None or state["devices"]["active_id"] == device)):
                return state
            if time.monotonic() > deadline:
                self.fail(f"Offline dashboard did not become ready: {state.get('error')}")
            time.sleep(.005)

    @staticmethod
    def context(state):
        return {"_device_id": state["devices"]["active_id"], "_device_epoch": state["device_epoch"]}

    def post(self, path, payload, *, context=None):
        state = self.state()
        body = dict(payload, **(self.context(state) if context is None else context))
        if not path.startswith("/api/assistant/"):
            body["request_id"] = uuid.uuid4().hex
        return self.client.post(path, json=body, headers={"Origin": "http://127.0.0.1:8080",
                                                       "X-CSRF-Token": state["csrf_token"]})

    def ask(self, text):
        response = self.post("/api/assistant/ask", {"text": text})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def prepare(self, draft):
        response = self.post("/api/assistant/prepare", {"proposal_id": draft["proposal_id"]})
        self.assertEqual(response.status_code, 202, response.text)
        state = self.wait_idle()
        self.assertIsNone(state["error"])
        return state, response.json()

    def test_missing_photo_to_inline_preview_requires_one_confirm_and_cannot_replay(self):
        self.assertIsNone(self.state()["inventory"])
        draft = self.ask("Pick the white cup")
        self.assertTrue(draft["camera_refreshed"])
        self.assertEqual(self.fixtures["mac"].captures, ["scan"])
        scene = self.provider.interpret.call_args.args[1]
        self.assertEqual(scene["capture_id"], draft["capture_id"])
        self.assertTrue(scene["image_attached"])
        self.assertIsInstance(self.provider.interpret.call_args.kwargs["image"][0], bytes)
        state, accepted = self.prepare(draft)
        proposal = state["proposal"]
        self.assertEqual(accepted["assistant_request_id"], draft["proposal_id"])
        self.assertEqual(proposal["assistant_request_id"], draft["proposal_id"])
        self.assertEqual(self.fixtures["mac"].robot.jobs, [])
        confirmation = {key: proposal[key] for key in ("token", "capture_id", "cup_id", "action")}
        response = self.post("/api/confirm", confirmation)
        self.assertEqual(response.status_code, 202, response.text)
        state = self.wait_idle()
        self.assertIsNone(state["error"])
        self.assertIsNone(state["proposal"])
        count = len(self.fixtures["mac"].robot.jobs)
        self.assertGreater(count, 0)
        response = self.post("/api/confirm", confirmation)
        self.assertEqual(response.status_code, 202)
        self.assertIsNotNone(self.wait_idle()["error"])
        self.assertEqual(len(self.fixtures["mac"].robot.jobs), count)
        self.assertEqual(self.fixtures["pi"].robot.jobs, [])
        self.assertEqual(self.fixtures["lynx"].robot.jobs, [])

    def test_switching_disables_pi_assistant_and_restores_mac_library_client_and_fresh_scan(self):
        first = self.ask("Take a photo")
        self.assertEqual(first["mode"], "camera_command")
        self.provider.interpret.assert_not_called()
        state, accepted = self.prepare(self.ask("Remember cup 1 as Kasun's cup"))
        self.assertNotIn("assistant_request_id", accepted)
        reference = state["saved_cups"]["cups"][0]
        self.assertTrue(self.client.get(reference["thumbnail_url"]).content.startswith(b"\xff\xd8"))
        old_context = self.context(state)
        old_controller = self.agents["mac"].controller
        response = self.post("/api/device_select", {"device_id": "pi"})
        self.assertEqual(response.status_code, 202, response.text)
        pi = self.wait_idle("pi")
        self.assertFalse(pi["assistant_available"])
        response = self.post("/api/assistant/ask", {"text": "Take a photo"})
        self.assertEqual(response.status_code, 403, response.text)
        response = self.post("/api/device_select", {"device_id": "mac"})
        self.assertEqual(response.status_code, 202, response.text)
        state = self.wait_idle("mac")
        self.assertTrue(state["assistant_available"])
        self.assertNotEqual(state["device_epoch"], old_context["_device_epoch"])
        self.assertIsNot(self.agents["mac"].controller, old_controller)
        self.assertIs(self.agents["mac"].controller._astra_client, self.provider)
        self.assertIs(self.agents["mac"].controller._astra_settings, self.settings)
        self.assertIs(self.fixtures["mac"].language.cup_matcher, self.provider)
        self.assertEqual(state["saved_cups"]["cups"][0]["id"], reference["id"])
        self.assertIsNone(state["inventory"])
        response = self.post("/api/assistant/ask", {"text": "scan"}, context=old_context)
        self.assertEqual(response.status_code, 409, response.text)
        fresh = self.ask("scan")
        self.assertTrue(fresh["camera_refreshed"])
        self.assertNotEqual(fresh["capture_id"], first["capture_id"])
        state, _ = self.prepare(self.ask("Pick Kasun's cup"))
        self.assertEqual(state["proposal"]["saved_cup"]["id"], reference["id"])
        self.provider.match.assert_called_once()
        self.assertTrue(state["proposal"]["assistant_request_id"])
        self.assertTrue(all(not fixture.robot.jobs for fixture in self.fixtures.values()))

    def test_missing_or_stale_http_device_context_never_starts_camera_or_draft(self):
        state = self.state()
        headers = {"Origin": "http://127.0.0.1:8080", "X-CSRF-Token": state["csrf_token"]}
        response = self.client.post("/api/assistant/ask", json={"text": "scan"}, headers=headers)
        self.assertEqual(response.status_code, 409, response.text)
        response = self.post("/api/assistant/prepare", {"proposal_id": "a" * 32},
                             context={"_device_id": "mac", "_device_epoch": "retired-epoch"})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.fixtures["mac"].captures, [])
        self.provider.interpret.assert_not_called()
        self.assertIsNone(self.state()["proposal"])


if __name__ == "__main__":
    unittest.main()
