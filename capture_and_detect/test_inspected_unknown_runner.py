"""Explicit unknown-queue retirement: real runner/store, entirely offline gateway."""

import copy
import json
from pathlib import Path

import pytest
import requests

from cup_inventory import SelectionError
from failover_coordinator import CoordinatorError, CoordinatorUnavailable
from queued_robot_client import QueueWaitCancelled
from robot_client import RobotCommandError
from test_failover_workflow import CLEAR, observed_scene, rig, start
from test_queued_robot_client import gateway, move


def lost_queue(rig, *, stop_latched=False):
    start(rig)
    rig.robot.try_begin_standby(expected_client_id="primary", expected_priority=300)
    rig.robot.bind_batch(rig.coordinator, "batch-one", rig.runner.pending_path)
    jobs = [move("old_left"), move("old_right", arm="right")]

    def lose_connection(queue):
        queue.update(state="running", finished_at=None, message="RUNNING")
        for step in queue["steps"]:
            step.update(state="pending", message="")
        queue["steps"][0].update(state="done", message="DONE")
        rig.robot.cancel_wait()

    rig.gateway.submit_hook = lose_connection
    with pytest.raises(QueueWaitCancelled):
        rig.robot.execute_jobs(jobs)
    rig.gateway.submit_hook = None
    record = rig.robot._read_pending(rig.runner.pending_path)
    rig.coordinator.receipt(record["run_id"], record["submission_id"])
    original = copy.deepcopy(rig.gateway.queue)
    rig.coordinator.stop("batch-one", "Operator stopped the old batch")
    rig.runner._release()
    new_queue = copy.deepcopy(original)
    new_queue.update(state="done", started_at="2026-11-25T12:00:00+00:00",
                     finished_at="2026-11-25T12:00:05+00:00", message="DONE")
    for index, step in enumerate(new_queue["steps"]):
        step.update(name=f"external_move_{index}", state="done", message="DONE")
    rig.gateway.queue = new_queue
    receipt = rig.coordinator.receipt(record["run_id"], record["submission_id"])
    assert receipt["state"] == "unknown" and receipt["queue"] == original
    context = {"failover": {"run_id": record["run_id"], "submission_id": record["submission_id"]},
               "execution": {"completed_steps": [], "pending_jobs": {job["name"]: job for job in jobs}},
               "stop_requested": stop_latched}
    rig.session.recovery.begin(context)
    rig.session.motion_uncertain = True
    if stop_latched:
        rig.session.latch_stop()
        rig.session._stop_pending.clear()
        rig.robot._stop_finished.set()
    rig.gateway.calls.clear()
    rig.gateway.submissions.clear()
    return record, original, copy.deepcopy(new_queue)


def no_motion(rig):
    assert not rig.gateway.submissions
    assert not any(method == "DELETE" or path.startswith("/robot/") and method == "POST"
                   for method, path, _ in rig.gateway.calls)
    rig.capture.assert_not_called()


@pytest.mark.parametrize("stop_latched", [False, True])
def test_inspection_retires_unknown_without_claiming_completion_or_touching_grid(rig, stop_latched):
    before, original, newer = lost_queue(rig, stop_latched=stop_latched)
    grid = rig.grid.path.read_bytes()
    journal = rig.session.recovery.path.read_bytes()
    result = rig.session.acknowledge_recovery()
    assert "completion remains unknown" in result["message"]
    pending = rig.robot._read_pending(rig.runner.pending_path)
    assert pending["state"] == "unknown" and pending["reconciled"]
    assert pending["queue"] == before["queue"]
    assert pending["started_at"] == before["started_at"]
    for key in ("callback_attempted", "callback_completed", "home_verified", "callbacks_applied"):
        assert pending[key] == before[key]
    assert pending["abandoned_receipt_queue"] == original
    assert pending["inspected_abandonment"]["observed_queue"] == newer
    assert rig.gateway.queue == newer and rig.grid.path.read_bytes() == grid
    assert rig.coordinator.state()["head"] is None
    receipt = rig.coordinator.receipt(before["run_id"], before["submission_id"])
    assert receipt["state"] == "unknown" and receipt["queue"] == original
    assert not rig.session.stop_requested and not rig.session.motion_uncertain
    assert not rig.session.recovery.required() and not rig.session.scan_usable
    archives = [json.loads(path.read_text()) for path in
                (rig.runner.pending_path.parent / "recovery-history").glob("*.json")]
    assert any(item.get("pending") == before and item.get("journal") == json.loads(journal) for item in archives)
    no_motion(rig)


