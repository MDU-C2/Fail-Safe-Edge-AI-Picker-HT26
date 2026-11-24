"""Finite shared batches across the real session, queue adapter and receipt store.

Every HTTP request is intercepted by the existing in-memory gateway fixture.
"""

import copy
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from cup_commands import CupSession
from cup_inventory import SelectionError
from failover_coordinator import CoordinatorConflict, CoordinatorStore, CoordinatorUnavailable
from failover_workflow import FailoverRunner, transient
from handover_plan import right_via_pose
from queued_robot_client import ControlUnavailable, QueueNotSubmitted, QueueRobotClient, plan_to_queue_steps
from recovery_journal import RecoveryJournal
from robot_client import RobotCommandError
from test_between_cup_grid import CLEAR, observed_scene
from test_queued_robot_client import API, gateway, home, move
from test_support_grid import grid_fixture


class LocalCoordinator:
    """Exercise the complete store contract without a local HTTP listener."""

    def __init__(self, store, role):
        self.store, self.role = store, role

    def __getattr__(self, name):
        return lambda *args, **kwargs: getattr(self.store, name)(self.role, *args, **kwargs)


@pytest.fixture
def rig(tmp_path, gateway, monkeypatch):
    gateway.client_id, gateway.priority = "primary", 300
    monkeypatch.setattr(requests, "request", Mock(side_effect=AssertionError("Real HTTP forbidden")))
    store = CoordinatorStore(tmp_path / "coordinator.json", API, "offline-fixture")
    monkeypatch.setattr(store, "_get", lambda path: gateway.get(API + path).json())
    coordinator = LocalCoordinator(store, "primary")
    robot = QueueRobotClient(API, {})
    robot.QUEUE_POLL = 0.001
    grid = grid_fixture(tmp_path)
    capture = Mock(return_value=(observed_scene(grid, 10, [], CLEAR), 99))
    session = CupSession(capture, Mock(), robot, "cal-1", recovery=RecoveryJournal(tmp_path / "recovery.json"),
                         placing_grid=grid, between_cup_check=True, between_cup_grid_check=True,
                         continuous_batch=False, right_park_pose=right_via_pose())
    runner = FailoverRunner(session, coordinator, role="primary", state_dir=tmp_path, output=Mock())
    result = SimpleNamespace(session=session, robot=robot, runner=runner, coordinator=coordinator,
                             store=store, gateway=gateway, capture=capture, grid=grid)
    yield result
    runner.close()
    robot.end_standby()


def start(rig, *, phase="active"):
    rig.coordinator.start("batch-one", rig.runner.settings())
    if phase == "finalizing":
        rig.gateway.owner = {"active": True, "client_id": "primary", "priority": 300}
        rig.coordinator.set_phase("batch-one", phase)
        rig.gateway.owner = None


def test_armed_worker_never_acquires_scans_or_moves_without_explicit_start(rig, monkeypatch):
    monkeypatch.setattr(rig.runner._closed, "wait", lambda _delay: rig.runner._closed.set())
    rig.runner.serve()
    assert rig.coordinator.state()["run"] is None
    assert rig.gateway.calls == []
    rig.capture.assert_not_called()


def test_one_cup_finishes_full_queue_then_measured_home_and_never_scans_again(rig):
    rig.capture.side_effect = [
        (observed_scene(rig.grid, 10, [(100, 450)], CLEAR), 99),
        (observed_scene(rig.grid, 11, [(100, 450)], CLEAR), 100),
        (observed_scene(rig.grid, 12, [], CLEAR), 101),
    ]
    rig.session.scan()
    jobs = rig.runner.execute_primary("goto", "frame-10", [], None)
    state = rig.coordinator.state()
    assert state["run"]["phase"] == "complete"
    assert state["run"]["confirmed_homes"] == ["right", "left"]
    assert len(rig.gateway.submissions) == 3
    assert [len(item["steps"]) for item in rig.gateway.submissions[1:]] == [1, 1]
    assert any(job["name"] == "right_park" for job in jobs)
    assert rig.capture.call_count == 3
    assert not rig.session.recovery.required() and not rig.session.motion_uncertain
    assert rig.session.batch_progress["arms_home"]
    assert not any(method == "DELETE" or path == "/robot/rapid/stop" for method, path, _ in rig.gateway.calls)


@pytest.mark.parametrize("demoted", [False, True], ids=["confirmed", "previously-demoted"])
@pytest.mark.parametrize("restart", [False, True], ids=["same-worker", "restarted-worker"])
@pytest.mark.parametrize("legacy_client", [False, True], ids=["fixed-client", "running-older-client"])
def test_next_batch_reconciles_historical_non_submission_without_replaying_it(rig, tmp_path, monkeypatch,
                                                                           demoted, restart, legacy_client):
    start(rig)
    rig.robot.try_begin_standby(expected_client_id="primary", expected_priority=300)
    rig.robot.bind_batch(rig.coordinator, "batch-one", rig.runner.pending_path)
    with pytest.raises(QueueNotSubmitted):
        rig.robot.execute_jobs([move("historical_unsent_cup")],
                               before_send=Mock(side_effect=SelectionError("Preview changed before dispatch")))
    record = rig.robot.pending_completion()
    assert record["state"] == "not_submitted" and record["rejected_at_coordinator"]
    assert not record["dispatch_started"] and not rig.gateway.submissions
    rig.robot.acknowledge_pending(record["submission_id"])
    rig.coordinator.block("batch-one", "Preview changed before dispatch")
    rig.runner._release()

    if demoted:
        # Older clients reset the durable confirmation when they tried to
        # reject this retired receipt again after admitting another batch.
        rig.coordinator.start("intervening-batch", rig.runner.settings())
        rig.coordinator.block("intervening-batch", "Historical receipt was rejected again")
        record = rig.robot._read_pending(rig.runner.pending_path)
        record["rejected_at_coordinator"] = False
        rig.robot._write_pending(rig.runner.pending_path, record)

    runner, robot, session, coordinator = rig.runner, rig.robot, rig.session, rig.coordinator
    if restart:
        runner.close()
        store = CoordinatorStore(rig.store.path, API, "offline-fixture")
        monkeypatch.setattr(store, "_get", lambda path: rig.gateway.get(API + path).json())
        coordinator = LocalCoordinator(store, "primary")
        robot = QueueRobotClient(API, {})
        robot.QUEUE_POLL = 0.001
        session = CupSession(rig.capture, Mock(), robot, "cal-1",
                             recovery=RecoveryJournal(tmp_path / "recovery.json"), placing_grid=rig.grid,
                             between_cup_check=True, between_cup_grid_check=True, continuous_batch=False,
                             right_park_pose=right_via_pose())
        runner = FailoverRunner(session, coordinator, role="primary", state_dir=tmp_path, output=Mock())

    historical_receipt = coordinator.receipt("batch-one", record["submission_id"])
    reject = Mock(wraps=coordinator.reject_queue)
    monkeypatch.setattr(coordinator, "reject_queue", reject)
    if legacy_client:
        fixed_pending_completion = robot.pending_completion
        def older_pending_completion(*, wait=False):
            saved = robot._read_pending(robot._batch_binding[2])
            if saved is not None and (not saved["dispatch_started"] or saved["state"] == "not_submitted"):
                robot._reject_prepared(saved)
                return copy.deepcopy(saved)
            return fixed_pending_completion(wait=wait)
        monkeypatch.setattr(robot, "pending_completion", older_pending_completion)
    rig.gateway.calls.clear()
    rig.capture.side_effect = [
        (observed_scene(rig.grid, 10, [(100, 450)], CLEAR), 99),
        (observed_scene(rig.grid, 11, [(100, 450)], CLEAR), 100),
        (observed_scene(rig.grid, 12, [], CLEAR), 101),
    ]
    try:
        session.scan()
        jobs = runner.execute_primary("goto", "frame-10", [], None)
        state = coordinator.state()
        assert state["run"]["id"] not in {"batch-one", "intervening-batch"}
        assert state["run"]["phase"] == "complete"
        assert state["run"]["confirmed_homes"] == ["right", "left"]
        assert len(rig.gateway.submissions) == 3  # One new cup, then measured right and left Home.
        assert any(job["name"] == "right_park" for job in jobs)
        assert not any("historical_unsent_cup" in step["name"] for submission in rig.gateway.submissions
                       for step in submission["steps"])
        assert rig.capture.call_count == 3
        assert not session.motion_uncertain and not session.recovery.required()
        assert coordinator.receipt("batch-one", record["submission_id"]) == historical_receipt
        if legacy_client:
            reject.assert_called_once_with("batch-one", record["submission_id"])
        else:
            reject.assert_not_called()
        assert not any(method == "DELETE" or path == "/robot/rapid/stop"
                       for method, path, _ in rig.gateway.calls)
    finally:
        if restart:
            runner.close()
            robot.end_standby()


