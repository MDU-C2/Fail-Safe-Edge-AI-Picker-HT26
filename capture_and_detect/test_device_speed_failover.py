"""Per-device speed preferences and immutable shared plans; fake gateway only."""

import copy
import json
from unittest.mock import Mock

import pytest

from cup_inventory import SelectionError
from failover_coordinator import (CoordinatorError, CoordinatorConflict, CoordinatorStore,
                                  CoordinatorUnavailable, DEVICE_SPEED_CAPABILITY,
                                  validate_device_speeds)
from failover_workflow import FailoverRunner
from queued_robot_client import ControlUnavailable, QueueRobotClient
from robot_client import RobotCommandError
from test_between_cup_grid import CLEAR, observed_scene
from test_continuous_failover import successor
from test_failover_workflow import rig, start
from test_queued_robot_client import API, gateway


PROFILE = {"primary": 140.0, "secondary": 80.0, "tertiary": 260.0}
INVALID = [None, [], {}, {"primary": 100}, {**PROFILE, "other": 100},
           *[{**PROFILE, "secondary": value} for value in [True, "100", None, 0, 1501, 10 ** 1000, float("nan"), float("inf")]]]


def test_defaults_follow_local_speed_without_persisting_or_changing_legacy_settings(rig):
    assert rig.runner.supports_device_speeds is True
    assert rig.runner.device_speed_settings() == dict.fromkeys(PROFILE, float(rig.session.speed))
    assert "device_speeds" not in rig.runner.settings()
    rig.session.set_speed(90)
    assert rig.runner.device_speed_settings() == dict.fromkeys(PROFILE, 90.0)
    assert not rig.runner.device_speeds_path.exists()
    assert rig.gateway.calls == []


def test_explicit_apply_persists_only_future_preferences_and_survives_restart(rig, tmp_path):
    old_phases = dict(rig.session.phase_speeds)
    result = rig.runner.set_device_speeds(PROFILE)
    assert result["device_speeds"] == PROFILE
    assert (rig.session.speed, rig.session.home_speed) == (140.0, 140.0)
    assert rig.session.phase_speeds == old_phases
    assert rig.runner.settings()["device_speeds"] == PROFILE
    saved = rig.runner.device_speed_settings()
    saved["secondary"] = 999
    assert rig.runner.device_speed_settings() == PROFILE
    assert json.loads((tmp_path / "device-speeds.json").read_text()) == {
        "schema_version": 1, "device_speeds": PROFILE}
    rig.session.speed = 300
    restored = FailoverRunner(rig.session, rig.coordinator, role="primary", state_dir=tmp_path, output=Mock())
    assert restored.device_speed_settings() == PROFILE
    assert (rig.session.speed, rig.session.home_speed) == (140.0, 140.0)
    assert rig.gateway.calls == []
    rig.capture.assert_not_called()


@pytest.mark.parametrize("bad", INVALID)
def test_invalid_preferences_never_change_memory_or_create_a_file(rig, bad):
    before = rig.runner.settings()
    with pytest.raises(SelectionError, match="Device speeds"):
        rig.runner.set_device_speeds(bad)
    assert rig.runner.settings() == before
    assert not rig.runner.device_speeds_path.exists()
    assert rig.gateway.calls == []


@pytest.mark.parametrize("role", ["secondary", "tertiary"])
def test_standby_cannot_edit_profiles(rig, tmp_path, role):
    with successor(rig, tmp_path / role, role=role) as worker:
        assert worker.runner.supports_device_speeds is False
        with pytest.raises(SelectionError, match="Only the primary"):
            worker.runner.set_device_speeds(PROFILE)
        assert not worker.runner.device_speeds_path.exists()
        assert worker.gateway.calls == []


@pytest.mark.parametrize("phase", ["active", "finalizing", "watching", "unknown"])
def test_shared_active_or_unknown_phase_locks_preferences(rig, monkeypatch, phase):
    monkeypatch.setattr(rig.coordinator, "state", lambda: {
        "run": {"id": "running", "phase": phase}, "head": None,
        "capabilities": [DEVICE_SPEED_CAPABILITY]})
    with pytest.raises(SelectionError, match="shared batch"):
        rig.runner.set_device_speeds(PROFILE)
    assert not rig.runner.device_speeds_path.exists()
    assert rig.gateway.calls == []


