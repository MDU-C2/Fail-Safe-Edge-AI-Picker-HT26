"""Observer boundary and disconnect tests. No real gateway or robot calls."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from gateway_observer import (DemoObserver, ENDPOINTS, GatewayObserver, MAX_RESPONSE,
                              RWS_KEYS, diagnostics_token, normalize_url, observer_from_options, public_data)
from observer_app import create_observer_app


class Response:
    def __init__(self, data, status=200):
        self.status_code = status
        self.raw = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, size):
        yield self.raw


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.monitor = GatewayObserver("http://offline.invalid:8000", "private-token", clock=lambda: self.now)
        self.monitor._stop = Mock(is_set=lambda: False, wait=lambda _: None)
        self.calls = []
        self.values = {key: copy.deepcopy(value["data"]) for key, value in DemoObserver().snapshot()["sections"].items()}
        self.overrides = {}
        self.monitor._session = Mock(get=self.get)
        self.monitor.snapshot()  # A viewer creates demand, without doing IO.

    def get(self, url, **options):
        key = next(key for key, path in ENDPOINTS.items() if url == self.monitor.url + path)
        self.calls.append((key, options))
        result = self.overrides.get(key, Response(self.values[key]))
        if isinstance(result, Exception):
            raise result
        return result

    def test_fixed_gets_use_diagnostics_and_never_redirect_credentials(self):
        self.monitor._poll_once()
        self.assertEqual([key for key, _ in self.calls], list(ENDPOINTS))
        for key, options in self.calls:
            self.assertIs(options["allow_redirects"], False)
            self.assertEqual(options["headers"]["X-Dev-Client"], "diagnostics")
            self.assertEqual(options["headers"]["Connection"], "close")
        state = self.monitor.snapshot()
        self.assertEqual(state["status"], "live")
        self.assertNotIn("private-token", json.dumps(state))

    def test_many_browser_reads_share_cache_and_do_not_poll_gateway(self):
        for _ in range(20):
            self.monitor.snapshot()
        self.assertEqual(self.calls, [])
        self.monitor._poll_once()
        before = len(self.calls)
        for _ in range(20):
            self.monitor.snapshot()
        self.assertEqual(len(self.calls), before)

    def test_no_requests_without_viewer_demand(self):
        self.now += self.monitor.DEMAND_WINDOW + 1
        self.monitor._poll_once()
        self.assertEqual(self.calls, [])

    def test_health_failure_preserves_last_sample_and_backs_off(self):
        self.monitor._poll_once()
        before = self.monitor.snapshot()["sections"]["health"]["updated_at"]
        self.now += 3
        self.overrides["health"] = requests.Timeout("secret private-token")
        self.calls.clear()
        self.monitor._poll_once()
        state = self.monitor.snapshot()
        self.assertEqual(state["status"], "offline")
        self.assertEqual(state["sections"]["health"]["updated_at"], before)
        self.assertEqual(state["sections"]["health"]["age_seconds"], 3)
        self.assertTrue(state["sections"]["health"]["stale"])
        self.assertEqual([key for key, _ in self.calls], ["health"])
        self.assertGreaterEqual(self.monitor._retry_at, self.now + 12)
        self.assertNotIn("private-token", json.dumps(state))

    def test_reconnect_and_takeover_do_not_reassign_accepted_queue_owner(self):
        self.monitor._poll_once()
        self.values["control"].update(client_id="secondary", priority=200)
        self.monitor._poll_once()
        state = self.monitor.snapshot()
        self.assertEqual(state["sections"]["control"]["data"]["client_id"], "secondary")
        self.assertEqual(state["sections"]["queue"]["data"]["owner"], "primary")
        self.assertIn("Control held by secondary", [e["message"] for e in state["events"]])

    def test_rejected_credentials_never_become_idle_or_healthy(self):
        self.overrides["control"] = Response({"detail": "private-token"}, 403)
        self.monitor._poll_once()
        state = self.monitor.snapshot()
        self.assertEqual(state["status"], "auth_error")
        self.assertEqual(state["sections"]["control"]["state"], "auth_error")
        self.assertNotIn("private-token", json.dumps(state))
        self.assertEqual(len(self.calls), 2)

    def test_unreachable_controller_skips_rws_but_keeps_gateway_status(self):
        self.values["health"]["robot_controller"]["reachable"] = False
        self.values["health"]["status"] = "degraded"
        self.monitor._poll_once()
        self.assertFalse(set(RWS_KEYS).intersection(key for key, _ in self.calls))
        self.assertEqual(self.monitor.snapshot()["sections"]["arms"]["state"], "skipped")

    def test_old_unsupported_queue_does_not_hide_camera_or_arms(self):
        self.overrides["queue"] = Response({}, 404)
        self.monitor._poll_once()
        state = self.monitor.snapshot()
        self.assertEqual(state["sections"]["queue"]["state"], "unsupported")
        self.assertEqual(state["sections"]["arms"]["state"], "ok")

    def test_stale_snapshot_does_not_invent_new_timestamp(self):
        self.monitor._poll_once()
        self.now += 25
        state = self.monitor.snapshot()
        self.assertEqual(state["status"], "offline")
        self.assertTrue(all(s["stale"] for s in state["sections"].values()))

    def test_response_limit_and_malformed_health_fail_closed(self):
        for value in ({"detail": "x" * MAX_RESPONSE}, [], {"status": "invented"}, {"status": "ok", "uptime_seconds": 10**400}):
            self.overrides["health"] = Response(value)
            self.assertEqual(self.monitor._get("health")["state"], "unavailable")

    def test_public_projection_removes_jobs_and_redacts_token(self):
        projected = public_data("queue", {"state": "running", "owner": "private-token", "token": "secret",
                                          "jobs": [{"body": "secret"}], "steps": [{"name": "cup", "body": "secret"}]}, "private-token")
        self.assertEqual(projected["owner"], "[redacted]")
        self.assertNotIn("secret", json.dumps(projected))

    def test_quaternion_normalization_and_missing_pose(self):
        pose = public_data("arms", {"left": {"q1": 0, "q2": 2, "q3": 0, "q4": 0}})
        self.assertAlmostEqual(pose["left"]["rotation"][0], 180)
        self.assertEqual(pose["right"], {})

    def test_url_rejects_embedded_credentials_and_arbitrary_paths(self):
        self.assertEqual(normalize_url("http://localhost:8000/api/v1/"), "http://localhost:8000/api/v1")
        for url in ("http://user:secret@host", "file:///tmp/token", "http://host/control", "http://host?token=secret"):
            with self.assertRaises(ValueError):
                normalize_url(url)

    def test_token_file_precedes_environment_and_is_never_required_for_demo(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GATEWAY_DIAG_TOKEN": "environment"}):
            path = Path(directory) / "diagnostics-token"
            path.write_text("file-token\n")
            self.assertEqual(diagnostics_token(path), "file-token")
            self.assertEqual(diagnostics_token(), "environment")

    def test_main_dashboard_can_reuse_its_existing_read_credential(self):
        args = SimpleNamespace(gateway="http://offline.invalid", device_config=None, observer_token_file=None)
        with patch("gateway_observer.diagnostics_token", return_value=None), patch("active_device_status._environment_token", return_value="existing-ui-token"):
            monitor = observer_from_options(args, mode="live")
        self.assertEqual(monitor._token, "existing-ui-token")
        self.assertIsNone(monitor._thread)

    def test_standalone_token_lookup_never_uses_primary_environment(self):
        with patch.dict(os.environ, {"GATEWAY_TOKEN": "primary-secret"}, clear=True), patch.object(Path, "read_text", side_effect=FileNotFoundError):
            self.assertIsNone(diagnostics_token())


class ObserverHTTPTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        self.monitor = DemoObserver()
        self.client = TestClient(create_observer_app(self.monitor), base_url="http://127.0.0.1:8088", client=("127.0.0.1", 40000))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_standalone_page_has_no_picker_scripts_and_only_read_routes(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('/static/app.js', page.text)
        self.assertNotIn('stop-button', page.text)
        self.assertIn('id="view-observer"', page.text)
        for path in ("/api/stop", "/api/confirm", "/api/control_preview", "/api/observer"):
            self.assertEqual(self.client.post(path, json={}).status_code, 405)
        self.assertEqual(self.client.get("/api/state").status_code, 404)

    def test_snapshot_and_assets_are_no_store_and_demo_is_explicit(self):
        for path in ("/", "/observer", "/static/observer.js", "/static/observer.css", "/api/observer"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
        data = self.client.get("/api/observer").json()
        self.assertTrue(data["demo"])
        self.assertTrue(data["read_only"])
        self.assertEqual(data["status"], "demo")

    def test_cross_origin_bad_hosts_and_unlisted_assets_are_rejected(self):
        self.assertEqual(self.client.get("/api/observer", headers={"Origin": "https://evil.invalid"}).status_code, 403)
        self.assertEqual(self.client.get("/api/observer", headers={"Host": "evil.invalid"}).status_code, 403)
        self.assertEqual(self.client.get("/static/app.js").status_code, 404)

    def test_main_dashboard_exposes_same_tab_with_existing_authentication(self):
        from fastapi.testclient import TestClient
        from web_app import create_app
        controller = Mock()
        controller.astra_named_matching = False
        controller.state.return_value = {"csrf_token": "test-csrf"}
        app = create_app(controller, observer_monitor=self.monitor)
        with TestClient(app, base_url="http://127.0.0.1:8080") as client:
            self.assertEqual(client.get("/api/observer").status_code, 200)
            self.assertIn('id="nav-observer"', client.get("/").text)
            self.assertEqual(client.get("/observer").status_code, 200)
            self.assertEqual(client.get("/api/observer", headers={"Origin": "https://evil.invalid"}).status_code, 403)
        controller.start.assert_called_once()
        controller.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