def test_initial_empty_workspace_homes_once_and_completes_finite_batch(rig):
    start(rig)
    rig.runner.resume("batch-one")
    assert rig.capture.call_count == 1
    assert len(rig.gateway.submissions) == 2
    assert rig.coordinator.state()["run"]["phase"] == "complete"
    assert rig.session.batch_progress["completion_reason"] == "workspace_empty"


def test_primary_refreshes_preview_if_standby_accepts_a_cup_between_start_and_acquire(rig, monkeypatch):
    rig.capture.side_effect = [(observed_scene(rig.grid, 10, [(100, 450)], CLEAR), 99),
                               (observed_scene(rig.grid, 11, [], CLEAR), 100)]
    rig.session.scan()
    admit = rig.coordinator.start
    def standby_wins_admission_gap(run_id, settings):
        result = admit(run_id, settings)
        gateway = rig.gateway
        gateway.client_id, gateway.priority = "secondary", 200
        gateway.owner = {"active": True, "client_id": "secondary", "priority": 200}
        identity = "b" * 16
        steps = plan_to_queue_steps([move("inherited_cup")], name_prefix="q" + identity + "_")
        rig.store.prepare_queue("secondary", run_id, identity, steps)
        gateway.post(API + "/robot/rws/queue", json={"steps": steps})
        gateway.client_id, gateway.priority = "primary", 300
        return result
    monkeypatch.setattr(rig.coordinator, "start", standby_wins_admission_gap)
    jobs = rig.runner.execute_primary("goto", "frame-10", [], None)
    assert rig.capture.call_count == 2
    assert len(rig.gateway.submissions) == 3  # Inherited cup and two Home barriers.
    assert all(job["endpoint"] == rig.robot.HOME_ENDPOINT for job in jobs)
    assert rig.coordinator.state()["run"]["phase"] == "complete"


def test_stop_during_admission_outage_never_retries_start(rig, monkeypatch):
    attempts = []
    def unavailable(run_id, _settings):
        attempts.append(run_id)
        assert rig.runner.run_id == run_id
        rig.session.latch_stop()
        raise CoordinatorUnavailable("Offline")
    monkeypatch.setattr(rig.coordinator, "start", unavailable)
    with pytest.raises(SelectionError, match="Software stop requested"):
        rig.runner.execute_primary("goto", "preview", [], None)
    assert len(attempts) == 1
    assert rig.coordinator.state()["run"] is None
    assert rig.gateway.calls == []


@pytest.mark.parametrize("lost_reply", [False, True])
def test_stop_racing_accepted_admission_stops_that_exact_run_without_robot_work(rig, monkeypatch, lost_reply):
    admit = rig.coordinator.start
    def stop_after_commit(run_id, settings):
        result = admit(run_id, settings)
        rig.session.latch_stop()
        if lost_reply:
            raise CoordinatorUnavailable("Reply lost")
        return result
    monkeypatch.setattr(rig.coordinator, "start", stop_after_commit)
    with pytest.raises(SelectionError, match="Software stop requested"):
        rig.runner.execute_primary("goto", "preview", [], None)
    assert rig.coordinator.state()["run"]["phase"] == "stopped"
    assert rig.coordinator.state()["run"]["id"] == rig.runner.run_id
    assert rig.gateway.calls == []
    rig.capture.assert_not_called()


def test_closed_primary_does_not_admit_a_batch(rig):
    rig.runner.close()
    assert rig.runner.execute_primary("goto", "preview", [], None) == []
    assert rig.coordinator.state()["run"] is None
    assert rig.gateway.calls == []


def test_takeover_after_confirmed_right_home_never_repeats_it(rig, monkeypatch):
    start(rig, phase="finalizing")
    def lose_after_right(queue):
        if queue["steps"][0]["arm"] == "right":
            rig.gateway.owner = {"active": True, "client_id": "secondary", "priority": 200}
            rig.robot._lease_error = ControlUnavailable("Control changed")
    rig.gateway.submit_hook = lose_after_right
    monkeypatch.setattr(rig.runner._closed, "wait", lambda _delay: setattr(rig.gateway, "owner", None))
    rig.runner.resume("batch-one")
    assert [item["steps"][0]["body"]["arm"] for item in rig.gateway.submissions] == ["right", "left"]
    rig.capture.assert_not_called()
    assert rig.coordinator.state()["run"]["confirmed_homes"] == ["right", "left"]
    assert not rig.session.recovery.required()


def test_home_receipt_recovers_after_confirmation_transport_loss_without_replay(rig, monkeypatch):
    start(rig, phase="finalizing")
    confirm = rig.coordinator.confirm_home
    calls = []
    def confirmation(*args):
        calls.append(args)
        if len(calls) == 1:
            raise CoordinatorUnavailable("Offline")
        return confirm(*args)
    monkeypatch.setattr(rig.coordinator, "confirm_home", confirmation)
    monkeypatch.setattr(rig.runner._closed, "wait", lambda _delay: None)
    rig.runner.resume("batch-one")
    assert [item["steps"][0]["body"]["arm"] for item in rig.gateway.submissions] == ["right", "left"]
    assert calls[0] == calls[1]
    assert not rig.session.recovery.required()


