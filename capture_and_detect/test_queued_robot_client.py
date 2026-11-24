"""Queued execution and three-tier lease behavior, with entirely fake HTTP."""

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
from unittest.mock import Mock

import pytest
import requests

from handover_plan import handover_jobs, right_via_pose
from queued_robot_client import (ControlUnavailable, QueueNotSubmitted, QueueRobotClient,
                                 QueueWaitCancelled, plan_to_queue_steps)
from robot_client import RobotClient, RobotCommandError


API = "http://offline.invalid/api/v1"


def move(name="left_move", arm="left", after=None):
    result = {"name": name, "endpoint": RobotClient.MOVE_ENDPOINT,
              "body": {"arm": arm, "wait": True, "x": 450, "y": 100, "z": 350,
                       "rx": 180, "ry": 0, "rz": 180}}
    if after is not None:
        result["after"] = after
    return result


def home(arm):
    return {"name": f"{arm}_home", "endpoint": RobotClient.HOME_ENDPOINT,
            "body": {"arm": arm, "wait": True, "speed": 100}}


class Gateway:
    def __init__(self):
        self.client_id = "tertiary"
        self.priority = 100
        self.owner = None
        self.acquired_at = None
        self.queue = {"state": "idle", "length": 0, "steps": []}
        self.calls = []
        self.replies = []
        self.submit_error = None
        self.submit_hook = None
        self.queue_hook = None
        self.delete_error = None
        self.arm_status = {arm: {"accepted": True, "message": "DONE"} for arm in ("left", "right")}
        self.joints = {arm: {"arm": arm, "joints": list(RobotClient.HOME_JOINTS),
                             "arm_angle": RobotClient.HOME_ARM_ANGLES[arm]} for arm in ("left", "right")}

    def response(self, value, status=200):
        result = Mock(status_code=status, headers={})
        result.json.return_value = copy.deepcopy(value)
        if status >= 400:
            result.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}", response=result)
        self.replies.append(result)
        return result

    def lease(self):
        now = datetime.now(timezone.utc)
        if self.acquired_at is None:
            self.acquired_at = now.isoformat()
        return {"active": True, "client_id": self.client_id, "priority": self.priority,
                "acquired_at": self.acquired_at, "expires_at": (now + timedelta(seconds=5)).isoformat()}

    def post(self, url, **options):
        path = url.removeprefix(API)
        self.calls.append(("POST", path, copy.deepcopy(options.get("json"))))
        if path == "/control/acquire":
            if self.owner and self.owner["priority"] >= self.priority:
                return self.response({"detail": "Control owned"}, 409)
            self.owner = {"active": True, "client_id": self.client_id, "priority": self.priority}
            return self.response({"success": True, "lease": self.lease()})
        if path == "/control/renew":
            if not self.owner or self.owner["client_id"] != self.client_id:
                return self.response({"detail": "Control lost"}, 409)
            return self.response({"success": True, "lease": self.lease()})
        if path == "/control/release":
            if self.owner and self.owner["client_id"] == self.client_id:
                self.owner = None
            return self.response({"success": True, "lease": {"active": False}})
        if path == "/robot/rapid/stop":
            return self.response({"execution_state": "stopped"})
        assert path == "/robot/rws/queue", path
        steps = options["json"]["steps"]
        self.queue = {"state": "done", "owner": self.client_id, "length": len(steps),
                      "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": "finished",
                      "message": "DONE", "cancel_requested": False,
                      "steps": [{"name": step["name"], "type": step["type"], "arm": step["body"]["arm"],
                                 "group": step["group"], "state": "done", "message": "DONE"}
                                for step in steps]}
        if self.submit_hook:
            self.submit_hook(self.queue)
        if self.submit_error:
            raise self.submit_error
        return self.response({"accepted": True}, 202)

    def get(self, url, **options):
        path = url.removeprefix(API)
        self.calls.append(("GET", path, None))
        if path == "/control":
            return self.response(self.owner or {"active": False})
        if path == "/robot/rws/queue":
            if self.queue_hook:
                self.queue_hook(self.queue)
            return self.response(self.queue)
        if path.startswith("/robot/rws/status?arm="):
            return self.response(self.arm_status[path.rsplit("=", 1)[-1]])
        if path.endswith("/joints"):
            return self.response(self.joints[path.split("/")[-2]])
        raise AssertionError(f"Unexpected GET {path}")

    def delete(self, url, **options):
        path = url.removeprefix(API)
        assert path == "/robot/rws/queue"
        self.calls.append(("DELETE", path, None))
        if self.delete_error:
            raise self.delete_error
        if self.queue["state"] == "running":
            self.queue["cancel_requested"] = True
        else:
            self.queue = {"state": "idle", "length": 0, "steps": []}
        return self.response({"accepted": True})

    @property
    def submissions(self):
        return [body for method, path, body in self.calls if method == "POST" and path == "/robot/rws/queue"]


@pytest.fixture
def gateway(monkeypatch):
    fixture = Gateway()
    for method in ("get", "post", "delete"):
        monkeypatch.setattr(requests, method, getattr(fixture, method))
    monkeypatch.setattr("socket.socket.connect", Mock(side_effect=AssertionError("Real network forbidden")))
    return fixture


@pytest.fixture
def client(gateway):
    result = QueueRobotClient(API, {"Authorization": "Bearer test-token"})
    result.QUEUE_POLL = 0.001
    yield result
    result.end_standby()


def running_queue(owner="primary"):
    return {"state": "running", "owner": owner, "length": 1, "step": 0, "cancel_requested": False,
            "started_at": "2026-11-20T00:00:00Z", "steps": [{"name": "inherited", "type": "move",
                "arm": "left", "group": "g0", "state": "running", "message": "BUSY"}]}


def test_shared_smooth_plan_groups_keep_every_barrier_and_arm_order():
    plan = handover_jobs({"x": 500, "y": 100, "z": 70, "diameter": 60, "rz": 0},
                         place={"id": 1, "x": 450, "y": -300, "z": 250, "occupied": False}, home_after_cup=False)
    queue = plan_to_queue_steps(plan)
    grouped = {step["name"]: int(step["group"][1:]) for step in queue}
    arms = {job["name"]: job["body"]["arm"] for job in plan}
    for job in plan:
        for dependency in job["after"]:
            assert grouped[job["name"]] >= grouped[dependency] + (arms[job["name"]] != arms[dependency])
    for arm in ("left", "right"):
        assert [step["name"] for step in queue if step["body"]["arm"] == arm] == [
            job["name"] for job in plan if job["body"]["arm"] == arm]
    assert all("wait" not in step["body"] for step in queue)


@pytest.mark.parametrize("settings,left_departure", [
    ({}, "left_park"),
    ({"right_park_pose": right_via_pose()}, "left_park"),
    ({"home_after_cup": True}, "left_close_return"),
])
def test_queued_handover_preserves_parallel_separation_and_post_turnover_departure(settings, left_departure):
    plan = handover_jobs({"x": 500, "y": 100, "z": 70},
                         place={"id": 1, "x": 450, "y": -300, "z": 250, "occupied": False},
                         **settings)
    queue = plan_to_queue_steps(plan)
    groups = {step["name"]: int(step["group"][1:]) for step in queue}

    # The released arms separate together. The turnover cannot start until
    # BOTH clearances complete; the left's lateral movement must wait for it.
    assert groups["left_clear"] == groups["right_lower"] > groups["left_release"]
    assert groups["right_retreat"] > groups["left_clear"]
    assert groups[left_departure] == groups["right_place"] > groups["right_retreat"]
    if not settings.get("home_after_cup"):
        assert groups["approach_cup"] == groups["right_via"]
        assert all(step["type"] != "home" for step in queue)
    if settings.get("right_park_pose"):
        right_names = [step["name"] for step in queue if step["body"]["arm"] == "right"]
        assert right_names[-3:] == ["right_release", "right_clear", "right_park"]

    # Queue scheduling never changes the tuned motion or selected speed.
    bodies = {job["name"]: {key: value for key, value in job["body"].items() if key != "wait"}
              for job in plan}
    assert all(step["body"] == bodies[step["name"]] for step in queue)
    assert bodies["left_clear"]["speed"] == 100


@pytest.mark.parametrize("source_arm,other_arm", [("left", "right"), ("right", "left")])
def test_cross_arm_fork_does_not_serialize_the_same_arm_continuation(source_arm, other_arm):
    # Same-arm order is implicit here, as it is for callers that only specify
    # cross-arm barriers. Neither continuation may run before release; both
    # must be allowed to start together after it.
    jobs = [move("release", source_arm, after=[]),
            move("continue", source_arm, after=[]),
            move("other", other_arm, after=["release"])]
    queue = plan_to_queue_steps(jobs)
    groups = {step["name"]: int(step["group"][1:]) for step in queue}
    assert groups["continue"] == groups["other"] > groups["release"]


def test_forward_dependency_and_serial_plans_preserve_order():
    parallel = [move("left", after=["right"]), move("right", "right", after=[])]
    steps = plan_to_queue_steps(parallel)
    assert [(step["name"], step["group"]) for step in steps] == [("right", "g0"), ("left", "g1")]
    serial = plan_to_queue_steps([move("a"), move("b", "right"), move("c")])
    assert [step["group"] for step in serial] == ["g0", "g1", "g2"]


@pytest.mark.parametrize("jobs", [[], [move()] * 65, [move(), move()],
    [move("a", after=["b"]), move("b", after=["a"])], [move("a", after=["missing"])],
    [move("a", after=[]), move("b")]])
def test_invalid_queue_graphs_rejected(jobs):
    with pytest.raises(RobotCommandError):
        plan_to_queue_steps(jobs)


def test_one_queue_all_pre_callbacks_before_submission_original_post_callbacks(client, gateway):
    jobs = [move("left", after=[]), move("right", "right", after=[])]
    seen = []
    def before(index, job):
        assert not gateway.submissions
        seen.append(("before", index, job["name"]))
    def after(index, job):
        assert len(gateway.submissions) == 1
        seen.append(("after", index, job["name"]))
    client.execute_jobs(jobs, before_send=before, after_done=after)
    assert seen == [("before", 0, "left"), ("before", 1, "right"), ("after", 0, "left"), ("after", 1, "right")]
    assert len(gateway.submissions) == 1
    assert [call[1] for call in gateway.calls if call[0] == "POST"] == [
        "/control/acquire", "/robot/rws/queue", "/control/release"]
    assert all(response.close.call_count == 1 for response in gateway.replies)


