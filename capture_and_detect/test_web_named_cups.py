"""Local named-cup HTTP integration; synthetic images and mocked model/robot only."""

from contextlib import redirect_stdout
import importlib.util
import io
import json
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from test_cup_selection import TRANSFER_JOB_COUNT
from test_named_cup_flow import NamedCupFixture, visual_result
from web_app import create_app
from web_controller import WebController


WEB_DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))


@unittest.skipUnless(WEB_DEPENDENCIES, "Install requirements-web.txt for local HTTP tests")
class WebNamedCupTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = NamedCupFixture(directory.name, dry_run=True)
        self.controller = WebController(self.fixture.factory, mode="dry_run")
        self.client = TestClient(create_app(self.controller, image_root=self.fixture.image_root),
                                 base_url="http://127.0.0.1:8080")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.wait_idle()
        self.headers = {"Origin": "http://127.0.0.1:8080", "X-CSRF-Token": self.state()["csrf_token"]}

    def state(self):
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def wait_idle(self):
        deadline = time.monotonic() + 3
        while self.controller.state()["busy"]:
            if time.monotonic() > deadline:
                self.fail("Synthetic named-cup controller did not become idle")
            time.sleep(.005)
        return self.controller.state()

    def post(self, operation, **payload):
        payload.setdefault("request_id", uuid.uuid4().hex)
        response = self.client.post(f"/api/{operation}", json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.text)
        return self.wait_idle()

    def save(self):
        scan = self.post("scan")
        state = self.post("save_cup", capture_id=scan["inventory"]["capture"], cup_id=1, name="coffee cup")
        self.assertIsNone(state["error"])
        return state["saved_cups"]["cups"][0]

    def test_save_publishes_bounded_metadata_and_local_thumbnail_without_motion(self):
        self.assertEqual(self.state()["saved_cups"], {"enabled": True, "matcher_enabled": True, "cups": []})
        saved = self.save()
        state = self.state()
        self.assertTrue(state["inventory"]["cups"][0]["can_save"])
        self.assertEqual(saved["name"], "coffee cup")
        self.assertEqual(saved["thumbnail_url"], f"/api/saved-image/{saved['id']}")
        self.assertEqual(set(saved), {"id", "name", "saved_at", "capture_id", "cup_id",
                                      "reference_sha256", "thumbnail_url"})
        serialized = json.dumps(state)
        self.assertNotIn("image_bytes", serialized)
        self.assertNotIn("image_jpeg_base64", serialized)
        self.assertNotIn(str(self.fixture.root), serialized)
        self.assertEqual(self.fixture.robot.jobs, [])
        self.fixture.matcher.match.assert_not_called()
        response = self.client.get(saved["thumbnail_url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.content, self.fixture.store.get_id(saved["id"])["image_bytes"])

    def test_pick_saved_scans_then_previews_and_only_confirm_executes(self):
        saved = self.save()
        state = self.post("pick_saved", id=saved["id"])
        self.assertIsNone(state["error"])
        proposal = state["proposal"]
        self.assertEqual(proposal["action"], "goto")
        self.assertEqual(proposal["saved_cup"]["id"], saved["id"])
        self.assertEqual(proposal["saved_cup"]["thumbnail_url"], saved["thumbnail_url"])
        self.assertEqual(self.fixture.captures, ["scan", "scan"])
        self.assertEqual(self.fixture.robot.jobs, [])
        self.fixture.interpreter.interpret.assert_not_called()
        payload = {key: proposal[key] for key in ("token", "capture_id", "cup_id", "action")}
        state = self.post("confirm", **payload)
        self.assertIsNone(state["error"])
        self.assertEqual(len(self.fixture.robot.jobs), TRANSFER_JOB_COUNT)
        self.assertEqual(self.fixture.captures, ["scan", "scan", "verification"])
        self.assertEqual(self.fixture.matcher.match.call_count, 2)
        self.assertIsNone(state["proposal"])

    def test_forget_removes_thumbnail_and_pending_proposal_without_movement(self):
        saved = self.save()
        self.post("pick_saved", id=saved["id"])
        state = self.post("forget_cup", id=saved["id"])
        self.assertIsNone(state["error"])
        self.assertEqual(state["saved_cups"]["cups"], [])
        self.assertIsNone(state["proposal"])
        self.assertEqual(self.client.get(saved["thumbnail_url"]).status_code, 404)
        self.assertEqual(self.fixture.robot.jobs, [])
        self.assertEqual(self.fixture.captures, ["scan", "scan"])

    def test_name_command_route_uses_visual_match_without_text_interpretation(self):
        saved = self.save()
        state = self.post("command", text="pick coffee cup")
        self.assertIsNone(state["error"])
        self.assertEqual(state["proposal"]["saved_cup"]["id"], saved["id"])
        self.fixture.interpreter.interpret.assert_not_called()
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_ambiguous_match_is_visible_and_cannot_create_proposal(self):
        saved = self.save()
        self.fixture.matcher.match.return_value = visual_result(None, "ambiguous")
        state = self.post("pick_saved", id=saved["id"])
        self.assertIn("uniquely identify", state["error"])
        self.assertIsNone(state["proposal"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_unknown_saved_id_and_stale_capture_do_not_move_or_save(self):
        scan = self.post("scan")
        state = self.post("save_cup", capture_id="older-scan", cup_id=1, name="coffee cup")
        self.assertIn("active scan", state["error"])
        self.assertEqual(state["saved_cups"]["cups"], [])
        state = self.post("pick_saved", id="0" * 32)
        self.assertIn("no longer exists", state["error"])
        self.assertEqual(self.fixture.robot.jobs, [])
        self.fixture.matcher.match.assert_not_called()

    def test_named_writes_require_same_origin_and_csrf(self):
        saved = self.save()
        for operation, payload in (("save_cup", {"capture_id": self.state()["inventory"]["capture"],
                                                "cup_id": 1, "name": "second name"}),
                                   ("pick_saved", {"id": saved["id"]}),
                                   ("forget_cup", {"id": saved["id"]})):
            for headers in ({}, {"Origin": "http://untrusted.invalid", "X-CSRF-Token": self.headers["X-CSRF-Token"]},
                            {"Origin": self.headers["Origin"], "X-CSRF-Token": "wrong"}):
                with self.subTest(operation=operation, headers=headers):
                    response = self.client.post(f"/api/{operation}",
                                                json=dict(payload, request_id=uuid.uuid4().hex), headers=headers)
                    self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.fixture.store.list()), 1)
        self.assertEqual(self.fixture.robot.jobs, [])
        self.fixture.matcher.match.assert_not_called()

    def test_invalid_fields_names_and_ids_rejected_at_http_boundary(self):
        capture = self.post("scan")["inventory"]["capture"]
        malformed = [("save_cup", {"capture_id": capture, "cup_id": True, "name": "coffee"}),
                     ("save_cup", {"capture_id": capture, "cup_id": 1, "name": "<script>"}),
                     ("save_cup", {"capture_id": capture, "cup_id": 1, "name": "coffee", "x": 500}),
                     ("pick_saved", {"id": "../private"}), ("pick_saved", {"id": 1}),
                     ("pick_saved", {"id": "0" * 32, "cup_id": 1}),
                     ("forget_cup", {"id": "../private"})]
        for operation, payload in malformed:
            with self.subTest(operation=operation, payload=payload):
                response = self.client.post(f"/api/{operation}", json=dict(payload, request_id=uuid.uuid4().hex),
                                            headers=self.headers)
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.fixture.store.list(), [])
        self.assertEqual(self.fixture.robot.jobs, [])
        self.assertEqual(self.client.get("/api/saved-image/not-an-id").status_code, 404)
        self.assertEqual(self.client.get("/api/saved-image/" + "0" * 32).status_code, 404)

    def test_duplicate_save_request_is_not_replayed(self):
        capture = self.post("scan")["inventory"]["capture"]
        payload = {"capture_id": capture, "cup_id": 1, "name": "coffee cup", "request_id": uuid.uuid4().hex}
        self.post("save_cup", **payload)
        repeated = self.client.post("/api/save_cup", json=payload, headers=self.headers)
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(len(self.fixture.store.list()), 1)
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_library_still_lists_saved_cups_when_matcher_disabled(self):
        saved = self.save()
        self.fixture.language.cup_matcher = None
        self.controller.publish_session()
        state = self.state()
        self.assertTrue(state["saved_cups"]["enabled"])
        self.assertFalse(state["saved_cups"]["matcher_enabled"])
        self.assertEqual(state["saved_cups"]["cups"][0]["id"], saved["id"])
        state = self.post("pick_saved", id=saved["id"])
        self.assertIn("--llm", state["error"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_stop_during_visual_inference_blocks_late_proposal_and_does_not_queue_delete(self):
        saved = self.save()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def wait_for_release(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Synthetic matcher was not released")
            return visual_result()

        self.fixture.matcher.match.side_effect = wait_for_release
        response = self.client.post("/api/pick_saved", json={"id": saved["id"], "request_id": uuid.uuid4().hex},
                                    headers=self.headers)
        self.assertEqual(response.status_code, 202)
        self.assertTrue(entered.wait(1))
        delete = self.client.post("/api/forget_cup", json={"id": saved["id"], "request_id": uuid.uuid4().hex},
                                  headers=self.headers)
        self.assertEqual(delete.status_code, 409)
        try:
            with patch.object(self.fixture.session, "request_stop", return_value={
                    "dry_run": True, "stopped": False, "message": "Synthetic stop acknowledged."}):
                stop = self.client.post("/api/stop", json={"request_id": uuid.uuid4().hex}, headers=self.headers)
                self.assertEqual(stop.status_code, 202)
                self.assertTrue(self.fixture.session.stop_requested)
                release.set()
                state = self.wait_idle()
        finally:
            release.set()
        self.assertTrue(state["stop_requested"])
        self.assertTrue(state["recovery_required"])
        self.assertIsNone(state["proposal"])
        self.assertEqual(self.fixture.robot.jobs, [])
        self.assertEqual(len(self.fixture.store.list()), 1)


if __name__ == "__main__":
    unittest.main()