def test_successor_measures_and_adopts_exact_unconfirmed_home_before_left(rig):
    start(rig, phase="finalizing")
    rig.gateway.owner = {"active": True, "client_id": "primary", "priority": 300}
    job = home("right")
    job["name"] = "workspace_right_home"
    identity = "a" * 16
    steps = plan_to_queue_steps([job], name_prefix="q" + identity + "_")
    rig.coordinator.prepare_queue("batch-one", identity, steps)
    rig.gateway.post(API + "/robot/rws/queue", json={"steps": steps})
    rig.gateway.owner = None
    rig.gateway.client_id, rig.gateway.priority = "tertiary", 100
    rig.coordinator.role = rig.runner.role = "tertiary"
    rig.runner.resume("batch-one")
    assert [item["steps"][0]["body"]["arm"] for item in rig.gateway.submissions] == ["right", "left"]
    assert rig.coordinator.state()["run"]["home_receipts"]["right"] == identity
    rig.capture.assert_not_called()


def test_successor_home_pose_mismatch_prevents_left_submission(rig):
    start(rig, phase="finalizing")
    rig.gateway.owner = {"active": True, "client_id": "primary", "priority": 300}
    job = home("right")
    job["name"] = "workspace_right_home"
    identity = "a" * 16
    steps = plan_to_queue_steps([job], name_prefix="q" + identity + "_")
    rig.coordinator.prepare_queue("batch-one", identity, steps)
    rig.gateway.post(API + "/robot/rws/queue", json={"steps": steps})
    rig.gateway.owner = None
    rig.gateway.client_id, rig.gateway.priority = "tertiary", 100
    rig.coordinator.role = rig.runner.role = "tertiary"
    rig.gateway.joints["right"]["joints"][0] += 20
    with pytest.raises(RobotCommandError, match="home pose was not confirmed"):
        rig.runner.resume("batch-one")
    assert len(rig.gateway.submissions) == 1
    assert rig.coordinator.state()["run"]["phase"] == "blocked"


def test_finalizing_all_confirmed_does_not_repeat_home_after_finish_preemption(rig, monkeypatch):
    start(rig, phase="finalizing")
    finish = rig.coordinator.finish
    calls = []
    def preempt(*args):
        calls.append(args)
        if len(calls) == 1:
            rig.gateway.owner = {"active": True, "client_id": "secondary", "priority": 200}
            raise CoordinatorConflict("Lease changed")
        return finish(*args)
    monkeypatch.setattr(rig.coordinator, "finish", preempt)
    monkeypatch.setattr(rig.runner._closed, "wait", lambda _delay: setattr(rig.gateway, "owner", None))
    rig.runner.resume("batch-one")
    assert len(rig.gateway.submissions) == 2
    assert len(calls) == 2
    assert rig.coordinator.state()["run"]["phase"] == "complete"
    rig.capture.assert_not_called()


def test_initial_journal_never_inherits_previous_cup_submission(rig, monkeypatch):
    rig.runner.executing, rig.runner.run_id = True, "batch-one"
    read = Mock(side_effect=AssertionError("No previous queue should be read"))
    monkeypatch.setattr(rig.robot, "_read_pending", read)
    context = {"execution": {"completed_steps": [], "pending_jobs": {}}, "stop_requested": False}
    assert rig.runner.stamp_context(context)["failover"] == {
        "run_id": "batch-one", "submission_id": None, "not_dispatched": True}


@pytest.mark.parametrize("other_owner,expected", [("secondary", True), (None, True), ("primary", False)])
def test_coordinator_conflict_yields_only_after_independent_ownership_check(rig, other_owner, expected):
    start(rig)
    rig.runner.run_id = "batch-one"
    rig.gateway.owner = {"active": True, "client_id": other_owner} if other_owner else None
    assert rig.runner._yieldable(CoordinatorConflict("Refused")) is expected


def test_transient_wrapped_before_submit_failure_remains_yieldable():
    cause = CoordinatorUnavailable("Offline")
    error = QueueNotSubmitted("No robot command sent")
    error.__cause__ = cause
    assert transient(error)


def test_completed_run_reconciles_own_exact_cup_receipt_without_acquiring_or_capturing(rig):
    start(rig)
    rig.robot.try_begin_standby(expected_client_id="primary", expected_priority=300)
    rig.robot.bind_batch(rig.coordinator, "batch-one", rig.runner.pending_path)
    job = move()
    rig.robot.execute_jobs([job])
    record = rig.robot._read_pending(rig.runner.pending_path)
    rig.session.recovery.begin({"failover": {"run_id": "batch-one", "submission_id": record["submission_id"]},
                                "execution": {"completed_steps": [job], "pending_jobs": {}}, "stop_requested": False})
    rig.session.motion_uncertain = True
    rig.coordinator.finish("batch-one", "no_pickable_cups")
    rig.runner._release()
    rig.gateway.calls.clear()
    rig.runner.resume("batch-one")
    assert rig.gateway.calls == []
    assert not rig.session.recovery.required() and not rig.session.motion_uncertain
    rig.capture.assert_not_called()


@pytest.mark.parametrize("mismatch", ["run", "submission", "stop"])
def test_inactive_receipt_cannot_clear_unrelated_or_stopped_recovery(rig, mismatch):
    start(rig)
    rig.robot.try_begin_standby(expected_client_id="primary", expected_priority=300)
    rig.robot.bind_batch(rig.coordinator, "batch-one", rig.runner.pending_path)
    job = move()
    rig.robot.execute_jobs([job])
    record = rig.robot._read_pending(rig.runner.pending_path)
    context = {"failover": {"run_id": "batch-one", "submission_id": record["submission_id"]},
               "execution": {"completed_steps": [job], "pending_jobs": {}}, "stop_requested": False}
    if mismatch == "stop":
        context["stop_requested"] = True
    else:
        context["failover"]["run_id" if mismatch == "run" else "submission_id"] = "unrelated"
    rig.session.recovery.begin(context)
    rig.session.motion_uncertain = True
    rig.coordinator.finish("batch-one", "no_pickable_cups")
    rig.runner._release()
    rig.gateway.calls.clear()
    with pytest.raises(SelectionError, match="does not belong"):
        rig.runner.resume("batch-one")
    assert rig.session.recovery.required()
    assert rig.gateway.calls == []