def test_persistent_lease_spans_multiple_queues_and_camera_idle(client, gateway):
    assert client.try_begin_standby()
    client.execute_jobs([move()])
    client.check_lease()
    assert gateway.owner["client_id"] == "tertiary"
    client.execute_jobs([move()])
    assert len(gateway.submissions) == 2
    assert gateway.submissions[0]["steps"][0]["name"] != gateway.submissions[1]["steps"][0]["name"]
    assert [call[1] for call in gateway.calls if call[0] == "POST"].count("/control/acquire") == 1
    assert not any(call[1] == "/control/release" for call in gateway.calls)
    client.end_standby()
    assert gateway.owner is None


@pytest.mark.parametrize("owner,priority", [("primary", 300), ("secondary", 200)])
def test_higher_priority_live_tiers_refuse_lynx_without_motion(client, gateway, owner, priority):
    gateway.owner = {"active": True, "client_id": owner, "priority": priority}
    assert client.try_begin_standby() is False
    assert not gateway.submissions
    # Rejected requests still register this tier with the gateway contender list.
    assert any(call[1] == "/control/acquire" for call in gateway.calls)
    gateway.owner = None
    assert client.try_begin_standby() is True


def test_existing_same_identity_is_not_adopted(client, gateway):
    gateway.owner = {"active": True, "client_id": "tertiary", "priority": 100}
    assert not client.try_begin_standby()
    assert not any(call[0] == "POST" for call in gateway.calls)


@pytest.mark.parametrize("identity,priority", [("primary", 300), ("tertiary", 300), ("operator", 50)])
def test_wrong_role_token_releases_acquisition_before_any_queue(client, gateway, identity, priority):
    gateway.client_id, gateway.priority = identity, priority
    with pytest.raises(RobotCommandError, match="configured standby identity"):
        client.try_begin_standby()
    assert not gateway.submissions and gateway.owner is None


def test_expired_or_lost_lease_never_reacquires_or_cancels(client, gateway):
    assert client.try_begin_standby()
    before = len(gateway.calls)
    client._lease_deadline = 0
    with pytest.raises(RobotCommandError, match="expired"):
        client.execute_jobs([move()])
    assert gateway.calls[before:] == []
    assert not gateway.submissions


def test_renewal_loss_stops_persistent_dispatch(client, gateway):
    assert client.try_begin_standby()
    client._standby_stop.set()
    client._standby_renewal.join()
    gateway.owner = {"active": True, "client_id": "primary", "priority": 300}
    client._renew_after = 0.001
    client._renew_loop(threading.Event())
    with pytest.raises(RobotCommandError, match="renewal failed"):
        client.execute_jobs([move()])
    assert not gateway.submissions
    assert not any(call[0] == "DELETE" for call in gateway.calls)


def test_waits_complete_inherited_queue_before_arm_reads(client, gateway):
    assert client.try_begin_standby()
    gateway.queue = running_queue()
    reads = 0
    def progress(queue):
        nonlocal reads
        reads += 1
        assert not any("/robot/rws/status" in path for _, path, _ in gateway.calls)
        if reads == 3:
            queue["state"] = "done"
            queue["steps"][0].update(state="done", message="DONE")
    gateway.queue_hook = progress
    result = client.wait_for_idle_queue(timeout=1, poll=0.001)
    assert result["state"] == "done" and reads == 3
    assert len([path for _, path, _ in gateway.calls if "/robot/rws/status" in path]) == 2
    assert not gateway.submissions


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_inherited_failed_queue_requires_recovery_without_clear_or_home(client, gateway, state):
    assert client.try_begin_standby()
    gateway.queue = running_queue()
    gateway.queue["state"] = state
    gateway.queue["steps"][0].update(state="failed", message="ERR move failed")
    with pytest.raises(RobotCommandError, match=f"queue is {state}"):
        client.wait_for_idle_queue()
    assert not gateway.submissions and not any(call[0] == "DELETE" for call in gateway.calls)


@pytest.mark.parametrize("result", [{"accepted": False, "message": "ERR move failed"},
                                    {"message": "DONE"}, {"accepted": True, "message": "unexpected"}])
def test_takeover_rejects_unconfirmed_arm_state(client, gateway, result):
    assert client.try_begin_standby()
    gateway.arm_status["left"] = result
    with pytest.raises(RobotCommandError, match="confirmed idle"):
        client.wait_for_idle_queue()
    assert not gateway.submissions


def inspected_failure(client, gateway, *, state="failed"):
    gateway.client_id, gateway.priority = "primary", 300
    assert client.try_begin_standby(expected_client_id="primary", expected_priority=300)
    gateway.queue = running_queue("secondary")
    gateway.queue["state"] = state
    gateway.queue["steps"][0].update(state="failed", message="ERR move failed")
    gateway.arm_status["right"] = {"accepted": False, "message": "ERR move failed"}
    gateway.calls.clear()
    return copy.deepcopy(gateway.queue)


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_explicit_recovery_archives_terminal_failure_and_never_moves(client, gateway, tmp_path, state):
    expected = inspected_failure(client, gateway, state=state)
    result = client.recover_failed_queue(expected_queue=expected, archive_dir=tmp_path, acknowledged=True)
    assert result["cleared"] is True and result["queue_state"] == "idle"
    assert result["acknowledged_arm_errors"] == ["right"]
    evidence = json.loads((tmp_path / result["archive"]).read_text())
    assert evidence["queue"] == expected
    assert evidence["queue"]["state"] == state
    assert evidence["arms"] == gateway.arm_status
    assert evidence["state"] == "recovered" and evidence["clear_requested"] is True
    assert len([call for call in gateway.calls if call[0] == "DELETE"]) == 1
    assert not any(call[0] == "POST" for call in gateway.calls)
    assert all(response.close.called for response in gateway.replies)


def test_inspected_stale_arm_error_allows_next_queue_after_repeated_waits_and_reacquisition(client, gateway, tmp_path):
    expected = inspected_failure(client, gateway)
    client.recover_failed_queue(expected_queue=expected, archive_dir=tmp_path, acknowledged=True)
    assert client.wait_for_idle_queue()["state"] == "idle"
    client.end_standby()
    assert client.try_begin_standby(expected_client_id="primary", expected_priority=300)
    assert client.wait_for_idle_queue()["state"] == "idle"
    assert gateway.arm_status["right"] == {"accepted": False, "message": "ERR move failed"}
    def check_again(*_args):
        assert client.wait_for_idle_queue()["state"] == "idle"
    before = Mock(side_effect=check_again)
    gateway.submit_hook = lambda _queue: assert_no_recovery_permission(client)
    client.execute_jobs([move()], before_send=before)
    before.assert_called_once()
    assert len(gateway.submissions) == 1
    assert client._acknowledged_idle_errors == {}
    with pytest.raises(RobotCommandError, match="confirmed idle"):
        client.wait_for_idle_queue()


def assert_no_recovery_permission(client):
    assert client._acknowledged_idle_errors == {}


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_inspection_never_allows_a_new_failed_queue(client, gateway, tmp_path, state):
    expected = inspected_failure(client, gateway)
    client.recover_failed_queue(expected_queue=expected, archive_dir=tmp_path, acknowledged=True)
    gateway.queue = copy.deepcopy(expected)
    gateway.queue["state"] = state
    gateway.queue["started_at"] = "2026-11-23T00:00:00Z"
    with pytest.raises(RobotCommandError, match=f"queue is {state}"):
        client.wait_for_idle_queue()
    assert client._acknowledged_idle_errors == {}
    assert not gateway.submissions


@pytest.mark.parametrize("acknowledged", [False, None, 1, "true"])
def test_queue_recovery_requires_explicit_boolean_acknowledgement(client, gateway, tmp_path, acknowledged):
    with pytest.raises(RobotCommandError, match="acknowledging queue recovery"):
        client.recover_failed_queue(expected_queue={"state": "idle", "length": 0, "steps": []},
                                    archive_dir=tmp_path, acknowledged=acknowledged)
    assert gateway.calls == [] and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("owner", [None, "tertiary"])
def test_queue_recovery_requires_an_existing_primary_lease(client, gateway, tmp_path, owner):
    if owner:
        assert client.try_begin_standby()
    gateway.calls.clear()
    with pytest.raises(RobotCommandError, match="control lease"):
        client.recover_failed_queue(expected_queue=copy.deepcopy(gateway.queue), archive_dir=tmp_path, acknowledged=True)
    assert gateway.calls == [] and list(tmp_path.iterdir()) == []


def test_queue_recovery_never_takes_the_execution_lock_from_active_work(client, gateway, tmp_path):
    expected = inspected_failure(client, gateway)
    with client._execution_lock, pytest.raises(RobotCommandError, match="execution worker"):
        client.recover_failed_queue(expected_queue=expected, archive_dir=tmp_path, acknowledged=True)
    assert gateway.calls == []


@pytest.mark.parametrize("arm", ["left", "right"])
def test_queue_recovery_refuses_busy_arms_without_deleting(client, gateway, tmp_path, arm):
    expected = inspected_failure(client, gateway)
    gateway.arm_status[arm] = {"accepted": True, "message": "BUSY"}
    with pytest.raises(RobotCommandError, match="still busy"):
        client.recover_failed_queue(expected_queue=expected, archive_dir=tmp_path, acknowledged=True)
    assert not any(call[0] == "DELETE" for call in gateway.calls)
    assert gateway.queue == expected and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("result", [{"accepted": False, "message": "DONE"}, {"message": "DONE"},
                                   {"accepted": True, "message": "ERR move failed"},
                                   {"accepted": False, "message": "ERR different failure"}])
def test_queue_recovery_refuses_unknown_arm_results(client, gateway, tmp_path, result):
    expected = inspected_failure(client, gateway)
    gateway.arm_status["left"] = result
    with pytest.raises(RobotCommandError, match="unconfirmed state"):
        client.recover_failed_queue(expected_queue=expected, archive_dir=tmp_path, acknowledged=True)
    assert not any(call[0] == "DELETE" for call in gateway.calls)


