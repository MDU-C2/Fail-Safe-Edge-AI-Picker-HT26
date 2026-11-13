"""Local HTTP boundary tests. All camera, model and robot activity is mocked."""

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from test_web_controller import WebFixture
from web_app import ProcessLock, create_app, demo_factory, main
from web_controller import WebController

WEB_DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))


@unittest.skipUnless(WEB_DEPENDENCIES, "Install requirements-web.txt to run local HTTP tests")
class WebAppTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.fixture = WebFixture(self.root / "audit")
        self.controller = WebController(self.fixture.factory)
        self.app = create_app(self.controller, image_root=self.root)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8080")
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
                self.fail("Controller did not become idle")
            time.sleep(0.005)
        return self.controller.state()

    def post(self, operation, **payload):
        payload.setdefault("request_id", uuid.uuid4().hex)
        response = self.client.post(f"/api/{operation}", json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.text)
        return self.wait_idle()

    def proposal(self):
        capture = self.post("scan")["inventory"]["capture"]
        state = self.post("select", capture_id=capture, cup_id=1, action="hover")
        self.assertIsNone(state["error"])
        return {key: state["proposal"][key] for key in ("token", "capture_id", "cup_id", "action")}

    def test_refresh_and_state_reads_never_execute_or_consume_a_proposal(self):
        proposal = self.proposal()
        for _ in range(3):
            state = self.state()
            self.assertEqual(state["proposal"]["token"], proposal["token"])
        self.assertEqual(self.fixture.robot.jobs, [])
        self.assertEqual(self.fixture.captures, ["scan"])

    def test_start_endpoint_makes_fresh_batch_proposal_without_movement(self):
        state = self.post("start")
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assertEqual(state["proposal"]["action"], "goto_all")
        self.assertEqual(state["proposal"]["cup_ids"], [1, 2])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_status_and_help_gets_are_read_only_and_leave_proposal_untouched(self):
        proposal = self.proposal()
        with patch("requests.post") as post, patch("requests.get") as get:
            status = self.client.get("/api/status")
            help_result = self.client.get("/api/help")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(help_result.status_code, 200)
        self.assertIn("Cached application state", status.json()["source"])
        self.assertIn("software", help_result.json()["message"].lower())
        self.assertEqual(self.state()["proposal"]["token"], proposal["token"])
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assertEqual(self.fixture.robot.jobs, [])
        post.assert_not_called()
        get.assert_not_called()

    def test_stop_endpoint_latches_and_invalidates_confirm_without_motion(self):
        proposal = self.proposal()
        with patch.object(self.controller.session, "request_stop", return_value={
                "dry_run": True, "stopped": False, "message": "Demo stop; no robot command sent."}) as stop:
            state = self.post("stop")
        stop.assert_called_once()
        self.assertTrue(state["stop_requested"])
        self.assertTrue(state["recovery_required"])
        self.assertFalse(state["scan_usable"])
        self.assertIsNone(state["proposal"])
        response = self.client.post("/api/confirm", json=dict(proposal, request_id=uuid.uuid4().hex),
                                    headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_stop_requires_same_origin_and_capability_even_while_busy(self):
        response = self.client.post("/api/stop", json={"request_id": uuid.uuid4().hex},
                                    headers={"Origin": "http://untrusted.invalid"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.controller.session.stop_requested)

    def test_location_release_endpoint_is_local_only_and_requires_inspection_ack(self):
        grid = self.fixture.session.placing_grid
        grid.reserve(grid.preview(1))
        before = grid.path.read_bytes()
        payload = {"request_id": uuid.uuid4().hex, "fingerprint": grid.snapshot()["fingerprint"],
                   "acknowledged": True}
        for headers in ({}, {"Origin": self.headers["Origin"]},
                        dict(self.headers, Origin="http://untrusted.invalid")):
            with self.subTest(headers=headers):
                response = self.client.post("/api/release_locations", json=payload, headers=headers)
                self.assertEqual(response.status_code, 403)
        response = self.client.post("/api/release_locations", json=dict(payload, acknowledged=False),
                                    headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(grid.path.read_bytes(), before)
        self.assertEqual(self.fixture.captures, [])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_location_release_endpoint_clears_slots_and_consumes_pending_selection(self):
        self.proposal()
        grid = self.fixture.session.placing_grid
        grid.reserve(grid.preview(1))
        request_id = uuid.uuid4().hex
        payload = {"request_id": request_id, "fingerprint": grid.snapshot()["fingerprint"],
                   "acknowledged": True}
        state = self.post("release_locations", **payload)
        self.assertIsNone(state["error"])
        self.assertIsNone(state["proposal"])
        self.assertFalse(state["scan_usable"])
        self.assertFalse(any(slot["occupied"] or slot.get("reserved", False)
                             for slot in grid.snapshot()["locations"]))
        response = self.client.post("/api/release_locations", json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_confirm_endpoint_executes_once_and_duplicate_post_conflicts(self):
        proposal = self.proposal()
        request_id = uuid.uuid4().hex
        state = self.post("confirm", **proposal, request_id=request_id)
        self.assertIsNone(state["error"])
        self.assertEqual(len(self.fixture.robot.jobs), 2)
        response = self.client.post("/api/confirm", json=dict(proposal, request_id=request_id), headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.fixture.robot.jobs), 2)

    def test_batch_endpoint_requires_confirmation_and_does_not_replay(self):
        capture = self.post("scan")["inventory"]["capture"]
        proposal = self.post("batch", capture_id=capture)["proposal"]
        self.assertEqual(proposal["action"], "goto_all")
        self.assertEqual(proposal["cup_ids"], [1, 2])
        self.assertIsNone(proposal["cup_id"])
        self.assertEqual(self.fixture.robot.jobs, [])
        payload = {key: proposal[key] for key in ("token", "capture_id", "cup_id", "action")}
        request_id = uuid.uuid4().hex
        state = self.post("confirm", **payload, request_id=request_id)
        self.assertIsNone(state["error"])
        expected = sum(len(cup["last_verification"]["jobs"])
                       for cup in self.fixture.session.inventory["cups"] if cup["id"] in proposal["cup_ids"])
        self.assertEqual(len(self.fixture.robot.jobs), expected)
        duplicate = self.client.post("/api/confirm", json=dict(payload, request_id=request_id), headers=self.headers)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(len(self.fixture.robot.jobs), expected)

    def test_batch_endpoint_rejects_injected_cup_ids_coordinates_and_unknown_capture(self):
        capture = self.post("scan")["inventory"]["capture"]
        for extra in ({"cup_ids": [2]}, {"placement": {"x": 800}}, {"action": "goto"}):
            with self.subTest(extra=extra):
                response = self.client.post("/api/batch", json={"request_id": uuid.uuid4().hex,
                    "capture_id": capture, **extra}, headers=self.headers)
                self.assertEqual(response.status_code, 422)
        state = self.post("batch", capture_id="stale-capture")
        self.assertIsNotNone(state["error"])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_host_allowlist_rejects_rebinding_and_wrong_ports(self):
        for host in ("evil.example:8080", "localhost.evil.example:8080", "127.0.0.1:8081", "192.168.1.2:8080"):
            with self.subTest(host=host):
                response = self.client.get("/api/state", headers={"Host": host})
                self.assertEqual(response.status_code, 403)
        accepted = self.client.get("/api/state", headers={"Host": "localhost:8080"})
        self.assertEqual(accepted.status_code, 200)

    def test_mutations_require_matching_origin_and_csrf_capability(self):
        cases = ({}, {"Origin": self.headers["Origin"]},
                 {"X-CSRF-Token": self.headers["X-CSRF-Token"]},
                 dict(self.headers, **{"X-CSRF-Token": "wrong"}),
                 dict(self.headers, Origin="http://evil.example"),
                 dict(self.headers, Origin="null"),
                 dict(self.headers, Origin="http://localhost:8080"))
        for headers in cases:
            with self.subTest(headers=headers):
                response = self.client.post("/api/scan", json={"request_id": uuid.uuid4().hex}, headers=headers)
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.fixture.captures, [])
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_cross_origin_read_is_rejected_and_no_cors_permission_is_granted(self):
        response = self.client.get("/api/state", headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_json_limits_duplicate_keys_and_malformed_fields_fail_without_dispatch(self):
        cases = [("not-json", 422), ('{"request_id":"12345678","request_id":"abcdefgh"}', 422),
                 (json.dumps(["12345678"]), 422), (json.dumps({"request_id": "12345678", "extra": 1}), 422),
                 ("x" * 8193, 413), ("{" + "\"a\":" + "[" * 2000 + "0" + "]" * 2000 + "}", 422)]
        for content, expected in cases:
            with self.subTest(expected=expected, size=len(content)):
                response = self.client.post("/api/scan", content=content,
                    headers=dict(self.headers, **{"Content-Type": "application/json"}))
                self.assertEqual(response.status_code, expected, response.text)
        response = self.client.post("/api/scan", data={"request_id": "12345678"}, headers=self.headers)
        self.assertEqual(response.status_code, 415)
        self.assertEqual(self.fixture.captures, [])

    def test_unknown_operations_raw_coordinates_and_unsafe_id_types_are_rejected(self):
        cases = [("home", {"request_id": "12345678"}),
                 ("select", {"request_id": "12345678", "capture_id": "scan-1", "cup_id": True, "action": "hover"}),
                 ("select", {"request_id": "12345678", "capture_id": "scan-1", "cup_id": 1, "action": "hover", "x": 500}),
                 ("recovery", {"request_id": "12345678", "acknowledged": "yes"})]
        for operation, payload in cases:
            with self.subTest(operation=operation, payload=payload):
                response = self.client.post(f"/api/{operation}", json=payload, headers=self.headers)
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_local_assets_and_state_have_browser_security_headers(self):
        for path in ("/", "/static/app.js", "/static/app.css", "/api/state"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual(self.client.get("/static/secret.txt").status_code, 404)
        self.assertEqual(self.client.get("/openapi.json").status_code, 404)

    def test_images_are_registered_capture_ids_within_the_configured_root(self):
        state = self.post("scan")
        capture = state["inventory"]["capture"]
        allowed = self.root / "scan.jpg"
        allowed.write_bytes(b"offline-test-image")
        self.fixture.session.inventory["images"]["annotated"] = str(allowed)
        self.controller.publish_session()
        image = self.client.get(f"/api/image/{capture}")
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.content, b"offline-test-image")
        self.assertEqual(self.client.get("/api/image/missing").status_code, 404)
        self.assertEqual(self.client.get("/api/image/%2E%2E%2Fsecret").status_code, 404)
        self.post("scan")
        self.assertEqual(self.client.get(f"/api/image/{capture}").status_code, 404)

    def test_image_paths_cannot_escape_root_even_through_symlinks(self):
        self.post("scan")
        capture = self.fixture.session.inventory["capture"]
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        secret = Path(outside.name) / "secret.jpg"
        secret.write_bytes(b"private")
        symlink = self.root / "linked.jpg"
        symlink.symlink_to(secret)
        non_image = self.root / "token.txt"
        non_image.write_text("private")
        for path in (secret, symlink, non_image):
            with self.subTest(path=path):
                self.fixture.session.inventory["images"]["annotated"] = str(path)
                self.controller.publish_session()
                self.assertEqual(self.client.get(f"/api/image/{capture}").status_code, 404)


class WebStartupTests(unittest.TestCase):
    def test_process_lock_prevents_second_web_controller_and_releases_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProcessLock(directory):
                with self.assertRaisesRegex(SystemExit, "Another cup web app"):
                    with ProcessLock(directory):
                        self.fail("Duplicate owner acquired the lock")
            with ProcessLock(directory):
                pass

    def test_unsafe_mode_flags_and_nonfinite_values_fail_before_server_start(self):
        for flags in (["--live"], ["--demo", "--live"], ["--demo", "--gateway", "http://offline.invalid"],
                      ["--port", "80"], ["--speed", "nan"], ["--max-result-age", "inf"]):
            with self.subTest(flags=flags), redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(flags)
                self.assertNotEqual(error.exception.code, 0)

    def test_demo_uses_real_checks_without_any_network_requests(self):
        with tempfile.TemporaryDirectory() as directory, patch("requests.Session.request", side_effect=AssertionError("Network forbidden")):
            controller = Mock()
            session, language = demo_factory(Path(directory))(controller)
            self.assertTrue(session.robot.dry_run)
            self.assertIsNone(session.inventory)
            scan = session.scan()
            self.assertEqual(len(scan["cups"]), 3)
            self.assertEqual([cup["selectable"] for cup in scan["cups"]], [True, True, False])
            proposal = language.propose("hover above cup two")
            with redirect_stdout(io.StringIO()):
                result = language.confirm(proposal["token"])
            self.assertEqual(result["action"], "hover")
            self.assertEqual(len(result["jobs"]), 2)
            self.assertIn("no robot commands", result["message"])


if __name__ == "__main__":
    unittest.main()