def inactive_receipt(rig, job=None):
    """Retain a completed queue and its interrupted local journal."""
    start(rig)
    rig.robot.try_begin_standby(expected_client_id="primary", expected_priority=300)
    rig.robot.bind_batch(rig.coordinator, "batch-one", rig.runner.pending_path)
    job = job or move()
    rig.robot.execute_jobs([job])
    record = rig.robot._read_pending(rig.runner.pending_path)
    context = {"failover": {"run_id": "batch-one", "submission_id": record["submission_id"]},
               "execution": {"completed_steps": [job], "pending_jobs": {}}, "stop_requested": False}
    rig.session.recovery.begin(context)
    rig.session.motion_uncertain = True
    rig.coordinator.finish("batch-one", "no_pickable_cups")
    rig.runner._release()
    rig.gateway.calls.clear()
    return record, context


def monitor_polls(rig, monkeypatch, callback, count=4):
    polls = []
    def tick(_delay):
        polls.append(None)
        if len(polls) >= count:
            rig.runner._closed.set()
    monkeypatch.setattr(rig.runner._closed, "wait", tick)
    rig.runner.start_monitor(callback)
    rig.runner._monitor.join(timeout=3)
    assert not rig.runner._monitor.is_alive()
    assert len(polls) == count


@pytest.mark.parametrize("mismatch", ["operator", "run", "submission", "stop", "session_stop"])
def test_monitor_leaves_unrelated_recovery_idle_without_repeated_ui_errors(rig, monkeypatch, mismatch):
    record, context = inactive_receipt(rig)
    if mismatch == "operator":
        context = {"action": "operator_control", "control": "home", "arm": "both"}
    elif mismatch == "stop":
        context["stop_requested"] = True
    elif mismatch == "session_stop":
        rig.session.latch_stop()
    else:
        context["failover"]["run_id" if mismatch == "run" else "submission_id"] = "unrelated"
    rig.session.recovery.begin(context)
    before = rig.session.recovery.path.read_bytes(), rig.runner.pending_path.read_bytes()
    callback = Mock()
    monitor_polls(rig, monkeypatch, callback)
    callback.assert_not_called()
    assert before == (rig.session.recovery.path.read_bytes(), rig.runner.pending_path.read_bytes())
    assert rig.session.motion_uncertain and rig.session.recovery.required()
    rig.capture.assert_not_called()
    assert rig.gateway.calls == []


@pytest.mark.parametrize("unused", [False, True])
def test_monitor_still_reconciles_own_completed_receipt_once_without_motion(rig, monkeypatch, unused):
    inactive_receipt(rig)
    if unused:
        rig.session.recovery.begin({"failover": {"run_id": "batch-one", "submission_id": None,
                                                "not_dispatched": True}})
    callback = Mock(side_effect=rig.runner.resume)
    monitor_polls(rig, monkeypatch, callback)
    callback.assert_called_once_with("batch-one")
    assert not rig.session.recovery.required() and not rig.session.motion_uncertain
    assert rig.robot._read_pending(rig.runner.pending_path)["reconciled"]
    assert rig.gateway.calls == []
    rig.capture.assert_not_called()


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_monitor_does_not_retry_unsuccessful_inactive_queue(rig, monkeypatch, state):
    record, _ = inactive_receipt(rig)
    record.update(state=state, callbacks_applied=False, reconciled=False)
    record["queue"]["state"] = state
    rig.robot._write_pending(rig.runner.pending_path, record)
    callback = Mock()
    monitor_polls(rig, monkeypatch, callback)
    callback.assert_not_called()
    assert rig.session.recovery.required() and rig.session.motion_uncertain
    assert rig.gateway.calls == []


@pytest.mark.parametrize("state,completed,pending", [
    ("done", [], {}),
    ("done", [move("another_job")], {}),
    ("not_submitted", [move()], {}),
    ("not_submitted", [], {"left": move("another_job")}),
])
def test_monitor_does_not_repeat_same_identity_with_contradictory_jobs(rig, monkeypatch, state, completed, pending):
    record, context = inactive_receipt(rig)
    record.update(state=state, callbacks_applied=False, reconciled=False)
    rig.robot._write_pending(rig.runner.pending_path, record)
    context["execution"] = {"completed_steps": completed, "pending_jobs": pending}
    rig.session.recovery.begin(context)
    before = rig.session.recovery.path.read_bytes(), rig.runner.pending_path.read_bytes()
    callback = Mock()
    monitor_polls(rig, monkeypatch, callback)
    callback.assert_not_called()
    assert before == (rig.session.recovery.path.read_bytes(), rig.runner.pending_path.read_bytes())
    assert rig.session.recovery.required()
    assert rig.gateway.calls == []


def test_monitor_waits_for_inactive_receipt_without_taking_ui_worker(rig, monkeypatch):
    record, _ = inactive_receipt(rig)
    record.update(state="pending", callbacks_applied=False, reconciled=False)
    rig.robot._write_pending(rig.runner.pending_path, record)
    receipt = Mock(side_effect=[{"state": "pending"}, {"state": "unknown"},
                               {"state": "done", "queue": record["queue"]}])
    monkeypatch.setattr(rig.coordinator, "receipt", receipt)
    callback = Mock()
    monitor_polls(rig, monkeypatch, callback, count=3)
    callback.assert_called_once_with("batch-one")
    assert receipt.call_count == 3
    assert rig.session.recovery.required()
    assert rig.gateway.calls == []


def test_monitor_retries_scheduling_when_ui_was_temporarily_busy(rig, monkeypatch):
    inactive_receipt(rig)
    calls = []
    def resume(run_id):
        calls.append(run_id)
        if len(calls) == 1:
            return False
        rig.runner.resume(run_id)
        return True
    monitor_polls(rig, monkeypatch, resume)
    assert calls == ["batch-one", "batch-one"]
    assert not rig.session.recovery.required()
    assert rig.gateway.calls == []


def test_monitor_does_not_schedule_inactive_unmeasured_home(rig, monkeypatch):
    record, _ = inactive_receipt(rig, home("right"))
    record.update(home_verified=[], callbacks_applied=False, reconciled=False)
    rig.robot._write_pending(rig.runner.pending_path, record)
    callback = Mock()
    monitor_polls(rig, monkeypatch, callback)
    callback.assert_not_called()
    assert rig.session.recovery.required()
    assert rig.gateway.calls == []