@pytest.mark.parametrize("at_read", [1, 2, 3])
def test_queue_recovery_refuses_changed_queue_even_after_archiving(client, gateway, tmp_path, at_read):
    expected = inspected_failure(client, gateway)
    reads = 0
    def change(queue):
        nonlocal reads
        reads += 1
        if reads == at_read:
            queue["started_at"] = "2026-11-23T00:00:00Z"
    gateway.queue_hook = change
    with pytest.raises(RobotCommandError, match="queue changed"):
        client.recover_failed_queue(expected_queue=expected, archive_dir=tmp_path, acknowledged=True)
    assert not any(call[0] == "DELETE" for call in gateway.calls)
    assert client._acknowledged_idle_errors == {}
    if at_read == 3:
        evidence = json.loads(next(tmp_path.glob("gateway-recovery-*.json")).read_text())
        assert evidence["queue"] == expected and evidence["state"] == "prepared"


@pytest.mark.parametrize("queue_state,step_state", [("running", "running"), ("failed", "running"), ("done", "done")])
def test_recovery_cannot_cancel_running_work_or_clear_success_history(client, gateway, tmp_path, queue_state, step_state):
    expected = inspected_failure(client, gateway)
    expected["state"] = queue_state
    expected["steps"][0].update(state=step_state, message="DONE" if step_state == "done" else "BUSY")
    with pytest.raises(RobotCommandError, match="inspected failed|still running"):
        client.recover_failed_queue(expected_queue=expected, archive_dir=tmp_path, acknowledged=True)
    assert gateway.calls == [] and list(tmp_path.iterdir()) == []


