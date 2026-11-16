"""In-process Mac ownership and canonical-state adoption, without network or hardware."""
import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock, patch

from assistant_api import AssistantSettings
from cup_inventory import atomic_json
from device_agent import DeviceAgent, HandoffStore
from device_dashboard import DeviceDashboard
from device_service import python_factory
from local_device import LocalDeviceClient
from test_device_handoff import AgentClient, FakeController, FakeFactory, FakeStore
from test_named_cup_flow import NamedCupFixture, two_cups, visual_result
from web_controller import WebError


class LocalDeviceLabelTests(unittest.TestCase):
    def test_platform_label_depends_on_local_backend_without_changing_device_ids(self):
        for platform, expected in (("darwin", "MacBook"), ("win32", "Windows PC"), ("linux", "This PC")):
            for local in (True, False):
                with self.subTest(platform=platform, local=local), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    clients = {name: AgentClient(DeviceAgent(name, FakeFactory(), FakeStore(root / name),
                               controller_class=FakeController)) for name in ("mac", "pi", "lynx")}
                    if local:
                        clients["mac"] = LocalDeviceClient(clients["mac"].agent)
                    with patch("device_dashboard.sys.platform", platform):
                        dashboard = DeviceDashboard(clients, root / "owner.json", initial_device="pi")
                    try:
                        for name in clients:
                            dashboard._read(name)
                        entries = {entry["id"]: entry for entry in dashboard.state()["devices"]["entries"]}
                        self.assertEqual(set(entries), {"mac", "pi", "lynx"})
                        self.assertEqual(entries["mac"]["label"], expected if local else "MacBook")
                        self.assertEqual(entries["pi"]["label"], "Raspberry Pi")
                        self.assertEqual(entries["lynx"]["label"], "Lynx")
                        self.assertFalse(dashboard.state()["assistant_available"])
                    finally:
                        dashboard.close()


class LocalDashboardTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        network = patch("requests.Session.request", side_effect=AssertionError("No network in offline tests"))
        network.start()
        self.addCleanup(network.stop)
        self.fixture = NamedCupFixture(self.root / "mac", dry_run=True)
        store = FakeStore(self.root / "mac-state")
        store.capture_root = self.fixture.image_root
        local = LocalDeviceClient(DeviceAgent("mac", self.fixture.factory, store))
        self.clients = {"mac": local}
        self.clients.update({name: AgentClient(DeviceAgent(name, FakeFactory(), FakeStore(self.root / name),
                            controller_class=FakeController)) for name in ("pi", "lynx")})
        self.dashboard = DeviceDashboard(self.clients, self.root / "owner.json", initial_device="mac")
        self.addCleanup(self.dashboard.close)
        self.settings = AssistantSettings(self.root / "assistant.json")
        self.settings.update({"api_key": "sk-offline-local-test"})
        self.client = Mock()
        self.client.match.return_value = visual_result()
        self.dashboard.configure_astra(self.client, self.settings)
        self.dashboard._resume()
        for name in self.clients:
            self.dashboard._read(name)
        self.wait_idle()

    def wait_idle(self):
        deadline = time.monotonic() + 3
        while self.dashboard.state()["busy"]:
            if time.monotonic() > deadline:
                self.fail("Offline picker did not finish")
            time.sleep(.005)
        return self.dashboard.state()

    def payload(self, **values):
        state = self.dashboard.state()
        return dict(_device_id=state["devices"]["active_id"], _device_epoch=state["device_epoch"],
                    request_id=uuid.uuid4().hex, **values)

    def submit(self, operation, **values):
        self.dashboard.submit(operation, self.payload(**values))
        return self.wait_idle()

    def test_real_local_preview_has_source_and_rejects_old_context_after_round_trip(self):
        state = self.submit("scan")
        original_payload = self.payload(capture_id=state["inventory"]["capture"], cup_id=1, action="goto")
        source = uuid.uuid4().hex
        self.dashboard.submit("select", original_payload, assistant_request_id=source)
        state = self.wait_idle()
        self.assertTrue(state["assistant_available"])
        self.assertEqual(state["proposal"]["assistant_request_id"], source)
        self.assertEqual(self.fixture.robot.jobs, [])
        self.dashboard._switch("pi")
        self.assertFalse(self.dashboard.state()["assistant_available"])
        with self.assertRaises(WebError):
            self.dashboard.submit("scan", self.payload(), assistant_camera=True)
        self.assertEqual(self.clients["pi"].count("/v1/command"), 0)
        self.dashboard._switch("mac")
        state = self.wait_idle()
        self.assertTrue(state["assistant_available"])
        self.assertIsNone(state["proposal"])
        with self.assertRaises(WebError):
            self.dashboard.submit("select", original_payload, assistant_request_id=source)
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_named_library_images_and_shared_key_survive_reactivation(self):
        self.submit("scan")
        capture = self.fixture.session.inventory["capture"]
        state = self.submit("save_cup", capture_id=capture, cup_id=1, name="Kasun's cup")
        saved = self.fixture.store.list()[0]
        reference = self.dashboard.saved_image(saved["id"])
        self.assertIsNotNone(reference)
        self.assertIsNotNone(self.dashboard.image_bytes(capture))
        self.dashboard._switch("lynx")
        self.assertIsNone(self.dashboard.saved_image(saved["id"]))
        self.dashboard._switch("mac")
        self.wait_idle()
        self.assertEqual(self.dashboard.saved_image(saved["id"]), reference)
        self.fixture.observations.append({"objects": two_cups()[:2]})
        source = uuid.uuid4().hex
        self.dashboard.submit("pick_saved", self.payload(id=saved["id"]), assistant_request_id=source)
        state = self.wait_idle()
        self.assertIsNone(state["error"])
        self.assertEqual(state["proposal"]["assistant_request_id"], source)
        self.client.match.assert_called_once()
        self.submit("cancel", token=state["proposal"]["token"])
        self.settings.clear()
        self.assertFalse(self.dashboard.state()["saved_cups"]["matcher_enabled"])
        self.client.match.assert_called_once()
        self.assertEqual(self.fixture.robot.jobs, [])

    def test_auto_camera_admission_cannot_cancel_new_preview(self):
        state = self.submit("scan")
        self.submit("select", capture_id=state["inventory"]["capture"], cup_id=1, action="goto")
        pending = copy.deepcopy(self.fixture.language.pending)
        captures = list(self.fixture.captures)
        with self.assertRaisesRegex(WebError, "current preview"):
            self.dashboard.submit("scan", self.payload(), assistant_camera=True)
        self.assertEqual(self.fixture.language.pending, pending)
        self.assertEqual(self.fixture.captures, captures)
        self.submit("cancel")
        self.dashboard.submit("scan", self.payload(), assistant_camera=True)
        self.assertIsNone(self.wait_idle()["error"])
        self.assertEqual(len(self.fixture.captures), len(captures) + 1)

    def test_auto_camera_rejects_controls_recovery_stop_and_untrusted_payload(self):
        controller = self.clients["mac"].agent.controller
        for field, value in (("recovery_required", True), ("stop_requested", True),
                             ("robot_controls", {"proposal": {"token": "offline"}})):
            with self.subTest(field=field), controller._lock:
                before = copy.deepcopy(controller._state[field])
                controller._state[field] = value
                try:
                    with self.assertRaises(WebError):
                        self.dashboard.submit("scan", self.payload(), assistant_camera=True)
                finally:
                    controller._state[field] = before
        for values in ({"assistant_camera": True}, {"assistant_request_id": "a" * 32}):
            with self.assertRaises(WebError):
                self.dashboard.submit("scan", self.payload(**values))
        self.assertEqual(self.fixture.captures, [])

    def test_local_read_failure_has_newer_revision_and_removes_capability(self):
        before = self.dashboard.state()
        with patch.object(self.clients["mac"], "request", side_effect=WebError("Offline test failure")):
            after = self.dashboard.state()
        self.assertEqual(before["instance_id"], after["instance_id"])
        self.assertGreater(after["revision"], before["revision"])
        self.assertFalse(after["assistant_available"])
        self.assertFalse(after["ready"])

    def test_malformed_owner_startup_creates_no_lock_and_close_is_idempotent(self):
        broken = self.root / "broken-owner.json"
        broken.write_text("not json")
        with self.assertRaises(ValueError):
            DeviceDashboard(self.clients, broken)
        self.assertFalse(broken.with_suffix(".lock").exists())
        self.dashboard.close()
        self.dashboard.close()
        self.assertFalse(self.dashboard.state()["assistant_available"])


class CanonicalRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.metadata = self.root / "state"
        self.runtime = self.root / "current"
        self.metadata.mkdir()
        self.runtime.mkdir()
        (self.runtime / "captures").mkdir()
        self.paths = {"placing_grid": self.runtime / "grid.json", "output": self.runtime / "captures",
                      "calibration": self.runtime / "calibration.json",
                      "table_reference": self.runtime / "grid.table-reference.json"}
        atomic_json(self.paths["placing_grid"], {"cell_size_mm": 90, "locations": [
            {"id": number, "x": 350 + number * 90, "y": -260, "z": 230, "occupied": number == 1,
             "reserved": False, "vision_blocked": False} for number in (1, 2)]})
        self.paths["calibration"].write_text('{"test_calibration":true}')
        self.paths["table_reference"].write_text('{"test_reference":true}')
        self.store = HandoffStore(self.metadata, "offline-robot", runtime_paths=self.paths)
        self.session = SimpleNamespace(speed=250, home_speed=100, camera_z=False, handover_offset=(0, 0, 0))

    def bundle(self):
        grid = self.store.grid._read()
        for p in grid["locations"]:
            p["occupied"] = p["id"] == 2
        return {"identity": self.store.identity(), "grid": grid,
                "recovery": {"schema_version": 1, "recovery_required": False},
                "motion": {"speed": 250, "home_speed": 100, "camera_z": False, "handover_offset": [0, 0, 0]}}

    def test_first_adoption_only_adds_occupancy_and_preserves_both_originals(self):
        bundle = self.bundle()
        original = copy.deepcopy(bundle)
        self.assertFalse(self.store._binding_path.exists())
        self.store.install(bundle)
        self.assertEqual(bundle, original)
        self.assertEqual([p["occupied"] for p in self.store.grid._read()["locations"]], [True, True])
        self.assertIn("both devices", self.store.handoff_notice)
        backup = json.loads((self.metadata / "device-state-before-handoff.json").read_text())
        self.assertEqual([p["occupied"] for p in backup["grid"]["locations"]], [True, False])
        self.assertFalse(self.store.pending.exists())
        self.assertFalse(self.store.recovery.required())
        # Once adopted, a normal source-authoritative handoff can transfer a
        # deliberate clear, without permanently merging stale occupancy.
        self.store.install(original)
        self.assertEqual(self.store.grid._read(), original["grid"])
        self.assertIsNone(self.store.handoff_notice)

    def test_later_standalone_change_requires_conservative_adoption_again(self):
        self.store.export(self.session)
        current = self.store.grid._read()
        current["locations"][1]["occupied"] = True
        atomic_json(self.store.grid.path, current)
        bundle = self.bundle()
        for p in bundle["grid"]["locations"]:
            p["occupied"] = False
        self.store.install(bundle)
        self.assertTrue(all(p["occupied"] for p in self.store.grid._read()["locations"]))

    def test_adoption_never_bypasses_recovery_reservation_or_geometry_guard(self):
        original = self.store.grid.path.read_bytes()
        bundle = self.bundle()
        bundle["grid"]["locations"][0]["x"] += 1
        with self.assertRaises(WebError):
            self.store.install(bundle)
        self.assertEqual(self.store.grid.path.read_bytes(), original)
        self.assertFalse(self.store._binding_path.exists())
        self.store.recovery.begin({"test": True})
        with self.assertRaises(WebError):
            self.store.install(self.bundle())
        self.assertEqual(self.store.grid.path.read_bytes(), original)
        self.store.recovery.clear()
        grid = self.store.grid._read()
        grid["locations"][0]["reserved"] = True
        atomic_json(self.store.grid.path, grid)
        with self.assertRaises(WebError):
            self.store.install(self.bundle())
        self.assertFalse(self.store._binding_path.exists())

    def test_failed_adoption_baseline_write_keeps_interrupted_transfer_guard(self):
        original_atomic = atomic_json
        def fail_binding(path, value):
            if Path(path) == self.store._binding_path:
                raise OSError("Offline injected storage failure")
            return original_atomic(path, value)
        with patch("device_agent.atomic_json", side_effect=fail_binding), self.assertRaises(OSError):
            self.store.install(self.bundle())
        self.assertTrue(self.store.pending.exists())
        self.assertTrue(self.store.receive_blocked())

    def test_local_config_uses_current_paths_without_activation_or_lock_files(self):
        model = self.root / "fake-model.pt"
        model.write_bytes(b"offline fixture")
        value = {"backend": "local", "state_dir": str(self.metadata), "model": str(model),
                 "gateway": "http://127.0.0.1:59999", "robot_id": "offline-robot",
                 "runtime_paths": {key: str(path) for key, path in self.paths.items()}}
        factory = FakeFactory()
        with patch("local_device.python_factory", return_value=factory) as create:
            local = LocalDeviceClient.from_config(value, self.root, mode="dry_run")
        try:
            self.assertEqual(create.call_args.args[0].max_result_age, 30)
            self.assertTrue(create.call_args.kwargs["prefer_environment_token"])
            self.assertEqual(factory.calls, 0)
            self.assertFalse((self.metadata / "device-owner.json").exists())
            self.assertFalse(self.store._binding_path.exists())
            locks = [self.metadata / "picker.lock", self.metadata / ".placing-grid.json.controller.lock",
                     self.runtime / ".grid.json.controller.lock", self.paths["output"] / ".cup-web.lock"]
            for path in locks:
                with self.subTest(path=path.name):
                    self.assertFalse(path.exists())
        finally:
            local.close()
        for path in locks:
            self.assertFalse(path.exists())

    def test_factory_maps_canonical_paths_and_preserves_explicit_gateway_environment(self):
        args = SimpleNamespace(state_dir=self.metadata, model=self.root / "fake-model.pt", threads=3,
                               max_result_age=30.0, depth_frames=5)
        factory = python_factory(args, runtime_paths=self.paths, prefer_environment_token=True)
        sentinel = object()
        with patch.dict(os.environ, {"GATEWAY_TOKEN": "offline-only-environment"}), \
                patch("device_service.read_gateway_token", side_effect=AssertionError("Must use existing environment")), \
                patch("web_app.hardware_factory", return_value=lambda _: sentinel) as hardware:
            self.assertIs(factory(object()), sentinel)
        options = hardware.call_args.args[0]
        self.assertEqual(options.output, str(self.paths["output"]))
        self.assertEqual(options.placing_grid, str(self.paths["placing_grid"]))
        self.assertEqual(options.calibration, str(self.paths["calibration"]))
        self.assertFalse(options.llm)
        self.assertTrue(options.execution_lock_held)
        self.assertEqual(options.verification_frames, 1)


if __name__ == "__main__":
    unittest.main()