@pytest.mark.parametrize("local_state", ["done", "pending", "unknown"])
def test_monitor_adopts_exact_shared_home_measurement_after_session_ended(rig, monkeypatch, local_state):
    start(rig, phase="finalizing")
    rig.robot.try_begin_standby(expected_client_id="primary", expected_priority=300)
    rig.robot.bind_batch(rig.coordinator, "batch-one", rig.runner.pending_path)
    job = home("right")
    job["name"] = "workspace_right_home"
    rig.robot.execute_jobs([job])
    record = rig.robot._read_pending(rig.runner.pending_path)
    rig.coordinator.confirm_home("batch-one", record["submission_id"], "right")
    rig.coordinator.stop("batch-one", "operator_stop")
    rig.runner._release()
    # Simulate the original worker losing connection before it saved the
    # successor's measured result. The shared session has since ended.
    record.update(state=local_state, home_verified=[], callbacks_applied=False, reconciled=False)
    if local_state != "done":
        record.update(queue=None, started_at=None)
    rig.robot._write_pending(rig.runner.pending_path, record)
    rig.session.recovery.begin({
        "failover": {"run_id": "batch-one", "submission_id": record["submission_id"]},
        "execution": {"completed_steps": [], "pending_job": job}, "stop_requested": False,
    })
    rig.session.motion_uncertain = True
    rig.gateway.calls.clear()
    callback = Mock(side_effect=rig.runner.resume)
    monitor_polls(rig, monkeypatch, callback)
    callback.assert_called_once_with("batch-one")
    final = rig.robot._read_pending(rig.runner.pending_path)
    assert final["state"] == "done" and final["home_verified"] == ["workspace_right_home"]
    assert final["reconciled"] and final["callbacks_applied"] is False
    assert not rig.session.recovery.required() and not rig.session.motion_uncertain
    assert rig.coordinator.state()["run"]["phase"] == "stopped"
    assert rig.gateway.calls == [], "Historical proof must not measure current joints or replay movement"
    rig.capture.assert_not_called()


@pytest.mark.parametrize("change", ["owner", "step", "started_at", "state", "measurement", "dispatch",
                                    "rejected", "saved_queue", "malformed_queue"])
def test_shared_home_proof_rejects_mismatched_or_unconfirmed_evidence(rig, change):
    record, _ = inactive_receipt(rig, home("right"))
    receipt = {"state": "done", "queue": copy.deepcopy(record["queue"]), "home_verified": True}
    assert rig.runner._shared_home_verified(record, receipt)
    if change == "owner": receipt["queue"]["owner"] = "other-device"
    if change == "step": receipt["queue"]["steps"][0]["group"] = "g99"
    if change == "started_at": record["started_at"] = "2026-11-01T00:00:00Z"
    if change == "state": receipt["state"] = "failed"
    if change == "measurement": receipt["home_verified"] = 1
    if change == "dispatch": record["dispatch_started"] = False
    if change == "rejected": record["rejected_at_coordinator"] = True
    if change == "saved_queue": record["queue"]["message"] = "different evidence"
    if change == "malformed_queue": receipt["queue"] = None
    assert not rig.runner._shared_home_verified(record, receipt)
    assert rig.gateway.calls == []


def test_monitor_continues_to_schedule_active_batch(rig, monkeypatch):
    start(rig)
    callback = Mock()
    monitor_polls(rig, monkeypatch, callback, count=2)
    assert callback.call_count == 2
    callback.assert_called_with("batch-one")
    rig.capture.assert_not_called()
    assert rig.gateway.calls == []


def external_failed_queue(rig):
    """An unrelated old terminal picker replaced the last shared queue."""
    inactive_receipt(rig)
    rig.gateway.queue = copy.deepcopy(rig.gateway.queue)
    rig.gateway.queue.update(state="failed", owner="secondary", message="Old right_via failure")
    rig.gateway.queue["steps"][0].update(name="right_via", arm="right", state="failed", message="ERR move failed")
    rig.gateway.arm_status["right"] = {"accepted": False, "message": "ERR move failed"}
    rig.session.recovery.begin({"action": "operator_control", "control": "home", "arm": "both"})


def test_inspected_external_queue_recovery_preserves_successful_shared_history(rig):
    external_failed_queue(rig)
    head = rig.coordinator.state()["head"]
    pending = rig.runner.pending_path.read_bytes()
    result = rig.session.acknowledge_recovery()
    assert isinstance(result, dict) and "Scan again" in result["message"]
    assert rig.gateway.queue["state"] == "idle"
    assert rig.coordinator.state()["head"] == head
    assert rig.runner.pending_path.read_bytes() == pending
    assert not rig.session.recovery.required() and not rig.session.motion_uncertain
    assert not rig.session.scan_usable
    assert len(list((rig.runner.pending_path.parent / "recovery-history").glob("*.json"))) == 1
    assert len([1 for method, path, _ in rig.gateway.calls if method == "DELETE"]) == 1
    assert not rig.gateway.submissions
    assert not any(path == "/robot/rapid/stop" for _, path, _ in rig.gateway.calls)
    rig.capture.assert_not_called()


def test_first_batch_after_recovery_owns_control_before_shared_admission(rig, monkeypatch):
    external_failed_queue(rig)
    rig.session.acknowledge_recovery()
    rig.capture.return_value = (observed_scene(rig.grid, 30, [(100, 450)], CLEAR), 99)
    admit = rig.coordinator.start
    def inspect_admission(run_id, settings):
        assert rig.gateway.owner == {"active": True, "client_id": "primary", "priority": 300}
        assert rig.robot.has_acknowledged_idle_errors
        return admit(run_id, settings)
    monkeypatch.setattr(rig.coordinator, "start", inspect_admission)
    def finish_new_queue(queue):
        rig.gateway.arm_status["right"] = {"accepted": True, "message": "DONE"}
        rig.capture.return_value = (observed_scene(rig.grid, 31, [], CLEAR), 100)
    rig.gateway.submit_hook = finish_new_queue
    rig.runner.execute_primary("goto", "fresh-preview", [], None)
    assert rig.coordinator.state()["run"]["phase"] == "complete"
    assert not rig.robot.has_acknowledged_idle_errors
    assert not rig.session.motion_uncertain
    assert rig.gateway.owner is None


def test_recovery_admission_failure_releases_its_early_lease(rig, monkeypatch):
    external_failed_queue(rig)
    rig.session.acknowledge_recovery()
    monkeypatch.setattr(rig.coordinator, "start", Mock(side_effect=CoordinatorConflict("Admission refused")))
    with pytest.raises(CoordinatorConflict, match="Admission refused"):
        rig.runner.execute_primary("goto", "fresh-preview", [], None)
    assert rig.gateway.owner is None
    assert not rig.gateway.submissions
    assert rig.robot.has_acknowledged_idle_errors


@pytest.mark.parametrize("blocker", ["active", "head", "pending", "owner", "offline", "running"])
def test_explicit_recovery_preserves_local_block_for_unresolved_or_competing_work(rig, monkeypatch, blocker):
    external_failed_queue(rig)
    before = rig.session.recovery.path.read_bytes()
    if blocker in {"active", "head"}:
        state = rig.coordinator.state()
        if blocker == "active":
            state["run"]["phase"] = "active"
        else:
            state["head"]["state"] = "unknown"
        monkeypatch.setattr(rig.coordinator, "state", Mock(return_value=state))
    elif blocker == "pending":
        record = rig.robot._read_pending(rig.runner.pending_path)
        record.update(state="unknown", reconciled=False, callbacks_applied=False)
        rig.robot._write_pending(rig.runner.pending_path, record)
    elif blocker == "owner":
        rig.gateway.owner = {"active": True, "client_id": "secondary", "priority": 200}
    elif blocker == "offline":
        monkeypatch.setattr(rig.coordinator, "state", Mock(side_effect=CoordinatorUnavailable("Offline")))
    elif blocker == "running":
        rig.gateway.queue["state"] = "running"
    with pytest.raises((SelectionError, CoordinatorUnavailable)):
        rig.session.acknowledge_recovery()
    assert rig.session.recovery.path.read_bytes() == before
    assert rig.session.motion_uncertain and not rig.session.scan_usable
    assert not any(method in {"POST", "DELETE"} for method, _, _ in rig.gateway.calls)
    rig.capture.assert_not_called()