def test_archive_failure_prevents_delete(client, gateway, tmp_path, monkeypatch):
    expected = inspected_failure(client, gateway)
    monkeypatch.setattr(client, "_write_pending", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        client.recover_failed_queue(expected_queue=expected, archive_dir=tmp_path, acknowledged=True)
    assert not any(call[0] == "DELETE" for call in gateway.calls)
    assert client._acknowledged_idle_errors == {}


def test_uncertain_delete_is_never_retried_and_idle_retry_never_deletes(client, gateway, tmp_path, monkeypatch):
    expected = inspected_failure(client, gateway)
    delete = gateway.delete
    def lost_response(url, **options):
        delete(url, **options)
        raise requests.Timeout("clear response lost")
    monkeypatch.setattr(requests, "delete", lost_response)
    with pytest.raises(RobotCommandError, match="not retried"):
        client.recover_failed_queue(expected_queue=expected, archive_dir=tmp_path, acknowledged=True)
    assert gateway.queue["state"] == "idle"
    assert client._acknowledged_idle_errors == {}
    original_evidence = json.loads(next(tmp_path.glob("gateway-recovery-*.json")).read_text())
    assert original_evidence["queue"] == expected and original_evidence["state"] == "prepared"
    result = client.recover_failed_queue(expected_queue=copy.deepcopy(gateway.queue),
                                         archive_dir=tmp_path, acknowledged=True)
    assert result["cleared"] is False and result["queue_state"] == "idle"
    assert len([call for call in gateway.calls if call[0] == "DELETE"]) == 1
    assert len(list(tmp_path.glob("gateway-recovery-*.json"))) == 2
    assert client.wait_for_idle_queue()["state"] == "idle"


def test_nonidle_delete_readback_preserves_block_without_retry(client, gateway, tmp_path, monkeypatch):
    expected = inspected_failure(client, gateway)
    def refused_clear(url, **options):
        gateway.calls.append(("DELETE", "/robot/rws/queue", None))
        return gateway.response({"accepted": True})
    monkeypatch.setattr(requests, "delete", refused_clear)
    with pytest.raises(RobotCommandError, match="did not confirm an idle queue"):
        client.recover_failed_queue(expected_queue=expected, archive_dir=tmp_path, acknowledged=True)
    assert len([call for call in gateway.calls if call[0] == "DELETE"]) == 1
    assert gateway.queue == expected and client._acknowledged_idle_errors == {}


def test_delete_readback_connection_error_never_retries(client, gateway, tmp_path):
    expected = inspected_failure(client, gateway)
    def unavailable(queue):
        if queue["state"] == "idle":
            raise requests.ConnectionError("readback lost")
    gateway.queue_hook = unavailable
    with pytest.raises(RobotCommandError, match="status failed"):
        client.recover_failed_queue(expected_queue=expected, archive_dir=tmp_path, acknowledged=True)
    assert len([call for call in gateway.calls if call[0] == "DELETE"]) == 1
    assert client._acknowledged_idle_errors == {}


def test_stop_invalidates_inspected_arm_error_without_clearing_gateway(client, gateway, tmp_path):
    expected = inspected_failure(client, gateway)
    client.recover_failed_queue(expected_queue=expected, archive_dir=tmp_path, acknowledged=True)
    gateway.calls.clear()
    client.latch_stop()
    assert client._acknowledged_idle_errors == {}
    assert gateway.calls == []


def test_stop_during_recovery_preserves_failed_queue_and_archived_evidence(client, gateway, tmp_path, monkeypatch):
    expected = inspected_failure(client, gateway)
    archive = client._write_pending
    def stop_after_archive(path, record):
        archive(path, record)
        client.latch_stop()
    monkeypatch.setattr(client, "_write_pending", stop_after_archive)
    with pytest.raises(RobotCommandError, match="Software stop requested"):
        client.recover_failed_queue(expected_queue=expected, archive_dir=tmp_path, acknowledged=True)
    assert not any(call[0] == "DELETE" for call in gateway.calls)
    assert gateway.queue == expected and client._acknowledged_idle_errors == {}
    evidence = json.loads(next(tmp_path.glob("gateway-recovery-*.json")).read_text())
    assert evidence["queue"] == expected and evidence["state"] == "prepared"


def test_an_arm_result_change_retires_its_old_error_acknowledgement(client, gateway, tmp_path):
    expected = inspected_failure(client, gateway)
    client.recover_failed_queue(expected_queue=expected, archive_dir=tmp_path, acknowledged=True)
    gateway.arm_status["right"] = {"accepted": True, "message": "DONE"}
    client.wait_for_idle_queue()
    assert client._acknowledged_idle_errors == {}
    gateway.arm_status["right"] = {"accepted": False, "message": "ERR move failed"}
    with pytest.raises(RobotCommandError, match="confirmed idle"):
        client.wait_for_idle_queue()


def test_dry_run_recovery_never_reads_or_archives_gateway(client, gateway, tmp_path):
    preview = QueueRobotClient(API, {}, dry_run=True)
    result = preview.recover_failed_queue(expected_queue=copy.deepcopy(gateway.queue), archive_dir=tmp_path, acknowledged=True)
    assert result["dry_run"] and not result["cleared"]
    assert gateway.calls == [] and list(tmp_path.iterdir()) == []


def test_busy_arm_timeout_does_not_continue(client, gateway):
    assert client.try_begin_standby()
    gateway.arm_status["right"]["message"] = "BUSY"
    with pytest.raises(RobotCommandError, match="Timed out"):
        client.wait_for_idle_queue(timeout=0.003, poll=0.001)
    assert not gateway.submissions


@pytest.mark.parametrize("error", [requests.Timeout("lost response"), requests.ConnectionError("connection lost")])
def test_ambiguous_submission_never_retried_or_cancelled(client, gateway, error):
    gateway.submit_error = error
    after = Mock()
    with pytest.raises(RobotCommandError, match="completion is unknown"):
        client.execute_jobs([move()], after_done=after)
    assert len(gateway.submissions) == 1
    after.assert_not_called()
    assert not any(call[0] == "DELETE" for call in gateway.calls)


@pytest.mark.parametrize("change", [lambda q: q.update(owner="primary"),
    lambda q: q["steps"][0].update(name="other"), lambda q: q["steps"][0].update(group="g99"),
    lambda q: q["steps"][0].update(arm="right"), lambda q: q["steps"][0].update(type="home"),
    lambda q: q.update(state="idle", length=0, steps=[])])
def test_unrelated_queue_cannot_confirm_execution(client, gateway, change):
    gateway.submit_hook = change
    after = Mock()
    with pytest.raises(RobotCommandError, match="no longer matches"):
        client.execute_jobs([move()], after_done=after)
    after.assert_not_called()
    assert len(gateway.submissions) == 1


def test_changed_start_time_cannot_confirm_replaced_queue(client, gateway):
    gateway.submit_hook = lambda q: (q.update(state="running"), q["steps"][0].update(state="running", message="BUSY"))
    reads = 0
    def progress(queue):
        nonlocal reads
        if queue["state"] == "idle":
            return
        reads += 1
        if reads > 1:
            queue["started_at"] = "2026-11-21T00:00:00Z"
    gateway.queue_hook = progress
    with pytest.raises(RobotCommandError, match="identity changed"):
        client.execute_jobs([move()])
    assert len(gateway.submissions) == 1


def test_failed_queue_records_only_confirmed_steps(client, gateway):
    def fail(queue):
        queue.update(state="failed", message="move failed")
        queue["steps"][1].update(state="failed", message="ERR move failed")
        queue["steps"][2].update(state="skipped", message="")
    gateway.submit_hook = fail
    after = []
    with pytest.raises(RobotCommandError, match="queue failed"):
        client.execute_jobs([move("first"), move("second"), move("last")],
                            after_done=lambda index, job: after.append((index, job["name"])))
    assert after == [(0, "first")]
    assert len(gateway.submissions) == 1 and not any(call[0] == "DELETE" for call in gateway.calls)


@pytest.mark.parametrize("change", [lambda q: q["steps"][0].update(message="STARTED"),
    lambda q: q["steps"][0].update(state="running"), lambda q: q.update(length=2),
    lambda q: q.update(started_at="not a timestamp"), lambda q: q.update(state="unknown")])
def test_malformed_completion_never_reported_as_done(client, gateway, change):
    gateway.submit_hook = change
    after = Mock()
    with pytest.raises(RobotCommandError):
        client.execute_jobs([move()], after_done=after)
    after.assert_not_called()


def test_home_done_requires_real_joint_verification(client, gateway):
    gateway.joints["left"]["joints"][0] = 50
    after = Mock()
    with pytest.raises(RobotCommandError, match="home pose was not confirmed"):
        client.execute_jobs([home("left")], after_done=after)
    after.assert_not_called()


def test_both_homes_verify_and_report_completion(client, gateway):
    after = Mock()
    client.execute_jobs([home("right"), home("left")], after_done=after)
    assert after.call_count == 2
    assert len([path for _, path, _ in gateway.calls if path.endswith("/joints")]) == 2


def test_home_followed_by_motion_is_rejected_before_http(client, gateway):
    with pytest.raises(RobotCommandError, match="separate plan"):
        client.execute_jobs([home("left"), move()])
    assert gateway.calls == []


def test_pre_callback_failure_dispatches_nothing(client, gateway):
    before = Mock(side_effect=RuntimeError("journal unavailable"))
    with pytest.raises(RobotCommandError, match="journal unavailable"):
        client.execute_jobs([move()], before_send=before)
    assert not gateway.submissions


def test_post_callback_failure_not_retried(client, gateway):
    after = Mock(side_effect=RuntimeError("disk full"))
    with pytest.raises(RobotCommandError, match="disk full"):
        client.execute_jobs([move()], after_done=after)
    assert len(gateway.submissions) == 1 and after.call_count == 1


def test_delayed_gripper_measurement_is_not_fabricated(client, gateway):
    assert client.read_gripper_position("left") is None
    assert client.read_gripper_position("right") is None
    assert gateway.calls == []


def test_explicit_stop_cancels_before_rapid_stop_and_release(client, gateway):
    assert client.try_begin_standby()
    gateway.queue = running_queue("tertiary")
    start = len(gateway.calls)
    result = client.request_stop()
    assert result["stopped"] is True
    assert [call[:2] for call in gateway.calls[start:] if call[0] != "GET"] == [
        ("DELETE", "/robot/rws/queue"), ("POST", "/robot/rapid/stop"), ("POST", "/control/release")]
    assert client.stop_requested


def test_stop_attempts_rapid_stop_even_if_queue_cancel_unknown(client, gateway):
    assert client.try_begin_standby()
    gateway.queue = running_queue("tertiary")
    gateway.delete_error = requests.Timeout("unknown cancellation")
    with pytest.raises(RobotCommandError, match="RAPID reported stopped"):
        client.request_stop()
    assert any(call[1] == "/robot/rapid/stop" for call in gateway.calls)
    assert len([call for call in gateway.calls if call[0] == "DELETE"]) == 1


def test_stop_does_not_clear_failed_queue_history(client, gateway):
    assert client.try_begin_standby()
    gateway.queue = running_queue("tertiary")
    gateway.queue["state"] = "failed"
    gateway.queue["steps"][0].update(state="failed", message="ERR move failed")
    client.request_stop()
    assert not any(call[0] == "DELETE" for call in gateway.calls)
    assert gateway.queue["state"] == "failed"


def test_dry_run_never_reads_gateway_or_runs_callbacks(gateway):
    client = QueueRobotClient(API, {}, dry_run=True)
    before, after = Mock(), Mock()
    assert client.try_begin_standby()
    client.execute_jobs([move()], before_send=before, after_done=after)
    client.wait_for_idle_queue()
    client.request_stop()
    client.end_standby()
    assert gateway.calls == []
    before.assert_not_called()
    after.assert_not_called()


def test_keyboard_interrupt_does_not_cancel_gateway_queue(client, gateway):
    gateway.submit_hook = lambda q: (q.update(state="running"), q["steps"][0].update(state="running", message="BUSY"))
    def interrupt(queue):
        if queue["state"] == "running":
            raise KeyboardInterrupt()
    gateway.queue_hook = interrupt
    with pytest.raises(KeyboardInterrupt):
        client.execute_jobs([move()])
    assert len(gateway.submissions) == 1 and not any(call[0] == "DELETE" for call in gateway.calls)


def test_queue_endpoint_missing_fails_before_submission(client, gateway, monkeypatch):
    original = gateway.get
    def missing(url, **options):
        if url.endswith("/robot/rws/queue"):
            return gateway.response({"detail": "Not Found"}, 404)
        return original(url, **options)
    monkeypatch.setattr(requests, "get", missing)
    with pytest.raises(RobotCommandError, match="HTTP 404"):
        client.execute_jobs([move()])
    assert not gateway.submissions
    assert not any(call[0] == "DELETE" for call in gateway.calls)


def test_running_queue_timeout_preserves_gateway_work(client, gateway):
    client.QUEUE_TIMEOUT = 0.004
    gateway.submit_hook = lambda q: (q.update(state="running"), q["steps"][0].update(state="running", message="BUSY"))
    with pytest.raises(RobotCommandError, match="completion timed out"):
        client.execute_jobs([move()])
    assert gateway.queue["state"] == "running"
    assert len(gateway.submissions) == 1 and not any(call[0] == "DELETE" for call in gateway.calls)


def test_partial_completions_cannot_regress(client, gateway):
    gateway.submit_hook = lambda q: (q.update(state="running"), q["steps"][1].update(state="running", message="BUSY"))
    reads = 0
    def regress(queue):
        nonlocal reads
        if queue["state"] == "idle":
            return
        reads += 1
        if reads == 2:
            queue["steps"][0].update(state="pending", message="")
    gateway.queue_hook = regress
    after = Mock()
    with pytest.raises(RobotCommandError, match="completion regressed"):
        client.execute_jobs([move("one"), move("two")], after_done=after)
    assert after.call_count == 1


def test_pre_callback_can_adjust_coordinates_but_not_step_identity(client, gateway):
    original = move()
    def adjust(index, job):
        job["body"]["x"] = 460
        return job
    client.execute_jobs([original], before_send=adjust)
    assert gateway.submissions[0]["steps"][0]["body"]["x"] == 460
    assert original["body"]["x"] == 450
    def rename(index, job):
        job["name"] = "renamed"
        return job
    with pytest.raises(RobotCommandError, match="changed step identity"):
        client.execute_jobs([original], before_send=rename)
    assert len(gateway.submissions) == 1


def test_height_validation_happens_before_control_acquisition(client, gateway):
    job = move()
    job["body"]["z"] = 701
    with pytest.raises(RobotCommandError, match="flange Z"):
        client.execute_jobs([job])
    assert gateway.calls == []


def test_queue_poll_failure_does_not_repeat_submission(client, gateway):
    def error_after_submission(queue):
        if queue["state"] != "idle":
            raise requests.ConnectionError("disconnected")
    gateway.queue_hook = error_after_submission
    with pytest.raises(RobotCommandError, match="status failed"):
        client.execute_jobs([move()])
    assert len(gateway.submissions) == 1 and not any(call[0] == "DELETE" for call in gateway.calls)


def test_stop_during_poll_unwinds_and_cancels_queue_once(client, gateway):
    assert client.try_begin_standby()
    gateway.submit_hook = lambda q: (q.update(state="running"), q["steps"][0].update(state="running", message="BUSY"))
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    execute_thread = None
    def block_executor_poll(queue):
        if threading.current_thread() is execute_thread and queue["state"] == "running":
            entered.set()
            assert release.wait(2)
    gateway.queue_hook = block_executor_poll
    errors = []
    def execute():
        try:
            client.execute_jobs([move()])
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()
    execute_thread = threading.Thread(target=execute)
    execute_thread.start()
    try:
        assert entered.wait(2)
        result = client.request_stop()
        assert result["stopped"]
    finally:
        release.set()
        execute_thread.join(2)
    assert finished.is_set() and len(errors) == 1
    assert isinstance(errors[0], RobotCommandError) and "stop requested" in str(errors[0])
    assert len(gateway.submissions) == 1
    assert len([call for call in gateway.calls if call[0] == "DELETE"]) == 1


def test_sustained_standby_renewal_retains_same_acquisition(client, gateway):
    client._renew_after = 0.001
    assert client.try_begin_standby()
    acquired = client._lease_clock_anchor
    # Exercise the inherited renewal path directly, without waiting real seconds.
    reply, headers = client._post("/control/renew")
    client._record_lease(reply, headers)
    assert client._lease_clock_anchor == acquired
    client.check_lease()


def test_duplicate_stop_request_cannot_submit_two_cancels(client, gateway):
    client._stop_call_lock.acquire()
    try:
        with pytest.raises(RobotCommandError, match="already in progress"):
            client.request_stop()
    finally:
        client._stop_call_lock.release()
        client._stop_finished.set()
    assert gateway.calls == []


def test_active_standby_role_with_wrong_priority_is_reported_before_acquire(client, gateway):
    gateway.owner = {"active": True, "client_id": "tertiary", "priority": 300}
    with pytest.raises(RobotCommandError, match="different priority"):
        client.try_begin_standby()
    assert not any(call[0] == "POST" for call in gateway.calls)


def test_right_home_verification_failure_prevents_left_home_submission(client, gateway):
    gateway.joints["right"]["joints"][0] = 50
    before, after = Mock(return_value=None), Mock()
    with pytest.raises(RobotCommandError, match="home pose was not confirmed"):
        client.execute_jobs([home("right"), home("left")], before_send=before, after_done=after)
    assert len(gateway.submissions) == 1
    assert [step["body"]["arm"] for step in gateway.submissions[0]["steps"]] == ["right"]
    assert before.call_count == 1
    after.assert_not_called()
    assert not any(path.endswith("/left/joints") for _, path, _ in gateway.calls)


def test_sequential_home_queues_keep_one_lease_and_original_callback_indices(client, gateway):
    assert client.try_begin_standby()
    events = []
    def before(index, job):
        events.append(("before", index, job["body"]["arm"]))
        if index == 1:
            assert any(path.endswith("/right/joints") for _, path, _ in gateway.calls)
    def after(index, job):
        events.append(("done", index, job["body"]["arm"]))
    client.execute_jobs([home("right"), home("left")], before_send=before, after_done=after)
    assert events == [("before", 0, "right"), ("done", 0, "right"), ("before", 1, "left"), ("done", 1, "left")]
    assert len(gateway.submissions) == 2
    assert all(len(item["steps"]) == 1 for item in gateway.submissions)
    assert sum(path == "/control/acquire" for _, path, _ in gateway.calls) == 1
    assert not any(path == "/control/release" for _, path, _ in gateway.calls)


def test_home_dependency_order_is_preserved_when_split_into_verified_queues(client, gateway):
    left, right = home("left"), home("right")
    left["after"], right["after"] = [right["name"]], []
    callbacks = []
    client.execute_jobs([left, right], after_done=lambda index, job: callbacks.append((index, job["body"]["arm"])))
    assert [item["steps"][0]["body"]["arm"] for item in gateway.submissions] == ["right", "left"]
    assert callbacks == [(1, "right"), (0, "left")]


def test_mixed_home_and_other_arm_cup_plan_is_rejected_before_any_http(client, gateway):
    with pytest.raises(RobotCommandError, match="separate plan"):
        client.execute_jobs([home("right"), move("left_move", "left")])
    assert gateway.calls == []


def test_idempotent_acquire_never_adopts_or_releases_existing_wrong_role_owner(client, gateway, monkeypatch):
    gateway.client_id, gateway.priority = "primary", 300
    gateway.owner = {"active": True, "client_id": "primary", "priority": 300}
    original_post = gateway.post
    def idempotent(url, **options):
        if url.endswith("/control/acquire"):
            gateway.calls.append(("POST", "/control/acquire", None))
            return gateway.response({"success": True, "lease": gateway.lease()})
        return original_post(url, **options)
    monkeypatch.setattr(requests, "post", idempotent)
    with pytest.raises(RobotCommandError, match="already-active control owner"):
        client.try_begin_standby()
    client.end_standby()
    assert gateway.owner["client_id"] == "primary"
    assert client.lease_client_id is None and not client._owned_lease and not client._release_needed
    assert not gateway.submissions
    assert not any(path in {"/control/renew", "/control/release"} for _, path, _ in gateway.calls)


def test_current_gateway_duplicate_owner_conflict_does_not_release_other_process(client, gateway):
    gateway.client_id, gateway.priority = "primary", 300
    gateway.owner = {"active": True, "client_id": "primary", "priority": 300}
    assert client.try_begin_standby() is False
    client.end_standby()
    assert gateway.owner["client_id"] == "primary"
    assert not any(path == "/control/release" for _, path, _ in gateway.calls)
    assert not gateway.submissions


class Coordinator:
    """A durable receipt store independent of the gateway's latest queue."""
    role = "tertiary"

    def __init__(self, gateway):
        self.gateway = gateway
        self.records = {}
        self.calls = []
        self.prepare_hook = None
        self.receipt_hook = None

    def prepare_queue(self, run_id, submission_id, steps):
        self.calls.append(("prepare", run_id, submission_id))
        self.records[submission_id] = {"state": "pending", "queue": None, "steps": copy.deepcopy(steps)}
        if self.prepare_hook:
            self.prepare_hook()
        return {"state": "pending", "queue": None}

    def receipt(self, run_id, submission_id):
        self.calls.append(("receipt", run_id, submission_id))
        if self.receipt_hook:
            result = self.receipt_hook(run_id, submission_id)
            if result is not None:
                return result
        record = self.records[submission_id]
        if record["state"] not in {"done", "failed", "cancelled", "not_submitted"}:
            queue = self.gateway.queue
            if queue.get("steps") and queue["steps"][0]["name"].startswith(f"q{submission_id}_"):
                record.update(state=queue["state"] if queue["state"] != "running" else "pending",
                              queue=copy.deepcopy(queue))
        return {"state": record["state"], "queue": copy.deepcopy(record["queue"])}

    def reject_queue(self, run_id, submission_id):
        self.calls.append(("reject", run_id, submission_id))
        self.records[submission_id] = {"state": "not_submitted", "queue": None}
        return {"state": "not_submitted", "queue": None}


@pytest.fixture
def coordinated(client, gateway, tmp_path):
    coordinator = Coordinator(gateway)
    path = tmp_path / "pending.json"
    client.bind_batch(coordinator, "test-run", path)
    return coordinator, path


def legacy_pending_record(state):
    """Literal pre-update grouping, independent of the current serializer."""
    identity = "1234567890abcdef"
    jobs = [move("release", "left", after=[]),
            move("continue", "left", after=[]),
            move("other", "right", after=["release"])]
    steps = [{"name": f"q{identity}_{index}_{job['name']}", "type": "move",
              "body": {key: value for key, value in job["body"].items() if key != "wait"},
              "group": group}
             for index, (job, group) in enumerate(zip(jobs, ["g0", "g0", "g1"]))]
    queue = {"state": state, "owner": "secondary", "length": 3, "started_at": "earlier",
             "finished_at": "later", "cancel_requested": False,
             "message": "DONE" if state == "done" else "ERR move failed",
             "steps": [{"name": step["name"], "type": step["type"], "arm": step["body"]["arm"],
                        "group": step["group"], "state": "done" if state == "done" or index == 0 else "failed",
                        "message": "DONE" if state == "done" or index == 0 else "ERR move failed"}
                       for index, step in enumerate(steps)]}
    return {"schema_version": 1, "run_id": "old-run", "submission_id": identity,
            "expected_owner": "secondary", "jobs": jobs, "steps": steps, "original_indices": [0, 1, 2],
            "state": state, "dispatch_started": True, "queue": queue, "started_at": "earlier",
            "callbacks_applied": False, "reconciled": False, "rejected_at_coordinator": False,
            "callback_attempted": [], "callback_completed": [], "home_verified": []}


@pytest.mark.parametrize("state", ["done", "failed"])
@pytest.mark.parametrize("explicit_version", [False, True])
def test_old_receipt_grouping_and_failure_evidence_remain_readable_unchanged(tmp_path, state, explicit_version):
    record = legacy_pending_record(state)
    if explicit_version:
        record["grouping_version"] = 1
    path = tmp_path / "old-pending.json"
    path.write_text(json.dumps(record))
    saved = path.read_bytes()
    assert QueueRobotClient._read_pending(path) == record
    assert path.read_bytes() == saved
    assert record["steps"] != plan_to_queue_steps(record["jobs"], name_prefix="q1234567890abcdef_")


@pytest.mark.parametrize("change", [
    lambda record: record["steps"][1].update(group="g99"),
    lambda record: record["steps"][1]["body"].update(x=451),
    lambda record: record.update(grouping_version=2),  # Old groups cannot masquerade as new ones.
    lambda record: record.update(grouping_version=0),
    lambda record: record.update(grouping_version=3),
    lambda record: record.update(grouping_version=True),
    lambda record: record.update(grouping_version="1"),
    lambda record: record.update(grouping_version=None),
])
def test_old_receipt_compatibility_does_not_accept_tampering_or_unknown_grouping(tmp_path, change):
    record = legacy_pending_record("failed")
    change(record)
    path = tmp_path / "old-pending.json"
    path.write_text(json.dumps(record))
    saved = path.read_bytes()
    with pytest.raises(RobotCommandError, match="metadata is invalid"):
        QueueRobotClient._read_pending(path)
    assert path.read_bytes() == saved


def retired_pending_record(state="failed"):
    record = legacy_pending_record(state)
    record["started_at"] = record["queue"]["started_at"] = "2026-11-23T10:00:00Z"
    record["queue"]["finished_at"] = "2026-11-23T10:00:01Z"
    record["reconciled"] = True
    record["inspected_recovery"] = {
        "run_id": record["run_id"], "submission_id": record["submission_id"],
        "acknowledged": True, "acknowledged_by": "primary", "recovered_at": "2026-11-23T10:01:00Z",
        "queue_sha256": hashlib.sha256(json.dumps(record["queue"], allow_nan=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
        "arm_status": {"left": {"accepted": True, "message": "DONE"},
                       "right": {"accepted": False, "message": "ERR move failed"}},
    }
    return record


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_inspected_retired_pending_preserves_failure_and_never_becomes_done(client, gateway, tmp_path, state):
    record = retired_pending_record(state)
    path = tmp_path / "retired-pending.json"
    path.write_text(json.dumps(record))
    saved = path.read_bytes()
    assert client._read_pending(path) == record
    client.bind_batch(Coordinator(gateway), "fresh-batch", path)
    result = client.pending_completion()
    assert result == record
    assert result["state"] == state and result["callbacks_applied"] is False
    assert result["queue"] == record["queue"] and result["reconciled"] is True
    with pytest.raises(RobotCommandError, match="Only this confirmed completion"):
        client.acknowledge_pending(record["submission_id"])
    assert path.read_bytes() == saved and gateway.calls == []


@pytest.mark.parametrize("change", [
    "owner", "queue_arm", "queue_group", "queue_name", "started_at", "state",
    "job", "run_id", "submission_id", "queue_evidence", "inspection_digest",
    "inspection_timestamp", "inspection_ack", "inspection_role", "inspection_arm",
    "dispatch_started", "rejected", "not_reconciled", "callbacks_applied", "fake_done",
])
def test_inspected_pending_rejects_mismatched_identity_or_failure_evidence(tmp_path, change):
    record = retired_pending_record()
    marker = record["inspected_recovery"]
    if change == "owner": record["expected_owner"] = "tertiary"
    if change == "queue_arm": record["queue"]["steps"][0]["arm"] = "right"
    if change == "queue_group": record["queue"]["steps"][0]["group"] = "g99"
    if change == "queue_name": record["queue"]["steps"][0]["name"] += "_other"
    if change in {"queue_arm", "queue_group", "queue_name"}:
        # This remains valid recovery metadata for a different gateway queue;
        # the local saved job identity must reject it independently of its hash.
        marker["queue_sha256"] = hashlib.sha256(json.dumps(record["queue"], allow_nan=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    if change == "started_at": record["started_at"] = "2026-11-23T10:00:05Z"
    if change == "state": record["state"] = "cancelled"
    if change == "job": record["jobs"][0]["body"]["x"] = 451
    if change == "run_id": marker["run_id"] = "another-run"
    if change == "submission_id": marker["submission_id"] = "a" * 16
    if change == "queue_evidence": record["queue"]["message"] = "different failure"
    if change == "inspection_digest": marker["queue_sha256"] = "0" * 64
    if change == "inspection_timestamp": marker["recovered_at"] = "2026-11-23T10:01:00"
    if change == "inspection_ack": marker["acknowledged"] = 1
    if change == "inspection_role": marker["acknowledged_by"] = "secondary"
    if change == "inspection_arm": marker["arm_status"]["left"] = {"accepted": True, "message": "BUSY"}
    if change == "dispatch_started": record["dispatch_started"] = False
    if change == "rejected": record["rejected_at_coordinator"] = True
    if change == "not_reconciled": record["reconciled"] = False
    if change == "callbacks_applied": record["callbacks_applied"] = True
    if change == "fake_done": record["state"] = "done"
    path = tmp_path / "retired-pending.json"
    path.write_text(json.dumps(record))
    saved = path.read_bytes()
    with pytest.raises(RobotCommandError, match="metadata is invalid"):
        QueueRobotClient._read_pending(path)
    assert path.read_bytes() == saved


def test_inspected_arm_error_is_consumed_only_at_coordinated_physical_dispatch(client, gateway, coordinated, tmp_path):
    coordinator, path = coordinated
    coordinator.role = "primary"
    expected = inspected_failure(client, gateway)
    client.recover_failed_queue(expected_queue=expected, archive_dir=tmp_path, acknowledged=True)
    def preparing():
        assert client._acknowledged_idle_errors == {"right": gateway.arm_status["right"]}
        assert client.wait_for_idle_queue()["state"] == "idle"
    coordinator.prepare_hook = preparing
    gateway.submit_hook = lambda _queue: assert_no_recovery_permission(client)
    client.execute_jobs([move()])
    assert len(gateway.submissions) == 1
    assert client._read_pending(path)["state"] == "done"
    assert client._acknowledged_idle_errors == {}


def test_coordinated_plan_is_durable_and_prepared_before_callbacks(client, gateway, coordinated):
    coordinator, path = coordinated
    callbacks = []

    def before(index, job):
        record = json.loads(path.read_text())
        assert record["grouping_version"] == 2
        assert record["jobs"] == [move("first"), move("second")]
        assert record["steps"] == coordinator.records[record["submission_id"]]["steps"]
        assert record["dispatch_started"] is False
        assert not gateway.submissions
        callbacks.append(("before", index))

    client.execute_jobs([move("first"), move("second")], before_send=before,
                        after_done=lambda index, job: callbacks.append(("done", index)))
    assert callbacks == [("before", 0), ("before", 1), ("done", 0), ("done", 1)]
    result = client.pending_completion()
    assert result["state"] == "done" and result["callbacks_applied"]
    assert result["callback_completed"] == ["first", "second"]
    assert len(gateway.submissions) == 1
    assert not any(call[0] == "DELETE" for call in gateway.calls)


def test_coordinated_done_callbacks_survive_lease_loss(client, gateway, coordinated):
    assert client.try_begin_standby()

    def preempt(queue):
        client._owned_lease = False
        client._lease_error = RobotCommandError("higher priority acquired")
        gateway.owner = {"active": True, "client_id": "primary", "priority": 300}

    gateway.submit_hook = preempt
    after = Mock()
    client.execute_jobs([move()], after_done=after)
    after.assert_called_once()
    assert client.pending_completion()["state"] == "done"
    with pytest.raises(ControlUnavailable):
        client.execute_jobs([move("next")])
    assert len(gateway.submissions) == 1


def test_coordinated_receipt_survives_latest_queue_replacement(client, gateway, coordinated):
    coordinator, path = coordinated

    def receipt(run_id, submission_id):
        saved = copy.deepcopy(gateway.queue)
        gateway.queue = running_queue(owner="primary")
        coordinator.receipt_hook = None
        return {"state": "done", "queue": saved}

    coordinator.receipt_hook = receipt
    client.execute_jobs([move()])
    assert client.pending_completion()["state"] == "done"
    assert gateway.queue["owner"] == "primary"
    assert len(gateway.submissions) == 1


def test_coordinated_ambiguous_post_observes_receipt_without_retry(client, gateway, coordinated):
    gateway.submit_error = requests.Timeout("accepted but reply lost")
    client.execute_jobs([move()])
    assert client.pending_completion()["callbacks_applied"]
    assert len(gateway.submissions) == 1


def test_coordinated_503_after_acceptance_observes_receipt_without_retry(client, gateway, coordinated, monkeypatch):
    original = gateway.post

    def accepted_but_unavailable(url, **options):
        reply = original(url, **options)
        if url.endswith("/robot/rws/queue"):
            return gateway.response({"detail": "Temporarily unavailable"}, 503)
        return reply

    monkeypatch.setattr(requests, "post", accepted_but_unavailable)
    client.execute_jobs([move()])
    assert client.pending_completion()["callbacks_applied"]
    assert len(gateway.submissions) == 1
    assert not any(method == "DELETE" for method, _, _ in gateway.calls)


def test_coordinated_transient_receipt_loss_waits_then_completes(client, gateway, coordinated):
    coordinator, _ = coordinated
    reads = 0

    def reconnect(run_id, submission_id):
        nonlocal reads
        reads += 1
        if reads < 3:
            raise requests.ConnectionError("offline")

    coordinator.receipt_hook = reconnect
    client.execute_jobs([move()])
    assert reads == 3 and len(gateway.submissions) == 1


def test_coordinated_wait_can_end_without_stop_or_queue_cancellation(client, gateway, coordinated):
    coordinator, path = coordinated

    def unavailable(run_id, submission_id):
        client.cancel_wait()
        raise requests.ConnectionError("disconnected")

    coordinator.receipt_hook = unavailable
    after = Mock()
    with pytest.raises(QueueWaitCancelled):
        client.execute_jobs([move()], after_done=after)
    assert not client.stop_requested
    after.assert_not_called()
    assert json.loads(path.read_text())["dispatch_started"]
    assert len(gateway.submissions) == 1
    assert not any(method == "DELETE" or endpoint == "/robot/rapid/stop" for method, endpoint, _ in gateway.calls)


def test_coordinated_prepost_lease_loss_is_known_non_submission(client, gateway, coordinated):
    coordinator, path = coordinated

    def preempt():
        client._owned_lease = False
        client._lease_error = RobotCommandError("preempted")

    coordinator.prepare_hook = preempt
    before = Mock()
    with pytest.raises(QueueNotSubmitted) as caught:
        client.execute_jobs([move()], before_send=before)
    assert caught.value.control_unavailable
    before.assert_not_called()
    assert not gateway.submissions
    record = client.pending_completion()
    assert record["state"] == "not_submitted" and record["rejected_at_coordinator"]
    client.acknowledge_pending(record["submission_id"])


@pytest.fixture
def historical_non_submission(client, gateway, tmp_path, monkeypatch):
    """A real archived rejection, followed by a different admitted batch."""
    from failover_coordinator import CoordinatorStore

    identity = "a6531fa498764597"
    jobs = [move()]
    steps = plan_to_queue_steps(jobs, name_prefix=f"q{identity}_")
    store = CoordinatorStore(tmp_path / "coordinator.json", API, "synthetic-credential")
    monkeypatch.setattr(store, "_get", lambda path: gateway.get(API + path).json())
    gateway.owner = {"active": True, "client_id": "tertiary", "priority": 100}
    store.start("primary", "old-run", {})
    store.prepare_queue("tertiary", "old-run", identity, steps)
    store.reject_queue("tertiary", "old-run", identity)
    store.stop("primary", "old-run", "Batch stopped before dispatch")
    store.start("primary", "new-run", {})
    gateway.owner = None
    coordinator = Mock(spec=["role", "receipt", "reject_queue"])
    coordinator.role = "tertiary"
    coordinator.receipt.side_effect = lambda run, submission: store.receipt("tertiary", run, submission)
    coordinator.reject_queue.side_effect = lambda run, submission: store.reject_queue("tertiary", run, submission)
    record = {"schema_version": 1, "grouping_version": 2,
              "run_id": "old-run", "submission_id": identity, "expected_owner": "tertiary",
              "jobs": jobs, "steps": steps, "original_indices": [0],
              "state": "not_submitted", "dispatch_started": False,
              "queue": None, "started_at": None, "callbacks_applied": False,
              "reconciled": False, "rejected_at_coordinator": True,
              "callback_attempted": [], "callback_completed": [], "home_verified": []}
    path = tmp_path / "historical-pending.json"
    client._write_pending(path, record)
    client.bind_batch(coordinator, "new-run", path)
    gateway.calls.clear()
    return coordinator, store, path, record


@pytest.mark.parametrize("reconciled", [False, True])
@pytest.mark.parametrize("dispatch_started", [False, True])
def test_confirmed_non_submission_survives_new_batch_without_rejection_retry(
        client, gateway, historical_non_submission, reconciled, dispatch_started):
    coordinator, store, path, record = historical_non_submission
    record.update(reconciled=reconciled, dispatch_started=dispatch_started)
    client._write_pending(path, record)
    before, shared_before = path.read_bytes(), store.path.read_bytes()
    coordinator.receipt.side_effect = requests.ConnectionError("Coordinator temporarily offline")
    coordinator.reject_queue.side_effect = requests.ConnectionError("Coordinator temporarily offline")

    assert client.pending_completion() == record
    assert path.read_bytes() == before
    client.acknowledge_pending(record["submission_id"])

    assert client._read_pending(path) == {**record, "reconciled": True}
    assert store.path.read_bytes() == shared_before
    coordinator.receipt.assert_not_called()
    coordinator.reject_queue.assert_not_called()
    assert gateway.calls == []


@pytest.mark.parametrize("reconciled", [False, True])
@pytest.mark.parametrize("dispatch_started", [False, True])
def test_historical_non_submission_recovers_lost_confirmation_without_coordinator_mutation(
        client, gateway, historical_non_submission, reconciled, dispatch_started):
    coordinator, store, path, record = historical_non_submission
    record.update(reconciled=reconciled, dispatch_started=dispatch_started,
                  rejected_at_coordinator=False)
    client._write_pending(path, record)
    shared_before = store.path.read_bytes()

    result = client.pending_completion()
    assert result == {**record, "rejected_at_coordinator": True}
    client.acknowledge_pending(record["submission_id"])

    assert client._read_pending(path) == {**result, "reconciled": True}
    coordinator.receipt.assert_called_once_with("old-run", record["submission_id"])
    coordinator.reject_queue.assert_not_called()
    assert store.path.read_bytes() == shared_before
    assert gateway.calls == []


@pytest.mark.parametrize("change", ["queue", "started_at", "callback_attempted", "callback_completed"])
def test_confirmed_non_submission_rejects_local_evidence_of_robot_execution(
        client, gateway, historical_non_submission, change):
    coordinator, store, path, record = historical_non_submission
    if change == "queue":
        record["queue"] = {"state": "idle", "length": 0, "steps": []}
    elif change == "started_at":
        record["started_at"] = "2026-11-23T11:00:00Z"
    else:
        record["callback_attempted"] = [record["jobs"][0]["name"]]
        if change == "callback_completed":
            record["callback_completed"] = list(record["callback_attempted"])
    client._write_pending(path, record)
    before, shared_before = path.read_bytes(), store.path.read_bytes()

    with pytest.raises(RobotCommandError):
        client.pending_completion()
    with pytest.raises(RobotCommandError):
        client.acknowledge_pending(record["submission_id"])

    assert path.read_bytes() == before
    assert store.path.read_bytes() == shared_before
    coordinator.receipt.assert_not_called()
    coordinator.reject_queue.assert_not_called()
    assert gateway.calls == []


@pytest.mark.parametrize("receipt", [
    {"state": "done", "queue": None},
    {"state": "failed", "queue": None},
    {"state": "cancelled", "queue": None},
    {"state": "pending", "queue": None},
    {"state": "unknown", "queue": None},
    {"state": "not_submitted", "queue": {"state": "idle", "length": 0, "steps": []}},
    {"state": "not_submitted"},
    {"queue": None},
    None,
])
def test_historical_non_submission_never_accepts_contradictory_or_incomplete_receipt(
        client, gateway, historical_non_submission, receipt):
    coordinator, store, path, record = historical_non_submission
    record.update(rejected_at_coordinator=False)
    client._write_pending(path, record)
    coordinator.receipt.side_effect = None
    coordinator.receipt.return_value = receipt
    if isinstance(receipt, dict) and receipt.get("state") in {"pending", "unknown"}:
        # These provisional states permit retrying rejection, but cannot by
        # themselves confirm it. Keep that separate confirmation unavailable.
        from failover_coordinator import CoordinatorConflict
        coordinator.reject_queue.side_effect = CoordinatorConflict("Non-submission is not confirmed")
    before, shared_before = path.read_bytes(), store.path.read_bytes()

    # Reconciliation may report the conflicting receipt directly or retain the
    # unconfirmed record, but it may never permit replacement or submit work.
    try:
        result = client.pending_completion()
    except RobotCommandError:
        pass
    else:
        assert result["state"] == "not_submitted" and not result["rejected_at_coordinator"]
    with pytest.raises(RobotCommandError):
        client.acknowledge_pending(record["submission_id"])

    assert path.read_bytes() == before
    assert store.path.read_bytes() == shared_before
    if not isinstance(receipt, dict) or receipt.get("state") not in {"pending", "unknown"}:
        coordinator.reject_queue.assert_not_called()
    assert gateway.calls == []


@pytest.mark.parametrize("state", ["pending", "unknown"])
def test_provisional_receipt_can_recover_exact_archived_rejection_without_mutation(
        client, gateway, historical_non_submission, state):
    coordinator, store, path, record = historical_non_submission
    record.update(rejected_at_coordinator=False)
    client._write_pending(path, record)
    coordinator.receipt.side_effect = None
    coordinator.receipt.return_value = {"state": state, "queue": None}
    shared_before = store.path.read_bytes()

    result = client.pending_completion()
    assert result == {**record, "rejected_at_coordinator": True}
    client.acknowledge_pending(record["submission_id"])

    coordinator.receipt.assert_called_once_with("old-run", record["submission_id"])
    coordinator.reject_queue.assert_called_once_with("old-run", record["submission_id"])
    assert client._read_pending(path) == {**result, "reconciled": True}
    assert store.path.read_bytes() == shared_before
    assert gateway.calls == []


def test_unconfirmed_non_submission_remains_blocked_during_coordinator_outage(
        client, gateway, historical_non_submission):
    coordinator, store, path, record = historical_non_submission
    record.update(rejected_at_coordinator=False)
    client._write_pending(path, record)
    before, shared_before = path.read_bytes(), store.path.read_bytes()
    coordinator.receipt.side_effect = requests.ConnectionError("Coordinator temporarily offline")
    coordinator.reject_queue.side_effect = requests.ConnectionError("Coordinator temporarily offline")

    try:
        result = client.pending_completion()
    except RobotCommandError:
        pass
    else:
        assert result["state"] == "not_submitted" and not result["rejected_at_coordinator"]
    with pytest.raises(RobotCommandError, match="Coordinator has not confirmed"):
        client.acknowledge_pending(record["submission_id"])

    assert path.read_bytes() == before
    assert store.path.read_bytes() == shared_before
    assert gateway.calls == []


def test_non_submission_confirmation_outage_preserves_retry_reason_and_then_recovers(
        client, gateway, historical_non_submission):
    from failover_coordinator import CoordinatorUnavailable
    from failover_workflow import transient

    coordinator, store, path, record = historical_non_submission
    record["rejected_at_coordinator"] = False
    client._write_pending(path, record)
    shared_before = store.path.read_bytes()
    read, reject = coordinator.receipt.side_effect, coordinator.reject_queue.side_effect
    coordinator.receipt.side_effect = CoordinatorUnavailable("Coordinator is temporarily busy")
    coordinator.reject_queue.side_effect = CoordinatorUnavailable("Coordinator is temporarily busy")

    with pytest.raises(RobotCommandError) as failure:
        client.pending_completion()
    assert transient(failure.value)
    assert client._read_pending(path) == record
    with pytest.raises(RobotCommandError, match="Coordinator has not confirmed"):
        client.acknowledge_pending(record["submission_id"])

    coordinator.receipt.side_effect, coordinator.reject_queue.side_effect = read, reject
    restored = client.pending_completion()
    assert restored["rejected_at_coordinator"]
    client.acknowledge_pending(record["submission_id"])
    assert store.path.read_bytes() == shared_before
    assert gateway.calls == []


def test_coordinated_callback_replacement_is_not_sent(client, gateway, coordinated):
    replacement = move()
    replacement["body"]["x"] = 460
    with pytest.raises(QueueNotSubmitted):
        client.execute_jobs([move()], before_send=lambda index, job: replacement)
    assert not gateway.submissions
    assert client.pending_completion()["state"] == "not_submitted"


@pytest.mark.parametrize("code", [400, 403, 409, 422])
def test_coordinated_definite_http_rejection_is_never_retried(client, gateway, coordinated, monkeypatch, code):
    original = gateway.post

    def reject(url, **options):
        if url.endswith("/robot/rws/queue"):
            gateway.calls.append(("POST", "/robot/rws/queue", copy.deepcopy(options["json"])))
            return gateway.response({"detail": "Rejected"}, code)
        return original(url, **options)

    monkeypatch.setattr(requests, "post", reject)
    with pytest.raises(QueueNotSubmitted) as caught:
        client.execute_jobs([move()])
    assert caught.value.http_status == code
    assert client.pending_completion()["state"] == "not_submitted"
    assert len(gateway.submissions) == 1


@pytest.mark.parametrize("change", [lambda q: q.update(owner="primary"),
    lambda q: q["steps"][0].update(name="different"),
    lambda q: q["steps"][0].update(message="STARTED"),
    lambda q: q.update(started_at="invalid")])
def test_coordinated_receipts_require_exact_completion_identity(client, gateway, coordinated, change):
    gateway.submit_hook = change
    coordinator, _ = coordinated
    coordinator.receipt_hook = lambda *_: {"state": "done", "queue": copy.deepcopy(gateway.queue)}
    after = Mock()
    with pytest.raises(RobotCommandError):
        client.execute_jobs([move()], after_done=after)
    after.assert_not_called()
    assert len(gateway.submissions) == 1


def test_coordinated_failed_receipt_records_only_completed_jobs(client, gateway, coordinated):
    def fail(queue):
        queue["state"] = "failed"
        queue["steps"][1].update(state="failed", message="ERR move failed")
    gateway.submit_hook = fail
    after = []
    with pytest.raises(RobotCommandError, match="queue failed"):
        client.execute_jobs([move("first"), move("second")],
                            after_done=lambda index, job: after.append(job["name"]))
    assert after == ["first"]
    record = client.pending_completion()
    assert record["state"] == "failed" and not record["callbacks_applied"]
    with pytest.raises(RobotCommandError, match="Only this confirmed"):
        client.acknowledge_pending(record["submission_id"])


def test_coordinated_restart_reconciles_without_replaying_callbacks(client, gateway, coordinated):
    coordinator, path = coordinated
    after = Mock(side_effect=OSError("local journal write failed"))
    with pytest.raises(RobotCommandError, match="Post-command check"):
        client.execute_jobs([move()], after_done=after)
    restarted = QueueRobotClient(API, {})
    restarted.bind_batch(coordinator, "test-run", path)
    record = restarted.pending_completion()
    assert record["state"] == "done" and not record["callbacks_applied"]
    assert record["callback_attempted"] == ["left_move"]
    restarted.acknowledge_pending(record["submission_id"])
    assert restarted.pending_completion()["reconciled"]
    after.assert_called_once()
    assert len(gateway.submissions) == 1


def test_coordinated_pending_requires_explicit_reconciliation_before_replacement(client, gateway, coordinated):
    with pytest.raises(RobotCommandError):
        client.execute_jobs([move()], after_done=Mock(side_effect=OSError("failed save")))
    with pytest.raises(RobotCommandError, match="Reconcile the previous"):
        client.execute_jobs([move("next")])
    assert len(gateway.submissions) == 1


def test_restart_requires_ack_even_when_previous_callbacks_returned(client, gateway, coordinated):
    coordinator, path = coordinated
    client.execute_jobs([move()])
    restarted = QueueRobotClient(API, {})
    restarted.bind_batch(coordinator, "test-run", path)
    try:
        with pytest.raises(RobotCommandError, match="Reconcile the previous"):
            restarted.execute_jobs([move("next")])
        record = restarted.pending_completion()
        restarted.acknowledge_pending(record["submission_id"])
        restarted.execute_jobs([move("next")])
        assert len(gateway.submissions) == 2
    finally:
        restarted.end_standby()


def test_unchanged_pending_receipt_poll_does_not_rewrite_durable_journal(client, gateway, coordinated, monkeypatch):
    coordinator, path = coordinated
    gateway.submit_hook = lambda queue: (queue.update(state="running"),
        queue["steps"][0].update(state="running", message="BUSY"))
    polls = []
    writes = []
    write = client._write_pending
    def record_write(target, value):
        writes.append(copy.deepcopy(value))
        write(target, value)
    monkeypatch.setattr(client, "_write_pending", record_write)
    def receipt(_run, _submission):
        polls.append(len(writes))
        if len(polls) == 4:
            gateway.queue.update(state="done")
            gateway.queue["steps"][0].update(state="done", message="DONE")
    coordinator.receipt_hook = receipt
    client.execute_jobs([move()])
    assert len(polls) == 4
    assert polls[1] == polls[2] == polls[3]
    assert client._read_pending(path)["state"] == "done"


def test_coordinated_home_still_verifies_right_before_left(client, gateway, coordinated):
    gateway.joints["right"]["joints"][1] = -110
    with pytest.raises(RobotCommandError, match="home pose was not confirmed"):
        client.execute_jobs([home("right"), home("left")])
    assert len(gateway.submissions) == 1
    record = client.pending_completion()
    assert record["state"] == "done" and not record["home_verified"]
    assert not record["callbacks_applied"]


def test_coordinated_home_queues_keep_original_callback_indices(client, gateway, coordinated):
    after = []
    client.execute_jobs([home("right"), home("left")],
                        after_done=lambda index, job: after.append((index, job["name"])))
    assert after == [(0, "right_home"), (1, "left_home")]
    assert len(gateway.submissions) == 2
    assert client.pending_completion()["home_verified"] == ["left_home"]


def test_control_unavailable_never_reclassifies_explicit_stop(client):
    client.latch_stop()
    with pytest.raises(RobotCommandError) as caught:
        client.check_lease()
    assert not isinstance(caught.value, ControlUnavailable)


def test_coordinated_queue_callbacks_preserve_durable_identity_and_completion_order(client, gateway, coordinated):
    coordinator, path = coordinated
    jobs = [move("left", after=["right"]), move("right", "right", after=[])]
    calls = []
    before_send, after_done = Mock(), Mock()

    def before_queue(pairs):
        record = client._read_pending(path)
        assert record["jobs"] == jobs
        assert record["state"] == "prepared" and not record["dispatch_started"]
        assert coordinator.records[record["submission_id"]]["steps"] == record["steps"]
        assert not gateway.submissions
        assert pairs == list(enumerate(jobs))
        calls.append("before")
        return [job for _, job in pairs]

    def after_queue(pairs):
        record = client._read_pending(path)
        assert record["state"] == "done"
        assert record["callback_attempted"] == ["right", "left"]
        assert record["callback_completed"] == [] and not record["callbacks_applied"]
        assert pairs == [(1, jobs[1]), (0, jobs[0])]
        # Completion consumers cannot mutate the durable movement plan.
        pairs[0][1]["body"]["x"] = 499
        calls.append("after")

    assert client.supports_queue_callbacks is True
    client.execute_jobs(jobs, before_send=before_send, after_done=after_done,
                        before_queue=before_queue, after_queue=after_queue)
    assert calls == ["before", "after"]
    before_send.assert_not_called()
    after_done.assert_not_called()
    record = client._read_pending(path)
    assert record["jobs"] == jobs
    assert record["callback_completed"] == ["right", "left"]
    assert record["callbacks_applied"]
    assert len(gateway.submissions) == 1


@pytest.mark.parametrize("aggregate,expected_writes", [(False, 39), (True, 2)])
def test_coordinated_whole_cup_callback_uses_two_durable_completion_writes(
        client, gateway, coordinated, monkeypatch, aggregate, expected_writes):
    jobs = [move(f"step_{index}") for index in range(19)]
    writes = []
    fsync = Mock()
    monkeypatch.setattr("queued_robot_client.os.fsync", fsync)
    original_write = client._write_pending

    def write(path, record):
        before = fsync.call_count
        original_write(path, record)
        if record["state"] == "done" and record["callback_attempted"]:
            writes.append(fsync.call_count - before)

    monkeypatch.setattr(client, "_write_pending", write)
    callback = Mock()
    client.execute_jobs(jobs, after_done=callback, after_queue=callback if aggregate else None)
    assert callback.call_count == (1 if aggregate else 19)
    assert len(writes) == expected_writes
    import os
    assert sum(writes) == expected_writes * (1 if os.name == "nt" else 2)
    assert client.pending_completion()["callbacks_applied"]


@pytest.mark.parametrize("replacement", [None, [], [move("changed")], [move(), move("extra")],
                                         [{**move(), "body": {**move()["body"], "x": 460}}]])
def test_coordinated_before_queue_cannot_change_or_omit_prepared_jobs(client, gateway, coordinated, replacement):
    with pytest.raises(QueueNotSubmitted):
        client.execute_jobs([move()], before_queue=lambda pairs: replacement)
    assert not gateway.submissions
    assert client.pending_completion()["state"] == "not_submitted"


@pytest.mark.parametrize("phase", ["before_callback", "during_callback"])
def test_coordinated_before_queue_checks_lease_around_callback(client, gateway, coordinated, phase):
    coordinator, _ = coordinated

    def preempt():
        client._owned_lease = False
        client._lease_error = RobotCommandError("preempted")

    def callback(pairs):
        preempt()
        return [job for _, job in pairs]

    before = Mock(side_effect=callback)
    if phase == "before_callback":
        coordinator.prepare_hook = preempt
    with pytest.raises(QueueNotSubmitted) as caught:
        client.execute_jobs([move()], before_queue=before)
    assert caught.value.control_unavailable
    assert before.call_count == (0 if phase == "before_callback" else 1)
    assert not gateway.submissions


def test_coordinated_before_queue_stop_prevents_dispatch(client, gateway, coordinated):
    def callback(pairs):
        client.latch_stop()
        return [job for _, job in pairs]

    with pytest.raises(RobotCommandError):
        client.execute_jobs([move()], before_queue=callback)
    assert client.stop_requested and not gateway.submissions
    assert client.pending_completion()["state"] == "not_submitted"


def test_coordinated_after_queue_failure_is_durable_and_never_replayed(client, gateway, coordinated):
    coordinator, path = coordinated
    callback = Mock(side_effect=OSError("journal unavailable"))
    with pytest.raises(RobotCommandError, match="Post-queue check"):
        client.execute_jobs([move("first"), move("second")], after_queue=callback)
    record = client._read_pending(path)
    assert record["callback_attempted"] == ["first", "second"]
    assert record["callback_completed"] == [] and not record["callbacks_applied"]
    with pytest.raises(RobotCommandError, match="Reconcile the previous"):
        client.execute_jobs([move("next")], after_queue=callback)
    restarted = QueueRobotClient(API, {})
    restarted.bind_batch(coordinator, "test-run", path)
    assert restarted.pending_completion()["callback_attempted"] == ["first", "second"]
    callback.assert_called_once()
    assert len(gateway.submissions) == 1


@pytest.mark.parametrize("phase", ["before_attempt", "after_attempt", "during_callback"])
def test_coordinated_after_queue_stop_preserves_incomplete_callback_evidence(
        client, gateway, coordinated, monkeypatch, phase):
    coordinator, path = coordinated
    callback = Mock(side_effect=client.latch_stop if phase == "during_callback" else None)
    if phase == "during_callback":
        callback.side_effect = lambda pairs: client.latch_stop()
    if phase == "before_attempt":
        coordinator.receipt_hook = lambda *_: client.latch_stop()
    if phase == "after_attempt":
        original_write = client._write_pending

        def write(target, record):
            original_write(target, record)
            if record["callback_attempted"]:
                client.latch_stop()

        monkeypatch.setattr(client, "_write_pending", write)
    with pytest.raises(RobotCommandError):
        client.execute_jobs([move()], after_queue=callback)
    record = client._read_pending(path)
    assert record["callback_attempted"] == ([] if phase == "before_attempt" else ["left_move"])
    assert record["callback_completed"] == [] and not record["callbacks_applied"]
    assert callback.call_count == (1 if phase == "during_callback" else 0)
    assert len(gateway.submissions) == 1


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_coordinated_incomplete_receipt_uses_per_job_callbacks(client, gateway, coordinated, state):
    def incomplete(queue):
        queue["state"] = state
        queue["steps"][1].update(state=state, message="ERR move failed" if state == "failed" else "cancelled")

    gateway.submit_hook = incomplete
    before_queue = Mock(side_effect=lambda pairs: [job for _, job in pairs])
    after_queue, after_done = Mock(), Mock()
    with pytest.raises(RobotCommandError, match=f"queue {state}"):
        client.execute_jobs([move("first"), move("second")], before_queue=before_queue,
                            after_queue=after_queue, after_done=after_done)
    before_queue.assert_called_once()
    after_queue.assert_not_called()
    after_done.assert_called_once_with(0, move("first"))
    record = client.pending_completion()
    assert record["callback_completed"] == ["first"] and not record["callbacks_applied"]


def test_coordinated_after_queue_survives_accepted_cup_lease_loss(client, gateway, coordinated):
    assert client.try_begin_standby()

    def preempt(queue):
        client._owned_lease = False
        client._lease_error = RobotCommandError("higher priority acquired")
        gateway.owner = {"active": True, "client_id": "primary", "priority": 300}

    gateway.submit_hook = preempt
    callback = Mock()
    client.execute_jobs([move()], after_queue=callback)
    callback.assert_called_once()
    assert client.pending_completion()["callbacks_applied"]
    assert len(gateway.submissions) == 1


def test_coordinated_home_ignores_queue_callbacks_and_retains_measured_checks(client, gateway, coordinated):
    before_queue, after_queue = Mock(), Mock()
    before_send, after_done = Mock(return_value=None), Mock()
    client.execute_jobs([home("right"), home("left")], before_send=before_send, after_done=after_done,
                        before_queue=before_queue, after_queue=after_queue)
    before_queue.assert_not_called()
    after_queue.assert_not_called()
    assert before_send.call_count == after_done.call_count == 2
    assert client.pending_completion()["home_verified"] == ["left_home"]
    assert [path for method, path, _ in gateway.calls if method == "GET" and path.endswith("/joints")] == [
        "/robot/arms/right/joints", "/robot/arms/left/joints"]


@pytest.mark.parametrize("dry_run", [False, True])
def test_unbound_and_dry_run_ignore_aggregate_callbacks(client, gateway, dry_run):
    client.dry_run = dry_run
    before_queue, after_queue = Mock(), Mock()
    before_send, after_done = Mock(), Mock()
    before_send.return_value = None
    client.execute_jobs([move()], before_send=before_send, after_done=after_done,
                        before_queue=before_queue, after_queue=after_queue)
    before_queue.assert_not_called()
    after_queue.assert_not_called()
    assert before_send.call_count == after_done.call_count == (0 if dry_run else 1)