@pytest.mark.parametrize("condition", ["executing", "execution_lock", "activity_lock", "stop", "recovery", "closed"])
def test_local_work_stop_recovery_or_shutdown_locks_preferences(rig, condition):
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
            rig.runner.set_device_speeds(PROFILE)
        assert not rig.runner.device_speeds_path.exists()
        assert rig.gateway.calls == []
    finally:
        if acquired:
            acquired.release()


@pytest.mark.parametrize("state", [{}, {"run": None},
    {"run": None, "head": {"state": "failed"}, "capabilities": [DEVICE_SPEED_CAPABILITY]},
    {"run": None, "head": None, "capabilities": []}])
def test_unknown_recovery_or_unsupported_shared_state_cannot_apply(rig, monkeypatch, state):
    monkeypatch.setattr(rig.coordinator, "state", lambda: state)
    with pytest.raises(SelectionError):
        rig.runner.set_device_speeds(PROFILE)
    assert not rig.runner.device_speeds_path.exists()
    assert rig.gateway.calls == []


def test_coordinator_outage_and_disk_failure_preserve_last_saved_preferences(rig, monkeypatch):
    rig.runner.set_device_speeds(PROFILE)
    before = rig.runner.device_speeds_path.read_bytes()
    replacement = {**PROFILE, "primary": 55}
    with monkeypatch.context() as patcher:
        patcher.setattr(rig.coordinator, "state", Mock(side_effect=CoordinatorUnavailable("Offline")))
        with pytest.raises(CoordinatorUnavailable):
            rig.runner.set_device_speeds(replacement)
    with monkeypatch.context() as patcher:
        patcher.setattr("failover_workflow.atomic_json", Mock(side_effect=OSError("Disk full")))
        with pytest.raises(SelectionError, match="Could not save"):
            rig.runner.set_device_speeds(replacement)
    assert rig.runner.device_speed_settings() == PROFILE
    assert rig.session.speed == PROFILE["primary"]
    assert rig.runner.device_speeds_path.read_bytes() == before
    assert rig.gateway.calls == []


@pytest.mark.parametrize("role", ["primary", "secondary", "tertiary"])
def test_each_role_uses_its_admitted_speed_in_actual_cup_and_home_queues(rig, tmp_path, role):
    rig.runner.set_device_speeds(PROFILE)
    start(rig)
    with successor(rig, tmp_path / role, role=role) as worker:
        local_before = (worker.session.speed, worker.session.home_speed)
        worker.capture.side_effect = [
            (observed_scene(worker.grid, 10, [(100, 450)], CLEAR), 99),
            (observed_scene(worker.grid, 11, [], CLEAR), 100),
        ]
        worker.runner.resume("batch-one")
        assert worker.coordinator.state()["run"]["settings"]["device_speeds"] == PROFILE
        assert worker.coordinator.state()["run"]["phase"] == "complete"
        cup = worker.gateway.submissions[0]["steps"]
        approach = next(step for step in cup if step["name"].endswith("_approach_cup"))
        assert approach["body"]["speed"] == PROFILE[role]
        for step in cup:
            body = step["body"]
            if "speed" in body:
                assert body["speed"] <= PROFILE[role]
            for point in body.get("points", []):
                assert point["speed"] <= PROFILE[role]
        # Existing pick/flip caps remain effective under a higher overall speed.
        assert approach["body"]["points"][-1]["speed"] == min(PROFILE[role], 100)
        home_queues = worker.gateway.submissions[1:]
        assert len(home_queues) == 2
        assert all(item["steps"][0]["body"]["speed"] == min(PROFILE[role], 100) for item in home_queues)
        assert (worker.session.speed, worker.session.home_speed) == local_before
        assert not worker.runner.device_speeds_path.exists(), "Takeover does not overwrite local preferences"


