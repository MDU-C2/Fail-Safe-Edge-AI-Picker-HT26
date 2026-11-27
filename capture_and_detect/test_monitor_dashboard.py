"""Second-computer monitoring: no commands, no ownership and honest data age."""
import copy
import io
import json
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from fastapi.testclient import TestClient

from gateway_observer import DemoObserver, GatewayObserver
from monitor_dashboard import CameraReader, MonitorAssistant, MonitorController, camera_jpeg
from web_app import create_app, main


class FullMonitorTests(unittest.TestCase):
    def setUp(self):
        self.observation = DemoObserver().snapshot()
        self.observation["demo"] = False
        self.observer = Mock(url="http://offline.invalid/api/v1", snapshot=lambda **_: copy.deepcopy(self.observation))
        self.controller = MonitorController(self.observer, headers={"Authorization": "Bearer diagnostics-only"})
        self.client = TestClient(create_app(self.controller, port=8087, observer_monitor=self.observer,
                                           assistant=MonitorAssistant()),
                                 base_url="http://127.0.0.1:8087", client=("127.0.0.1", 1234))

    def tearDown(self):
        self.client.close()
        self.controller.close()

    def test_full_page_and_all_assets_exist_but_never_initialize_a_picker(self):
        with patch("web_app.hardware_factory", side_effect=AssertionError("Cannot construct a picker")):
            page = self.client.get("/")
            for tab in ("pick", "assistant", "robot", "scene", "observer", "settings"):
                self.assertIn(f'id="nav-{tab}"', page.text)
            for asset in ("app.css", "app.js", "assistant.js", "observer.css", "observer.js", "3d/viewer3d.js"):
                self.assertEqual(self.client.get("/static/" + asset).status_code, 200)
            state = self.client.get("/api/state").json()
        self.assertTrue(state["read_only"])
        self.assertFalse(state["ready"])
        self.assertIsNone(state["placement_grid"])
        self.assertIsNone(state["inventory"])
        self.assertNotIn("diagnostics-only", json.dumps(state))
        self.assertIsNone(self.controller.camera.thread)
        self.assertIsNone(self.controller.telemetry._thread)

    def test_every_mutation_is_blocked_even_with_valid_csrf(self):
        state = self.client.get("/api/state").json()
        headers = {"Origin": "http://127.0.0.1:8087", "X-CSRF-Token": state["csrf_token"]}
        for path in ("/api/scan", "/api/stop", "/api/confirm", "/api/recover", "/api/device_select",
                     "/api/assistant/ask", "/api/assistant/settings", "/api/robot_status"):
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                self.assertEqual(self.client.request(method, path, headers=headers, json={}).status_code, 405)
        self.assertFalse(self.client.get("/api/assistant/status").json()["settings_editable"])

    def test_origin_and_remote_clients_rejected(self):
        self.assertEqual(self.client.get("/api/state", headers={"Origin": "http://foreign.invalid"}).status_code, 403)
        self.assertEqual(self.client.get("/api/state", headers={"Host": "foreign.invalid"}).status_code, 403)
        other = TestClient(self.client.app, base_url="http://127.0.0.1:8087", client=("192.0.2.2", 12))
        self.assertEqual(other.get("/api/state").status_code, 403)
        other.close()

    def test_pi_takeover_does_not_change_original_queue_owner(self):
        self.observation["sections"]["control"]["data"]["client_id"] = "secondary"
        role = self.client.get("/api/active_device").json()
        self.assertEqual(role["device_id"], "pi")
        self.assertEqual(role["queue"]["owner"], "primary")
        state = self.client.get("/api/state").json()
        self.assertTrue(state["busy"])
        self.assertEqual(state["progress"]["completed"], 8)
        self.assertIsNone(state["batch"], "A queue is not the whole cup batch")

    def test_lost_and_reconnected_data_never_looks_like_idle_or_a_new_scan(self):
        self.observation["status"] = "offline"
        self.observation["sections"]["health"]["stale"] = True
        # A failed health read must invalidate ownership immediately, even
        # before the separate cached control reading reaches its age limit.
        self.assertIsNone(self.client.get("/api/active_device").json()["client_id"])
        for key in ("health", "control", "queue"):
            self.observation["sections"][key]["stale"] = True
        self.observation["status"] = "offline"
        role = self.client.get("/api/active_device").json()
        self.assertEqual(role["status"], "unavailable")
        self.assertIsNone(role["client_id"])
        state = self.client.get("/api/state").json()
        self.assertFalse(state["monitor"]["gateway_online"])
        self.assertIsNone(state["progress"])
        self.assertIsNone(state["inventory"])
        self.observation = DemoObserver().snapshot()
        self.assertTrue(self.client.get("/api/state").json()["monitor"]["gateway_online"])

    def test_only_visible_pick_requests_camera_demand(self):
        with patch.object(self.controller.camera, "snapshot", return_value=None) as snapshot:
            self.client.get("/api/state")
            snapshot.assert_called_with(demand=False)
            self.client.get("/api/state?camera=true")
            snapshot.assert_called_with(demand=True)
        with patch.object(self.controller.telemetry, "snapshot", return_value={"arms": {}, "error": "private diagnostics-only"}):
            self.assertNotIn("diagnostics-only", self.client.get("/api/robot-pose").text)

    def test_monitor_launch_does_not_call_normal_dashboard_factories(self):
        with patch("web_app.hardware_factory", side_effect=AssertionError("Picker startup")), \
                patch("device_dashboard.DeviceDashboard.from_config", side_effect=AssertionError("Owner startup")), \
                patch("uvicorn.run") as server:
            main(["--monitor", "--demo", "--port", "8087"])
            self.assertEqual(server.call_args.kwargs["host"], "127.0.0.1")
        for extra in (["--live"], ["--device-config", "private.json"], ["--host", "0.0.0.0"]):
            with self.assertRaises(SystemExit), patch("uvicorn.run") as server:
                main(["--monitor", "--gateway", "http://offline.invalid", *extra])
            server.assert_not_called()

    def test_overview_uses_no_rws_details_until_observer_is_opened(self):
        monitor = GatewayObserver("http://offline.invalid", clock=lambda: 100)
        monitor._stop = Mock(is_set=lambda: False, wait=lambda _: None)
        calls = []
        monitor._get = lambda key: calls.append(key) or {"state": "ok", "data": {"robot_controller": {"reachable": True}}}
        monitor.snapshot(overview=True)
        monitor._poll_once()
        self.assertEqual(calls, ["health", "control", "queue", "camera"])
        calls.clear()
        monitor.snapshot()
        monitor._poll_once()
        self.assertIn("arms", calls)


