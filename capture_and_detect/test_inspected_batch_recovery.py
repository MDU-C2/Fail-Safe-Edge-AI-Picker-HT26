"""The operator's unblock action retires a failed batch without replaying it."""

import copy
import json
from unittest.mock import Mock

import pytest
import requests

from cup_inventory import SelectionError
from failover_coordinator import CoordinatorConflict, CoordinatorUnavailable
from robot_client import RobotCommandError
from test_between_cup_grid import CLEAR, observed_scene
from test_failover_workflow import rig
from test_queued_robot_client import gateway


def fail_cup(rig, state="failed"):
    rig.capture.return_value = (observed_scene(rig.grid, 10, [(100, 450)], CLEAR), 99)
    rig.session.scan()
    def fail(queue):
        queue.update(state=state, message="left_park did not finish")
        failed = next(i for i, step in enumerate(queue["steps"]) if step["name"].endswith("left_park"))
        for index, step in enumerate(queue["steps"]):
            step.update(state="done" if index < failed else state if index == failed else "skipped",
                        message="DONE" if index < failed else "ERR move failed")
    rig.gateway.submit_hook = fail
    with pytest.raises(RobotCommandError):
        rig.runner.execute_primary("goto", "frame-10", [], None)
    rig.gateway.submit_hook = None
    rig.capture.reset_mock()
    rig.gateway.calls.clear()
    return copy.deepcopy(rig.coordinator.state()["head"])


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_button_retires_current_failure_preserves_history_and_requires_new_scan(rig, state):
    head = fail_cup(rig, state)
    old_run = rig.coordinator.state()["run"]
    result = rig.session.acknowledge_recovery()
    assert "unblocked" in result["message"]
    current = rig.coordinator.state()
    assert current["head"] is None and current["run"]["phase"] == "stopped"
    assert current["run"]["reason"] == old_run["reason"]
    receipt = rig.coordinator.receipt(head["run_id"], head["submission_id"])
    assert receipt["state"] == state and receipt["queue"] == head["queue"]
    pending = rig.robot._read_pending(rig.runner.pending_path)
    assert pending["state"] == state and pending["reconciled"]
    assert pending["inspected_recovery"] == receipt["recovery"]
    assert not pending["callbacks_applied"]
    assert not rig.session.recovery.required() and not rig.session.motion_uncertain
    assert not rig.session.scan_usable and not rig.session.stop_requested
    assert rig.gateway.queue["state"] == "idle" and rig.gateway.owner is None
    assert sum(method == "DELETE" for method, _, _ in rig.gateway.calls) == 1
    assert not rig.gateway.submissions
    assert not any(path == "/robot/rapid/stop" for _, path, _ in rig.gateway.calls)
    rig.capture.assert_not_called()
    assert len(list((rig.runner.pending_path.parent / "recovery-history").glob("*.json"))) == 3
    rig.runner.resume(head["run_id"])
    rig.capture.assert_not_called()  # The retired run never resumes.
    rig.coordinator.start("new-batch", rig.runner.settings())
    assert rig.coordinator.state()["run"]["id"] == "new-batch"
    assert not rig.gateway.submissions


@pytest.mark.parametrize("failure", ["before_commit", "lost_reply", "local_save"])
def test_interrupted_unblock_keeps_local_block_and_retries_without_second_delete(rig, monkeypatch, failure):
    head = fail_cup(rig)
    original = rig.coordinator.recover
    write = rig.robot._write_pending
    with monkeypatch.context() as patch:
        if failure == "before_commit":
            patch.setattr(rig.coordinator, "recover", Mock(side_effect=CoordinatorUnavailable("Offline")))
        elif failure == "lost_reply":
            def lost(*args, **kwargs):
                original(*args, **kwargs)
                raise CoordinatorUnavailable("Reply lost")
            patch.setattr(rig.coordinator, "recover", lost)
        else:
            def fail_save(path, record):
                if path == rig.runner.pending_path and record.get("inspected_recovery"):
                    raise OSError("Disk full")
                return write(path, record)
            patch.setattr(rig.robot, "_write_pending", fail_save)
        with pytest.raises((CoordinatorUnavailable, OSError)):
            rig.session.acknowledge_recovery()
    assert rig.session.recovery.required() and rig.session.motion_uncertain
    assert rig.gateway.queue["state"] == "idle"
    rig.session.acknowledge_recovery()
    assert not rig.session.recovery.required()
    assert rig.robot._read_pending(rig.runner.pending_path)["reconciled"]
    assert rig.coordinator.receipt(head["run_id"], head["submission_id"])["state"] == "failed"
    assert sum(method == "DELETE" for method, _, _ in rig.gateway.calls) == 1
    assert not rig.gateway.submissions
    rig.capture.assert_not_called()


