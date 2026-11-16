"""Assistant previews share the existing confirmation, using offline doubles only."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from assistant_api import AssistantSettings
from robot_assistant import RobotAssistant
from test_web_controller import WebFixture
from web_controller import WebController, WebError


class AssistantConfirmationTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = WebFixture(temporary.name)
        self.controller = WebController(self.fixture.factory, mode="dry_run", llm_enabled=False)
        self.addCleanup(self.controller.close)
        self.controller.start()
        self.wait_idle()
        settings = AssistantSettings(Path(temporary.name).resolve() / "assistant.json")
        settings.update({"api_key": "sk-offline-confirmation-test"})
        self.client = Mock()
        self.client.interpret.return_value = dict(message="Pick cup 1.", action="select", cup_ids=[1],
                                                 groups=[], cup_name=None, saved_cup_id=None)
        self.assistant = RobotAssistant(self.controller, settings=settings, client=self.client)
        self.network = patch("requests.Session.request", side_effect=AssertionError("No network in this test"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.submit("scan")

    def wait_idle(self):
        deadline = time.monotonic() + 3
        while self.controller.state()["busy"]:
            if time.monotonic() > deadline:
                self.fail("Offline controller did not finish")
            time.sleep(.005)
        return self.controller.state()

    def submit(self, operation, **payload):
        self.controller.submit(operation, dict(payload, request_id=uuid.uuid4().hex))
        return self.wait_idle()

    def prepare(self, *, batch=False):
        if batch:
            self.client.interpret.return_value.update(action="batch", cup_ids=[1, 2])
        draft = self.assistant.ask("Please pick the selected cups")
        accepted = self.assistant.prepare(draft["proposal_id"])
        self.assertEqual(accepted, {"accepted": True, "assistant_request_id": draft["proposal_id"]})
        state = self.wait_idle()
        self.assertIsNone(state["error"])
        self.assertEqual(state["proposal"]["assistant_request_id"], draft["proposal_id"])
        self.assertEqual(self.fixture.robot.jobs, [])
        return state["proposal"]

    @staticmethod
    def confirmation(proposal):
        return {key: proposal[key] for key in ("token", "capture_id", "cup_id", "action")}

    def test_one_inline_confirmation_executes_once_and_cannot_replay(self):
        proposal = self.prepare()
        with patch.object(self.fixture.robot, "execute_jobs", wraps=self.fixture.robot.execute_jobs) as execute:
            state = self.submit("confirm", **self.confirmation(proposal))
            self.assertIsNone(state["error"])
            self.assertIsNone(state["proposal"])
            execute.assert_called_once()
            self.assertTrue(self.fixture.robot.jobs)
            replay = self.submit("confirm", **self.confirmation(proposal))
            self.assertIsNotNone(replay["error"])
            execute.assert_called_once()

    def test_batch_gets_its_own_real_preview_without_executing(self):
        proposal = self.prepare(batch=True)
        self.assertEqual(proposal["action"], "goto_all")
        self.assertEqual(proposal["cup_ids"], [1, 2])
        self.assertEqual(len(proposal["placements"]), 2)

    def test_unrelated_selection_replaces_source_and_old_confirmation_fails(self):
        earlier = self.prepare()
        state = self.submit("select", capture_id=earlier["capture_id"], cup_id=2, action="goto")
        self.assertNotIn("assistant_request_id", state["proposal"])
        self.assertNotEqual(state["proposal"]["token"], earlier["token"])
        state = self.submit("confirm", **self.confirmation(earlier))
        self.assertIsNotNone(state["error"])
        self.assertIsNone(state["proposal"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_expiry_removes_source_without_renewing_confirmation(self):
        proposal = self.prepare()
        self.fixture.now += self.fixture.language.proposal_ttl + 1
        # Polling alone must hide expired ownership, even without another
        # camera/session callback to rebuild the public proposal.
        self.controller._proposal_deadline = time.monotonic() - 1
        self.assertNotIn("assistant_request_id", self.controller.state()["proposal"])
        self.controller.publish_session()
        state = self.controller.state()
        self.assertEqual(state["proposal"]["expires_in"], 0)
        self.assertNotIn("assistant_request_id", state["proposal"])
        self.assertIsNone(self.controller._assistant_proposal_source)
        state = self.submit("confirm", **self.confirmation(proposal))
        self.assertIsNotNone(state["error"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_clearing_assistant_source_keeps_normal_pick_confirmation_working(self):
        proposal = self.prepare()
        self.submit("cancel")
        state = self.submit("select", capture_id=proposal["capture_id"], cup_id=2, action="goto")
        self.assertNotIn("assistant_request_id", state["proposal"])
        state = self.submit("confirm", **self.confirmation(state["proposal"]))
        self.assertIsNone(state["error"])
        self.assertTrue(self.fixture.robot.jobs)

    def test_stale_cancel_token_cannot_cancel_a_newer_preview_or_clear_its_source(self):
        earlier = self.prepare()
        self.client.interpret.return_value["cup_ids"] = [2]
        newer = self.prepare()
        before = self.controller.state()
        self.assertNotEqual(earlier["token"], newer["token"])
        with patch.object(self.fixture.language, "cancel", wraps=self.fixture.language.cancel) as cancel:
            with self.assertRaises(WebError) as caught:
                self.controller.submit("cancel", {"request_id": uuid.uuid4().hex, "token": earlier["token"]})
            self.assertEqual(caught.exception.status, 409)
            cancel.assert_not_called()
        after = self.controller.state()
        self.assertEqual(after["revision"], before["revision"])
        self.assertFalse(after["busy"])
        self.assertEqual(after["proposal"]["token"], newer["token"])
        self.assertEqual(after["proposal"]["assistant_request_id"], newer["assistant_request_id"])
        self.assertEqual(self.fixture.language.pending["token"], newer["token"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_matching_cancel_token_is_one_use_and_manual_cancel_remains_supported(self):
        proposal = self.prepare()
        state = self.submit("cancel", token=proposal["token"])
        self.assertIsNone(state["proposal"])
        self.assertIsNone(self.controller._assistant_proposal_source)
        with self.assertRaises(WebError) as caught:
            self.controller.submit("cancel", {"request_id": uuid.uuid4().hex, "token": proposal["token"]})
        self.assertEqual(caught.exception.status, 409)
        self.prepare()
        self.assertIsNone(self.submit("cancel")["proposal"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_cancel_token_validation_rejects_malformed_or_extra_fields(self):
        proposal = self.prepare()
        for token in (None, True, 12, "", "abc", "A" * 8, "a" * 32):
            with self.subTest(token=token), self.assertRaises(WebError) as caught:
                self.controller.submit("cancel", {"request_id": uuid.uuid4().hex, "token": token})
            self.assertEqual(caught.exception.status, 422)
        with self.assertRaises(WebError) as caught:
            self.controller.submit("cancel", {"request_id": uuid.uuid4().hex, "token": proposal["token"],
                                              "assistant_request_id": proposal["assistant_request_id"]})
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(self.controller.state()["proposal"]["assistant_request_id"], proposal["assistant_request_id"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_cancel_scan_stop_and_worker_error_remove_source(self):
        for operation in ("cancel", "scan", "stop", "error"):
            with self.subTest(operation=operation):
                if self.controller.state()["stop_requested"]:
                    self.submit("recovery", acknowledged=True)
                    self.submit("scan")
                proposal = self.prepare()
                if operation == "error":
                    self.fixture.capture_error = RuntimeError("Synthetic camera failure")
                    state = self.submit("scan")
                else:
                    state = self.submit(operation)
                self.assertIsNone(state["proposal"])
                self.assertIsNone(self.controller._assistant_proposal_source)
                self.assertEqual(self.fixture.robot.jobs, [])

    def test_source_is_not_an_http_payload_or_confirmation_capability(self):
        capture = self.controller.state()["inventory"]["capture"]
        payload = dict(capture_id=capture, cup_id=1, action="goto", request_id=uuid.uuid4().hex)
        with self.assertRaises(WebError) as caught:
            self.controller.submit("select", dict(payload, assistant_request_id="a" * 32))
        self.assertEqual(caught.exception.status, 422)
        for value in ("", "not-a-server-id", True, 12):
            with self.subTest(value=value), self.assertRaises(WebError):
                self.controller.submit("select", payload, assistant_request_id=value)
        with self.assertRaises(WebError):
            self.controller.submit("scan", {"request_id": uuid.uuid4().hex}, assistant_request_id="a" * 32)
        self.assertIsNone(self.controller.state()["proposal"])
        self.assertEqual(self.fixture.robot.jobs, [])


if __name__ == "__main__":
    unittest.main()