def test_recovery_rechecks_shared_admission_after_acquiring_control(rig, monkeypatch):
    external_failed_queue(rig)
    state = rig.coordinator.state()
    changed = copy.deepcopy(state)
    changed["run"]["id"] = "a-new-retired-run"
    monkeypatch.setattr(rig.coordinator, "state", Mock(side_effect=[state, changed]))
    before = rig.session.recovery.path.read_bytes()
    with pytest.raises(SelectionError, match="shared batch changed"):
        rig.session.acknowledge_recovery()
    assert rig.session.recovery.path.read_bytes() == before
    assert not any(method == "DELETE" for method, _, _ in rig.gateway.calls)
    assert rig.gateway.owner is None


def test_stop_arriving_during_queue_recovery_keeps_local_block(rig, monkeypatch):
    external_failed_queue(rig)
    def stop_after_recovery(**_kwargs):
        rig.session.latch_stop()
        return {"message": "Queue cleared"}
    monkeypatch.setattr(rig.robot, "recover_failed_queue", stop_after_recovery)
    with pytest.raises(SelectionError, match="new software stop"):
        rig.session.acknowledge_recovery()
    assert rig.session.stop_requested and rig.session.recovery.required()
    assert not rig.session.scan_usable
    assert not rig.gateway.submissions


def test_release_transport_failure_does_not_mask_yield_or_send_stop(rig, monkeypatch):
    with monkeypatch.context() as context:
        context.setattr(rig.robot, "end_standby", Mock(side_effect=requests.ConnectionError("Offline")))
        unbind = Mock()
        context.setattr(rig.robot, "unbind_batch", unbind)
        rig.runner._release()
    unbind.assert_called_once_with()
    assert not rig.gateway.calls


def test_delayed_shared_stop_cannot_target_a_new_batch(rig, monkeypatch):
    rig.runner.run_id = "old-batch"
    def delayed_state():
        rig.runner.run_id = "new-batch"
        return {"run": {"id": "new-batch", "phase": "active"}}
    stop = Mock()
    monkeypatch.setattr(rig.coordinator, "state", delayed_state)
    monkeypatch.setattr(rig.coordinator, "stop", stop)
    rig.runner.stop_batch()
    stop.assert_not_called()


def test_priority_epochs_primary_pi_lynx_and_primary_reconnect_do_not_repeat_cups(rig, tmp_path, monkeypatch):
    """Each predecessor's accepted queue finishes; the next owner scans anew."""
    start(rig)
    workers = []
    try:
        for sequence, (role, priority) in enumerate([
                ("primary", 300), ("secondary", 200), ("tertiary", 100), ("primary", 300)]):
            directory = tmp_path / f"worker-{sequence}"
            grid = grid_fixture(directory)
            gateway = rig.gateway
            gateway.client_id, gateway.priority, gateway.owner = role, priority, None
            coordinator = LocalCoordinator(rig.store, role)
            robot = QueueRobotClient(API, {})
            capture = Mock(return_value=(observed_scene(grid, 20 + sequence,
                [(100, 450 + 10 * sequence)] if sequence < 3 else [], CLEAR), 99))
            session = CupSession(capture, Mock(), robot, "cal-1", recovery=RecoveryJournal(directory / "recovery.json"),
                placing_grid=grid, between_cup_check=True, between_cup_grid_check=True,
                continuous_batch=False, right_park_pose=right_via_pose())
            runner = FailoverRunner(session, coordinator, role=role, state_dir=directory, output=Mock())
            workers.append((runner, robot, capture))
            if sequence < 3:
                def disconnect(_queue, robot=robot):
                    gateway.owner = None
                    robot._lease_error = ControlUnavailable("Disconnected after accepted cup")
                gateway.submit_hook = disconnect
                monkeypatch.setattr(runner._closed, "wait", lambda _delay, runner=runner: runner._closed.set())
            else:
                gateway.submit_hook = None
            runner.resume("batch-one")
            assert capture.call_count == 1
            assert not session.recovery.required()
            assert coordinator.state()["run"]["phase"] == ("active" if sequence < 3 else "complete")
        submitted = rig.gateway.submissions
        assert len(submitted) == 5
        assert all(len(item["steps"]) > 2 for item in submitted[:3])
        assert [item["steps"][0]["type"] for item in submitted[3:]] == ["home", "home"]
        assert len({item["steps"][0]["name"].split("_")[0] for item in submitted}) == 5
        assert not any(method == "DELETE" or path == "/robot/rapid/stop" for method, path, _ in rig.gateway.calls)
    finally:
        for runner, robot, _capture in workers:
            runner.close()
            robot.end_standby()


class TakeoverCamera:
    """Real camera protocol/exhaustion with deterministic in-memory responses."""

    def __init__(self, rig, monkeypatch, *, fail_capture, cups=1):
        from camera_client import CameraClient
        from test_camera_client import FakeClock, Response, frame, status
        self.rig, self.fail_capture, self.cups = rig, fail_capture, cups
        self.capture_count = 0
        self.status_count = 0
        self.responses = []
        self.attempts = []
        self.clock = FakeClock()
        monkeypatch.setattr("camera_client.time", SimpleNamespace(
            monotonic=self.clock.monotonic, sleep=self.clock.sleep))

        def get(url, **_options):
            path = url.removeprefix(API)
            if path == "/camera/status":
                self.status_count += 1
                reply = status(10 if self.status_count == 1 else 11)
            elif path == "/camera/rgb":
                reply = frame("rgb")
            else:
                assert path == "/camera/depth"
                reply = (Response("depth", status=503)
                         if self.capture_count == self.fail_capture else frame("depth"))
            self.responses.append(reply)
            return reply

        self.camera = CameraClient(API, {}, session=SimpleNamespace(get=get))
        rig.capture.side_effect = self.capture

    def capture(self, purpose):
        self.capture_count += 1
        self.status_count = 0
        rig = self.rig
        assert rig.gateway.queue["state"] != "running", "No capture before inherited cup finishes"
        transfers = [s for s in rig.gateway.submissions if len(s["steps"]) > 2]
        self.attempts.append((purpose, len(transfers), rig.coordinator.state()["run"]["phase"]))
        self.camera.capture_raw(timeout_s=0.11)
        remaining = [(100, 450), (200, 500)][:self.cups][len(transfers):]
        sequence = 20 + self.capture_count
        return observed_scene(rig.grid, sequence, remaining, CLEAR), sequence