@pytest.mark.parametrize("blocker", ["old_coordinator", "different_queue"])
def test_unblock_refuses_unsupported_server_or_unrelated_gateway_failure(rig, monkeypatch, blocker):
    fail_cup(rig)
    before = rig.runner.pending_path.read_bytes()
    if blocker == "old_coordinator":
        state = rig.coordinator.state()
        state.pop("capabilities")
        monkeypatch.setattr(rig.coordinator, "state", Mock(return_value=state))
    else:
        rig.gateway.queue["steps"][0]["name"] = "an-unrelated-command"
    with pytest.raises(SelectionError):
        rig.session.acknowledge_recovery()
    assert not any(method in {"POST", "DELETE"} for method, _, _ in rig.gateway.calls)
    assert rig.runner.pending_path.read_bytes() == before
    assert rig.session.recovery.required()


def test_worker_adopts_only_matching_shared_inspection_without_callbacks_or_motion(rig):
    head = fail_cup(rig)
    pending = rig.runner.pending_path.read_bytes()
    journal = rig.session.recovery.path.read_bytes()
    rig.session.acknowledge_recovery()
    # Model a disconnected worker returning with its old unretired local files.
    rig.runner.pending_path.write_bytes(pending)
    rig.session.recovery.path.write_bytes(journal)
    rig.session.motion_uncertain = True
    rig.gateway.calls.clear()
    rig.runner.resume(head["run_id"])
    assert not rig.session.recovery.required() and not rig.session.motion_uncertain
    assert not rig.session.scan_usable
    assert rig.robot._read_pending(rig.runner.pending_path)["reconciled"]
    assert rig.gateway.calls == []
    rig.capture.assert_not_called()


def test_shared_inspection_never_clears_a_later_unrelated_stop_or_journal(rig):
    head = fail_cup(rig)
    pending = rig.runner.pending_path.read_bytes()
    rig.session.acknowledge_recovery()
    rig.runner.pending_path.write_bytes(pending)
    rig.session.recovery.begin({"action": "operator_control", "control": "home", "arm": "left"})
    old = rig.session.recovery.path.read_bytes()
    rig.session.motion_uncertain = True
    with pytest.raises(SelectionError, match="Recovery does not belong"):
        rig.runner.resume(head["run_id"])
    assert rig.session.recovery.path.read_bytes() == old
    rig.session.latch_stop()
    with pytest.raises(SelectionError):
        rig.runner.resume(head["run_id"])
    assert rig.session.stop_requested


def test_previous_retired_local_failure_does_not_block_recovering_another_devices_failure(rig):
    fail_cup(rig)
    rig.session.acknowledge_recovery()
    retired = rig.runner.pending_path.read_bytes()
    head = fail_cup(rig)
    rig.runner.pending_path.write_bytes(retired)  # Mac's older acknowledged cup.
    rig.session.acknowledge_recovery()
    assert rig.coordinator.state()["head"] is None
    assert rig.coordinator.receipt(head["run_id"], head["submission_id"])["recovery"]
    assert rig.runner.pending_path.read_bytes() == retired
    assert not rig.session.recovery.required()


def test_lost_queue_clear_reply_requires_new_click_without_repeating_delete(rig, monkeypatch):
    fail_cup(rig)
    delete = rig.gateway.delete
    def lost(*args, **kwargs):
        delete(*args, **kwargs)
        raise requests.ConnectionError("Clear reply lost")
    with monkeypatch.context() as patch:
        patch.setattr(requests, "delete", lost)
        with pytest.raises(RobotCommandError, match="not retried"):
            rig.session.acknowledge_recovery()
    assert rig.session.recovery.required()
    assert rig.coordinator.state()["head"]["state"] == "failed"
    rig.session.acknowledge_recovery()
    assert not rig.session.recovery.required()
    assert sum(method == "DELETE" for method, _, _ in rig.gateway.calls) == 1
    assert not rig.gateway.submissions


def test_new_stop_during_shared_retirement_dominates_successful_unblock(rig, monkeypatch):
    fail_cup(rig)
    recover = rig.coordinator.recover
    def stop_after_commit(*args, **kwargs):
        result = recover(*args, **kwargs)
        rig.session.latch_stop()
        return result
    monkeypatch.setattr(rig.coordinator, "recover", stop_after_commit)
    with pytest.raises(SelectionError, match="new software stop"):
        rig.session.acknowledge_recovery()
    assert rig.coordinator.state()["head"] is None
    assert rig.session.recovery.required() and rig.session.stop_requested
    assert not rig.session.scan_usable
    assert not rig.gateway.submissions
