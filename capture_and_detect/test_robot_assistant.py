"""Assistant tests use saved synthetic scenes; no cloud, gateway or motion."""

import copy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from assistant_api import AssistantSettings
from robot_assistant import RobotAssistant
from web_controller import WebError


def scene(mode="dry_run"):
    return {"mode": mode, "instance_id": "test", "csrf_token": "private-csrf",
            "ready": True, "busy": False, "scan_usable": True, "recovery_required": False,
            "stop_requested": False, "motion": {"speed": 100}, "proposal": None,
            "inventory": {"capture": "scan_1", "saved_at": datetime.now(timezone.utc).isoformat(), "cups": [
                {"id": 1, "pixel": {"u": 100}, "pick_available": True, "selectable": True, "can_save": True},
                {"id": 2, "pixel": {"u": 200}, "pick_available": True, "selectable": True, "can_save": True},
                {"id": 3, "pixel": {"u": 300}, "pick_available": False, "selectable": False}]},
            "placement_grid": {"locations": [{"id": 2, "occupied": False}]}}


class AssistantCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = scene()
        self.controller = Mock()
        self.controller.state.side_effect = lambda: copy.deepcopy(self.state)
        self.controller.image_bytes.return_value = None
        self.settings = AssistantSettings(Path(self.tmp.name).resolve() / "settings.json")
        self.settings.update({"api_key": "sk-test-only-no-real-provider"})
        self.client = Mock()
        self.client.interpret.return_value = {"message": "Pick cup 2.", "action": "select", "cup_ids": [2], "groups": []}
        self.time = 100
        self.service = RobotAssistant(self.controller, settings=self.settings, client=self.client, clock=lambda: self.time)

    def ask(self):
        return self.service.ask("Pick the white cup")

    def test_ask_never_submits_and_prepare_only_creates_existing_preview_once(self):
        result = self.ask()
        self.controller.submit.assert_not_called()
        self.service.prepare(result["proposal_id"])
        operation, payload = self.controller.submit.call_args.args
        self.assertEqual(operation, "select")
        self.assertEqual(payload["action"], "goto")
        self.assertEqual(payload["cup_id"], 2)
        self.assertEqual(payload["capture_id"], "scan_1")
        self.assertNotIn("token", payload)
        self.assertEqual(self.controller.submit.call_args.kwargs,
                         {"assistant_request_id": result["proposal_id"]})
        with self.assertRaises(WebError):
            self.service.prepare(result["proposal_id"])
        self.controller.submit.assert_called_once()

    def test_managed_device_assistant_is_disabled_without_cloud_or_command_calls(self):
        for device in ("mac", "pi", "lynx"):
            self.state.update(devices={"active_id": device, "switching": False}, device_epoch="epoch1")
            self.assertFalse(self.service.status()["available"])
            with self.assertRaises(WebError) as caught:
                self.ask()
            self.assertEqual(caught.exception.status, 403)
        self.client.interpret.assert_not_called()
        self.controller.submit.assert_not_called()

    def test_stale_scan_device_stop_settings_and_new_preview_invalidate(self):
        changes = [lambda s: s["inventory"].update(capture="scan_2"),
                   lambda s: s.update(device_epoch="new"), lambda s: s.update(stop_requested=True),
                   lambda s: s["motion"].update(speed=500),
                   lambda s: s.update(proposal={"token": "manual-preview"}),
                   lambda s: s["placement_grid"]["locations"][0].update(occupied=True)]
        for change in changes:
            with self.subTest(change=change):
                self.state = scene()
                result = self.ask()
                change(self.state)
                with self.assertRaises(WebError):
                    self.service.prepare(result["proposal_id"])
        self.controller.submit.assert_not_called()

    def test_expiration_does_not_submit(self):
        result = self.ask()
        self.time += 121
        with self.assertRaises(WebError):
            self.service.prepare(result["proposal_id"])
        self.controller.submit.assert_not_called()

    def test_scene_change_during_api_call_discards_result(self):
        def change(*args, **kwargs):
            self.state["inventory"]["capture"] = "new"
            return {"message": "Pick cup 2", "action": "select", "cup_ids": [2], "groups": []}
        self.client.interpret.side_effect = change
        result = self.ask()
        self.assertIsNone(result["proposal_id"])
        self.assertEqual(result["action"], "answer")

    def test_busy_recovery_blocked_cup_and_missing_scan_have_no_preview(self):
        for field in ("busy", "recovery_required", "stop_requested"):
            self.state = scene()
            self.state[field] = True
            self.assertIsNone(self.ask()["proposal_id"])
        self.state = scene()
        self.client.interpret.return_value["cup_ids"] = [3]
        self.assertIsNone(self.ask()["proposal_id"])
        self.controller.submit.assert_not_called()

    def test_model_cannot_invent_cup_or_choose_raw_motion(self):
        for updates in ({"cup_ids": [999]}, {"cup_ids": [True]}, {"action": "home"}, {"cup_ids": [1, 2]}):
            self.client.interpret.return_value = dict(message="proposal", action="select", cup_ids=[2], groups=[])
            self.client.interpret.return_value.update(updates)
            with self.assertRaises(WebError):
                self.ask()
        self.controller.submit.assert_not_called()

    def test_batch_only_all_available_and_grouping_is_not_a_motion(self):
        self.client.interpret.return_value.update(action="batch", cup_ids=[2])
        self.assertIsNone(self.ask()["proposal_id"])
        self.client.interpret.return_value.update(cup_ids=[1, 2])
        result = self.ask()
        self.service.prepare(result["proposal_id"])
        self.assertEqual(self.controller.submit.call_args.args[0], "batch")
        self.client.interpret.return_value.update(action="sort", groups=[{"label": "white", "cup_ids": [1, 2]}])
        self.assertIsNone(self.ask()["proposal_id"])

    def test_cloud_context_excludes_credentials_and_model_cannot_select_unknown_state(self):
        self.ask()
        context = self.client.interpret.call_args.args[1]
        self.assertNotIn("csrf_token", context)
        self.assertNotIn("instance_id", context)
        self.assertNotIn("private-csrf", str(context))
        self.assertFalse(context["image_attached"])

    def test_demo_without_key_is_explicit_and_never_calls_model(self):
        self.state["mode"] = "demo"
        self.settings.clear()
        result = self.ask()
        self.assertEqual(result["cup_ids"], [2])
        self.assertEqual(result["mode"], "scripted_demo")
        self.assertIn("not GPT", result["message"])
        self.client.interpret.assert_not_called()

    def test_inflight_request_rejects_duplicate_without_blocking_state_reads(self):
        self.service._lock.acquire()
        try:
            with self.assertRaises(WebError):
                self.ask()
            self.assertTrue(self.service.status()["configured"])
        finally:
            self.service._lock.release()

    def test_scripted_demo_does_not_turn_negation_or_stop_into_a_selection(self):
        self.state["mode"] = "demo"
        self.settings.clear()
        for text in ("don't pick cup 2", "do not pick cup two", "what if we pick cup 1?", "stop then pick cup 2"):
            result = self.service.ask(text)
            self.assertEqual(result["action"], "answer")
            self.assertIsNone(result["proposal_id"])
        result = self.service.ask("Pick the small white cup")
        self.assertEqual(result["action"], "select")
        self.assertEqual(result["cup_ids"], [2])

    def test_slow_remote_image_has_deadline_no_paid_call_and_no_parallel_fetch(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.controller.image_bytes.side_effect = lambda capture: release.wait(2) and None
        self.service.image_timeout = .02
        started = time.monotonic()
        with self.assertRaises(WebError) as caught:
            self.ask()
        self.assertEqual(caught.exception.status, 504)
        self.assertLess(time.monotonic() - started, .3)
        self.client.interpret.assert_not_called()
        with self.assertRaises(WebError):
            self.ask()
        self.controller.image_bytes.assert_called_once()
        self.service.forget()
        self.assertFalse(self.settings.status()["configured"])

    def test_missing_key_never_fetches_a_remote_image(self):
        self.settings.clear()
        with self.assertRaises(ValueError):
            self.ask()
        self.controller.image_bytes.assert_not_called()

    def test_forget_invalidates_suggestion(self):
        result = self.ask()
        self.service.forget()
        with self.assertRaises(WebError):
            self.service.prepare(result["proposal_id"])

    def library(self, *, matcher=True):
        self.state["saved_cups"] = {"enabled": True, "matcher_enabled": matcher, "cups": [
            {"id": "a" * 32, "name": "Kasun's cup", "reference_sha256": "1" * 64,
             "capture_id": "old_capture", "cup_id": 99, "thumbnail_url": "/private/reference"}]}

    def test_exact_named_pick_uses_reference_not_old_cup_number_or_paid_model(self):
        self.library()
        self.state.update(inventory=None, scan_usable=False)
        result = self.service.ask("Pick Kasun’s cup")
        self.assertEqual(result["action"], "pick_saved")
        self.assertEqual(result["mode"], "saved_cup_command")
        self.assertEqual(result["cup_ids"], [])
        self.controller.submit.assert_not_called()
        self.service.prepare(result["proposal_id"])
        operation, payload = self.controller.submit.call_args.args
        self.assertEqual(operation, "pick_saved")
        self.assertEqual(set(payload), {"id", "request_id"})
        self.assertEqual(payload["id"], "a" * 32)
        self.assertEqual(self.controller.submit.call_args.kwargs,
                         {"assistant_request_id": result["proposal_id"]})
        self.client.interpret.assert_not_called()
        self.controller.image_bytes.assert_not_called()

    def test_exact_save_normalizes_name_and_is_only_submitted_after_button(self):
        self.library()
        result = self.service.ask("Remember cup 2 as Maya’s cup")
        self.assertEqual(result["action"], "save_cup")
        self.assertEqual(result["cup_name"], "Maya's cup")
        self.controller.submit.assert_not_called()
        self.service.prepare(result["proposal_id"])
        operation, payload = self.controller.submit.call_args.args
        self.assertEqual(operation, "save_cup")
        self.assertEqual(payload["cup_id"], 2)
        self.assertEqual(payload["capture_id"], "scan_1")
        self.assertEqual(payload["name"], "Maya's cup")
        self.assertEqual(self.controller.submit.call_args.kwargs, {})
        self.client.interpret.assert_not_called()
        with self.assertRaises(WebError):
            self.service.prepare(result["proposal_id"])
        self.controller.submit.assert_called_once()

    def test_duplicate_unknown_or_unavailable_named_requests_have_no_action(self):
        self.library()
        for request in ("Remember cup 2 as Kasun’s cup", "Remember cup 88 as Maya cup",
                        "Remember cup 2 as <script>", "Pick Maya’s cup", "Pick saved unknown cup"):
            with self.subTest(request=request):
                result = self.service.ask(request)
                self.assertIsNone(result["proposal_id"])
                self.assertEqual(result["action"], "answer")
        self.library(matcher=False)
        result = self.service.ask("Pick Kasun's cup")
        self.assertIsNone(result["proposal_id"])
        self.assertIn("not enabled", result["message"])
        self.state["saved_cups"]["enabled"] = False
        self.assertIsNone(self.service.ask("Remember cup 2 as New cup")["proposal_id"])
        self.controller.submit.assert_not_called()
        self.client.interpret.assert_not_called()

    def test_saved_reference_changes_invalidate_proposal(self):
        self.library()
        for action in ("Pick Kasun's cup", "Remember cup 2 as New cup"):
            result = self.service.ask(action)
            self.state["saved_cups"]["cups"][0]["reference_sha256"] = str(self.time) * 16
            self.time += 1
            with self.assertRaises(WebError):
                self.service.prepare(result["proposal_id"])
        self.controller.submit.assert_not_called()

    def test_natural_language_model_routes_only_valid_library_actions(self):
        self.library()
        self.client.interpret.return_value = dict(message="Find the saved cup.", action="pick_saved",
            cup_ids=[], groups=[], cup_name=None, saved_cup_id="a" * 32)
        result = self.service.ask("Please find the mug I registered for Kasun")
        context = self.client.interpret.call_args.args[1]
        self.assertEqual(context["saved_cups"]["cups"], [{"id": "a" * 32, "name": "Kasun's cup"}])
        self.assertNotIn("old_capture", str(context))
        self.assertNotIn("/private/reference", str(context))
        self.service.prepare(result["proposal_id"])
        self.assertEqual(self.controller.submit.call_args.args[0], "pick_saved")
        self.client.interpret.return_value["saved_cup_id"] = "b" * 32
        with self.assertRaises(WebError):
            self.service.ask("Please find the mug I registered for Kasun")

    def test_named_actions_are_blocked_for_devices_and_stop_state(self):
        self.library()
        for field in ("busy", "recovery_required", "stop_requested"):
            self.state[field] = True
            self.assertIsNone(self.service.ask("Pick Kasun's cup")["proposal_id"])
            self.state[field] = False
        result = self.service.ask("Pick Kasun's cup")
        self.state.update(devices={"active_id": "lynx", "switching": False}, device_epoch="epoch-lynx")
        with self.assertRaises(WebError):
            self.service.prepare(result["proposal_id"])
        self.controller.submit.assert_not_called()

    def test_chat_and_connection_test_do_not_compete_with_named_verification(self):
        self.state.update(busy=True, operation="confirm")
        with self.assertRaisesRegex(WebError, "picker is checking"):
            self.ask()
        with self.assertRaisesRegex(WebError, "picker is checking"):
            self.service.test()
        self.client.interpret.assert_not_called()
        self.client.test_connection.assert_not_called()

    def test_named_negations_hypotheticals_never_use_command_shortcut(self):
        self.library()
        self.state["mode"] = "demo"
        self.settings.clear()
        for request in ("don't pick Kasun's cup", "do not remember cup 2 as Maya's cup",
                        "what if I pick Kasun's cup?", "stop then pick Kasun's cup"):
            result = self.service.ask(request)
            self.assertEqual(result["action"], "answer")
            self.assertIsNone(result["proposal_id"])
        self.controller.submit.assert_not_called()

    def test_named_references_do_not_shadow_numbered_or_batch_commands(self):
        self.library()
        self.state["mode"] = "demo"
        self.settings.clear()
        for name, command, action in (("cup 2", "pick cup 2", "select"),
                                      ("all available cups", "pick all available cups", "batch")):
            self.state["saved_cups"]["cups"][0]["name"] = name
            result = self.service.ask(command)
            self.assertEqual(result["action"], action)
            self.assertEqual(self.service.ask("pick saved " + name)["action"], "pick_saved")
        self.controller.submit.assert_not_called()

    def test_incomplete_detection_cannot_offer_save(self):
        self.library()
        result = self.service.ask("Remember cup 3 as Incomplete cup")
        self.assertIsNone(result["proposal_id"])
        self.assertIn("no full cup reference", result["message"])
        self.controller.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