def test_retired_unknown_allows_future_settings_and_new_admission_without_callbacks(rig):
    before, _, _ = lost_queue(rig)
    rig.session.acknowledge_recovery()
    rig.runner.set_pick_height(True)
    rig.runner.set_device_speeds({"primary": 500, "secondary": 400, "tertiary": 300})
    assert rig.session.camera_z is True
    rig.robot.bind_batch(rig.coordinator, before["run_id"], rig.runner.pending_path)
    rig.runner.run_id = before["run_id"]
    assert rig.runner._reconcile(wait=False)
    pending = rig.robot.pending_completion(wait=False)
    assert pending["reconciled"] and pending["state"] == "unknown"
    rig.coordinator.start("new-explicit-batch", rig.runner.settings())
    assert rig.coordinator.state()["run"]["id"] == "new-explicit-batch"
    assert rig.robot._read_pending(rig.runner.pending_path)["callback_completed"] == []
    no_motion(rig)


def test_retired_unknown_allows_actual_new_batch_dispatch(rig):
    before, original, _ = lost_queue(rig)
    rig.session.acknowledge_recovery()
    rig.capture.return_value = (observed_scene(rig.grid, 30, [(100, 450)], CLEAR), 99)

    def finish_new_queue(queue):
        rig.capture.return_value = (observed_scene(rig.grid, 31, [], CLEAR), 100)

    rig.gateway.submit_hook = finish_new_queue
    rig.runner.execute_primary("goto", "fresh-preview", [], None)
    assert rig.coordinator.state()["run"]["phase"] == "complete"
    assert rig.gateway.submissions
    old_receipt = rig.coordinator.receipt(before["run_id"], before["submission_id"])
    assert old_receipt["state"] == "unknown" and old_receipt["queue"] == original
    assert old_receipt.get("abandonment")
    assert not rig.session.motion_uncertain and not rig.session.recovery.required()


@pytest.mark.parametrize("change", ["state", "owner", "started_at", "steps", "proof", "proof_queue", "callbacks"])
def test_forged_or_inconsistent_retired_local_metadata_remains_blocked(rig, change):
    lost_queue(rig)
    rig.session.acknowledge_recovery()
    pending = rig.robot._read_pending(rig.runner.pending_path)
    if change == "state": pending["state"] = "done"
    elif change == "owner": pending["expected_owner"] = "secondary"
    elif change == "started_at": pending["started_at"] = "2026-11-25T13:00:00+00:00"
    elif change == "steps": pending["abandoned_receipt_queue"]["steps"][0]["group"] = "changed"
    elif change == "proof": pending["inspected_abandonment"]["submission_id"] = "f" * 16
    elif change == "proof_queue": pending.pop("abandoned_receipt_queue")
    elif change == "callbacks": pending["callbacks_applied"] = True
    rig.robot._write_pending(rig.runner.pending_path, pending)
    with pytest.raises(RobotCommandError):
        rig.robot._read_pending(rig.runner.pending_path)
    no_motion(rig)


