"""Named Assistant requests enter the real guarded picker using synthetic images."""

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
from robot_assistant import RobotAssistant
from test_named_cup_flow import NamedCupFixture, visual_result
from web_app import create_app
from web_controller import WebController


class AssistantNamedCupHttpTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = NamedCupFixture(temporary.name, dry_run=True)
        self.controller = WebController(self.fixture.factory, mode="dry_run")
        self.provider = Mock()
        self.provider.interpret.side_effect = AssertionError("Exact name commands must not call a provider")
        settings = AssistantSettings(Path(temporary.name).resolve() / "settings.json")
        self.assistant = RobotAssistant(self.controller, image_root=self.fixture.image_root,
                                        settings=settings, client=self.provider)
        self.client = TestClient(create_app(self.controller, assistant=self.assistant,
                                 image_root=self.fixture.image_root), base_url="http://127.0.0.1:8080")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.wait_idle()
        self.headers = {"Origin": "http://127.0.0.1:8080", "X-CSRF-Token": self.controller.state()["csrf_token"]}
        self.network = patch("requests.Session.request", side_effect=AssertionError("No network in named-cup tests"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def wait_idle(self):
        deadline = time.monotonic() + 4
        while self.controller.state()["busy"]:
            if time.monotonic() > deadline:
                self.fail("Named-cup worker did not finish")
            time.sleep(.005)
        return self.controller.state()

    def command(self, operation, **payload):
        response = self.client.post("/api/" + operation, json=dict(payload, request_id=uuid.uuid4().hex),
                                    headers=self.headers)
        self.assertEqual(response.status_code, 202, response.text)
        return self.wait_idle()

    def ask(self, text):
        response = self.client.post("/api/assistant/ask", json={"text": text}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def prepare(self, result):
        response = self.client.post("/api/assistant/prepare", json={"proposal_id": result["proposal_id"]},
                                    headers=self.headers)
        self.assertEqual(response.status_code, 202, response.text)
        return self.wait_idle()

    def save(self):
        self.command("scan")
        answer = self.ask("Remember cup 1 as Kasun’s cup")
        self.assertEqual(self.fixture.store.list(), [])
        state = self.prepare(answer)
        self.assertIsNone(state["error"])
        self.assertIsNone(state["proposal"])
        self.assertEqual(self.fixture.robot.jobs, [])
        return state["saved_cups"]["cups"][0]

    def test_save_and_find_fresh_reference_requires_existing_confirm(self):
        saved = self.save()
        self.assertEqual(saved["name"], "Kasun's cup")
        self.assertTrue(self.client.get(saved["thumbnail_url"]).content.startswith(b"\xff\xd8"))
        # A new scan can give the same reference a different detection number.
        self.fixture.matcher.match.return_value = visual_result(2)
        answer = self.ask("Pick Kasun's cup")
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assertEqual(self.fixture.robot.jobs, [])
        state = self.prepare(answer)
        proposal = state["proposal"]
        self.assertIsNone(state["error"])
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assertEqual(proposal["cup_id"], 2)
        self.assertEqual(proposal["saved_cup"]["id"], saved["id"])
        self.assertEqual(self.fixture.robot.jobs, [])
        self.assertIn("Kasun's cup", state["message"])
        self.provider.interpret.assert_not_called()
        # A changed visual identity at confirmation cannot dispatch a job.
        self.fixture.matcher.match.return_value = visual_result(None, "not_found")
        state = self.command("confirm", **{key: proposal[key] for key in ("capture_id", "cup_id", "action", "token")})
        self.assertIsNotNone(state["error"])
        self.assertIsNone(state["proposal"])
        self.assertEqual(self.fixture.robot.jobs, [])
        self.assertEqual(self.fixture.matcher.match.call_count, 2)

    def test_ambiguous_reference_never_creates_robot_preview(self):
        self.save()
        self.fixture.matcher.match.return_value = visual_result(None, "ambiguous")
        state = self.prepare(self.ask("Pick Kasun’s cup"))
        self.assertIn("uniquely identify", state["error"])
        self.assertIsNone(state["proposal"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_deleted_reference_invalidates_unsubmitted_assistant_draft(self):
        saved = self.save()
        draft = self.ask("Pick Kasun's cup")
        self.command("forget_cup", id=saved["id"])
        response = self.client.post("/api/assistant/prepare", json={"proposal_id": draft["proposal_id"]},
                                    headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_natural_language_uses_supplied_library_and_existing_reference_workflow(self):
        saved = self.save()
        self.assistant.settings.update({"api_key": "sk-fake-no-network"})
        self.provider.interpret.side_effect = None
        self.provider.interpret.return_value = dict(message="Find the saved reference.", action="pick_saved",
            cup_ids=[], groups=[], cup_name=None, saved_cup_id=saved["id"])
        draft = self.ask("Please find the mug I saved for Kasun earlier")
        context = self.provider.interpret.call_args.args[1]
        self.assertEqual(context["saved_cups"]["cups"], [{"id": saved["id"], "name": "Kasun's cup"}])
        self.assertIsNotNone(self.provider.interpret.call_args.kwargs["image"])
        self.assertEqual(self.fixture.robot.jobs, [])
        state = self.prepare(draft)
        self.assertEqual(state["proposal"]["saved_cup"]["id"], saved["id"])
        self.assertEqual(self.fixture.robot.jobs, [])


if __name__ == "__main__":
    unittest.main()
