"""Device-speed HTTP and controller integration without camera or robot I/O."""

import copy
import time
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest

from failover_workflow import FailoverRunner
from queued_robot_client import QueueRobotClient
from test_web_controller import WebFixture
from web_app import create_app
from web_controller import WebController, WebError


SPEEDS = {"primary": 900, "secondary": 500, "tertiary": 250}


def wait_idle(controller):
    deadline = time.monotonic() + 3
    while controller.state()["busy"]:
        assert time.monotonic() < deadline, "Offline controller did not become idle"
        time.sleep(.005)
    return controller.state()


@pytest.fixture
def ui(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr("requests.sessions.Session.request",
                        Mock(side_effect=AssertionError("Unexpected real network request")))
    fixture = WebFixture(tmp_path, dry_run=False)
    fixture.session.between_cup_check = fixture.session.between_cup_grid_check = True
    fixture.robot.cancel_wait = Mock()
    fixture.robot._read_pending = QueueRobotClient._read_pending
    coordinator = Mock()
    coordinator.state.return_value = {"run": None, "head": None, "capabilities": ["device_speed_profiles"]}
    runner = FailoverRunner(fixture.session, coordinator, role="primary", state_dir=tmp_path)
    monkeypatch.setattr(runner, "start_monitor", lambda callback: None)
    controller = WebController(fixture.factory, mode="live")
    with TestClient(create_app(controller), base_url="http://127.0.0.1:8080") as client:
        state = wait_idle(controller)
        assert state["ready"], state["error"]
        headers = {"Origin": "http://127.0.0.1:8080", "X-CSRF-Token": state["csrf_token"]}
        yield SimpleNamespace(fixture=fixture, runner=runner, coordinator=coordinator,
                              controller=controller, client=client, headers=headers, root=tmp_path)
    assert fixture.robot.jobs == []


def apply(ui, **values):
    response = ui.client.post("/api/device_speeds", headers=ui.headers,
                              json={**values, "request_id": uuid.uuid4().hex})
    assert response.status_code == 202, response.text
    return wait_idle(ui.controller)


def test_device_speeds_http_updates_preview_and_shared_settings_without_motion(ui):
    before = ui.client.get("/api/state").json()
    assert before["motion"]["device_speeds_supported"] is True
    assert set(before["motion"]["device_speeds"]) == set(SPEEDS)
    state = apply(ui, **SPEEDS)
    assert state["error"] is None
    assert state["motion"]["device_speeds"] == SPEEDS
    assert state["motion"]["speed"] == 900
    assert state["motion"]["home_speed"] == 200
    assert ui.runner.settings()["device_speeds"] == SPEEDS
    for _ in range(3):
        assert ui.client.get("/api/state").json()["motion"]["device_speeds"] == SPEEDS
    assert ui.fixture.captures == []
    ui.coordinator.start.assert_not_called()


@pytest.mark.parametrize("values", [
    {"primary": 100, "secondary": 100}, {**SPEEDS, "other": 100},
    {**SPEEDS, "primary": True}, {**SPEEDS, "secondary": "200"},
    {**SPEEDS, "tertiary": 0}, {**SPEEDS, "tertiary": 1501},
])
def test_invalid_fields_are_rejected_before_persistence(ui, values):
    before = ui.runner.device_speed_settings()
    response = ui.client.post("/api/device_speeds", headers=ui.headers,
                              json={**values, "request_id": uuid.uuid4().hex})
    assert response.status_code == 422
    assert ui.runner.device_speed_settings() == before
    assert not (ui.root / "device-speeds.json").exists()


def test_existing_overall_endpoint_updates_only_macbook_profile(ui):
    apply(ui, **SPEEDS)
    ui.controller.submit("speed", {"speed": 650, "request_id": uuid.uuid4().hex})
    state = wait_idle(ui.controller)
    assert state["error"] is None
    assert state["motion"]["device_speeds"] == {**SPEEDS, "primary": 650}
    assert state["motion"]["speed"] == 650


def test_changing_only_remote_speed_invalidates_existing_confirmation(ui):
    ui.fixture.session.scan()
    proposal = ui.fixture.language.propose_selection(action="hover", capture_id=ui.fixture.session.inventory["capture"], cup_id=1)
    ui.controller.publish_session()
    assert ui.controller.state()["proposal"]
    values = ui.runner.device_speed_settings()
    values["tertiary"] = 75
    state = apply(ui, **values)
    assert state["error"] is None
    assert state["proposal"] is None and ui.fixture.language.pending is None
    with pytest.raises(ValueError, match="pending"):
        ui.fixture.language.confirm(proposal["token"])
    assert len(ui.fixture.captures) == 1


@pytest.mark.parametrize("phase", ["active", "finalizing", "watching"])
def test_shared_batch_blocks_changes_even_when_local_ui_idle(ui, phase):
    apply(ui, **SPEEDS)
    before = (ui.root / "device-speeds.json").read_bytes()
    shared = {"id": "running-batch", "phase": phase, "settings": {"device_speeds": copy.deepcopy(SPEEDS)}}
    ui.coordinator.state.return_value["run"] = shared
    state = apply(ui, **{**SPEEDS, "secondary": 50})
    assert state["error"]
    assert state["motion"]["device_speeds"] == SPEEDS
    assert (ui.root / "device-speeds.json").read_bytes() == before
    assert shared["settings"]["device_speeds"] == SPEEDS
    assert ui.fixture.captures == []


def test_busy_stop_and_recovery_guards_do_not_mutate_profiles(ui):
    before = ui.runner.device_speed_settings()
    with ui.controller._lock:
        ui.controller._update(busy=True)
    with pytest.raises(WebError, match="busy"):
        ui.controller.submit("device_speeds", {**SPEEDS, "request_id": uuid.uuid4().hex})
    with ui.controller._lock:
        ui.controller._update(busy=False)
    ui.fixture.session.motion_uncertain = True
    assert apply(ui, **SPEEDS)["error"]
    assert ui.runner.device_speed_settings() == before
    ui.fixture.session.motion_uncertain = False
    ui.fixture.session.latch_stop()
    ui.controller.publish_session()
    with pytest.raises(WebError, match="stop"):
        ui.controller.submit("device_speeds", {**SPEEDS, "request_id": uuid.uuid4().hex})
    assert ui.runner.device_speed_settings() == before


def test_device_speeds_http_requires_current_csrf(ui):
    response = ui.client.post("/api/device_speeds", headers={"Origin": "http://127.0.0.1:8080"},
                              json={**SPEEDS, "request_id": uuid.uuid4().hex})
    assert response.status_code == 403
    assert not (ui.root / "device-speeds.json").exists()