class CameraTests(unittest.TestCase):
    def test_planar_oak_frame_has_correct_color_and_rejects_bad_dimensions(self):
        headers = {"X-Frame-Width": "2", "X-Frame-Height": "2", "X-Pixel-Format": "BGR888p"}
        raw = bytes([0] * 8 + [255] * 4)  # Blue plane, green plane, red plane.
        image = Image.open(io.BytesIO(camera_jpeg(raw, headers)))
        r, g, b = image.getpixel((0, 0))
        self.assertGreater(r, 240)
        self.assertLess(g + b, 15)
        for bad in ({**headers, "X-Frame-Width": "99999999"}, {**headers, "X-Pixel-Format": "unknown"}):
            with self.assertRaises(ValueError):
                camera_jpeg(raw, bad)
        with self.assertRaises(ValueError):
            camera_jpeg(raw[:-1], headers)

    def test_cache_is_bounded_and_uses_only_fixed_get_with_no_redirect(self):
        reader = CameraReader("http://offline.invalid/api/v1", {"Authorization": "Bearer diagnostics-only"}, clock=lambda: 10)
        response = Mock(status_code=200, headers={"X-Frame-Width": "2", "X-Frame-Height": "2", "X-Pixel-Format": "RGB8"},
                        iter_content=lambda _: [bytes([0] * 12)])
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        session = Mock(get=Mock(return_value=response))
        for _ in range(5):
            reader._read(session)
        self.assertEqual(len(reader.images), 3)
        self.assertEqual(session.get.call_args.args, ("http://offline.invalid/api/v1/camera/rgb",))
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertIsNone(reader.snapshot()["saved_at"], "Do not invent a sensor timestamp")
        self.assertFalse(reader.snapshot()["stale"])
        reader.failed = True
        self.assertTrue(reader.snapshot()["stale"])
        self.assertEqual(reader.snapshot()["age_seconds"], 0, "Errors must retain the true observation time")
        self.assertIsNone(reader.image("../../private-file"))


if __name__ == "__main__":
    unittest.main()
