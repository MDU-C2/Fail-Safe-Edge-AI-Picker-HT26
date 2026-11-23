"""Opt-in failover UI lifecycle checks, with no camera or robot requests."""

import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest

from local_device import LocalDeviceClient
from recovery_journal import RecoveryJournal
from test_camera_parking import factory_rig
from test_web_controller import WebFixture
from web_controller import WebController, WebError


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr("requests.sessions.Session.request",
                        Mock(side_effect=AssertionError("Offline test attempted network IO")))


def wait_idle(controller):
    deadline = time.monotonic() + 3
    while controller.state()["busy"]:
        if time.monotonic() > deadline:
            raise AssertionError("Controller did not finish offline work")
        time.sleep(0.005)
    return controller.state()


@pytest.fixture
def config(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "captures").mkdir()
    (state / "calibration.json").write_text('{"offline":true}')
    (state / "placing-grid.table-reference.json").write_text('{"offline":true}')
    (state / "placing-grid.json").write_text(json.dumps({"cell_size_mm": 90, "locations": [
        {"id": 1, "x": 450, "y": -200, "z": 230, "occupied": False}]}))
    model = tmp_path / "model.pt"
    model.write_bytes(b"offline")
    (tmp_path / "coordinator-token").write_text("offline-private-token")
    return {"backend": "local", "state_dir": "state", "model": "model.pt",
            "gateway": "http://gateway.invalid", "robot_id": "offline-robot",
            "failover": {"coordinator_url": "http://coordinator.invalid:8092/", "token_file": "coordinator-token"}}


def test_config_resolves_failover_paths_without_loading_model_or_starting_monitor(config, tmp_path, monkeypatch):
    create = Mock(return_value=lambda _: None)
    monkeypatch.setattr("local_device.python_factory", create)
    client = LocalDeviceClient.from_config(config, tmp_path, mode="live")
    try:
        args = create.call_args.args[0]
        assert args.coordinator_url == "http://coordinator.invalid:8092"
        assert args.coordinator_token_file == tmp_path / "coordinator-token"
        assert not (tmp_path / "state" / "picker-native.lock").exists()
        assert not (tmp_path / "state" / "device-owner.json").exists()
    finally:
        client.close()


@pytest.mark.parametrize("invalid", [{}, {"coordinator_url": "http://u:p@offline.invalid", "token_file": "coordinator-token"},
                                    {"coordinator_url": "http://offline.invalid/path", "token_file": "coordinator-token"},
                                    {"coordinator_url": "http://offline.invalid", "token_file": "missing"},
                                    {"coordinator_url": "http://offline.invalid", "token_file": "coordinator-token", "token": "bad"}])
def test_config_rejects_malformed_failover_before_factory(config, tmp_path, monkeypatch, invalid):
    config["failover"] = invalid
    create = Mock()
    monkeypatch.setattr("local_device.python_factory", create)
    with pytest.raises(ValueError):
        LocalDeviceClient.from_config(config, tmp_path, mode="live")
    create.assert_not_called()


def test_config_rejects_symlinked_coordinator_token(config, tmp_path):
    (tmp_path / "linked-token").symlink_to(tmp_path / "coordinator-token")
    config["failover"]["token_file"] = "linked-token"
    with pytest.raises(ValueError, match="regular file"):
        LocalDeviceClient.from_config(config, tmp_path, mode="live")


@pytest.mark.parametrize("live, configured, queued", [(True, True, True), (False, True, False), (True, False, False)])
def test_only_live_opted_in_robot_uses_queues(live, configured, queued):
    from inference_integration import create_robot
    from queued_robot_client import QueueRobotClient
    args = SimpleNamespace(live=live, coordinator_url="http://offline.invalid" if configured else None)
    robot = create_robot(args, "http://gateway.invalid/api/v1", {})
    assert isinstance(robot, QueueRobotClient) is queued
    assert robot.dry_run is not live