def test_legacy_transferred_settings_restore_primary_preferences_after_release(rig):
    rig.runner.set_device_speeds(PROFILE)
    legacy = rig.runner.settings()
    del legacy["device_speeds"]
    legacy["motion"]["speed"], legacy["motion"]["home_speed"] = 65, 40
    rig.runner._apply_settings(legacy)
    assert (rig.session.speed, rig.session.home_speed) == (65, 40)
    assert rig.runner.device_speed_settings() == PROFILE
    rig.runner._release()
    assert (rig.session.speed, rig.session.home_speed) == (140, 140)
    assert rig.runner.settings()["device_speeds"] == PROFILE
    assert rig.gateway.calls == []


def test_legacy_worker_without_preferences_restores_its_local_default_after_transfer(rig):
    rig.session.set_speed(90)
    legacy = rig.runner.settings()
    legacy["motion"]["speed"], legacy["motion"]["home_speed"] = 250, 180
    rig.runner._apply_settings(legacy)
    assert (rig.session.speed, rig.session.home_speed) == (250, 180)
    assert rig.runner.device_speed_settings() == dict.fromkeys(PROFILE, 90.0)
    rig.runner._release()
    assert (rig.session.speed, rig.session.home_speed) == (90, 90)
    assert "device_speeds" not in rig.runner.settings()


def test_profile_admission_requires_new_coordinator_capability_before_lease_or_start(rig, monkeypatch):
    rig.runner.set_device_speeds(PROFILE)
    original = rig.coordinator.state
    monkeypatch.setattr(rig.coordinator, "state", lambda: {**original(), "capabilities": []})
    with pytest.raises(SelectionError, match="device-speed update"):
        rig.runner.execute_primary("goto", "preview", [], None)
    assert rig.store.state("primary")["run"] is None
    assert rig.gateway.calls == []
    rig.capture.assert_not_called()


@pytest.mark.parametrize("bad", INVALID)
def test_coordinator_rejects_invalid_admission_profile_without_creating_run(rig, bad):
    with pytest.raises(CoordinatorError):
        rig.coordinator.start("profile-run", {"device_speeds": bad})
    assert rig.coordinator.state()["run"] is None
    assert rig.gateway.calls == []


def test_profile_is_immutable_and_survives_coordinator_restart(rig):
    profile = {"primary": 140, "secondary": 80, "tertiary": 260}
    admitted = rig.coordinator.start("profile-run", {"device_speeds": profile})
    assert DEVICE_SPEED_CAPABILITY in admitted["capabilities"]
    assert admitted["run"]["settings"]["device_speeds"] == PROFILE
    assert all(type(value) is float for value in admitted["run"]["settings"]["device_speeds"].values())
    profile["primary"] = 800
    with pytest.raises(CoordinatorConflict, match="cannot change settings"):
        rig.coordinator.start("profile-run", {"device_speeds": profile})
    restarted = CoordinatorStore(rig.store.path, API, "offline-fixture")
    assert restarted.state("tertiary")["run"]["settings"]["device_speeds"] == PROFILE
    assert rig.gateway.calls == []


def test_corrupt_saved_coordinator_profile_is_rejected_on_load(rig):
    rig.coordinator.start("profile-run", {"device_speeds": PROFILE})
    saved = json.loads(rig.store.path.read_text())
    saved["run"]["settings"]["device_speeds"]["secondary"] = True
    saved["runs"]["profile-run"] = copy.deepcopy(saved["run"])
    rig.store.path.write_text(json.dumps(saved))
    with pytest.raises(CoordinatorError, match="Device speeds"):
        CoordinatorStore(rig.store.path, API, "offline-fixture")
    assert rig.gateway.calls == []


@pytest.mark.parametrize("saved", ["not json", {"primary": 40},
    {"schema_version": True, "device_speeds": PROFILE},
    {"schema_version": 1, "device_speeds": {**PROFILE, "secondary": -1}}])
