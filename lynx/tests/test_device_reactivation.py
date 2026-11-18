"""Real controller/agent lifecycle with fake native workers and no motion IO."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lynx.tests.test_cli_capture import ConsumingWorker, capture_rig
from lynx.tests.test_native_web_runtime import native_rig
from device_agent import DeviceAgent, HandoffStore
from device_dashboard import DeviceDashboard
from device_service import native_factory
from web_controller import WebError


@pytest.fixture
def reactivation_rig(native_rig):
    rig = native_rig
    # This temporary fixture represents a completed, unreserved workspace.
    # Existing occupied cells remain occupied during model lifecycle changes.
    grid = json.loads(rig.grid.path.read_text())
    for location in grid["locations"]:
        location.pop("reserved", None)
        location.pop("reservation_id", None)
    rig.grid.path.write_text(json.dumps(grid))
    rig.workers, rig.agents = [], []
    def start_worker(*args, **kwargs):
        worker = ConsumingWorker()
        worker.model = rig.worker.model
        worker.process = SimpleNamespace(poll=Mock(return_value=None),
                                          stdin=SimpleNamespace(closed=False))
        def close():
            worker.process.poll.return_value = 0
            worker.process.stdin.closed = True
        worker.close = Mock(side_effect=close)
        rig.workers.append(worker)
        return worker
    rig.worker_start.side_effect = start_worker
    def agent():
        value = DeviceAgent("lynx", native_factory(rig.args),
            HandoffStore(rig.args.state_dir, "offline_robot_01"), native=True)
        rig.agents.append(value)
        return value
    rig.new_agent = agent
    yield rig
    for value in rig.agents:
        value.close()


def context(agent, generation):
    return {"boot_id": agent.boot_id, "owner_id": "a" * 32, "generation": generation}


def settle(agent):
    assert agent.controller is not None
    agent.controller._worker.submit(lambda: None).result(timeout=3)
    return agent.state()


def test_native_agent_reactivation_creates_fresh_worker_and_restores_handoff_settings(reactivation_rig):
    rig = reactivation_rig
    original_grid = json.loads(rig.grid.path.read_text())
    rig.args.camera_z = True
    agent = rig.new_agent()
    first = context(agent, 1)
    assert agent.health()["metadata"]["state"] == "not_started"
    agent.activate(dict(first, handoff=None))
    assert settle(agent)["state"]["ready"]
    session = agent.controller.session
    session.speed, session.home_speed = 80, 70
    session.handover_offset = (1.0, 2.0, 3.0)
    agent.submit(dict(first, operation="control_preview", payload={
        "request_id": "offline_home_preview_01", "action": "home", "arm": "both"}))
    assert settle(agent)["state"]["robot_controls"]["proposal"] is not None
    old_controller = agent.controller
    acknowledgement = agent.deactivate(first)
    assert acknowledgement["deactivated"]
    assert old_controller.state()["ready"] is False
    assert old_controller.session.inventory is None and not old_controller.session.scan_usable
    assert agent.controller is None and agent.owner["active_generation"] is None
    assert agent.factory.metadata()["state"] == "closed"
    rig.workers[0].close.assert_called_once()
    assert agent.last_handoff()["handoff"] == acknowledgement["handoff"]
    with pytest.raises(WebError):
        agent.submit(dict(first, operation="scan", payload={"request_id": "retired_scan_01"}))

    second = context(agent, 2)
    body = dict(second, handoff=acknowledgement["handoff"])
    agent.activate(body)
    state = settle(agent)
    assert state["state"]["ready"] and state["health"]["metadata"]["worker_running"]
    assert len(rig.workers) == 2 and rig.workers[0] is not rig.workers[1]
    assert agent.controller is not old_controller
    restored = agent.controller.session
    assert (restored.speed, restored.home_speed, restored.camera_z) == (80, 70, True)
    assert restored.handover_offset == (0.0, 0.0, 0.0)
    assert not restored.scan_usable and restored.inventory is None
    assert state["state"]["robot_controls"]["proposal"] is None
    assert json.loads(rig.grid.path.read_text()) == original_grid
    assert not agent.store.recovery.required() and not agent.store.pending.exists()
    agent.activate(body)  # Lost activation acknowledgement does not spawn another model.
    assert len(rig.workers) == 2
    assert rig.camera.calls == 0 and all(not worker.calls for worker in rig.workers)
    agent.close()
    assert all(worker.close.call_count == 1 for worker in rig.workers)


def test_failed_native_startup_stays_closed_until_explicit_service_restart(reactivation_rig):
    rig = reactivation_rig
    reference = rig.reference_path.read_bytes()
    original_grid = rig.grid.path.read_bytes()
    rig.reference_path.write_text("{}")
    failed = rig.new_agent()
    old = context(failed, 1)
    failed.activate(dict(old, handoff=None))
    state = settle(failed)
    assert not state["state"]["ready"] and not state["state"]["busy"]
    assert state["state"]["error"] and failed.controller.session is None
    assert state["health"]["metadata"]["state"] == "failed"
    assert not state["health"]["metadata"]["worker_running"]
    rig.workers[0].close.assert_called_once()
    with pytest.raises(WebError):
        failed.deactivate(old)
    with pytest.raises(WebError):
        failed.submit(dict(old, operation="scan", payload={"request_id": "failed_scan_01"}))
    failed.activate(dict(old, handoff=None))
    assert len(rig.workers) == 1  # There is deliberately no automatic model retry.
    assert failed.owner["active_generation"] == 1
    failed.close()
    rig.workers[0].close.assert_called_once()

    rig.reference_path.write_bytes(reference)
    restarted = rig.new_agent()
    assert restarted.controller is None and len(rig.workers) == 1
    assert restarted.owner["active_generation"] == 1
    with pytest.raises(WebError):
        restarted.activate(dict(old, handoff=None))
    with pytest.raises(WebError):
        restarted.submit(dict(old, operation="scan", payload={"request_id": "stale_boot_scan_01"}))
    new = context(restarted, 2)
    restarted.activate(dict(new, handoff=None))
    assert settle(restarted)["state"]["ready"]
    assert len(rig.workers) == 2 and restarted.owner["active_generation"] == 2
    assert rig.grid.path.read_bytes() == original_grid
    assert rig.camera.calls == 0 and all(not worker.calls for worker in rig.workers)


def test_transferred_camera_height_is_applied_before_immutable_session_construction(reactivation_rig):
    rig = reactivation_rig
    rig.args.camera_z = False
    settings = {"speed": 90, "home_speed": 75, "camera_z": True,
                "handover_offset": [2.0, -1.0, 3.0]}
    (rig.args.state_dir / "device-motion.json").write_text(json.dumps(settings))
    agent = rig.new_agent()
    agent.activate(dict(context(agent, 1), handoff=None))
    state = settle(agent)
    assert state["state"]["ready"]
    session = agent.controller.session
    assert session.camera_z is True and rig.args.camera_z is False
    assert (session.speed, session.home_speed) == (90, 75)
    assert session.handover_offset == (0.0, 0.0, 0.0)
    assert state["health"]["metadata"]["policy"]["camera_z"] is True
    assert len(rig.workers) == 1 and not rig.workers[0].calls and rig.camera.calls == 0


class DirectClient:
    def __init__(self, agent):
        self.agent = agent
        self.calls = []

    def request(self, path, body=None, **kwargs):
        self.calls.append((path, copy.deepcopy(body)))
        methods = {"/v1/state": self.agent.state, "/v1/health": self.agent.health,
                   "/v1/activate": self.agent.activate, "/v1/deactivate": self.agent.deactivate,
                   "/v1/command": self.agent.submit, "/v1/handoff": self.agent.last_handoff}
        return methods[path]() if body is None else methods[path](body)


def test_dashboard_recovers_same_authority_after_explicit_failed_service_restart(reactivation_rig):
    rig = reactivation_rig
    reference = rig.reference_path.read_bytes()
    rig.reference_path.write_text("{}")
    first = rig.new_agent()
    client = DirectClient(first)
    unavailable = SimpleNamespace(request=Mock(side_effect=WebError("Offline device", 503)))
    dashboard = DeviceDashboard({"lynx": client, "mac": unavailable, "pi": unavailable},
        rig.args.state_dir / "dashboard.json", initial_device="lynx", mode="dry_run")
    try:
        dashboard._resume()
        settle(first)
        dashboard._read("lynx")
        assert dashboard.record["phase"] == "active"
        assert not dashboard.state()["ready"] and not dashboard.state()["devices"]["can_switch"]
        old_context = copy.deepcopy(dashboard.record["context"])
        owner = dashboard.record["owner_id"]
        first.close()
        rig.reference_path.write_bytes(reference)
        second = rig.new_agent()
        client.agent = second
        dashboard._resume()  # Same selected authority, new boot; no handoff or motion resume.
        settle(second)
        dashboard._read("lynx")
        state = dashboard.state()
        assert state["ready"] and state["devices"]["active_id"] == "lynx"
        assert dashboard.record["owner_id"] == owner
        assert dashboard.record["context"]["generation"] > old_context["generation"]
        assert dashboard.record["context"]["boot_id"] != old_context["boot_id"]
        assert not state["scan_usable"] and state["inventory"] is None
        assert len(rig.workers) == 2 and rig.camera.calls == 0
        assert not any(path in ("/v1/command", "/v1/deactivate") for path, _ in client.calls)
        unavailable.request.assert_not_called()
    finally:
        dashboard.close()
