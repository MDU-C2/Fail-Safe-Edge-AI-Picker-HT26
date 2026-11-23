"""Pickup-height preference admission and failover, using offline doubles only."""

import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cup_commands import CupSession
from cup_inventory import GRIPPER_TCP_Z, SelectionError
from failover_coordinator import CoordinatorUnavailable
from handover_plan import handover_jobs
from robot_client import RobotCommandError
from test_between_cup_grid import CLEAR, observed_scene
from test_camera_z_motion import RobotDouble, scene
from test_continuous_failover import successor
from test_device_speed_failover import completed_speed_receipts, receipt_and_workplace_bytes
from test_failover_workflow import rig, start
from test_queued_robot_client import gateway
from test_support_grid import grid_fixture
from test_v4_terminal import load_v4_terminal


INVALID = [None, 0, 1, "false", "true", [], {}, float("nan")]


@pytest.fixture
def local(tmp_path):
    capture, save = Mock(return_value=(scene(80), 99)), Mock()
    robot = RobotDouble()
    grid = grid_fixture(tmp_path)
    session = CupSession(capture, save, robot, "cal-1", placing_grid=grid)
    return SimpleNamespace(session=session, capture=capture, save=save, robot=robot, grid=grid)


def test_local_choice_changes_only_future_policy_without_capture_or_robot_io(local):
    session = local.session
    before = session.motion_settings()
    grid = local.grid.snapshot()
    session.batch_progress = {"state": "complete"}
    for choice in (True, False):
        result = session.set_pick_height(choice)
        assert result["camera_z"] is choice
        assert session.camera_z is choice
        assert session.motion_settings() == {**before, "camera_z": choice}
        assert session.batch_progress is None
        assert local.grid.snapshot() == grid
    assert local.robot.jobs == []
    local.capture.assert_not_called()
    local.save.assert_not_called()
    with pytest.raises(AttributeError):
        session.camera_z = True


@pytest.mark.parametrize("choice", [False, True])
@pytest.mark.parametrize("base_height", [-30, 19.9, 63.26, 150])
def test_changed_policy_matches_terminal_exactly_without_altering_other_motion(local, monkeypatch, choice, base_height):
    terminal = load_v4_terminal()
    monkeypatch.setattr(terminal, "USE_CAMERA_Z", choice)
    monkeypatch.setattr(terminal, "log", Mock())
    cup = {"x": 500, "y": 100, "z": base_height, "selectable": True, "blocked_reason": None}
    place = local.grid.snapshot()["locations"][0]
    baseline = handover_jobs(cup, place=place)
    local.session.set_pick_height(choice)
    actual = handover_jobs(cup, place=place, camera_z=local.session.camera_z)
    expected = copy.deepcopy(baseline)
    for job in expected:
        if job["name"] == "approach_cup":
            job["body"]["points"][-1]["z"] = terminal.Handover.pick_z(cup) + GRIPPER_TCP_Z
    assert actual == expected
    assert local.robot.jobs == []


@pytest.mark.parametrize("value", INVALID)
def test_non_boolean_never_changes_local_policy(local, value):
    with pytest.raises(SelectionError, match="boolean"):
        local.session.set_pick_height(value)
    assert local.session.camera_z is False
    assert local.robot.jobs == []
    local.capture.assert_not_called()


@pytest.mark.parametrize("condition", ["busy", "stop", "recovery", "journal"])
def test_local_active_or_unresolved_operation_rejects_height_change(local, condition):
    locked = condition == "busy"
    if locked:
        local.session._activity_lock.acquire()
    elif condition == "stop":
        local.session.latch_stop()
    elif condition == "recovery":
        local.session.motion_uncertain = True
    else:
        local.session.recovery = Mock(required=Mock(return_value=True))
    try:
        with pytest.raises(SelectionError):
            local.session.set_pick_height(True)
        assert local.session.camera_z is False
        assert local.robot.jobs == []
    finally:
        if locked:
            local.session._activity_lock.release()


def test_changed_height_invalidates_prior_confirmation_before_motion(local):
    local.session.scan()
    previous = local.session.motion_settings()
    local.session.set_pick_height(True)
    with pytest.raises(SelectionError, match="settings changed"):
        local.session.execute_selection(action="goto", capture_id="scan-1", cup_id=1,
                                        expected_motion=previous)
    assert local.capture.call_count == 1
    assert local.robot.jobs == []


@pytest.mark.parametrize("value", INVALID)
def test_shared_height_rejects_non_boolean_before_coordinator_or_gateway(rig, monkeypatch, value):
    state = Mock(side_effect=AssertionError("Invalid preference must not read coordinator"))
    monkeypatch.setattr(rig.coordinator, "state", state)
    with pytest.raises(SelectionError, match="boolean"):
        rig.runner.set_pick_height(value)
    assert rig.session.camera_z is False
    assert rig.gateway.calls == []
    state.assert_not_called()


@pytest.mark.parametrize("phase", ["active", "finalizing", "watching", "unknown"])
def test_session_setter_cannot_bypass_shared_batch_owned_by_other_device(rig, monkeypatch, phase):
    monkeypatch.setattr(rig.coordinator, "state", lambda: {
        "run": {"id": "pi-batch", "phase": phase}, "head": None})
    with pytest.raises(SelectionError, match="shared batch"):
        rig.session.set_pick_height(True)
    assert rig.session.camera_z is False
    assert rig.gateway.calls == []
    rig.capture.assert_not_called()