@pytest.mark.parametrize("change", ["new_stop", "new_queue", "new_head", "offline"])
def test_concurrent_change_never_clears_local_inspection(rig, monkeypatch, change):
    lost_queue(rig)
    journal = rig.session.recovery.path.read_bytes()
    before = rig.runner.pending_path.read_bytes()
    abandon = rig.coordinator.abandon

    def intercepted(*args, **kwargs):
        if change == "new_stop":
            receipt = abandon(*args, **kwargs)
            rig.session.latch_stop()
            return receipt
        if change == "new_queue":
            rig.gateway.queue["started_at"] = "2026-11-25T13:00:00+00:00"
            return abandon(*args, **kwargs)
        if change == "offline":
            raise CoordinatorUnavailable("Offline")
        raise CoordinatorError("Current head changed")

    monkeypatch.setattr(rig.coordinator, "abandon", intercepted)
    with pytest.raises((RobotCommandError, SelectionError, CoordinatorError)):
        rig.session.acknowledge_recovery()
    assert rig.session.recovery.required() and rig.session.motion_uncertain
    assert rig.session.recovery.path.read_bytes() == journal
    assert rig.runner.pending_path.read_bytes() == before
    assert not rig.session.scan_usable
    no_motion(rig)


def test_lost_abandon_reply_can_be_retried_only_by_explicit_inspection(rig, monkeypatch):
    before, _, newer = lost_queue(rig)
    abandon = rig.coordinator.abandon

    def lose_reply(*args, **kwargs):
        abandon(*args, **kwargs)
        raise CoordinatorUnavailable("Reply lost")

    with monkeypatch.context() as patcher:
        patcher.setattr(rig.coordinator, "abandon", lose_reply)
        with pytest.raises(CoordinatorUnavailable):
            rig.session.acknowledge_recovery()
    assert rig.session.recovery.required()
    assert not rig.robot._read_pending(rig.runner.pending_path)["reconciled"]
    result = rig.session.acknowledge_recovery()
    assert "unknown" in result["message"]
    assert rig.robot._read_pending(rig.runner.pending_path)["reconciled"]
    assert rig.gateway.queue == newer
    assert rig.coordinator.receipt(before["run_id"], before["submission_id"])["state"] == "unknown"
    no_motion(rig)


@pytest.mark.parametrize("state", ["idle", "failed", "cancelled"])
def test_distinct_terminal_failure_is_cleared_before_unknown_retirement(rig, state):
    lost_queue(rig)
    if state == "idle":
        rig.gateway.queue = {"state": "idle", "length": 0, "steps": []}
    else:
        rig.gateway.queue["state"] = state
        rig.gateway.queue["steps"][-1].update(state="failed" if state == "failed" else "skipped",
                                             message="ERR external move" if state == "failed" else "")
    expected = copy.deepcopy(rig.gateway.queue)
    result = rig.session.acknowledge_recovery()
    assert "completion remains unknown" in result["message"]
    assert rig.gateway.queue["state"] == "idle"
    pending = rig.robot._read_pending(rig.runner.pending_path)
    assert pending["inspected_abandonment"]["observed_queue"]["state"] == "idle"
    assert sum(method == "DELETE" for method, _, _ in rig.gateway.calls) == (state != "idle")
    archives = [json.loads(path.read_text()) for path in
                (rig.runner.pending_path.parent / "recovery-history").glob("*.json")]
    assert any(item.get("gateway_queue") == expected for item in archives)
    assert not rig.gateway.submissions
    assert not any(method == "POST" and path.startswith("/robot/") for method, path, _ in rig.gateway.calls)
    rig.capture.assert_not_called()


def legacy_failed_queue():
    return json.loads((Path(__file__).parent / "test_fixtures" / "legacy_failed_duplicate_queue.json").read_text())