def test_corrupt_local_preferences_fail_explicitly_without_silent_speed_fallback(rig, tmp_path, saved):
    rig.runner.device_speeds_path.write_text(saved if isinstance(saved, str) else json.dumps(saved))
    before = (rig.session.speed, rig.session.home_speed)
    with pytest.raises(SelectionError, match="Saved device speeds"):
        FailoverRunner(rig.session, rig.coordinator, role="primary", state_dir=tmp_path)
    assert (rig.session.speed, rig.session.home_speed) == before
    assert rig.gateway.calls == []


def test_same_profile_follows_primary_pi_lynx_and_returning_primary_between_cups(rig, tmp_path, monkeypatch):
    rig.runner.set_device_speeds(PROFILE)
    start(rig)
    for index, role in enumerate(["primary", "secondary", "tertiary", "primary"]):
        with successor(rig, tmp_path / f"epoch-{index}", role=role) as worker:
            worker.capture.return_value = (observed_scene(worker.grid, 20 + index,
                [(100, 450 + index * 10)] if index < 3 else [], CLEAR), 99)
            if index < 3:
                def disconnect(_queue, robot=worker.robot):
                    rig.gateway.owner = None
                    robot._lease_error = ControlUnavailable("Disconnected after accepted cup")
                rig.gateway.submit_hook = disconnect
                monkeypatch.setattr(worker.runner._closed, "wait", lambda _delay, worker=worker: worker.runner._closed.set())
            else:
                rig.gateway.submit_hook = None
            worker.runner.resume("batch-one")
            assert worker.capture.call_count == 1
            assert worker.coordinator.state()["run"]["settings"]["device_speeds"] == PROFILE
            assert not worker.runner.device_speeds_path.exists()
    assert len(rig.gateway.submissions) == 5
    for submitted, role in zip(rig.gateway.submissions[:3], ["primary", "secondary", "tertiary"]):
        approach = next(step for step in submitted["steps"] if step["name"].endswith("_approach_cup"))
        assert approach["body"]["speed"] == PROFILE[role]
    assert all(item["steps"][0]["body"]["speed"] == min(PROFILE["primary"], 100) for item in rig.gateway.submissions[3:])
    assert rig.coordinator.state()["run"]["phase"] == "complete"
    assert rig.runner.device_speed_settings() == PROFILE
    assert rig.session.speed == PROFILE["primary"]
    assert not any(method == "DELETE" or path == "/robot/rapid/stop" for method, path, _ in rig.gateway.calls)


@pytest.fixture
def completed_speed_receipts(rig, monkeypatch):
    """Keep real cup and measured Home receipts from one completed fake batch."""
    completed = {}
    write_pending = rig.robot._write_pending

    def record_write(path, record):
        write_pending(path, record)
        if record["state"] == "done" and record["callbacks_applied"]:
            kind = "home" if record["jobs"][0]["endpoint"] == rig.robot.HOME_ENDPOINT else "cup"
            completed[kind] = copy.deepcopy(record)

    monkeypatch.setattr(rig.robot, "_write_pending", record_write)
    rig.capture.side_effect = [
        (observed_scene(rig.grid, 10, [(100, 450)], CLEAR), 99),
        (observed_scene(rig.grid, 11, [], CLEAR), 100),
    ]
    start(rig)
    rig.runner.resume("batch-one")
    assert rig.coordinator.state()["run"]["phase"] == "complete"
    assert set(completed) == {"cup", "home"}
    assert len(completed["cup"]["jobs"]) == 19
    assert completed["home"]["home_verified"] == ["workspace_left_home"]
    assert all(record["callbacks_applied"] and not record["reconciled"] for record in completed.values())
    assert not rig.session.recovery.required() and not rig.session.motion_uncertain
    return completed


def receipt_and_workplace_bytes(rig):
    return {path: path.read_bytes() for path in (
        rig.runner.pending_path, rig.session.recovery.path, rig.grid.path, rig.store.path)}


