"""Real Astra transport/selection code with synthetic camera and mocked HTTP only."""

from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from assistant_api import AssistantSettings, AstraClient
from robot_assistant import RobotAssistant
from test_assistant_api import Response, completion
from test_named_cup_flow import NamedCupFixture, two_cups
from web_app import create_app, main
from web_controller import WebController


class AstraMatcherHttpTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = NamedCupFixture(temporary.name, dry_run=True)
        # One unique synthetic cup avoids teaching a false match between the
        # fixture's otherwise identical drawings. Duplicate rejection is tested
        # separately at the Astra transport boundary.
        self.fixture.observations = [{"objects": two_cups()[:2]} for _ in range(10)]
        def factory(controller):
            return self.fixture.factory(controller)
        factory.astra_named_matching = True
        self.controller = WebController(factory, mode="dry_run", llm_enabled=False)
        self.settings = AssistantSettings(Path(temporary.name).resolve() / "settings.json")
        self.settings.update({"api_key": "sk-fake-astra-matching-tests"})
        self.transport = Mock()
        self.transport.post.side_effect = lambda *a, **kw: Response(completion({
            "candidates": {"1": "match"}, "reason": "Blue stripe matches reference."}))
        self.astra = AstraClient(self.settings, session=self.transport)
        self.assistant = RobotAssistant(self.controller, image_root=self.fixture.image_root,
                                        settings=self.settings, client=self.astra)
        self.http = TestClient(create_app(self.controller, assistant=self.assistant,
                               image_root=self.fixture.image_root), base_url="http://127.0.0.1:8080")
        self.http.__enter__()
        self.addCleanup(self.http.__exit__, None, None, None)
        self.wait_idle()
        self.headers = {"Origin": "http://127.0.0.1:8080", "X-CSRF-Token": self.controller.state()["csrf_token"]}
        network = patch("requests.Session.request", side_effect=AssertionError("No external network in tests"))
        network.start()
        self.addCleanup(network.stop)

    def wait_idle(self):
        deadline = time.monotonic() + 4
        while self.controller.state()["busy"]:
            if time.monotonic() > deadline:
                self.fail("Synthetic controller did not finish")
            time.sleep(.005)
        return self.controller.state()

    def post(self, operation, **payload):
        response = self.http.post("/api/" + operation, json=dict(payload, request_id=uuid.uuid4().hex), headers=self.headers)
        self.assertEqual(response.status_code, 202, response.text)
        return self.wait_idle()

    def ask(self, text):
        response = self.http.post("/api/assistant/ask", json={"text": text}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def prepare(self, draft):
        response = self.http.post("/api/assistant/prepare", json={"proposal_id": draft["proposal_id"]}, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.text)
        state = self.wait_idle()
        if draft["action"] == "save_cup":
            self.assertNotIn("assistant_request_id", response.json())
        else:
            self.assertEqual(response.json()["assistant_request_id"], draft["proposal_id"])
            if state["proposal"] is not None:
                self.assertEqual(state["proposal"]["assistant_request_id"], draft["proposal_id"])
        return state

    def save_and_find(self):
        self.post("scan")
        saved = self.prepare(self.ask("Remember cup 1 as Kasun's cup"))
        self.assertIsNone(saved["error"])
        self.assertEqual(self.transport.post.call_count, 0)
        return self.prepare(self.ask("Pick Kasun’s cup"))

    def test_one_shared_astra_client_handles_reference_and_final_identity_check(self):
        self.assertIs(self.controller.language.cup_matcher, self.astra)
        self.assertIs(self.astra.settings, self.assistant.settings)
        self.assertTrue(self.controller.state()["saved_cups"]["matcher_enabled"])
        state = self.save_and_find()
        self.assertIsNone(state["error"])
        proposal = state["proposal"]
        self.assertEqual(proposal["cup_id"], 1)
        self.assertEqual(self.fixture.robot.jobs, [])
        self.assertEqual(self.transport.post.call_count, 1)
        for request in self.transport.post.call_args_list:
            self.assertEqual(request.args[0], "https://api.openai.com/v1/responses")
            self.assertEqual(request.kwargs["json"]["model"], "gpt-6-astra")
            self.assertEqual(request.kwargs["json"]["text"]["format"]["name"], "cup_visual_match")
        state = self.post("confirm", **{key: proposal[key] for key in ("capture_id", "cup_id", "action", "token")})
        self.assertIsNone(state["error"])
        self.assertEqual(self.transport.post.call_count, 2)
        self.assertTrue(self.fixture.robot.jobs)  # Dry-run robot double, only after explicit confirmation.
        self.fixture.matcher.match.assert_not_called()
        self.fixture.interpreter.interpret.assert_not_called()

    def test_forgetting_key_disables_matching_and_blocks_pending_confirmation(self):
        state = self.save_and_find()
        proposal = state["proposal"]
        self.settings.clear()
        self.assertFalse(self.controller.state()["saved_cups"]["matcher_enabled"])
        self.assertEqual(len(self.controller.state()["saved_cups"]["cups"]), 1)
        state = self.post("confirm", **{key: proposal[key] for key in ("capture_id", "cup_id", "action", "token")})
        self.assertIn("API key", state["error"])
        self.assertEqual(self.transport.post.call_count, 1)
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_provider_failure_has_no_local_model_fallback_or_motion(self):
        self.post("scan")
        self.prepare(self.ask("Remember cup 1 as Kasun's cup"))
        self.transport.post.side_effect = lambda *a, **kw: Response(status=503)
        state = self.prepare(self.ask("Pick Kasun's cup"))
        self.assertIn("OpenAI", state["error"])
        self.assertIsNone(state["proposal"])
        self.assertEqual(self.fixture.robot.jobs, [])
        self.fixture.matcher.match.assert_not_called()
        self.fixture.interpreter.interpret.assert_not_called()

    def test_no_key_still_allows_save_but_not_paid_photo_matching(self):
        self.settings.clear()
        self.post("scan")
        state = self.prepare(self.ask("Remember cup 1 as Kasun's cup"))
        self.assertEqual(len(state["saved_cups"]["cups"]), 1)
        answer = self.ask("Pick Kasun's cup")
        self.assertIsNone(answer["proposal_id"])
        self.assertIn("OpenAI API key", answer["message"])
        self.transport.post.assert_not_called()
        self.assertEqual(self.fixture.robot.jobs, [])


class AstraDashboardStartupTests(unittest.TestCase):
    def test_old_local_model_flags_are_not_dashboard_options(self):
        for flags in (["--llm"], ["--llm-model", "qwen"], ["--ollama-url", "http://localhost:11434"]):
            with self.subTest(flags=flags), redirect_stderr(io.StringIO()), patch("uvicorn.run") as run:
                with self.assertRaises(SystemExit) as error:
                    main(["--demo", *flags])
                self.assertEqual(error.exception.code, 2)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