@pytest.mark.parametrize("condition", ["executing", "execution_lock", "activity_lock", "stop", "recovery", "closed"])
def test_shared_local_work_stop_recovery_or_shutdown_blocks_height(rig, condition):
    acquired = None
    if condition == "executing":
        rig.runner.executing = True
    elif condition in {"execution_lock", "activity_lock"}:
        acquired = rig.runner._execution if condition == "execution_lock" else rig.session._activity_lock
        acquired.acquire()
    elif condition == "stop":
        rig.session.latch_stop()
    elif condition == "recovery":
        rig.session.motion_uncertain = True
    else:
        rig.runner._closed.set()
    try:
        with pytest.raises(SelectionError):
            rig.runner.set_pick_height(True)
        assert rig.session.camera_z is False
        assert rig.gateway.calls == []
    finally:
        if acquired:
            acquired.release()


@pytest.mark.parametrize("state", [{}, {"run": None},
    {"run": None, "head": {"state": "failed"}},
    {"run": None, "head": {"state": "prepared"}},
    {"run": None, "head": {"state": "unknown"}}])
def test_unknown_or_unresolved_shared_state_cannot_change_height(rig, monkeypatch, state):
    monkeypatch.setattr(rig.coordinator, "state", lambda: state)
    with pytest.raises(SelectionError):
        rig.runner.set_pick_height(True)
    assert rig.session.camera_z is False
    assert rig.gateway.calls == []


@pytest.mark.parametrize("phase", ["complete", "stopped", "blocked"])
@pytest.mark.parametrize("head", [None, {"state": "done"}, {"state": "not_submitted"}])
def test_inactive_shared_batch_accepts_future_height_without_capability_change(rig, monkeypatch, phase, head):
    saved = {"run": {"id": "old-run", "phase": phase,
                      "settings": {"motion": {"camera_z": False}}}, "head": head}
    original = copy.deepcopy(saved)
    monkeypatch.setattr(rig.coordinator, "state", lambda: saved)
    assert rig.session.set_pick_height(True)["camera_z"] is True
    assert rig.runner.settings()["motion"]["camera_z"] is True
    assert saved == original
    assert rig.gateway.calls == []


def test_coordinator_outage_preserves_height_and_releases_locks(rig, monkeypatch):
    state = rig.coordinator.state
    monkeypatch.setattr(rig.coordinator, "state", Mock(side_effect=CoordinatorUnavailable("Offline")))
    with pytest.raises(CoordinatorUnavailable):
        rig.runner.set_pick_height(True)
    assert rig.session.camera_z is False
    monkeypatch.setattr(rig.coordinator, "state", state)
    assert rig.runner.set_pick_height(True)["camera_z"] is True
    assert rig.gateway.calls == []


@pytest.mark.parametrize("kind", ["cup", "home"])
def test_completed_receipt_allows_height_without_reconciling_history(rig, completed_speed_receipts, kind):
    rig.robot._write_pending(rig.runner.pending_path, completed_speed_receipts[kind])
    before = receipt_and_workplace_bytes(rig)
    calls, queue = copy.deepcopy(rig.gateway.calls), copy.deepcopy(rig.gateway.queue)
    captures = rig.capture.call_count
    assert rig.runner.set_pick_height(True)["camera_z"] is True
    assert receipt_and_workplace_bytes(rig) == before
    assert rig.gateway.calls == calls and rig.gateway.queue == queue
    assert rig.capture.call_count == captures


@pytest.mark.parametrize("change", ["callbacks", "pending", "failed", "owner", "missing_queue", "signature"])
def test_unresolved_or_invalid_receipt_blocks_height_without_changes(rig, completed_speed_receipts, change):
    record = copy.deepcopy(completed_speed_receipts["home"])
    if change == "callbacks":
        record["callbacks_applied"] = False
    elif change in {"pending", "failed"}:
        record.update(state=change, callbacks_applied=False)
    elif change == "owner":
        record["queue"]["owner"] = "secondary"
    elif change == "missing_queue":
        record["queue"] = None
    else:
        record["queue"]["steps"][0]["name"] = "another_home"
    rig.robot._write_pending(rig.runner.pending_path, record)
    before = receipt_and_workplace_bytes(rig)
    calls = copy.deepcopy(rig.gateway.calls)
    with pytest.raises((SelectionError, RobotCommandError)):
        rig.runner.set_pick_height(True)
    assert rig.session.camera_z is False
    assert receipt_and_workplace_bytes(rig) == before
    assert rig.gateway.calls == calls


@pytest.mark.parametrize("role", ["secondary", "tertiary"])
@pytest.mark.parametrize("choice", [False, True])
def test_pi_and_lynx_use_admitted_height_in_actual_cup_queue(rig, tmp_path, role, choice):
    rig.session.set_pick_height(choice)
    start(rig)
    with successor(rig, tmp_path / role, role=role) as worker:
        with pytest.raises(SelectionError, match="Only the primary"):
            worker.session.set_pick_height(not choice)
        inventory = observed_scene(worker.grid, 10, [(100, 450)], CLEAR)
        cup = inventory["cups"][0]
        cup["z"] = 83.4
        worker.capture.side_effect = [
            (inventory, 99), (observed_scene(worker.grid, 11, [], CLEAR), 100)]
        worker.runner.resume("batch-one")
        submitted = worker.gateway.submissions[0]["steps"]
        approach = next(step for step in submitted if step["name"].endswith("_approach_cup"))
        assert approach["body"]["points"][-1]["z"] == (103.4 if choice else 70) + GRIPPER_TCP_Z
        assert worker.coordinator.state()["run"]["settings"]["motion"]["camera_z"] is choice
        assert worker.coordinator.state()["run"]["phase"] == "complete"
        assert len(worker.gateway.submissions) == 3  # one cup and the existing two final Home queues