@pytest.mark.parametrize("kind", ["cup", "home"])
@pytest.mark.parametrize("restart", [False, True])
def test_completed_historical_receipt_allows_speed_preferences_without_reconciling_or_robot_io(
        rig, completed_speed_receipts, tmp_path, kind, restart):
    # A retained cup may be older than the latest gateway queue, for example
    # when another worker finished the batch. Its exact saved proof suffices.
    rig.robot._write_pending(rig.runner.pending_path, completed_speed_receipts[kind])
    runner = rig.runner
    if restart:
        restarted_robot = QueueRobotClient(API, {})
        rig.session.robot = restarted_robot
        runner = FailoverRunner(rig.session, rig.coordinator, role="primary", state_dir=tmp_path, output=Mock())
        assert not restarted_robot._completed_in_process
    before = receipt_and_workplace_bytes(rig)
    gateway_calls, queue = copy.deepcopy(rig.gateway.calls), copy.deepcopy(rig.gateway.queue)
    captures = rig.capture.call_count
    try:
        result = runner.set_device_speeds(PROFILE)
        assert result["device_speeds"] == PROFILE
        assert runner.device_speed_settings() == PROFILE
        assert rig.session.speed == PROFILE["primary"]
        assert json.loads(runner.device_speeds_path.read_text())["device_speeds"] == PROFILE
        assert receipt_and_workplace_bytes(rig) == before
        assert rig.gateway.calls == gateway_calls and rig.gateway.queue == queue
        assert rig.capture.call_count == captures
        assert rig.robot._read_pending(rig.runner.pending_path)["reconciled"] is False
    finally:
        if restart:
            runner.close()
            restarted_robot.end_standby()


@pytest.mark.parametrize("change", [
    "callbacks_not_applied", "partial_callbacks", "missing_home_proof", "queue_missing", "queue_list",
    "queue_owner", "queue_step_name", "queue_group", "queue_started_at", "queue_state",
    "queue_length", "queue_step_running", "queue_step_message",
    "local_prepared", "local_pending", "local_unknown", "local_failed", "local_cancelled",
])
def test_unfinished_or_invalid_completed_receipt_still_blocks_speed_preferences_without_mutation(
        rig, completed_speed_receipts, change):
    record = copy.deepcopy(completed_speed_receipts["home"])
    if change == "callbacks_not_applied":
        record["callbacks_applied"] = False
    elif change == "partial_callbacks":
        record.update(callbacks_applied=False, callback_completed=[])
    elif change == "missing_home_proof":
        record["home_verified"] = []
    elif change == "queue_missing":
        record["queue"] = None
    elif change == "queue_list":
        record["queue"] = []
    elif change == "queue_owner":
        record["queue"]["owner"] = "secondary"
    elif change == "queue_step_name":
        record["queue"]["steps"][0]["name"] = "another_home"
    elif change == "queue_group":
        record["queue"]["steps"][0]["group"] = "another_group"
    elif change == "queue_started_at":
        record["queue"]["started_at"] = "2020-01-01T00:00:00+00:00"
    elif change == "queue_state":
        record["queue"]["state"] = "failed"
    elif change == "queue_length":
        record["queue"]["length"] = 2
    elif change == "queue_step_running":
        record["queue"]["steps"][0]["state"] = "running"
    elif change == "queue_step_message":
        record["queue"]["steps"][0]["message"] = "STARTED"
    elif change.startswith("local_"):
        record.update(state=change.removeprefix("local_"), callbacks_applied=False)
    rig.robot._write_pending(rig.runner.pending_path, record)
    before = receipt_and_workplace_bytes(rig)
    settings = rig.runner.settings()
    gateway_calls, queue = copy.deepcopy(rig.gateway.calls), copy.deepcopy(rig.gateway.queue)
    captures = rig.capture.call_count
    with pytest.raises((SelectionError, RobotCommandError)):
        rig.runner.set_device_speeds(PROFILE)
    assert rig.runner.settings() == settings
    assert not rig.runner.device_speeds_path.exists()
    assert receipt_and_workplace_bytes(rig) == before
    assert rig.gateway.calls == gateway_calls and rig.gateway.queue == queue
    assert rig.capture.call_count == captures
