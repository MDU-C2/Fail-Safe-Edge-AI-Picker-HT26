"""Offline startup diagnostics and safe restart reconciliation, with no robot I/O."""
import copy
from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from device_agent import DeviceAgent
from device_dashboard import DeviceDashboard
from local_device import LocalDeviceClient
from test_device_handoff import AgentClient, FakeController, FakeFactory, FakeStore
from web_controller import WebController, WebError


class FailingStartup(FakeFactory):
    def __init__(self, error):
        super().__init__()
        self.error = error
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, controller):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(3):
            raise AssertionError("Offline startup fixture was not released")
        raise RuntimeError(self.error)


class DeviceStartupStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        stderr = redirect_stderr(io.StringIO())
        stderr.__enter__()
        self.addCleanup(stderr.__exit__, None, None, None)
        network = patch("requests.Session.request", side_effect=AssertionError("No network in startup tests"))
        network.start()
        self.addCleanup(network.stop)
        self.clients = {name: AgentClient(DeviceAgent(name, FakeFactory(), FakeStore(self.root / name),
                            controller_class=FakeController)) for name in ("mac", "pi", "lynx")}
        self.addCleanup(lambda: [client.agent.close() for client in self.clients.values()])

    def dashboard(self, *, initial="mac"):
        dashboard = DeviceDashboard(self.clients, self.root / "dashboard.json", initial_device=initial)
        self.addCleanup(dashboard.close)
        dashboard._resume()
        for name in self.clients:
            dashboard._read(name)
        return dashboard

    def failing(self, device, error, *, local=False):
        factory = FailingStartup(error)
        self.addCleanup(factory.release.set)
        agent = DeviceAgent(device, factory, FakeStore(self.root / device), controller_class=WebController)
        self.clients[device] = LocalDeviceClient(agent) if local else AgentClient(agent)
        return factory, agent

    def finish(self, dashboard, factory, device):
        factory.release.set()
        deadline = time.monotonic() + 3
        while self.clients[device].agent.controller.state()["busy"]:
            self.assertLess(time.monotonic(), deadline, "Offline startup did not finish")
            time.sleep(.005)
        dashboard._read(device)
        return dashboard.state()

    def entry(self, state, device):
        return next(item for item in state["devices"]["entries"] if item["id"] == device)

    def test_target_starting_and_failed_keep_ownership_but_never_claim_ready_or_allow_switch(self):
        error = "Model startup needs 128.0 MiB; only 110.0 MiB is available."
        factory, agent = self.failing("lynx", error)
        dashboard = self.dashboard()
        dashboard._switch("lynx")
        self.assertTrue(factory.entered.wait(1))
        dashboard._read("lynx")
        state = dashboard.state()
        self.assertEqual(dashboard.record["phase"], "active", "Ownership remains durable during model startup")
        self.assertEqual(state["devices"]["active_id"], "lynx")
        self.assertEqual(self.entry(state, "lynx")["status"], "Starting")
        self.assertIn("starting its picker and model", state["devices"]["message"])
        self.assertFalse(state["ready"])
        self.assertFalse(state["devices"]["can_switch"])
        owner, record = copy.deepcopy(agent.owner), copy.deepcopy(dashboard.record)

        state = self.finish(dashboard, factory, "lynx")
        entry = self.entry(state, "lynx")
        self.assertEqual(entry["status"], "Startup failed")
        self.assertIn(error, entry["reason"])
        self.assertIn("restart the Lynx picker service", entry["reason"])
        self.assertIn("Refreshing this page does not reload the model", entry["reason"])
        self.assertEqual(state["error"], error)
        self.assertEqual(state["message"], entry["reason"])
        self.assertEqual(state["devices"]["message"], entry["reason"])
        self.assertFalse(state["devices"]["can_switch"])
        with self.assertRaises(WebError):
            dashboard.submit("device_select", {"_device_id": "lynx", "_device_epoch": dashboard._epoch,
                "device_id": "mac", "request_id": "offline-switch-1"})
        dashboard._resume()
        self.assertEqual(factory.calls, 1, "Status and resume never retry a failed startup automatically")
        self.assertEqual(agent.owner, owner)
        self.assertEqual(dashboard.record, record)

    def test_local_mac_failure_requests_dashboard_restart_and_preserves_exact_child_error(self):
        error = "No module named 'lynx_picker'"
        factory, _ = self.failing("mac", error, local=True)
        dashboard = self.dashboard()
        self.assertTrue(factory.entered.wait(1))
        dashboard._error = "An earlier device switch was rejected."
        state = self.finish(dashboard, factory, "mac")
        entry = self.entry(state, "mac")
        self.assertEqual(entry["status"], "Startup failed")
        self.assertEqual(state["error"], error)
        self.assertIn("restart this dashboard to reload the Mac picker", entry["reason"])
        self.assertNotIn("restart the MacBook picker service", entry["reason"])
        self.assertFalse(state["assistant_available"])

    def test_remote_mac_failure_requests_service_restart(self):
        factory, _ = self.failing("mac", "Cannot load offline test weights")
        dashboard = self.dashboard()
        state = self.finish(dashboard, factory, "mac")
        self.assertIn("restart the MacBook picker service", self.entry(state, "mac")["reason"])
        self.assertNotIn("restart this dashboard", self.entry(state, "mac")["reason"])

    def test_service_restart_reconciles_generation_without_reinstalling_records_or_commands(self):
        factory, old_agent = self.failing("lynx", "Offline startup failure")
        dashboard = self.dashboard()
        dashboard._switch("lynx")
        self.finish(dashboard, factory, "lynx")
        old_context = copy.deepcopy(dashboard.record["context"])
        records = old_agent.store.root / "preserved-fixture-records.json"
        records.write_bytes(b'{"reserved":[2,5],"recovery_required":true}')
        before = records.read_bytes()
        old_agent.close()
        new_agent = DeviceAgent("lynx", FakeFactory(), FakeStore(old_agent.store.root), controller_class=FakeController)
        self.clients["lynx"].agent = new_agent
        dashboard._resume()
        state = dashboard.state()
        self.assertEqual(self.entry(state, "lynx")["status"], "Ready")
        self.assertIn("Picker and model are ready", state["devices"]["message"])
        self.assertNotIn("Startup failed", state["devices"]["message"])
        self.assertGreater(dashboard.record["context"]["generation"], old_context["generation"])
        self.assertNotEqual(dashboard.record["context"]["boot_id"], old_context["boot_id"])
        self.assertEqual(records.read_bytes(), before)
        self.assertEqual(new_agent.store.installed, [])
        self.assertEqual(new_agent.controller.calls, [])
        with self.assertRaises(WebError):
            new_agent.submit(dict(old_context, operation="scan", payload={"request_id": "old-request-1"}))

    def test_ready_status_does_not_relax_existing_busy_stop_recovery_or_reservation_guards(self):
        dashboard = self.dashboard()
        controller = self.clients["mac"].agent.controller
        original = copy.deepcopy(controller.snapshot)
        for change in ({"busy": True}, {"stop_requested": True}, {"recovery_required": True},
                       {"placement_grid": {"locations": [{"id": 2, "reserved": True}]}}):
            with self.subTest(change=change):
                controller.snapshot = dict(copy.deepcopy(original), **change)
                dashboard._read("mac")
                state = dashboard.state()
                self.assertEqual(self.entry(state, "mac")["status"], "Ready")
                self.assertFalse(state["devices"]["can_switch"])
                self.assertEqual(controller.calls, [])
        self.assertEqual(self.entry(state, "pi")["status"], "Standby")

    def test_stale_startup_snapshot_is_unavailable_not_a_fresh_failed_status(self):
        factory, _ = self.failing("lynx", "Offline failure")
        dashboard = self.dashboard(initial="lynx")
        self.finish(dashboard, factory, "lynx")
        dashboard._cache["lynx"]["observed_at"] = time.monotonic() - 10
        state = dashboard.state()
        self.assertEqual(self.entry(state, "lynx")["status"], "Unavailable")
        self.assertFalse(state["ready"])
        self.assertFalse(state["devices"]["can_switch"])


if __name__ == "__main__":
    unittest.main()
