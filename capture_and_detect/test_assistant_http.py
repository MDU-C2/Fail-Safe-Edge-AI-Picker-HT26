"""Assistant HTTP tests use a real synthetic controller and forbid all network I/O."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from assistant_api import AssistantSettings
from dashboard_access import DashboardAccess
from robot_assistant import RobotAssistant
from web_app import create_app, demo_factory, main
from web_controller import WebController


class AssistantHttpTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        network = patch("requests.Session.request", side_effect=AssertionError("External network forbidden in HTTP tests"))
        self.network = network.start()
        self.addCleanup(network.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.settings_path = self.directory / "private" / "assistant.json"
        self.controller = WebController(demo_factory(self.directory / "demo"), mode="demo", model="Scripted demo")
        self.assistant = RobotAssistant(self.controller, image_root=self.directory,
                                        settings=AssistantSettings(self.settings_path))
        self.app = create_app(self.controller, port=8087, image_root=self.directory, assistant=self.assistant)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8087", client=("127.0.0.1", 41000))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.wait_idle()
        self.headers = {"Origin": "http://127.0.0.1:8087", "X-CSRF-Token": self.controller.state()["csrf_token"]}
        self.execute = patch.object(self.controller.session.robot, "execute_jobs", side_effect=AssertionError("Unexpected movement"))
        self.movement = self.execute.start()
        self.addCleanup(self.execute.stop)

    def wait_idle(self):
        deadline = time.monotonic() + 4
        while self.controller.state()["busy"]:
            if time.monotonic() > deadline:
                self.fail("Demo controller did not become idle")
            time.sleep(0.005)
        return self.controller.state()

    def scan(self):
        response = self.client.post("/api/scan", json={"request_id": uuid.uuid4().hex}, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.text)
        state = self.wait_idle()
        self.assertIsNone(state["error"])
        return state

    def assistant_post(self, operation, payload):
        return self.client.post("/api/assistant/" + operation, json=payload, headers=self.headers)

    def test_settings_require_csrf_and_never_return_or_persist_memory_only_key(self):
        key = "sk-fake-http-test-no-provider-request"
        payload = {"api_key": key, "remember": False, "service_tier": "flex", "reasoning": "low"}
        for headers in ({}, {"Origin": self.headers["Origin"]},
                        dict(self.headers, **{"X-CSRF-Token": "wrong"}),
                        dict(self.headers, Origin="https://untrusted.example")):
            with self.subTest(headers=headers):
                response = self.client.post("/api/assistant/settings", json=payload, headers=headers)
                self.assertEqual(response.status_code, 403)
        self.assertFalse(self.assistant.status()["configured"])
        saved = self.assistant_post("settings", payload)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()["configured"])
        self.assertEqual(saved.json()["model"], "gpt-6-astra")
        self.assertEqual(saved.json()["service_tier"], "flex")
        self.assertFalse(self.settings_path.exists())
        for response in (saved, self.client.get("/api/assistant/status"), self.client.get("/api/state")):
            self.assertNotIn(key, response.text)
            self.assertNotIn('"api_key"', response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
        forgotten = self.assistant_post("forget", {})
        self.assertEqual(forgotten.status_code, 200)
        self.assertFalse(forgotten.json()["configured"])
        self.network.assert_not_called()

    def test_remember_is_explicit_private_and_forget_removes_the_saved_key(self):
        key = "sk-fake-remember-test-not-a-real-key"
        response = self.assistant_post("settings", {"api_key": key, "remember": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(key, response.text)
        self.assertEqual(self.settings_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.settings_path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(self.settings_path.read_text())["api_key"], key)
        self.assertEqual(self.assistant_post("forget", {}).status_code, 200)
        self.assertFalse(self.settings_path.exists())

    def test_ask_and_prepare_create_only_existing_reviewed_preview(self):
        initial = self.scan()
        before = self.controller.session.placing_grid.path.read_bytes()
        asked = self.assistant_post("ask", {"text": "Pick the white cup"})
        self.assertEqual(asked.status_code, 200, asked.text)
        suggested = asked.json()
        self.assertEqual(suggested["mode"], "scripted_demo")
        self.assertIn("not GPT", suggested["message"])
        self.assertEqual(suggested["cup_ids"], [2])
        self.assertIsNotNone(suggested["proposal_id"])
        self.assertIsNone(self.controller.state()["proposal"])
        prepared = self.assistant_post("prepare", {"proposal_id": suggested["proposal_id"]})
        self.assertEqual(prepared.status_code, 202, prepared.text)
        self.assertEqual(prepared.json()["assistant_request_id"], suggested["proposal_id"])
        state = self.wait_idle()
        self.assertIsNone(state["error"])
        self.assertEqual(state["proposal"]["action"], "goto")
        self.assertEqual(state["proposal"]["cup_id"], 2)
        self.assertTrue(state["proposal"]["token"])
        self.assertEqual(state["proposal"]["assistant_request_id"], suggested["proposal_id"])
        self.assertEqual(state["inventory"]["capture"], initial["inventory"]["capture"])
        self.assertEqual(self.controller.session.placing_grid.path.read_bytes(), before)
        self.assertEqual(self.assistant_post("prepare", {"proposal_id": suggested["proposal_id"]}).status_code, 409)
        self.movement.assert_not_called()
        self.network.assert_not_called()

    def test_changed_scan_invalidates_assistant_selection_before_prepare(self):
        self.scan()
        suggestion = self.assistant_post("ask", {"text": "Pick cup one"}).json()
        self.assertTrue(suggestion["proposal_id"])
        self.scan()
        response = self.assistant_post("prepare", {"proposal_id": suggestion["proposal_id"]})
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(self.controller.state()["proposal"])
        self.movement.assert_not_called()

    def test_token_bound_cancel_preserves_another_tabs_replacement_preview(self):
        scanned = self.scan()
        suggestion = self.assistant_post("ask", {"text": "Pick the white cup"}).json()
        response = self.assistant_post("prepare", {"proposal_id": suggestion["proposal_id"]})
        self.assertEqual(response.status_code, 202, response.text)
        earlier = self.wait_idle()["proposal"]
        replacement = self.client.post("/api/select", json={"request_id": uuid.uuid4().hex,
            "capture_id": scanned["inventory"]["capture"], "cup_id": 1, "action": "goto"}, headers=self.headers)
        self.assertEqual(replacement.status_code, 202, replacement.text)
        newer = self.wait_idle()["proposal"]
        stale = self.client.post("/api/cancel", json={"request_id": uuid.uuid4().hex,
            "token": earlier["token"]}, headers=self.headers)
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(self.controller.state()["proposal"]["token"], newer["token"])
        cancelled = self.client.post("/api/cancel", json={"request_id": uuid.uuid4().hex,
            "token": newer["token"]}, headers=self.headers)
        self.assertEqual(cancelled.status_code, 202, cancelled.text)
        self.assertIsNone(self.wait_idle()["proposal"])
        self.movement.assert_not_called()
        self.network.assert_not_called()

    def test_json_boundary_rejects_duplicates_unknown_fields_and_large_requests(self):
        cases = [("ask", '{"text":"hello","text":"pick cup one"}', 422),
                 ("ask", '{"text":"hello","confirm":true}', 422),
                 ("prepare", '{"proposal_id":"fake","token":"injected"}', 422),
                 ("prepare", '{"proposal_id":"fake","assistant_request_id":"injected"}', 422),
                 ("unknown", '{}', 404), ("ask", '[]', 422),
                 ("ask", 'x' * 8193, 413), ("ask", '{broken', 422)]
        for operation, body, expected in cases:
            with self.subTest(operation=operation, expected=expected):
                response = self.client.post("/api/assistant/" + operation, content=body,
                                            headers=dict(self.headers, **{"Content-Type": "application/json"}))
                self.assertEqual(response.status_code, expected, response.text)
        response = self.client.post("/api/assistant/ask", content="text=hello",
                                    headers=dict(self.headers, **{"Content-Type": "application/x-www-form-urlencoded"}))
        self.assertEqual(response.status_code, 415)
        self.movement.assert_not_called()

    def test_managed_assistant_requests_bind_the_observed_device_epoch(self):
        original = self.controller.state()
        managed = dict(original, devices={"active_id": "mac", "switching": False},
                       device_epoch="epoch-a", assistant_available=True)
        for operation, body in (("ask", {"text": "What is on the table?"}),
                                ("prepare", {"proposal_id": "draft"})):
            with self.subTest(operation=operation), patch.object(self.controller, "state", return_value=managed), \
                    patch.object(self.assistant, operation, return_value={"accepted": True}) as action:
                for context in ({}, {"_device_id": "mac", "_device_epoch": "old"},
                                {"_device_id": "pi", "_device_epoch": "epoch-a"}):
                    response = self.assistant_post(operation, dict(body, **context))
                    self.assertEqual(response.status_code, 409, response.text)
                action.assert_not_called()
                response = self.assistant_post(operation, dict(body, _device_id="mac", _device_epoch="epoch-a"))
                self.assertEqual(response.status_code, 202 if operation == "prepare" else 200, response.text)
                action.assert_called_once_with(next(iter(body.values())),
                    expected_context={"device_id": "mac", "device_epoch": "epoch-a"})
        self.movement.assert_not_called()

    def test_explicit_local_adapter_configures_the_shared_astra_client(self):
        with patch.object(self.controller, "astra_named_matching", True, create=True), \
                patch.object(self.controller, "configure_astra") as configure:
            create_app(self.controller, port=8087, image_root=self.directory, assistant=self.assistant)
            configure.assert_called_once_with(self.assistant.client, self.assistant.settings)
        self.network.assert_not_called()

    def test_removed_voice_endpoint_and_microphone_permission(self):
        result = self.client.post("/api/assistant/transcribe", content=b"old-recording",
                                 headers=dict(self.headers, **{"Content-Type": "audio/webm"}))
        self.assertEqual(result.status_code, 404)
        self.assertEqual(self.client.get("/").headers["permissions-policy"], "microphone=(), camera=(), geolocation=()")
        self.assertIsNone(self.controller.state()["proposal"])
        self.network.assert_not_called()

    def test_phone_pairing_protects_assistant_and_controller_reads_writes(self):
        origin = "http://192.168.1.20:8087"
        access = DashboardAccess([origin, "http://localhost:8087", "http://127.0.0.1:8087"],
                                 pairing_code_file=self.directory / "pairing-code", allow_lan_http=True)
        app = create_app(self.controller, port=8087, image_root=self.directory, assistant=self.assistant,
                         allowed_origin=origin, access=access)
        # The controller's lifespan is already running in self.client.
        phone = TestClient(app, base_url=origin, client=("192.168.1.50", 41000), follow_redirects=False)
        self.addCleanup(phone.close)
        for path in ("/api/state", "/api/assistant/status", "/api/status"):
            self.assertEqual(phone.get(path).status_code, 401)
        self.assertEqual(phone.get("/").headers["location"], "/pair")
        # Browser form POSTs need a same-origin referrer policy to retain
        # their Origin header and pass the exact-origin pairing check.
        self.assertEqual(phone.get("/pair").headers["referrer-policy"], "same-origin")
        self.assertEqual(phone.post("/pair", data={"code": access.pairing_code},
                                    headers={"Origin": "null"}).status_code, 403)
        self.assertEqual(phone.post("/pair", data={"code": access.pairing_code}).status_code, 403)
        paired = phone.post("/pair", data={"code": access.pairing_code}, headers={"Origin": origin})
        self.assertEqual(paired.status_code, 303, paired.text)
        self.assertNotIn("Secure", paired.headers["set-cookie"])
        self.assertIn("HttpOnly", paired.headers["set-cookie"])
        token = phone.get("/api/state").json()["csrf_token"]
        self.assertEqual(phone.get("/api/assistant/status").status_code, 200)
        payload = {"api_key": "sk-fake-phone-settings-no-network"}
        no_csrf = phone.post("/api/assistant/settings", json=payload, headers={"Origin": origin})
        self.assertEqual(no_csrf.status_code, 403)
        headers = {"Origin": origin, "X-CSRF-Token": token}
        saved = phone.post("/api/assistant/settings", json=payload, headers=headers)
        self.assertEqual(saved.status_code, 403, saved.text)
        self.assertNotIn(payload["api_key"], saved.text)
        self.assertFalse(phone.get("/api/assistant/status").json()["settings_editable"])
        self.assertEqual(phone.post("/api/scan", json={"request_id": uuid.uuid4().hex}, headers={"Origin": origin}).status_code, 403)
        self.assertEqual(phone.post("/api/scan", json={"request_id": uuid.uuid4().hex}, headers=headers).status_code, 202)
        self.wait_idle()
        self.assertEqual(phone.get("/api/assistant/status", headers={"Origin": "https://untrusted.example"}).status_code, 403)
        self.movement.assert_not_called()
        self.network.assert_not_called()

    def test_http_mac_and_paired_phone_share_state_but_key_management_stays_local(self):
        phone_origin = "http://192.168.1.20:8087"
        local_origin = "http://127.0.0.1:8087"
        local_origins = [local_origin, "http://localhost:8087"]
        access = DashboardAccess([phone_origin, *local_origins],
                                 pairing_code_file=self.directory / "shared-pairing-code", allow_lan_http=True)
        app = create_app(self.controller, port=8087, image_root=self.directory, assistant=self.assistant,
                         allowed_origin=phone_origin, access=access)
        local = TestClient(app, base_url=local_origin, client=("127.0.0.1", 41000))
        phone = TestClient(app, base_url=phone_origin, client=("192.168.1.50", 42000), follow_redirects=False)
        self.addCleanup(local.close)
        self.addCleanup(phone.close)
        local_state = local.get("/api/state")
        self.assertEqual(local_state.status_code, 200)
        token = local_state.json()["csrf_token"]
        self.assertEqual(phone.get("/api/state").status_code, 401)
        paired = phone.post("/pair", data={"code": access.pairing_code}, headers={"Origin": phone_origin})
        self.assertEqual(paired.status_code, 303)
        self.assertEqual(phone.get("/api/state").json()["csrf_token"], token)
        payload = {"api_key": "sk-shared-local-and-phone-test-no-network"}
        saved = local.post("/api/assistant/settings", json=payload,
                           headers={"Origin": local_origin, "X-CSRF-Token": token})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(phone.get("/api/assistant/status").json()["configured"])
        wrong_origin = phone.post("/api/assistant/forget", json={},
                                  headers={"Origin": local_origin, "X-CSRF-Token": token})
        self.assertEqual(wrong_origin.status_code, 403)
        cleared = phone.post("/api/assistant/forget", json={},
                             headers={"Origin": phone_origin, "X-CSRF-Token": token})
        self.assertEqual(cleared.status_code, 403)
        self.assertTrue(local.get("/api/assistant/status").json()["configured"])
        self.assertIsNone(phone.get("/api/assistant/status").json()["phone"]["pairing_code"])
        self.assertNotIn("certificate_url", local.get("/api/assistant/status").json()["phone"])
        self.assertEqual(local.get("/api/assistant/status").json()["phone"]["pairing_code"], access.pairing_code)
        local.post("/api/assistant/forget", json={}, headers={"Origin": local_origin, "X-CSRF-Token": token})
        self.assertFalse(local.get("/api/assistant/status").json()["configured"])
        result = phone.post("/api/assistant/ask", json={"text": "Which cups are available?"},
                            headers={"Origin": phone_origin, "X-CSRF-Token": token})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["mode"], "scripted_demo")
        self.movement.assert_not_called()
        self.network.assert_not_called()


class AssistantCliAccessTests(unittest.TestCase):
    def test_nonloopback_startup_requires_https_origin_and_pairing(self):
        with tempfile.TemporaryDirectory() as temporary, patch("uvicorn.run") as server, \
                patch("web_app.hardware_factory") as hardware, redirect_stderr(io.StringIO()):
            root = Path(temporary)
            certificate, key = root / "server.crt", root / "server.key"
            certificate.write_text("offline fixture: server creation is mocked")
            key.write_text("offline fixture")
            tls = ["--tls-cert", str(certificate), "--tls-key", str(key)]
            cases = [[], ["--lan-http"], ["--lan-http", "--allowed-origin", "http://192.168.1.20:8087"],
                     ["--lan-http", "--allowed-origin", "http://8.8.8.8:8087", "--pairing-code-file", str(root / "pairing")], tls, tls + ["--allowed-origin", "https://192.168.1.20:8087"],
                     ["--tls-cert", str(certificate)],
                     tls + ["--allowed-origin", "http://192.168.1.20:8087", "--pairing-code-file", str(root / "pairing")]]
            for arguments in cases:
                with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as error:
                    main(["--demo", "--host", "0.0.0.0", "--port", "8087", *arguments])
                self.assertEqual(error.exception.code, 2)
            server.assert_not_called()
            hardware.assert_not_called()

    def test_http_phone_demo_starts_without_certificates_or_gateway(self):
        with tempfile.TemporaryDirectory() as temporary, patch("uvicorn.run") as server, \
                patch("web_app.hardware_factory") as hardware, \
                patch("requests.Session.request", side_effect=AssertionError("Network forbidden")) as network, \
                redirect_stdout(io.StringIO()):
            root = Path(temporary).resolve()
            result = main(["--demo", "--host", "0.0.0.0", "--port", "8087", "--lan-http",
                           "--allowed-origin", "http://192.168.1.20:8087",
                           "--pairing-code-file", str(root / "pairing"),
                           "--assistant-settings", str(root / "private" / "assistant.json")])
            self.assertEqual(result, 0)
            self.assertEqual(server.call_args.kwargs["host"], "0.0.0.0")
            self.assertIsNone(server.call_args.kwargs["ssl_certfile"])
            self.assertIsNone(server.call_args.kwargs["ssl_keyfile"])
            self.assertFalse(server.call_args.kwargs["proxy_headers"])
            self.assertFalse(server.call_args.kwargs["access_log"])
            hardware.assert_not_called()
            network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