def test_hardware_factory_attaches_primary_continuous_runner_and_uses_shared_park(factory_rig, monkeypatch, tmp_path):
    from handover_plan import right_via_pose
    from web_app import hardware_factory
    rig = factory_rig
    token = tmp_path / "coordinator-token"
    token.write_text("offline-coordinator")
    rig.args.live = True
    rig.args.coordinator_url = "http://offline.invalid:8092"
    rig.args.coordinator_token_file = token
    rig.args.state_dir = tmp_path
    coordinator, runner = Mock(), Mock()
    monkeypatch.setitem(sys.modules, "failover_coordinator", SimpleNamespace(CoordinatorClient=coordinator))
    monkeypatch.setitem(sys.modules, "failover_workflow", SimpleNamespace(FailoverRunner=runner))
    sidecar = Mock(side_effect=AssertionError("Failover uses the shared right-via target"))
    monkeypatch.setattr("camera_parking.load_right_park_pose", sidecar)
    session, _ = hardware_factory(rig.args)(rig.controller)
    assert session.between_cup_check and session.between_cup_grid_check
    assert session.continuous_batch is True and session.right_park_pose == right_via_pose()
    assert session.failover is runner.return_value
    assert runner.call_args.kwargs["role"] == "primary"
    assert runner.call_args.kwargs["state_dir"] == tmp_path
    assert coordinator.call_args.args == ("http://offline.invalid:8092", "offline-coordinator", "primary")
    runner.return_value.start_monitor.assert_not_called()
    rig.captures.capture.assert_not_called()


@pytest.fixture
def ui(tmp_path):
    fixture = WebFixture(tmp_path, dry_run=False)
    runner = Mock()
    fixture.session.failover = runner
    controller = WebController(fixture.factory, mode="live")
    started = threading.Event()
    def start(callback):
        assert controller.state()["ready"] and not controller.state()["busy"]
        assert callback == controller.resume_failover
        assert fixture.captures == [] and fixture.robot.jobs == []
        started.set()
    runner.start_monitor.side_effect = start
    controller.start()
    assert started.wait(3)
    yield SimpleNamespace(fixture=fixture, controller=controller, runner=runner)
    controller.close()


def test_resume_is_serialized_and_is_not_a_public_ui_command(ui):
    entered, finish = threading.Event(), threading.Event()
    def resume(run_id):
        assert run_id == "active-run"
        entered.set()
        assert finish.wait(3)
        return "Batch continued."
    ui.runner.resume.side_effect = resume
    try:
        assert ui.controller.resume_failover("active-run")
        assert entered.wait(3)
        assert ui.controller.state()["operation"] == "failover_resume"
        assert not ui.controller.resume_failover("duplicate")
        with pytest.raises(WebError):
            ui.controller.submit("scan", {"request_id": uuid.uuid4().hex})
        with pytest.raises(WebError):
            ui.controller.submit("failover_resume", {"request_id": uuid.uuid4().hex, "run_id": "forged"})
    finally:
        finish.set()
    state = wait_idle(ui.controller)
    assert state["message"] == "Batch continued."
    ui.runner.resume.assert_called_once_with("active-run")


def test_recovery_reports_queue_recovery_only_after_explicit_acknowledgement(ui, tmp_path):
    ui.fixture.session.motion_uncertain = True
    ui.fixture.session.recovery = RecoveryJournal(tmp_path / "ui-recovery.json")
    ui.fixture.session.recovery.begin({"action": "operator_control", "control": "home", "arm": "both"})
    ui.controller.publish_session()
    ui.runner.recover_after_inspection.return_value = {
        "message": "Inspection acknowledged. Saved gateway failure cleared. No movement sent. Scan again."}
    with pytest.raises(WebError):
        ui.controller.submit("recovery", {"request_id": uuid.uuid4().hex, "acknowledged": False})
    ui.runner.recover_after_inspection.assert_not_called()
    ui.controller.submit("recovery", {"request_id": uuid.uuid4().hex, "acknowledged": True})
    state = wait_idle(ui.controller)
    ui.runner.recover_after_inspection.assert_called_once_with()
    assert state["message"] == ui.runner.recover_after_inspection.return_value["message"]
    assert not state["recovery_required"] and not state["scan_usable"]
    assert ui.fixture.robot.jobs == [] and ui.fixture.captures == []


def test_failed_gateway_recovery_does_not_clear_local_recovery(ui, tmp_path):
    ui.fixture.session.motion_uncertain = True
    ui.fixture.session.recovery = RecoveryJournal(tmp_path / "ui-recovery.json")
    ui.fixture.session.recovery.begin({"action": "operator_control", "control": "home", "arm": "both"})
    ui.controller.publish_session()
    before = ui.fixture.session.recovery.path.read_bytes()
    ui.runner.recover_after_inspection.side_effect = RuntimeError("Queue changed; inspect current state")
    ui.controller.submit("recovery", {"request_id": uuid.uuid4().hex, "acknowledged": True})
    state = wait_idle(ui.controller)
    assert "Queue changed" in state["error"]
    assert state["recovery_required"] and not state["scan_usable"]
    assert ui.fixture.session.recovery.path.read_bytes() == before
    assert ui.fixture.robot.jobs == [] and ui.fixture.captures == []