def retry_without_sleep(rig, monkeypatch):
    """An unexpected endless retry fails quickly, rather than hanging the suite."""
    waits = []
    def wait(delay):
        waits.append(delay)
        assert len(waits) <= 5, "Transient error did not recover within the scripted attempts"
        assert rig.coordinator.state()["run"]["phase"] in {"active", "finalizing"}
        return False
    monkeypatch.setattr(rig.runner._closed, "wait", wait)
    return waits


@pytest.mark.parametrize("fail_capture,cups", [(1, 1), (2, 2)],
                         ids=["initial-takeover-scan", "between-completed-cups"])
def test_camera_depth_503_exhaustion_keeps_shared_batch_active_and_retries_fresh(rig, monkeypatch,
                                                                            fail_capture, cups):
    start(rig)
    camera = TakeoverCamera(rig, monkeypatch, fail_capture=fail_capture, cups=cups)
    waits = retry_without_sleep(rig, monkeypatch)
    rig.runner.resume("batch-one")

    assert waits, "An exhausted camera attempt must yield before taking a fresh observation"
    assert len([r for r in camera.responses if r.status_code == 503]) >= 2
    assert all(response.closed for response in camera.responses)
    assert all(phase == "active" for _, _, phase in camera.attempts)
    transfers = [s for s in rig.gateway.submissions if len(s["steps"]) > 2]
    assert len(transfers) == cups
    assert len({s["steps"][0]["name"] for s in transfers}) == cups
    homes = [s["steps"][0]["body"]["arm"] for s in rig.gateway.submissions if len(s["steps"]) == 1]
    assert homes == ["right", "left"]
    assert rig.coordinator.state()["run"]["id"] == "batch-one"
    assert rig.coordinator.state()["run"]["phase"] == "complete"
    assert not rig.session.recovery.required() and not rig.session.motion_uncertain
    assert not any(method == "DELETE" or path == "/robot/rapid/stop"
                   for method, path, _ in rig.gateway.calls)


def test_pi_waits_for_primary_cup_then_recovers_camera_503_without_new_batch(rig, monkeypatch):
    start(rig)
    rig.gateway.owner = {"active": True, "client_id": "primary", "priority": 300}
    identity = "e" * 16
    inherited = plan_to_queue_steps([move("primary_already_accepted")], name_prefix="q" + identity + "_")
    rig.coordinator.prepare_queue("batch-one", identity, inherited)
    rig.gateway.post(API + "/robot/rws/queue", json={"steps": inherited})
    rig.gateway.queue.update(state="running", finished_at=None, step=0, message="BUSY")
    rig.gateway.queue["steps"][0].update(state="running", message="BUSY")
    polls = []
    def finish_inherited(queue):
        if queue["owner"] == "primary" and queue["state"] == "running":
            polls.append(queue["state"])
            if len(polls) == 2:
                queue.update(state="done", finished_at="finished", message="DONE")
                queue["steps"][0].update(state="done", message="DONE")
    rig.gateway.queue_hook = finish_inherited
    rig.gateway.owner = None  # Mac disconnected; its gateway lease expired.
    rig.gateway.client_id, rig.gateway.priority = "secondary", 200
    rig.coordinator.role = rig.runner.role = "secondary"
    camera = TakeoverCamera(rig, monkeypatch, fail_capture=1)
    waits = retry_without_sleep(rig, monkeypatch)
    rig.runner.resume("batch-one")

    assert len(polls) == 2 and waits
    assert rig.coordinator.receipt("batch-one", identity)["state"] == "done"
    assert len(rig.gateway.submissions) == 4  # Accepted primary cup, Pi cup, two final Home jobs.
    assert rig.gateway.submissions[0]["steps"] == inherited
    assert len([s for s in rig.gateway.submissions if len(s["steps"]) > 2]) == 1
    assert rig.coordinator.state()["run"]["phase"] == "complete"
    assert all(response.closed for response in camera.responses)
    assert not rig.session.recovery.required()

    # Reconnecting Mac observes completion and never replays either cup/Home.
    before = copy.deepcopy(rig.gateway.submissions), rig.capture.call_count
    rig.gateway.client_id, rig.gateway.priority = "primary", 300
    rig.coordinator.role = rig.runner.role = "primary"
    rig.runner.resume("batch-one")
    assert before == (rig.gateway.submissions, rig.capture.call_count)


@pytest.mark.parametrize("operation", ["read-control", "read-queue", "acquire-control"])
def test_temporary_gateway_503_before_dispatch_yields_without_blocking_or_duplicate_work(rig, monkeypatch,
                                                                                      operation):
    start(rig)
    failed = []
    method = "post" if operation == "acquire-control" else "get"
    path = {"read-control": "/control", "read-queue": "/robot/rws/queue",
            "acquire-control": "/control/acquire"}[operation]
    original = getattr(rig.gateway, method)
    def temporarily_overloaded(url, **options):
        if url == API + path and not failed:
            failed.append(path)
            return rig.gateway.response({"detail": "Too many connections"}, 503)
        return original(url, **options)
    monkeypatch.setattr(requests, method, temporarily_overloaded)
    waits = retry_without_sleep(rig, monkeypatch)
    rig.runner.resume("batch-one")
    assert failed == [path] and waits
    assert rig.coordinator.state()["run"]["phase"] == "complete"
    assert [s["steps"][0]["body"]["arm"] for s in rig.gateway.submissions] == ["right", "left"]
    assert not rig.session.recovery.required()
    assert not any(method == "DELETE" or path == "/robot/rapid/stop"
                   for method, path, _ in rig.gateway.calls)


@pytest.mark.parametrize("failure", ["unauthorized", "invalid-status", "failed-queue"])
def test_takeover_never_treats_credentials_malformed_status_or_failed_motion_as_transient(rig, monkeypatch,
                                                                                      failure):
    start(rig)
    original = rig.gateway.get
    def invalid_status(url, **options):
        if url == API + "/control" and failure in {"unauthorized", "invalid-status"}:
            return (rig.gateway.response({"detail": "Unauthorized"}, 401) if failure == "unauthorized"
                    else rig.gateway.response({"active": "yes"}))
        return original(url, **options)
    monkeypatch.setattr(requests, "get", invalid_status)
    if failure == "failed-queue":
        rig.gateway.queue = {"state": "failed", "owner": "primary", "length": 1, "step": 0,
            "started_at": "2026-11-23T11:00:00Z", "finished_at": "finished", "message": "ERR move failed",
            "cancel_requested": False,
            "steps": [{"name": "old_right_via", "type": "move", "arm": "right", "group": "g0",
                       "state": "failed", "message": "ERR move failed"}]}
    no_retry = Mock(side_effect=AssertionError("Permanent failure must not be retried"))
    monkeypatch.setattr(rig.runner._closed, "wait", no_retry)
    with pytest.raises((RobotCommandError, SelectionError)):
        rig.runner.resume("batch-one")
    assert rig.coordinator.state()["run"]["phase"] == "blocked"
    assert rig.gateway.submissions == []
    rig.capture.assert_not_called()
    no_retry.assert_not_called()


