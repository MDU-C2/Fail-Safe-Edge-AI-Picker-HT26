"""Assistant camera requests use offline captures, never robot or provider traffic."""

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from assistant_api import AssistantSettings
from robot_assistant import RobotAssistant
from test_robot_assistant import scene
from test_web_controller import WebFixture
from web_controller import WebController, WebError


def answer():
    return dict(message="Three cups are visible.", action="answer", cup_ids=[], groups=[],
                cup_name=None, saved_cup_id=None)


class AssistantAutoScanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = WebFixture(temporary.name)
        self.controller = WebController(self.fixture.factory, mode="dry_run", llm_enabled=False)
        self.addCleanup(self.controller.close)
        self.controller.start()
        self.wait_idle()
        settings = AssistantSettings(Path(temporary.name).resolve() / "assistant.json")
        settings.update({"api_key": "sk-offline-autoscan-test"})
        self.client = Mock()
        self.client.interpret.return_value = answer()
        self.assistant = RobotAssistant(self.controller, settings=settings, client=self.client)
        network = patch("requests.Session.request", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def wait_idle(self):
        deadline = time.monotonic() + 3
        while self.controller.state()["busy"]:
            if time.monotonic() > deadline:
                self.fail("Offline camera did not finish")
            time.sleep(.005)
        return self.controller.state()

    def test_missing_photo_scans_once_then_answers_without_motion(self):
        result = self.assistant.ask("What cups are on the table?")
        self.assertTrue(result["camera_refreshed"])
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assertEqual(result["capture_id"], self.client.interpret.call_args.args[1]["capture_id"])
        self.assertIsNone(result["proposal_id"])
        self.assertEqual(self.fixture.robot.jobs, [])
        self.assistant.ask("What colour are the cups?")
        self.assertEqual(self.fixture.captures, ["scan"])

    def test_stale_numbered_request_refreshes_but_never_reassigns_old_number(self):
        self.assistant.ask("What cups are on the table?")
        self.fixture.session.inventory["saved_at"] = (datetime.now(timezone.utc) - timedelta(seconds=31)).isoformat()
        self.controller.publish_session()
        self.client.reset_mock()
        result = self.assistant.ask("Pick cup two")
        self.assertEqual(self.fixture.captures, ["scan", "scan"])
        self.assertTrue(result["camera_refreshed"])
        self.assertIn("Cup numbers can change", result["message"])
        self.assertEqual(result["action"], "answer")
        self.assertIsNone(result["proposal_id"])
        self.client.interpret.assert_not_called()
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_explicit_camera_command_always_refreshes_without_key_or_cloud(self):
        self.assistant.settings.clear()
        commands = ("Take a new photo", "Please look at the table.", "scan again",
                    "Can you take a new photo of workspace now?",
                    "Could you please take a photo of the cups for me?",
                    "Please would you scan the workspace again?", "Will you look at the table now please?")
        for command in commands:
            result = self.assistant.ask(command)
            self.assertEqual(result["mode"], "camera_command")
            self.assertTrue(result["camera_refreshed"])
        self.assertEqual(self.fixture.captures, ["scan"] * len(commands))
        self.client.interpret.assert_not_called()
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_existing_pick_preview_is_not_cancelled_by_camera_request(self):
        self.assistant.ask("scan")
        capture = self.controller.state()["inventory"]["capture"]
        preview = self.fixture.language.propose_selection(action="goto", capture_id=capture, cup_id=1)
        self.controller.publish_session()
        with self.assertRaisesRegex(WebError, "Finish or cancel"):
            self.assistant.ask("take a photo")
        self.assertEqual(self.fixture.language.pending["token"], preview["token"])
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_failed_camera_never_calls_astra_or_prepares_motion(self):
        self.fixture.capture_error = RuntimeError("Offline camera failure")
        with self.assertRaisesRegex(WebError, "usable new photo") as caught:
            self.assistant.ask("What do you see on the table?")
        self.assertEqual(caught.exception.status, 503)
        self.client.interpret.assert_not_called()
        self.assertIsNone(self.assistant._proposal)
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_timeout_does_not_resume_reasoning_or_motion_when_camera_finishes_later(self):
        self.fixture.capture_gate = threading.Event()
        self.assistant.scan_timeout = .02
        try:
            with self.assertRaisesRegex(WebError, "took too long") as caught:
                self.assistant.ask("What cups do you see?")
            self.assertEqual(caught.exception.status, 504)
            self.assertTrue(self.controller.state()["busy"])
        finally:
            self.fixture.capture_gate.set()
            self.wait_idle()
        self.client.interpret.assert_not_called()
        self.assertIsNone(self.assistant._proposal)
        self.assertIsNone(self.controller.state()["proposal"])
        self.assertEqual(self.fixture.robot.jobs, [])


class AssistantCameraContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = scene()
        self.state.update(assistant_available=True, device_epoch="mac-epoch-1",
                          devices={"active_id": "mac", "switching": False})
        self.controller = Mock()
        self.controller.state.side_effect = lambda: copy.deepcopy(self.state)
        self.controller.image_bytes.return_value = None
        self.settings = AssistantSettings(Path(temporary.name).resolve() / "assistant.json")
        self.settings.update({"api_key": "sk-offline-context-test"})
        self.client = Mock()
        self.client.interpret.return_value = answer()
        self.assistant = RobotAssistant(self.controller, settings=self.settings, client=self.client)

    def test_only_explicit_owned_mac_capability_enables_managed_assistant(self):
        self.assertTrue(self.assistant.status()["available"])
        for change in ({"assistant_available": False},
                       {"devices": {"active_id": "pi", "switching": False}},
                       {"devices": {"active_id": "lynx", "switching": False}},
                       {"devices": {"active_id": "mac", "switching": True}}):
            with self.subTest(change=change):
                original = copy.deepcopy(self.state)
                self.state.update(change)
                self.assertFalse(self.assistant.status()["available"])
                with self.assertRaises(WebError):
                    self.assistant.ask("What cups do you see?")
                self.state = original
        self.client.interpret.assert_not_called()

    def test_expected_context_rejects_delayed_ask_and_prepare(self):
        stale = {"device_id": "mac", "device_epoch": "old-epoch"}
        with self.assertRaisesRegex(WebError, "selected device changed"):
            self.assistant.ask("scan", expected_context=stale)
        with self.assertRaisesRegex(WebError, "selected device changed"):
            self.assistant.prepare("old-draft", expected_context=stale)
        self.controller.submit.assert_not_called()
        self.client.interpret.assert_not_called()

    def test_device_context_is_forwarded_and_epoch_change_discards_scan(self):
        self.state.update(inventory=None, scan_usable=False)
        def scan(operation, payload, **kwargs):
            self.assertEqual(operation, "scan")
            self.assertEqual(payload["_device_id"], "mac")
            self.assertEqual(payload["_device_epoch"], "mac-epoch-1")
            self.assertEqual(kwargs, {"assistant_camera": True})
            self.state.update(device_epoch="mac-epoch-2")
            return {"accepted": True}
        self.controller.submit.side_effect = scan
        with self.assertRaisesRegex(WebError, "device changed"):
            self.assistant.ask("What is on the table?")
        self.controller.submit.assert_called_once()
        self.client.interpret.assert_not_called()

    def test_pending_stop_recovery_and_busy_never_trigger_scan(self):
        for changes in ({"busy": True}, {"stop_requested": True}, {"recovery_required": True},
                        {"proposal": {"token": "someone-elses-preview"}},
                        {"robot_controls": {"proposal": {"token": "home-preview"}}}):
            with self.subTest(changes=changes):
                original = copy.deepcopy(self.state)
                self.state.update(changes)
                with self.assertRaises(WebError):
                    self.assistant.ask("take a photo")
                self.state = original
        self.controller.submit.assert_not_called()

    def test_named_save_uses_displayed_photo_and_named_pick_leaves_refresh_to_prepare(self):
        self.state["inventory"]["saved_at"] = "2001-01-01T00:00:00+00:00"
        self.state["saved_cups"] = {"enabled": True, "matcher_enabled": True,
                                    "cups": [{"id": "a" * 32, "name": "Kasun's cup"}]}
        saved = self.assistant.ask("Remember cup 2 as Another cup")
        self.assertEqual(saved["action"], "save_cup")
        self.assertFalse(saved["camera_refreshed"])
        picked = self.assistant.ask("Pick Kasun's cup")
        self.assertEqual(picked["action"], "pick_saved")
        self.assertIsNotNone(picked["proposal_id"])
        self.assertFalse(picked["camera_refreshed"])
        self.controller.submit.assert_not_called()
        self.client.interpret.assert_not_called()

    def test_explicit_no_new_photo_request_does_not_capture(self):
        self.state["inventory"]["saved_at"] = "2001-01-01T00:00:00+00:00"
        commands = ("Don't take a photo; explain the cups in this saved image",
                    "Do not take a new photo of workspace now", "Don't scan the table",
                    "Can you not take a photo?", "Please do not automatically scan the table",
                    "I'd prefer not to take a photo", "Explain the cups without a fresh scan",
                    "Do not use the camera; explain this saved image")
        for command in commands:
            with self.subTest(command=command):
                result = self.assistant.ask(command)
                self.assertFalse(result["camera_refreshed"])
        self.controller.submit.assert_not_called()
        self.assertEqual(self.client.interpret.call_count, len(commands))


if __name__ == "__main__":
    unittest.main()