def test_real_legacy_duplicate_failure_is_preserved_then_cleared_before_abandonment(rig, monkeypatch):
    before, original, _ = lost_queue(rig)
    legacy = legacy_failed_queue()
    rig.gateway.queue = copy.deepcopy(legacy)
    with pytest.raises(RobotCommandError):
        rig.robot.queue_status()  # Execution and callback identity stay strict.
    abandon = rig.coordinator.abandon

    def checked_abandon(*args, **kwargs):
        assert kwargs["expected_gateway_queue"] == rig.gateway.queue == {"state": "idle", "length": 0, "steps": []}
        assert sum(method == "DELETE" for method, _, _ in rig.gateway.calls) == 1
        return abandon(*args, **kwargs)

    monkeypatch.setattr(rig.coordinator, "abandon", checked_abandon)
    rig.session.acknowledge_recovery()
    archives = [json.loads(path.read_text()) for path in
                (rig.runner.pending_path.parent / "recovery-history").glob("*.json")]
    assert any(item.get("gateway_queue") == legacy and item.get("pending") == before for item in archives)
    assert any(item.get("queue") == legacy and item.get("state") == "recovered" for item in archives)
    assert legacy["steps"][15]["name"] == legacy["steps"][17]["name"] == "right_release"
    receipt = rig.coordinator.receipt(before["run_id"], before["submission_id"])
    assert receipt["state"] == "unknown" and receipt["queue"] == original
    assert receipt["abandonment"]["observed_queue"]["state"] == "idle"
    assert not rig.gateway.submissions
    assert not any(method == "POST" and path.startswith("/robot/") for method, path, _ in rig.gateway.calls)
    rig.capture.assert_not_called()


@pytest.mark.parametrize("phase", ["clear_reply", "abandon_before", "abandon_reply"])
def test_partial_recovery_keeps_old_receipt_blocked_and_retries_from_idle(rig, monkeypatch, phase):
    before, original, _ = lost_queue(rig)
    rig.gateway.queue = legacy_failed_queue()
    pending = rig.runner.pending_path.read_bytes()
    journal = rig.session.recovery.path.read_bytes()
    abandon = rig.coordinator.abandon
    delete = rig.gateway.delete

    def lose_clear_reply(*args, **kwargs):
        delete(*args, **kwargs)
        raise requests.Timeout("Clear reply lost")

    def fail_abandon(*args, **kwargs):
        if phase == "abandon_reply":
            abandon(*args, **kwargs)
        raise CoordinatorUnavailable("Abandonment reply unavailable")

    with monkeypatch.context() as patcher:
        if phase == "clear_reply":
            patcher.setattr("queued_robot_client.requests.delete", lose_clear_reply)
        else:
            patcher.setattr(rig.coordinator, "abandon", fail_abandon)
        with pytest.raises((RobotCommandError, CoordinatorUnavailable)):
            rig.session.acknowledge_recovery()
    assert rig.gateway.queue["state"] == "idle"
    assert rig.runner.pending_path.read_bytes() == pending
    assert rig.session.recovery.path.read_bytes() == journal
    assert rig.session.motion_uncertain and rig.session.recovery.required()
    assert not rig.session.scan_usable
    shared = rig.coordinator.state()
    if phase == "abandon_reply":
        assert shared["head"] is None and shared["run"].get("abandonment")
    else:
        assert shared["head"]["state"] == "unknown" and not shared["run"].get("abandonment")
    result = rig.session.acknowledge_recovery()
    assert "completion remains unknown" in result["message"]
    assert rig.robot._read_pending(rig.runner.pending_path)["reconciled"]
    assert not rig.session.recovery.required() and not rig.session.motion_uncertain
    receipt = rig.coordinator.receipt(before["run_id"], before["submission_id"])
    assert receipt["state"] == "unknown" and receipt["queue"] == original
    assert sum(method == "DELETE" for method, _, _ in rig.gateway.calls) == 1
    assert not rig.gateway.submissions
    assert not any(method == "POST" and path.startswith("/robot/") for method, path, _ in rig.gateway.calls)
    rig.capture.assert_not_called()


