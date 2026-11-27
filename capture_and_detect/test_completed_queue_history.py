"""Completed legacy history is an idle baseline, never a completion receipt."""

import copy
import json
from pathlib import Path

import pytest

from queued_robot_client import QueueRobotClient
from robot_client import RobotCommandError
from test_queued_robot_client import client, gateway, move


def completed_legacy_queue():
    return json.loads((Path(__file__).parent / "test_fixtures" /
                       "legacy_completed_duplicate_queue.json").read_text())


def test_saved_completed_terminal_run_allows_idle_check_without_changing_history(client, gateway):
    gateway.queue = completed_legacy_queue()
    expected = copy.deepcopy(gateway.queue)
    assert client.try_begin_standby()
    gateway.calls.clear()
    assert client.wait_for_idle_queue(timeout=1, poll=0.001) == expected
    assert gateway.queue == expected
    assert all(method == "GET" for method, _, _ in gateway.calls)
    assert {path for _, path, _ in gateway.calls if "/robot/rws/status" in path} == {
        "/robot/rws/status?arm=left", "/robot/rws/status?arm=right"}


def test_new_work_after_legacy_history_reports_only_new_completion(client, gateway):
    gateway.queue = completed_legacy_queue()
    assert client.try_begin_standby()
    completed = []
    client.execute_jobs([move("new_cup")], after_done=lambda index, job: completed.append(job["name"]))
    assert completed == ["new_cup"]
    assert len(gateway.submissions) == 1
    assert not any(method == "DELETE" for method, _, _ in gateway.calls)


def test_legacy_completed_history_remains_invalid_as_a_completion_receipt():
    with pytest.raises(RobotCommandError, match="invalid queue step"):
        QueueRobotClient._validate_queue_status(completed_legacy_queue())


@pytest.mark.parametrize("arm_status", [
    {"accepted": True, "message": "BUSY"},
    {"accepted": False, "message": "ERR move failed"},
    {"message": "DONE"},
])
def test_completed_history_never_overrides_actual_arm_state(client, gateway, arm_status):
    gateway.queue = completed_legacy_queue()
    gateway.arm_status["right"] = arm_status
    assert client.try_begin_standby()
    with pytest.raises(RobotCommandError):
        client.wait_for_idle_queue(timeout=0.005, poll=0.001)
    assert not gateway.submissions
    assert not any(method == "DELETE" for method, _, _ in gateway.calls)


@pytest.mark.parametrize("change", [
    "running", "failed", "cancelled", "pending_step", "missing_done", "length",
    "missing_owner", "missing_timestamp", "coordinated_names", "mixed_names",
])
def test_invalid_or_coordinated_duplicate_history_cannot_admit_work(client, gateway, change):
    queue = completed_legacy_queue()
    if change in {"running", "failed", "cancelled"}:
        queue["state"] = change
    elif change == "pending_step":
        queue["steps"][0]["state"] = "pending"
    elif change == "missing_done":
        queue["steps"][0]["message"] = None
    elif change == "length":
        queue["length"] += 1
    elif change == "missing_owner":
        queue.pop("owner")
    elif change == "missing_timestamp":
        queue.pop("started_at")
    elif change == "coordinated_names":
        for step in queue["steps"]:
            step["name"] = "q0123456789abcdef_" + step["name"]
    elif change == "mixed_names":
        queue["steps"][0]["name"] = "q0123456789abcdef_0_first"
    gateway.queue = queue
    assert client.try_begin_standby()
    with pytest.raises(RobotCommandError):
        client.wait_for_idle_queue(timeout=1, poll=0.001)
    assert not gateway.submissions
    assert not any(method == "DELETE" for method, _, _ in gateway.calls)


def test_inspection_can_read_completed_history_but_does_not_clear_it(client, gateway, tmp_path):
    gateway.queue = completed_legacy_queue()
    expected = copy.deepcopy(gateway.queue)
    assert client.inspection_queue_status() == expected
    with pytest.raises(RobotCommandError):
        client.recover_failed_queue(expected_queue=expected, archive_dir=tmp_path, acknowledged=True)
    assert gateway.queue == expected
    assert all(method == "GET" for method, _, _ in gateway.calls)


def test_own_execution_still_rejects_duplicate_result_names(client, gateway):
    assert client.try_begin_standby()
    gateway.submit_hook = lambda queue: queue["steps"][1].update(name=queue["steps"][0]["name"])
    completed = []
    with pytest.raises(RobotCommandError, match="invalid queue step"):
        client.execute_jobs([move("first"), move("second")],
                            after_done=lambda index, job: completed.append(job["name"]))
    assert completed == []
    assert len(gateway.submissions) == 1