def test_physical_stop_is_not_delayed_by_gateway_recovery_io(ui, tmp_path):
    ui.fixture.session.motion_uncertain = True
    ui.fixture.session.recovery = RecoveryJournal(tmp_path / "ui-recovery.json")
    ui.fixture.session.recovery.begin({"action": "operator_control", "control": "home", "arm": "both"})
    ui.controller.publish_session()
    entered, release, physical = threading.Event(), threading.Event(), threading.Event()
    def recover():
        entered.set()
        assert release.wait(3)
        return {"message": "Recovered"}
    def stop():
        physical.set()
        return {"stopped": True, "dry_run": False}
    ui.runner.recover_after_inspection.side_effect = recover
    ui.fixture.robot.request_stop = Mock(side_effect=stop)
    try:
        ui.controller.submit("recovery", {"request_id": uuid.uuid4().hex, "acknowledged": True})
        assert entered.wait(1)
        ui.controller.submit("stop", {"request_id": uuid.uuid4().hex})
        assert physical.wait(1), "Physical Stop must not wait for the recovery request"
        deadline = time.monotonic() + 2
        while ui.controller.state().get("stop_in_flight"):
            assert time.monotonic() < deadline
            time.sleep(0.005)
    finally:
        release.set()
    state = wait_idle(ui.controller)
    assert state["stop_requested"] and state["recovery_required"]
    assert not state["scan_usable"] and ui.fixture.session.recovery.required()
    assert ui.fixture.robot.jobs == []


def test_stop_calls_shared_and_physical_stop_and_reports_coordinator_failure(ui):
    order = []
    def shared_stop():
        order.append("shared")
        raise RuntimeError("offline coordinator unavailable")
    def physical_stop():
        order.append("physical")
        return {"stopped": True, "dry_run": False}
    ui.runner.stop_batch.side_effect = shared_stop
    ui.fixture.session.request_stop = Mock(side_effect=physical_stop)
    ui.controller.submit("stop", {"request_id": uuid.uuid4().hex})
    state = wait_idle(ui.controller)
    assert sorted(order) == ["physical", "shared"]
    assert state["stop"]["stopped"] is True
    assert "shared failover stop was not confirmed" in state["error"]
    assert not ui.controller.resume_failover("active-run")


def test_unresponsive_coordinator_never_delays_physical_stop(ui):
    shared_entered, release, physical_called = threading.Event(), threading.Event(), threading.Event()
    def shared_stop():
        shared_entered.set()
        assert release.wait(3)
    def physical_stop():
        physical_called.set()
        return {"stopped": True, "dry_run": False}
    ui.runner.stop_batch.side_effect = shared_stop
    ui.fixture.session.request_stop = Mock(side_effect=physical_stop)
    try:
        ui.controller.submit("stop", {"request_id": uuid.uuid4().hex})
        assert shared_entered.wait(0.5)
        assert physical_called.wait(0.5), "Robot Stop must not wait for coordinator HTTP"
        state = wait_idle(ui.controller)
        assert "shared failover stop was not confirmed" in state["error"]
        assert state["stop"]["stopped"]
    finally:
        release.set()


def test_close_unblocks_runner_before_waiting_for_controller_worker(ui):
    entered, closed = threading.Event(), threading.Event()
    def resume(_run_id):
        entered.set()
        assert closed.wait(3)
        return "Disarmed."
    ui.runner.resume.side_effect = resume
    ui.runner.close.side_effect = closed.set
    assert ui.controller.resume_failover("active-run")
    assert entered.wait(3)
    ui.controller.close()
    assert closed.is_set()
    assert not ui.controller.resume_failover("active-run")
    ui.runner.close.assert_called_once()
    assert ui.fixture.robot.jobs == []


def test_pi_activation_excludes_armed_worker_before_model_loading(tmp_path, monkeypatch):
    from device_service import python_factory
    from lynx.runtime_lock import NativeRuntimeLock
    args = SimpleNamespace(device_id="pi", state_dir=tmp_path, model=tmp_path / "model.pt", depth_frames=5, threads=3)
    hardware = Mock()
    monkeypatch.setattr("web_app.hardware_factory", hardware)
    with NativeRuntimeLock(tmp_path):
        with pytest.raises(RuntimeError, match="already uses"):
            python_factory(args)(SimpleNamespace(attach_resource=Mock()))
    hardware.assert_not_called()