def test_camera_retry_keeps_owner_while_pi_contends_and_restores_configured_speeds(rig, monkeypatch):
    """A waiting Pi must not acquire in the primary's temporary camera gap."""
    rig.session.speed, rig.session.home_speed = 900, 150
    settings = rig.runner.settings()
    settings["motion"].update(speed=300, home_speed=80)
    rig.coordinator.start("batch-one", settings)
    camera = TakeoverCamera(rig, monkeypatch, fail_capture=1)
    pi = QueueRobotClient(API, {"X-Offline-Role": "secondary"})
    rig.robot.headers["X-Offline-Role"] = "primary"
    original_post, lock, acquisitions = rig.gateway.post, threading.Lock(), []
    def role_post(url, **options):
        # The shared fake normally has one identity. Route two actual clients
        # by their synthetic headers so renewal and contender threads can race.
        with lock:
            previous = rig.gateway.client_id, rig.gateway.priority
            role = options["headers"].get("X-Offline-Role", "primary")
            rig.gateway.client_id, rig.gateway.priority = role, {"primary": 300, "secondary": 200}[role]
            try:
                result = original_post(url, **options)
                if url.endswith("/control/acquire"):
                    acquisitions.append((role, result.status_code))
                return result
            finally:
                rig.gateway.client_id, rig.gateway.priority = previous
    monkeypatch.setattr(requests, "post", role_post)
    waits, attempts, failures = [], [], []
    def compete():
        try:
            attempts.extend(pi.try_begin_standby("secondary", 200) for _ in range(3))
        except BaseException as error:
            failures.append(error)
    def retry_wait(delay):
        waits.append(delay)
        assert len(waits) == 1
        assert rig.runner.executing and rig.robot._persistent
        assert rig.gateway.owner["client_id"] == "primary"
        assert (rig.session.speed, rig.session.home_speed) == (300, 80)
        assert rig.runner._restore_speeds == (900, 150)
        contender = threading.Thread(target=compete)
        contender.start()
        contender.join(2)
        assert not contender.is_alive() and not failures
        assert attempts == [False, False, False]
        return False
    monkeypatch.setattr(rig.runner._closed, "wait", retry_wait)
    try:
        rig.runner.resume("batch-one")
    finally:
        pi.end_standby()
    assert waits == [1.0]
    assert acquisitions.count(("primary", 200)) == 1
    assert acquisitions.count(("secondary", 409)) == 3
    assert len(rig.gateway.submissions) == 3  # One cup, then measured right and left Home.
    assert rig.coordinator.state()["run"]["phase"] == "complete"
    assert all(response.closed for response in camera.responses)
    assert rig.gateway.owner is None and not rig.robot._persistent
    assert (rig.session.speed, rig.session.home_speed) == (900, 150)
    assert rig.runner._restore_speeds is None
    assert sum(path == "/control/release" for method, path, _ in rig.gateway.calls if method == "POST") == 1


@pytest.mark.parametrize("exit_kind", ["close", "worker-stop", "operator-stop"])
def test_retained_lease_is_released_when_retry_wait_exits(rig, monkeypatch, exit_kind):
    from camera_client import TransientCameraError
    start(rig)
    stop_event = threading.Event()
    rig.capture.side_effect = TransientCameraError("Camera is temporarily unavailable")
    waits = []
    def end_wait(delay):
        waits.append(delay)
        assert rig.robot._persistent and rig.gateway.owner["client_id"] == "primary"
        if exit_kind == "close":
            rig.runner._closed.set()
        elif exit_kind == "worker-stop":
            stop_event.set()
        else:
            # Finish the separately requested software stop, then unwind the
            # held lease exactly as the real Stop worker does.
            rig.session.latch_stop()
            rig.robot._stop_finished.set()
        return False
    monkeypatch.setattr(rig.runner._closed, "wait", end_wait)
    if exit_kind == "operator-stop":
        with pytest.raises((SelectionError, RobotCommandError), match="stop|Stop"):
            rig.runner._run("batch-one", stop_event=stop_event)
    else:
        rig.runner._run("batch-one", stop_event=stop_event)
    assert waits == [1.0]
    assert rig.gateway.owner is None
    assert not rig.runner.executing and not rig.robot._persistent
    assert rig.robot._batch_binding is None
    assert rig.gateway.submissions == []
    assert rig.coordinator.state()["run"]["phase"] == "active"


@pytest.mark.parametrize("loss", ["network", "ownership", "coordinator", "ownership-read"])
def test_read_retry_never_keeps_lease_after_actual_loss_or_unconfirmed_owner(rig, monkeypatch, loss):
    from camera_client import TransientCameraError
    from robot_client import GatewayReadUnavailable
    start(rig)
    def capture(_purpose):
        error = TransientCameraError("Read temporarily unavailable")
        if loss == "network":
            error.__cause__ = requests.ConnectionError("Gateway link disconnected")
        elif loss == "ownership":
            rig.gateway.owner = {"active": True, "client_id": "admin", "priority": 1000}
        elif loss == "coordinator":
            error = CoordinatorUnavailable("Coordinator did not answer")
        else:
            original_get = rig.robot._get_object
            def unavailable_owner(endpoint):
                if endpoint == "/control":
                    raise GatewayReadUnavailable("Ownership read503")
                return original_get(endpoint)
            monkeypatch.setattr(rig.robot, "_get_object", unavailable_owner)
        raise error
    rig.capture.side_effect = capture
    def end_wait(_delay):
        assert not rig.robot._persistent
        assert not rig.runner.executing
        assert (rig.gateway.owner or {}).get("client_id") == ("admin" if loss == "ownership" else None)
        rig.runner._closed.set()
        return True
    monkeypatch.setattr(rig.runner._closed, "wait", end_wait)
    rig.runner.resume("batch-one")
    assert rig.gateway.submissions == []
    assert rig.coordinator.state()["run"]["phase"] == "active"
    assert rig.capture.call_count == 1


def test_retry_reason_keeps_cause_chain_bounded_and_redacts_bearer_credentials():
    error = QueueNotSubmitted("No robot command sent")
    error.__cause__ = CoordinatorUnavailable("Coordinator unavailable")
    error.__cause__.__cause__ = requests.ReadTimeout("Bearer synthetic-secret\n" + "slow request " * 100)
    result = FailoverRunner._retry_reason(error)
    assert result.startswith("QueueNotSubmitted -> CoordinatorUnavailable -> ReadTimeout:")
    assert "synthetic-secret" not in result and "Bearer [redacted]" in result
    assert "\n" not in result and len(result) <= 500