def test_completed_legacy_duplicate_history_retires_only_explicitly_inspected_unknown(rig):
    before, original, _ = lost_queue(rig)
    completed = json.loads((Path(__file__).parent / "test_fixtures" /
                            "legacy_completed_duplicate_queue.json").read_text())
    rig.gateway.queue = copy.deepcopy(completed)
    grid = rig.grid.path.read_bytes()
    result = rig.session.acknowledge_recovery()
    assert "completion remains unknown" in result["message"]
    pending = rig.robot._read_pending(rig.runner.pending_path)
    assert pending["state"] == "unknown" and pending["reconciled"]
    assert pending["queue"] == before["queue"] and pending["started_at"] == before["started_at"]
    for key in ("callback_attempted", "callback_completed", "home_verified", "callbacks_applied"):
        assert pending[key] == before[key]
    assert pending["abandoned_receipt_queue"] == original
    assert pending["inspected_abandonment"]["observed_queue"] == completed
    receipt = rig.coordinator.receipt(before["run_id"], before["submission_id"])
    assert receipt["state"] == "unknown" and receipt["queue"] == original
    assert rig.gateway.queue == completed and rig.grid.path.read_bytes() == grid
    assert rig.coordinator.state()["head"] is None
    assert not rig.session.recovery.required() and not rig.session.motion_uncertain
    assert not rig.session.scan_usable
    no_motion(rig)


@pytest.mark.parametrize("change", ["running", "incomplete_done", "running_step", "prefixed", "changed_duplicate", "same_start"])
def test_legacy_duplicate_relaxation_never_allows_active_or_changed_evidence(rig, monkeypatch, change):
    _, original, _ = lost_queue(rig)
    rig.gateway.queue = legacy_failed_queue()
    if change in {"running", "incomplete_done"}:
        rig.gateway.queue["state"] = "done" if change == "incomplete_done" else change
    elif change == "running_step":
        rig.gateway.queue["steps"][4]["state"] = "running"
    elif change == "prefixed":
        for step in rig.gateway.queue["steps"]:
            step["name"] = "q" + "a" * 16 + "_" + step["name"]
    elif change == "same_start":
        rig.gateway.queue["started_at"] = original["started_at"]
    else:
        write = rig.robot._write_pending
        def change_after_archive(path, value):
            write(path, value)
            if path.name.startswith("shared-inspection-"):
                rig.gateway.queue["steps"][17]["message"] = "Changed evidence"
        monkeypatch.setattr(rig.robot, "_write_pending", change_after_archive)
    with pytest.raises((RobotCommandError, SelectionError)):
        rig.session.acknowledge_recovery()
    assert rig.session.recovery.required() and rig.session.motion_uncertain
    assert not rig.robot._read_pending(rig.runner.pending_path)["reconciled"]
    no_motion(rig)


def test_new_stop_during_local_retirement_keeps_recovery_blocked(rig, monkeypatch):
    lost_queue(rig)
    journal = rig.session.recovery.path.read_bytes()
    write = rig.robot._write_pending

    def stop_after_retirement(path, value):
        write(path, value)
        if path == rig.runner.pending_path and value.get("inspected_abandonment"):
            rig.session.latch_stop()

    monkeypatch.setattr(rig.robot, "_write_pending", stop_after_retirement)
    with pytest.raises((SelectionError, RobotCommandError)):
        rig.session.acknowledge_recovery()
    assert rig.robot._read_pending(rig.runner.pending_path)["reconciled"]
    assert rig.session.stop_requested and rig.session.motion_uncertain
    assert rig.session.recovery.required()
    assert json.loads(rig.session.recovery.path.read_text())["context"]["action"] == "software_stop"
    archives = [json.loads(path.read_text()) for path in
                (rig.runner.pending_path.parent / "recovery-history").glob("*.json")]
    assert any(item.get("journal") == json.loads(journal) for item in archives)
    assert not rig.session.scan_usable
    no_motion(rig)
