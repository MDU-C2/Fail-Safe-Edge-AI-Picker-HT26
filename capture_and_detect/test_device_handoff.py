"""Offline handoff fault injection; fake controllers never contact a robot."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cup_inventory import atomic_json
from device_agent import DeviceAgent, ExecutionLock, HandoffStore
from device_dashboard import DeviceDashboard
from device_service import MAX_DOCUMENT, PickerHTTPServer
from web_controller import WebError


class FakeStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.pending = self.root / "pending.json"
        self.installed = []
        self.export_error = None
        self.identity_value = {"robot_id": "robot-test", "geometry": "same-grid",
                               "calibration": "same-calibration", "table_reference": "same-reference"}

    def identity(self):
        return copy.deepcopy(self.identity_value)

    def export(self, session):
        if self.export_error:
            raise self.export_error
        return {"identity": self.identity(), "grid": {"locations": [{"id": 2, "occupied": True}]},
                "recovery": {"schema_version": 1, "recovery_required": False},
                "motion": {"speed": session.speed, "home_speed": session.home_speed,
                           "camera_z": session.camera_z, "handover_offset": list(session.handover_offset)}}

    def install(self, bundle):
        self.installed.append(copy.deepcopy(bundle))
        atomic_json(self.root / "device-motion.json", bundle["motion"])


class FakeController:
    def __init__(self, factory, **kwargs):
        self.factory = factory
        self.closed = False
        self.calls = []
        self.cancelled = []
        self.session = None
        self.language = SimpleNamespace(cancel=lambda: self.cancelled.append("language"))
        self.controls = SimpleNamespace(cancel=lambda: self.cancelled.append("controls"))
        self.snapshot = {"ready": False, "busy": False, "stop_in_flight": False,
                         "recovery_required": False, "stop_requested": False,
                         "scan_usable": True, "inventory": {"capture": "old-frame"},
                         "robot_controls": {"proposal": None}, "placement_grid": {"locations": []}}

    def start(self):
        self.session, _ = self.factory(self)
        self.snapshot["ready"] = True

    def state(self):
        return copy.deepcopy(self.snapshot)

    def submit(self, operation, payload):
        self.calls.append((operation, copy.deepcopy(payload)))
        if operation == "stop":
            self.snapshot.update(stop_requested=True, recovery_required=True)
        else:
            self.snapshot["busy"] = True
        return {"accepted": True}

    def publish_session(self):
        self.snapshot.update(scan_usable=self.session.scan_usable, inventory=self.session.inventory)

    def close(self):
        self.closed = True


class FakeFactory:
    def __init__(self):
        self.calls = 0
        self.gateway = {"status": "ok", "robot_controller": {"ok": True, "move_in_progress": False},
                        "control_lease_active": False}

    def __call__(self, controller):
        self.calls += 1
        return SimpleNamespace(speed=250, home_speed=200, camera_z=False, handover_offset=(0, 0, 0),
                               inventory={"capture": "old-frame"}, scan_usable=True,
                               robot=SimpleNamespace(_get_object=lambda _: copy.deepcopy(self.gateway))), None

    def metadata(self):
        return {"model": "fake-model"}


class AgentClient:
    """Calls the real protocol directly; faults can happen before or after delivery."""
    def __init__(self, agent):
        self.agent = agent
        self.calls = []
        self.faults = {}

    def request(self, path, body=None, **kwargs):
        self.calls.append((path, copy.deepcopy(body)))
        fault = self.faults.pop(path, None)
        if fault == "before":
            raise WebError("Injected connection timeout before delivery", 503)
        routes = {"/v1/state": self.agent.state, "/v1/health": self.agent.health,
                  "/v1/handoff": self.agent.last_handoff, "/v1/activate": self.agent.activate,
                  "/v1/deactivate": self.agent.deactivate, "/v1/command": self.agent.submit}
        result = routes[path]() if body is None else routes[path](body)
        if fault == "after":
            raise WebError("Injected lost reply after delivery", 503)
        return result

    def count(self, path):
        return sum(entry[0] == path for entry in self.calls)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.owner = "a" * 32

    def agent(self, device="mac", *, live=False, root=None):
        store, factory = FakeStore(root or self.root / device), FakeFactory()
        agent = DeviceAgent(device, factory, store, live=live, controller_class=FakeController)
        return agent

    def context(self, agent, generation=1):
        return {"boot_id": agent.boot_id, "owner_id": self.owner, "generation": generation}

    def activate(self, agent, generation=1):
        context = self.context(agent, generation)
        agent.activate(dict(context, handoff=None))
        return context

    def command(self, agent, context, operation="scan"):
        return agent.submit(dict(context, operation=operation, payload={"request_id": "request-test-1"}))

    def dashboard(self, *, clients=None, filename="dashboard.json", bootstrap=True):
        clients = clients or {name: AgentClient(self.agent(name, root=self.root / (filename + ".devices") / name))
                              for name in ("mac", "pi", "lynx")}
        dashboard = DeviceDashboard(clients, self.root / filename, initial_device="mac")
        self.addCleanup(lambda: dashboard.close() if not dashboard._closed else None)
        if bootstrap:
            dashboard._resume()
            for name in clients:
                dashboard._read(name)
        return dashboard, clients

    def payload(self, dashboard, **values):
        return dict(_device_id=dashboard.record["device_id"], _device_epoch=dashboard._epoch,
                    request_id="request-test-1", **values)

    def test_activation_is_explicit_idempotent_and_owner_bound(self):
        agent = self.agent()
        self.assertEqual(agent.factory.calls, 0)
        context = self.activate(agent)
        agent.activate(dict(context, handoff=None))
        self.assertEqual(agent.factory.calls, 1)
        with self.assertRaises(WebError):
            agent.activate(dict(context, owner_id="b" * 32, handoff=None))
        self.assertEqual(agent.controller.calls, [])

    def test_malformed_persisted_authority_is_not_treated_as_a_clean_device(self):
        valid = {"owner_id": self.owner, "highest_generation": 2, "active_generation": 2}
        changes = [{"owner_id": "invalid"}, {"owner_id": None}, {"active_generation": True},
                   {"active_generation": "2"}, {"active_generation": 0}, {"active_generation": 3},
                   {"highest_generation": True}, {"highest_generation": -1}]
        for number, change in enumerate(changes):
            with self.subTest(change=change):
                root = self.root / ("owner-" + str(number)); root.mkdir()
                atomic_json(root / "device-owner.json", dict(valid, **change))
                with self.assertRaises(ValueError):
                    self.agent(root=root)

    def test_deactivation_clears_proposals_scan_and_retires_generation_durably(self):
        agent = self.agent()
        context = self.activate(agent)
        controller = agent.controller
        ack = agent.deactivate(context)
        self.assertTrue(ack["deactivated"])
        self.assertTrue(controller.closed)
        self.assertEqual(controller.cancelled, ["language", "controls"])
        self.assertFalse(controller.session.scan_usable)
        self.assertIsNone(controller.session.inventory)
        self.assertEqual(agent.last_handoff()["handoff"], ack["handoff"])
        self.assertIsNone(json.loads(agent.owner_path.read_text())["active_generation"])
        for operation in ("scan", "stop", "confirm"):
            with self.subTest(operation=operation), self.assertRaises(WebError):
                self.command(agent, context, operation)
        with self.assertRaisesRegex(WebError, "retired"):
            agent.activate(dict(context, handoff=None))
        self.activate(agent, 2)
        self.assertEqual(agent.factory.calls, 2)

    def test_retired_generation_stays_retired_after_agent_restart(self):
        first = self.agent(); context = self.activate(first); first.deactivate(context)
        restarted = self.agent(root=first.store.root)
        with self.assertRaises(WebError):
            restarted.activate(dict(context, handoff=None))
        with self.assertRaisesRegex(WebError, "retired"):
            restarted.activate(dict(self.context(restarted), handoff=None))
        self.activate(restarted, 2)

    def test_active_restart_invalidates_boot_commands_without_resuming_motion(self):
        first = self.agent(); old = self.activate(first); first.close()
        restarted = self.agent(root=first.store.root)
        self.assertIsNone(restarted.controller)
        self.assertEqual(restarted.factory.calls, 0)
        with self.assertRaises(WebError):
            self.command(restarted, old)
        self.activate(restarted, 2)
        self.assertEqual(restarted.controller.calls, [])
        with self.assertRaises(WebError):
            self.command(restarted, old)

    def test_busy_unready_recovery_and_stop_deny_deactivation_without_retirement(self):
        for key, value in (("busy", True), ("ready", False), ("recovery_required", True), ("stop_requested", True)):
            with self.subTest(key=key):
                agent = self.agent(root=self.root / key); context = self.activate(agent)
                agent.controller.snapshot[key] = value
                with self.assertRaises(WebError):
                    agent.deactivate(context)
                self.assertEqual(agent.owner["active_generation"], 1)
                self.assertFalse(agent.controller.closed)

    def test_gateway_must_report_explicit_idle_and_no_lease_for_live_handoff(self):
        variants = [{}, {"status": "unknown"},
                    {"status": "ok", "robot_controller": {"ok": True}},
                    {"status": "ok", "robot_controller": {"ok": True, "move_in_progress": True}, "control_lease_active": False},
                    {"status": "ok", "robot_controller": {"ok": True, "move_in_progress": False}, "control_lease_active": True}]
        for number, health in enumerate(variants):
            with self.subTest(health=health):
                agent = self.agent(live=True, root=self.root / str(number)); context = self.activate(agent)
                agent.factory.gateway = health
                with self.assertRaises(WebError):
                    agent.deactivate(context)
                self.assertEqual(agent.owner["active_generation"], 1)
                self.assertFalse(agent.transition)

    def test_stop_remains_admitted_during_gateway_check_and_cancels_handoff(self):
        agent = self.agent(); context = self.activate(agent)
        entered, release = threading.Event(), threading.Event()
        errors = []
        def check():
            entered.set()
            self.assertTrue(release.wait(2))
        def deactivate():
            try:
                agent.deactivate(context)
            except BaseException as error:
                errors.append(error)
        agent.gateway_idle = check
        thread = threading.Thread(target=deactivate)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(WebError):
                self.command(agent, context, "scan")
            self.command(agent, context, "stop")
        finally:
            release.set(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], WebError)
        self.assertEqual(agent.owner["active_generation"], 1)
        self.assertFalse(agent.controller.closed)

    def test_submitted_work_is_busy_before_handoff_can_admit(self):
        agent = self.agent(); context = self.activate(agent)
        self.command(agent, context)
        with self.assertRaises(WebError):
            agent.deactivate(context)
        self.assertEqual(agent.controller.calls[0][0], "scan")

    def test_failed_export_does_not_release_or_clear_the_authority(self):
        agent = self.agent(); context = self.activate(agent)
        agent.store.export_error = WebError("Reserved locations need inspection")
        with self.assertRaises(WebError):
            agent.deactivate(context)
        self.assertEqual(agent.owner["active_generation"], 1)
        self.assertEqual(agent.controller.cancelled, [])
        self.assertFalse(agent.transition)

    def test_successful_router_handoff_preserves_records_settings_and_fences_old_ui(self):
        dashboard, clients = self.dashboard()
        old_epoch = dashboard._epoch
        source = clients["mac"].agent
        source.controller.session.speed = 400
        source.controller.session.handover_offset = (1, 2, 3)
        dashboard._switch("pi")
        self.assertEqual(dashboard.record["phase"], "active")
        self.assertEqual(dashboard.record["device_id"], "pi")
        self.assertIsNone(source.owner["active_generation"])
        target = clients["pi"].agent
        self.assertEqual(target.controller.session.speed, 400)
        self.assertEqual(tuple(target.controller.session.handover_offset), (1, 2, 3))
        self.assertTrue(target.store.installed[0]["grid"]["locations"][0]["occupied"])
        with self.assertRaises(WebError):
            dashboard.submit("scan", {"_device_id": "mac", "_device_epoch": old_epoch, "request_id": "request-old-1"})
        self.assertEqual(clients["mac"].count("/v1/command"), 0)
        self.assertEqual(clients["pi"].count("/v1/command"), 0)

    def test_lost_deactivation_reply_is_recovered_after_router_restart_without_post_retry(self):
        dashboard, clients = self.dashboard()
        clients["mac"].faults["/v1/deactivate"] = "after"
        dashboard._switch("pi")
        self.assertEqual(dashboard.record["phase"], "deactivate_pending")
        self.assertEqual(clients["pi"].count("/v1/activate"), 0)
        dashboard.close()
        resumed, _ = self.dashboard(clients=clients, bootstrap=False)
        resumed._resume()
        self.assertEqual(resumed.record["phase"], "active")
        self.assertEqual(resumed.record["device_id"], "pi")
        self.assertEqual(clients["mac"].count("/v1/deactivate"), 1)
        self.assertEqual(clients["mac"].count("/v1/handoff"), 1)
        self.assertEqual(clients["pi"].count("/v1/activate"), 1)

    def test_deactivation_never_delivered_retains_old_owner_without_replaying_post(self):
        dashboard, clients = self.dashboard()
        clients["mac"].faults["/v1/deactivate"] = "before"
        dashboard._switch("pi"); dashboard._resume()
        self.assertEqual(dashboard.record["phase"], "active")
        self.assertEqual(dashboard.record["device_id"], "mac")
        self.assertEqual(clients["mac"].count("/v1/deactivate"), 1)
        self.assertEqual(clients["pi"].count("/v1/activate"), 0)

    def test_deactivation_reconciliation_waits_for_inflight_release_before_deciding_owner(self):
        dashboard, clients = self.dashboard()
        source = clients["mac"].agent
        context = copy.deepcopy(dashboard.record["context"])
        dashboard.record.update(phase="deactivate_pending", target_id="pi")
        dashboard._persist()
        source.transition = True
        dashboard._resume()
        self.assertEqual(dashboard.record["phase"], "deactivate_pending")
        self.assertEqual(dashboard.record["target_id"], "pi")
        self.assertEqual(clients["pi"].count("/v1/activate"), 0)
        self.assertEqual(clients["mac"].count("/v1/deactivate"), 0)
        with self.assertRaises(WebError):
            dashboard.submit("scan", self.payload(dashboard))
        # The original remote request subsequently finishes; its durable ACK is
        # reconciled by GET, never a second deactivation POST.
        source.transition = False
        source.deactivate(context)
        dashboard._resume()
        self.assertEqual(dashboard.record["phase"], "active")
        self.assertEqual(dashboard.record["device_id"], "pi")
        self.assertEqual(clients["mac"].count("/v1/deactivate"), 0)
        self.assertEqual(clients["mac"].count("/v1/handoff"), 1)

    def test_activation_lost_reply_blocks_commands_but_stop_targets_possible_new_owner(self):
        dashboard, clients = self.dashboard()
        clients["pi"].faults["/v1/activate"] = "after"
        dashboard._switch("pi")
        self.assertEqual(dashboard.record["phase"], "activate_pending")
        self.assertEqual(dashboard.record["device_id"], "pi")
        with self.assertRaises(WebError):
            dashboard.submit("scan", self.payload(dashboard))
        dashboard.submit("stop", self.payload(dashboard))
        self.assertEqual(clients["mac"].count("/v1/command"), 0)
        self.assertEqual(clients["pi"].agent.controller.calls, [("stop", {"request_id": "request-test-1"})])
        clients["pi"].faults["/v1/state"] = "before"
        dashboard._resume()
        self.assertIn("Waiting for confirmed", dashboard._message)
        dashboard._resume()
        self.assertEqual(dashboard.record["phase"], "active")
        self.assertEqual(clients["pi"].count("/v1/activate"), 1)
        self.assertIn("Raspberry Pi selected", dashboard.state()["devices"]["message"])
        self.assertNotIn("Waiting for confirmed", dashboard.state()["devices"]["message"])

    def test_old_agent_restart_before_lost_handoff_reconciliation_remains_safe(self):
        dashboard, clients = self.dashboard()
        old = clients["mac"].agent
        clients["mac"].faults["/v1/deactivate"] = "after"
        dashboard._switch("pi")
        clients["mac"].agent = self.agent("mac", root=old.store.root)
        dashboard._resume()
        self.assertEqual(dashboard.record["device_id"], "pi")
        self.assertEqual(dashboard.record["phase"], "active")
        self.assertEqual(clients["mac"].agent.factory.calls, 0)

    def test_activation_not_delivered_uses_new_generation_and_rejects_delayed_old_request(self):
        dashboard, clients = self.dashboard()
        clients["pi"].faults["/v1/activate"] = "before"
        dashboard._switch("pi")
        old_request = next(body for path, body in clients["pi"].calls if path == "/v1/activate")
        self.assertEqual(dashboard.record["phase"], "activate_pending")
        dashboard._resume()
        self.assertEqual(dashboard.record["phase"], "active")
        self.assertGreater(dashboard.record["context"]["generation"], old_request["generation"])
        with self.assertRaises(WebError):
            clients["pi"].agent.activate(old_request)
        self.assertEqual(clients["pi"].agent.factory.calls, 1)
        self.assertEqual(len(clients["pi"].agent.store.installed), 1)

    def test_target_restarts_after_lost_activation_reply_without_replaying_transferred_state(self):
        dashboard, clients = self.dashboard()
        clients["pi"].faults["/v1/activate"] = "after"
        dashboard._switch("pi")
        target = clients["pi"].agent
        target.close()
        clients["pi"].agent = self.agent("pi", root=target.store.root)
        dashboard._resume()
        self.assertEqual(dashboard.record["phase"], "active")
        self.assertEqual(clients["pi"].agent.store.installed, [])
        self.assertEqual(clients["pi"].agent.controller.calls, [])
        self.assertNotEqual(dashboard.record["context"]["boot_id"], target.boot_id)

    def test_inactive_foreign_owner_and_pending_target_transfer_do_not_release_source(self):
        for blocked in ("foreign", "pending"):
            with self.subTest(blocked=blocked):
                dashboard, clients = self.dashboard(filename=blocked + ".json")
                target = clients["pi"].agent
                if blocked == "foreign":
                    context = {"boot_id": target.boot_id, "owner_id": "b" * 32, "generation": 1}
                    target.activate(dict(context, handoff=None)); target.deactivate(context)
                else:
                    target.store.pending.write_text("{}")
                dashboard._switch("pi")
                self.assertEqual(clients["mac"].count("/v1/deactivate"), 0)
                self.assertEqual(dashboard.record["device_id"], "mac")
                self.assertIsNotNone(clients["mac"].agent.owner["active_generation"])
                dashboard.close()

    def test_mismatched_geometry_or_foreign_owner_never_deactivates_source(self):
        for mismatch in ("geometry", "owner"):
            with self.subTest(mismatch=mismatch):
                dashboard, clients = self.dashboard(filename=mismatch + ".json")
                if mismatch == "geometry":
                    clients["pi"].agent.store.identity_value["geometry"] = "different"
                else:
                    target = clients["pi"].agent
                    target.activate({"boot_id": target.boot_id, "owner_id": "b" * 32, "generation": 1, "handoff": None})
                dashboard._switch("pi")
                self.assertEqual(clients["mac"].count("/v1/deactivate"), 0)
                self.assertEqual(dashboard.record["device_id"], "mac")
                dashboard.close()

    def test_target_reservations_report_locations_without_releasing_source(self):
        dashboard, clients = self.dashboard()
        store = clients["pi"].agent.store
        store.receive_blocked = lambda: True
        store.receive_block_reason = lambda: "Unfinished placement reservations: P2, P5. Inspect these locations."
        dashboard._switch("pi")
        self.assertEqual(dashboard.record["device_id"], "mac")
        self.assertEqual(clients["mac"].count("/v1/deactivate"), 0)
        self.assertIn("P2, P5", dashboard._error)
        self.assertNotIn("another dashboard", dashboard._error)
        entry = next(item for item in dashboard.state()["devices"]["entries"] if item["id"] == "pi")
        self.assertEqual(entry["status"], "Needs inspection")
        self.assertIn("P2, P5", entry["reason"])

    def test_custom_stage_speeds_require_target_support_before_deactivation(self):
        from handover_plan import normalize_phase_speeds
        dashboard, clients = self.dashboard()
        clients["mac"].agent.controller.snapshot["motion"] = {
            "phase_speed_settings": dict(normalize_phase_speeds(), flip=180)}
        original = clients["pi"].request
        def old_service(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == "/v1/state":
                result["health"].pop("features", None)
            return result
        with patch.object(clients["pi"], "request", side_effect=old_service):
            dashboard._switch("pi")
            self.assertEqual(dashboard.record["device_id"], "mac")
            self.assertEqual(clients["mac"].count("/v1/deactivate"), 0)
            self.assertIn("movement-speed update", dashboard._error)
            clients["mac"].agent.controller.snapshot["motion"]["phase_speed_settings"] = normalize_phase_speeds()
            dashboard._switch("pi")
            self.assertEqual(dashboard.record["device_id"], "pi")
            self.assertIsNone(dashboard._error)

    def test_router_reopens_persisted_state_without_os_lock(self):
        dashboard, clients = self.dashboard()
        before = dashboard.path.read_bytes()
        reopened = DeviceDashboard(clients, dashboard.path, initial_device="mac")
        self.addCleanup(reopened.close)
        self.assertEqual(dashboard.path.read_bytes(), before)
        self.assertEqual(reopened.record, dashboard.record)
        self.assertFalse(dashboard.path.with_suffix(".lock").exists())

    def test_late_phase_update_keeps_source_active_and_reconciles_without_replaying_deactivation(self):
        from handover_plan import normalize_phase_speeds
        dashboard, clients = self.dashboard()
        source = clients["mac"].agent
        controller = source.controller
        generation = source.owner["active_generation"]
        controller.snapshot["motion"] = {"phase_speed_settings": normalize_phase_speeds()}
        original_source, original_target = clients["mac"].request, clients["pi"].request

        def admitted_update_arrives(path, body=None, **kwargs):
            if path == "/v1/deactivate":
                # The dashboard already read defaults, but a previously
                # admitted settings command completes before source admission.
                controller.session.phase_speeds = dict(normalize_phase_speeds(), flip=180)
                controller.snapshot["motion"]["phase_speed_settings"] = controller.session.phase_speeds
            return original_source(path, body, **kwargs)

        def old_target(path, body=None, **kwargs):
            result = original_target(path, body, **kwargs)
            if path == "/v1/state":
                result["health"].pop("features", None)
            return result

        with patch.object(clients["mac"], "request", side_effect=admitted_update_arrives), \
                patch.object(clients["pi"], "request", side_effect=old_target), \
                patch.object(source.store, "export", wraps=source.store.export) as export:
            dashboard._switch("pi")
            self.assertEqual(dashboard.record["phase"], "deactivate_pending")
            self.assertEqual(source.owner["active_generation"], generation)
            self.assertIs(source.controller, controller)
            self.assertFalse(controller.closed)
            self.assertFalse(source.transition)
            self.assertEqual(controller.cancelled, [])
            export.assert_not_called()
            self.assertFalse((source.store.root / "device-last-handoff.json").exists())
            self.assertIn("speeds changed", dashboard._error)
            self.assertEqual(clients["pi"].count("/v1/activate"), 0)
            dashboard._resume()
        self.assertEqual(dashboard.record["phase"], "active")
        self.assertEqual(dashboard.record["device_id"], "mac")
        self.assertEqual(clients["mac"].count("/v1/deactivate"), 1)
        self.assertEqual(source.owner["active_generation"], generation)
        envelope = next(body for path, body in clients["mac"].calls if path == "/v1/deactivate")
        self.assertIs(envelope["require_default_phase_speeds"], True)
        self.assertNotIn("require_default_phase_speeds", dashboard.record["context"])

    def test_default_phase_guard_rechecks_before_export_after_idle_check(self):
        from handover_plan import normalize_phase_speeds
        agent = self.agent()
        context = self.activate(agent)
        controller = agent.controller
        controller.session.phase_speeds = normalize_phase_speeds()
        def late_change():
            controller.session.phase_speeds["flip"] = 180
        agent.gateway_idle = late_change
        with patch.object(agent.store, "export", wraps=agent.store.export) as export:
            with self.assertRaisesRegex(WebError, "speeds changed"):
                agent.deactivate(dict(context, require_default_phase_speeds=True))
            export.assert_not_called()
        self.assertEqual(agent.owner["active_generation"], context["generation"])
        self.assertIs(agent.controller, controller)
        self.assertFalse(agent.transition)
        self.assertFalse(controller.closed)

    def test_default_phase_guard_accepts_only_literal_true(self):
        agent = self.agent()
        context = self.activate(agent)
        for value in (False, None, 0, 1, "true", [], {}):
            with self.subTest(value=value), self.assertRaises(WebError) as caught:
                agent.deactivate(dict(context, require_default_phase_speeds=value))
            self.assertEqual(caught.exception.status, 422)
        self.assertEqual(agent.owner["active_generation"], context["generation"])
        self.assertFalse(agent.transition)

    def test_old_source_keeps_exact_legacy_deactivation_fields(self):
        dashboard, clients = self.dashboard()
        original_source, original_target = clients["mac"].request, clients["pi"].request
        def legacy(original):
            def request(path, body=None, **kwargs):
                result = original(path, body, **kwargs)
                if path == "/v1/state":
                    result["health"].pop("features", None)
                return result
            return request
        with patch.object(clients["mac"], "request", side_effect=legacy(original_source)), \
                patch.object(clients["pi"], "request", side_effect=legacy(original_target)):
            dashboard._switch("pi")
        self.assertEqual(dashboard.record["device_id"], "pi")
        self.assertIsNone(dashboard._error)
        envelope = next(body for path, body in clients["mac"].calls if path == "/v1/deactivate")
        self.assertEqual(set(envelope), {"boot_id", "owner_id", "generation"})

    def test_quick_status_retains_stop_scan_and_operation_evidence_without_remote_read(self):
        dashboard, clients = self.dashboard()
        evidence = {"operation": "stop", "scan_usable": False, "recovery_required": True,
                    "stop_requested": True, "stop_in_flight": True,
                    "stop": {"state": "unconfirmed", "stopped": False, "message": "Inspect robot"},
                    "progress": {"completed": 2, "total": 5}, "batch": {"completed": 1},
                    "motion": {"speed": 250}, "message": "Stop pending", "error": "Reply unknown"}
        clients["mac"].agent.controller.snapshot.update(evidence)
        dashboard._read("mac")
        before = sum(len(client.calls) for client in clients.values())
        status = dashboard.status()
        for key, value in evidence.items():
            self.assertEqual(status[key], value, key)
        self.assertEqual(status["capture_id"], "old-frame")
        self.assertEqual(status["devices"]["active_id"], "mac")
        self.assertIn("Cached selected-device", status["source"])
        self.assertEqual(sum(len(client.calls) for client in clients.values()), before)
        self.assertEqual(clients["mac"].count("/v1/command"), 0)

    def test_execution_context_is_reentrant_and_does_not_create_files(self):
        path = self.root / "unused" / "picker.lock"
        with ExecutionLock(path) as first, ExecutionLock(path):
            first.close()
            first.close()
        self.assertFalse(path.parent.exists())


class HandoffStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = SimpleNamespace(speed=250, home_speed=100, camera_z=False, handover_offset=(1, -2, 3))
        self.source = self.store("source", occupied=True)
        self.target = self.store("target", occupied=False)

    def store(self, name, occupied):
        root = self.root / name; root.mkdir(); (root / "captures").mkdir()
        atomic_json(root / "placing-grid.json", {"cell_size_mm": 90,
            "locations": [{"id": 2, "x": 350, "y": -260, "z": 230,
                           "occupied": occupied, "reserved": False, "vision_blocked": False}]})
        # These byte-identical small files test transfer identity, not perception calibration.
        (root / "calibration.json").write_text('{"test_calibration":true}')
        (root / "placing-grid.table-reference.json").write_text('{"test_reference":true}')
        atomic_json(root / "captures" / "robot_recovery.json", {"schema_version": 1,
                    "recovery_required": False, "context": {"note": "completed"}})
        return HandoffStore(root, "offline-robot")

    def test_transfer_preserves_source_occupancy_clean_journal_motion_and_previous_target_backup(self):
        bundle = self.source.export(self.session)
        self.target.install(bundle)
        self.assertEqual(self.target.grid._read(), bundle["grid"])
        self.assertFalse(self.target.recovery.required())
        self.assertEqual(json.loads(self.target.recovery_path.read_text()), bundle["recovery"])
        self.assertEqual(json.loads((self.target.root / "device-motion.json").read_text()), bundle["motion"])
        before = json.loads((self.target.root / "device-state-before-handoff.json").read_text())
        self.assertFalse(before["grid"]["locations"][0]["occupied"])
        self.assertFalse(self.target.pending.exists())

    def test_export_rejects_reservations_recovery_and_partial_handoff(self):
        original = self.source.grid._read()
        for kind in ("reserved", "recovery", "pending"):
            with self.subTest(kind=kind):
                atomic_json(self.source.grid.path, original)
                self.source.recovery.clear()
                self.source.pending.unlink(missing_ok=True)
                if kind == "reserved":
                    grid = copy.deepcopy(original); grid["locations"][0]["reserved"] = True
                    atomic_json(self.source.grid.path, grid)
                elif kind == "recovery":
                    self.source.recovery.begin({"test": True})
                else:
                    self.source.pending.write_text("{}")
                with self.assertRaises(WebError):
                    self.source.export(self.session)

    def test_receive_reason_distinguishes_reservations_recovery_and_transfer(self):
        self.assertIsNone(self.target.receive_block_reason())
        grid = self.target.grid._read()
        grid["locations"][0].update(occupied=True, reserved=True)
        atomic_json(self.target.grid.path, grid)
        self.assertIn("P2", self.target.receive_block_reason())
        self.target.recovery.begin({"test": True})
        self.assertIn("acknowledge recovery", self.target.receive_block_reason())
        self.target.pending.write_text("{}")
        self.assertIn("state transfer", self.target.receive_block_reason())

    def test_custom_stage_speeds_transfer_and_defaults_keep_legacy_schema(self):
        from handover_plan import normalize_phase_speeds
        self.session.phase_speeds = normalize_phase_speeds()
        self.assertNotIn("phase_speeds", self.source.export(self.session)["motion"])
        self.session.phase_speeds["flip"] = 180
        bundle = self.source.export(self.session)
        self.target.install(bundle)
        self.assertEqual(json.loads((self.target.root / "device-motion.json").read_text())["phase_speeds"]["flip"], 180)
        before = self.target.grid.path.read_bytes()
        bundle["motion"]["phase_speeds"]["flip"] = float("nan")
        with self.assertRaises(ValueError):
            self.target.install(bundle)
        self.assertEqual(self.target.grid.path.read_bytes(), before)

    def test_invalid_bundle_never_writes_target_grid_or_journal(self):
        for kind in ("identity", "geometry", "reserved", "recovery", "speed", "home_cap", "home_over_speed", "offset", "camera_z"):
            with self.subTest(kind=kind):
                self.target = self.store("invalid-" + kind, occupied=False)
                original_grid = self.target.grid.path.read_bytes()
                original_recovery = self.target.recovery_path.read_bytes()
                bundle = self.source.export(self.session)
                if kind == "identity": bundle["identity"]["calibration"] = "different"
                elif kind == "geometry": bundle["grid"]["locations"][0]["x"] += 10
                elif kind == "reserved": bundle["grid"]["locations"][0]["reserved"] = True
                elif kind == "recovery": bundle["recovery"]["recovery_required"] = True
                elif kind == "speed": bundle["motion"]["speed"] = 1e12
                elif kind == "home_cap": bundle["motion"]["home_speed"] = 1000
                elif kind == "home_over_speed": bundle["motion"].update(speed=50, home_speed=100)
                elif kind == "offset": bundle["motion"]["handover_offset"] = [1000, 0, 0]
                else: bundle["motion"]["camera_z"] = "false"
                with self.assertRaises((WebError, ValueError)):
                    self.target.install(bundle)
                self.assertEqual(self.target.grid.path.read_bytes(), original_grid)
                self.assertEqual(self.target.recovery_path.read_bytes(), original_recovery)
                self.assertFalse(self.target.pending.exists())

    def test_partial_install_retains_recovery_and_pending_marker(self):
        bundle = self.source.export(self.session)
        original_atomic = atomic_json
        def interrupted(path, value):
            if Path(path) == self.target.grid.path:
                raise OSError("Injected storage failure")
            return original_atomic(path, value)
        with patch("device_agent.atomic_json", side_effect=interrupted), self.assertRaises(OSError):
            self.target.install(bundle)
        self.assertTrue(self.target.pending.exists())
        self.assertTrue(self.target.recovery.required())
        self.assertFalse(self.target.grid._read()["locations"][0]["occupied"])
        with self.assertRaises(WebError):
            self.target.export(self.session)

    def test_install_never_erases_target_recovery_reservations_or_pending_transfer(self):
        bundle = self.source.export(self.session)
        for kind in ("recovery", "reserved", "pending"):
            with self.subTest(kind=kind):
                target = self.store("blocked-" + kind, occupied=False)
                if kind == "recovery":
                    target.recovery.begin({"source": "unconfirmed_old_motion"})
                elif kind == "reserved":
                    grid = target.grid._read()
                    grid["locations"][0].update(occupied=True, reserved=True, reservation_id="old-run")
                    atomic_json(target.grid.path, grid)
                else:
                    atomic_json(target.pending, {"interrupted": "older-transfer"})
                before = {str(path.relative_to(target.root)): path.read_bytes()
                          for path in target.root.rglob("*") if path.is_file()}
                with self.assertRaises(WebError):
                    target.install(bundle)
                after = {str(path.relative_to(target.root)): path.read_bytes()
                         for path in target.root.rglob("*") if path.is_file()}
                self.assertEqual(after, before)


class DeviceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.agent = DeviceAgent("mac", FakeFactory(), FakeStore(self.tmp.name), controller_class=FakeController)
        self.token = "offline-test-token-" + "x" * 32
        self.server = PickerHTTPServer(("127.0.0.1", 0), self.agent, self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def request(self, method="GET", path="/v1/state", body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_read_and_write_endpoints_require_exact_bearer_authentication(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": self.token}):
            for method in ("GET", "POST"):
                with self.subTest(method=method, headers=bool(headers)):
                    status, _ = self.request(method, "/v1/state" if method == "GET" else "/v1/activate", headers=headers)
                    self.assertEqual(status, 401)
        status, body = self.request(headers={"Authorization": "Bearer " + self.token})
        self.assertEqual(status, 200)
        self.assertIsNone(body["state"])
        self.assertEqual(self.agent.factory.calls, 0)

    def test_bounded_json_rejects_wrong_type_duplicate_keys_nonfinite_and_oversize(self):
        base = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        for raw in ('[]', '{"x":1,"x":2}', '{"x":NaN}', '{', 'null'):
            with self.subTest(raw=raw):
                status, _ = self.request("POST", "/v1/activate", raw, base)
                self.assertEqual(status, 422)
        status, _ = self.request("POST", "/v1/activate", "{}", dict(base, **{"Content-Length": str(MAX_DOCUMENT + 1)}))
        self.assertEqual(status, 413)
        status, _ = self.request("POST", "/v1/activate", "{}", {"Authorization": "Bearer " + self.token})
        self.assertEqual(status, 422)
        self.assertEqual(self.agent.factory.calls, 0)

    def test_duplicate_auth_or_length_and_transfer_encoding_are_rejected(self):
        for kind in ("auth", "length", "transfer"):
            with self.subTest(kind=kind):
                connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
                try:
                    connection.putrequest("POST", "/v1/activate")
                    connection.putheader("Authorization", "Bearer " + self.token)
                    if kind == "auth": connection.putheader("Authorization", "Bearer " + self.token)
                    connection.putheader("Content-Type", "application/json")
                    connection.putheader("Content-Length", "2")
                    if kind == "length": connection.putheader("Content-Length", "2")
                    if kind == "transfer": connection.putheader("Transfer-Encoding", "chunked")
                    connection.endheaders(b"{}")
                    response = connection.getresponse(); response.read()
                    self.assertEqual(response.status, 401 if kind == "auth" else 422)
                finally:
                    connection.close()
        self.assertEqual(self.agent.factory.calls, 0)


if __name__ == "__main__":
    unittest.main()
