"""Pickup-height HTTP controls, previews and shared admission; no real hardware."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest

from failover_workflow import FailoverRunner
from queued_robot_client import QueueRobotClient
from test_device_speed_failover import completed_speed_receipts
from test_device_speed_ui import wait_idle
from test_failover_workflow import rig
from test_queued_robot_client import gateway
from test_web_controller import WebFixture
from web_app import create_app
from web_controller import WebController


@pytest.fixture
def ui(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr("requests.sessions.Session.request",
                        Mock(side_effect=AssertionError("Unexpected real network request")))
    root = tmp_path / "height-ui"
    fixture = WebFixture(root, dry_run=False)
    fixture.session.between_cup_check = fixture.session.between_cup_grid_check = True
    fixture.robot.cancel_wait = Mock()
    for name in ("_read_pending", "_validate_queue_status", "_signature"):
        setattr(fixture.robot, name, getattr(QueueRobotClient, name))
    coordinator = Mock()
    coordinator.state.return_value = {"run": None, "head": None, "capabilities": []}
    runner = FailoverRunner(fixture.session, coordinator, role="primary", state_dir=root)
    monkeypatch.setattr(runner, "start_monitor", lambda callback: None)
    controller = WebController(fixture.factory, mode="live")
    with TestClient(create_app(controller), base_url="http://127.0.0.1:8080") as client:
        state = wait_idle(controller)
        assert state["ready"], state["error"]
        headers = {"Origin": "http://127.0.0.1:8080", "X-CSRF-Token": state["csrf_token"]}
        yield SimpleNamespace(fixture=fixture, runner=runner, coordinator=coordinator,
                              controller=controller, client=client, headers=headers, root=root)
    assert fixture.robot.jobs == []


def apply(ui, camera_z, *, path="/api/pick-height", request_id=None):
    response = ui.client.post(path, headers=ui.headers,
        json={"camera_z": camera_z, "request_id": request_id or uuid.uuid4().hex})
    assert response.status_code == 202, response.text
    return wait_idle(ui.controller)


@pytest.mark.parametrize("path", ["/api/pick-height", "/api/pick_height"])
def test_both_routes_update_state_and_next_shared_settings_without_motion_or_capture(ui, path):
    before = ui.client.get("/api/state").json()
    assert before["motion"]["pick_height_supported"] is True
    assert before["motion"]["camera_z"] is False
    for choice in (True, False):
        state = apply(ui, choice, path=path)
        assert state["error"] is None
        assert state["motion"]["camera_z"] is choice
        assert ui.runner.settings()["motion"]["camera_z"] is choice
        assert ui.fixture.session.motion_settings()["camera_z"] is choice
        for _ in range(2):
            assert ui.client.get("/api/state").json()["motion"]["camera_z"] is choice
    assert ui.fixture.captures == []
    ui.coordinator.start.assert_not_called()


@pytest.mark.parametrize("choice", [None, 0, 1, "false", "true", [], {}])
def test_boolean_coercions_are_rejected_at_http_boundary(ui, choice):
    response = ui.client.post("/api/pick-height", headers=ui.headers,
        json={"camera_z": choice, "request_id": uuid.uuid4().hex})
    assert response.status_code == 422
    assert ui.fixture.session.camera_z is False
    assert ui.fixture.captures == []


@pytest.mark.parametrize("fields", [{}, {"mode": "camera"}, {"camera_z": True, "extra": 1}])
def test_missing_unknown_and_extra_fields_are_rejected(ui, fields):
    response = ui.client.post("/api/pick-height", headers=ui.headers,
                             json={**fields, "request_id": uuid.uuid4().hex})
    assert response.status_code == 422
    assert ui.fixture.session.camera_z is False


@pytest.mark.parametrize("body", [
    '{"camera_z":true,"camera_z":false,"request_id":"height-test"}',
    '{"camera_z":true,"request_id":"height-test","request_id":"duplicate"}',
    '{"camera_z":', '[true]', 'null',
])
def test_duplicate_or_malformed_json_is_rejected_without_changing_height(ui, body):
    response = ui.client.post("/api/pick-height", content=body,
                             headers={**ui.headers, "Content-Type": "application/json"})
    assert response.status_code == 422
    assert ui.fixture.session.camera_z is False
    assert ui.fixture.captures == []


@pytest.mark.parametrize("headers", [{}, {"Origin": "http://127.0.0.1:8080"},
    {"Origin": "http://untrusted.invalid", "X-CSRF-Token": "wrong"},
    {"Origin": "http://127.0.0.1:8080", "X-CSRF-Token": "wrong"}])
def test_pick_height_requires_current_csrf_and_same_origin(ui, headers):
    response = ui.client.post("/api/pick-height", headers=headers,
        json={"camera_z": True, "request_id": uuid.uuid4().hex})
    assert response.status_code == 403
    assert ui.fixture.session.camera_z is False
    assert ui.fixture.captures == []


def test_repeated_request_id_does_not_apply_a_second_choice(ui):
    request_id = uuid.uuid4().hex
    assert apply(ui, True, request_id=request_id)["error"] is None
    response = ui.client.post("/api/pick-height", headers=ui.headers,
                             json={"camera_z": False, "request_id": request_id})
    assert response.status_code == 409
    assert ui.fixture.session.camera_z is True
    assert ui.fixture.captures == []


def test_height_change_cancels_both_previews_and_requires_new_pick_confirmation(ui):
    ui.fixture.session.scan()
    capture = ui.fixture.session.inventory["capture"]
    old = ui.fixture.language.propose_selection(action="goto", capture_id=capture, cup_id=1)
    ui.controller.controls.propose("home", "left")
    old_control_token = ui.controller.controls.pending["token"]
    ui.controller.publish_session()
    assert ui.controller.state()["proposal"]
    state = apply(ui, True)
    assert state["error"] is None
    assert state["proposal"] is None
    assert ui.fixture.language.pending is None and ui.controller.controls.pending is None
    assert len(ui.fixture.captures) == 1
    with pytest.raises(ValueError, match="pending"):
        ui.fixture.language.confirm(old["token"])
    with pytest.raises(ValueError, match="no longer matches"):
        ui.controller.controls.confirm(old_control_token)
    response = ui.client.post("/api/select", headers=ui.headers,
        json={"action": "goto", "capture_id": capture, "cup_id": 1, "request_id": uuid.uuid4().hex})
    assert response.status_code == 202, response.text
    fresh = wait_idle(ui.controller)
    assert fresh["error"] is None
    assert fresh["proposal"]["camera_z"] is True
    assert "base plus 20 mm" in fresh["proposal"]["description"]
    assert "40 mm minimum" in fresh["proposal"]["description"]
    assert fresh["proposal"]["token"] != old["token"]
    assert len(ui.fixture.captures) == 1


@pytest.mark.parametrize("phase", ["active", "finalizing", "watching"])
def test_shared_active_run_blocks_height_even_when_browser_is_idle(ui, phase):
    shared = {"id": "pi-owned-run", "phase": phase,
              "settings": {"motion": {"camera_z": False}}}
    before = copy.deepcopy(shared)
    ui.coordinator.state.return_value["run"] = shared
    assert ui.controller.state()["busy"] is False
    state = apply(ui, True)
    assert "shared batch" in state["error"]
    assert state["motion"]["camera_z"] is False
    assert shared == before
    assert ui.fixture.captures == []


@pytest.mark.parametrize("condition", ["busy", "recovery", "stop"])
def test_local_busy_recovery_and_stop_reject_height_changes(ui, condition):
    if condition == "busy":
        with ui.controller._lock:
            ui.controller._update(busy=True)
    elif condition == "recovery":
        ui.fixture.session.motion_uncertain = True
    else:
        ui.fixture.session.latch_stop()
        ui.controller.publish_session()
    try:
        response = ui.client.post("/api/pick-height", headers=ui.headers,
                                 json={"camera_z": True, "request_id": uuid.uuid4().hex})
        if condition == "recovery":
            assert response.status_code == 202
            assert wait_idle(ui.controller)["error"]
        else:
            assert response.status_code == 409
        assert ui.fixture.session.camera_z is False
        assert ui.fixture.captures == []
    finally:
        if condition == "busy":
            with ui.controller._lock:
                ui.controller._update(busy=False)


@pytest.mark.parametrize("kind", ["cup", "home"])
def test_completed_previous_receipt_does_not_block_height_ui(completed_speed_receipts, ui, kind):
    # The receipt was produced by the real offline queue workflow, not a
    # simplified shape that could bypass validation of completion evidence.
    receipt = copy.deepcopy(completed_speed_receipts[kind])
    ui.runner.pending_path.write_text(json.dumps(receipt))
    before = ui.runner.pending_path.read_bytes()
    ui.coordinator.state.return_value = {"run": {"id": "old-batch", "phase": "complete"},
                                         "head": {"state": "done"}, "capabilities": []}
    state = apply(ui, True)
    assert state["error"] is None
    assert state["motion"]["camera_z"] is True
    assert ui.runner.pending_path.read_bytes() == before
    assert ui.fixture.captures == []
