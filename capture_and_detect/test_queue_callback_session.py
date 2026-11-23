"""Queue-wide persistence through real cup, failover and recovery sessions."""

import copy
import json
from types import SimpleNamespace

import pytest
import requests

from queued_robot_client import plan_to_queue_steps
from robot_client import RobotCommandError
from test_between_cup_grid import CLEAR, observed_scene
from test_failover_workflow import rig, start
from test_queued_robot_client import gateway


def observe_callbacks(rig, monkeypatch, *, fault=None):
    """Count only writes made inside cup callbacks, excluding Home/finalization."""
    counts = {phase: {"save": 0, "begin": 0} for phase in ("before", "after")}
    state = SimpleNamespace(phase=None, counts=counts, dispatched=None, completed=None, fault=False)
    execute = rig.robot.execute_jobs
    save, begin = rig.session.save, rig.session.recovery.begin
    post = requests.post

    def save_inventory(inventory):
        if state.phase:
            counts[state.phase]["save"] += 1
            if fault and fault[0] == state.phase and not state.fault:
                state.fault = True
                if fault[1] == "persist":
                    raise OSError("Injected inventory persistence failure")
                rig.session.latch_stop()
                rig.coordinator.stop("batch-one", "Operator requested Stop.")
        return save(inventory)

    def begin_recovery(context):
        if state.phase:
            counts[state.phase]["begin"] += 1
        return begin(context)

    def in_phase(callback, phase):
        def invoke(*args):
            state.phase = phase
            try:
                return callback(*args)
            finally:
                state.phase = None
        return invoke

    def execute_jobs(jobs, **callbacks):
        cup = all(job["endpoint"] != rig.robot.HOME_ENDPOINT for job in jobs)
        if cup:
            for key, phase in (("before_send", "before"), ("before_queue", "before"),
                               ("after_done", "after"), ("after_queue", "after")):
                if callbacks.get(key) is not None:
                    callbacks[key] = in_phase(callbacks[key], phase)
        result = execute(jobs, **callbacks)
        if cup:
            state.completed = {
                "pending": rig.robot._read_pending(rig.runner.pending_path),
                "recovery": json.loads(rig.session.recovery.path.read_text()),
            }
        return result

    def before_physical_dispatch(url, **options):
        steps = options.get("json", {}).get("steps", [])
        if url.endswith("/robot/rws/queue") and len(steps) > 1:
            pending = rig.robot._read_pending(rig.runner.pending_path)
            journal = json.loads(rig.session.recovery.path.read_text())
            context = journal["context"]
            names = [job["name"] for job in pending["jobs"]]
            assert len(names) == 19
            assert list(context["execution"]["pending_jobs"]) == names
            assert context["execution"]["pending_steps"] == names
            assert context["execution"]["completed_steps"] == []
            assert context["failover"] == {
                "run_id": "batch-one", "submission_id": pending["submission_id"]}
            assert journal["recovery_required"] and pending["dispatch_started"]
            assert pending["steps"] == steps and pending["state"] == "pending"
            assert pending["callback_attempted"] == [] and pending["callback_completed"] == []
            assert not rig.gateway.submissions
            state.dispatched = copy.deepcopy(pending)
        return post(url, **options)

    monkeypatch.setattr(rig.session, "save", save_inventory)
    monkeypatch.setattr(rig.session.recovery, "begin", begin_recovery)
    monkeypatch.setattr(rig.robot, "execute_jobs", execute_jobs)
    monkeypatch.setattr(requests, "post", before_physical_dispatch)
    rig.capture.side_effect = [
        (observed_scene(rig.grid, 10, [(100, 450)], CLEAR), 99),
        (observed_scene(rig.grid, 11, [], CLEAR), 100),
    ]
    return state


@pytest.mark.parametrize("aggregate,callback_writes", [(False, 19), (True, 1)])
def test_whole_cup_persistence_keeps_identical_plan_and_complete_journal(
        rig, monkeypatch, aggregate, callback_writes):
    rig.robot.supports_queue_callbacks = aggregate
    observed = observe_callbacks(rig, monkeypatch)
    start(rig)
    rig.runner.resume("batch-one")

    assert observed.counts == {
        "before": {"save": callback_writes, "begin": callback_writes},
        "after": {"save": callback_writes, "begin": callback_writes},
    }
    pending = observed.completed["pending"]
    names = [job["name"] for job in pending["jobs"]]
    assert len(names) == 19 and names[-1] == "right_park"
    assert pending["jobs"] == observed.dispatched["jobs"]
    assert rig.gateway.submissions[0]["steps"] == plan_to_queue_steps(
        pending["jobs"], name_prefix=f"q{pending['submission_id']}_")
    assert pending["state"] == "done" and pending["callbacks_applied"]
    assert set(pending["callback_attempted"]) == set(pending["callback_completed"]) == set(names)
    execution = observed.completed["recovery"]["context"]["execution"]
    assert execution["pending_jobs"] == {} and execution["pending_steps"] == []
    assert {job["name"] for job in execution["completed_steps"]} == set(names)
    assert len(execution["completed_steps"]) == 19
    assert rig.coordinator.state()["run"]["phase"] == "complete"
    assert rig.coordinator.state()["run"]["confirmed_homes"] == ["right", "left"]
    assert [len(item["steps"]) for item in rig.gateway.submissions] == [19, 1, 1]
    assert rig.capture.call_count == 2
    assert not rig.session.recovery.required() and not rig.session.motion_uncertain
    assert not any(method == "DELETE" or path == "/robot/rapid/stop"
                   for method, path, _ in rig.gateway.calls)


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("failure", ["persist", "stop"])
def test_aggregate_persistence_failure_or_stop_preserves_recovery_and_prevents_next_cup(
        rig, monkeypatch, phase, failure):
    observed = observe_callbacks(rig, monkeypatch, fault=(phase, failure))
    start(rig)
    with pytest.raises((RobotCommandError, OSError)):
        rig.runner.resume("batch-one")

    assert observed.fault and observed.completed is None
    assert rig.coordinator.state()["run"]["phase"] == ("stopped" if failure == "stop" else "blocked")
    assert rig.session.recovery.required() and rig.session.motion_uncertain
    assert rig.capture.call_count == 1
    assert len(rig.gateway.submissions) == (0 if phase == "before" else 1)
    pending = rig.robot._read_pending(rig.runner.pending_path)
    assert pending["state"] == ("not_submitted" if phase == "before" else "done")
    assert pending["callback_completed"] == [] and not pending["callbacks_applied"]
    assert len(pending["callback_attempted"]) == (0 if phase == "before" else 19)
    context = json.loads(rig.session.recovery.path.read_text())["context"]
    assert context["execution"]["state"] == "interrupted"
    assert context["failover"]["submission_id"] == pending["submission_id"]
    assert context["stop_requested"] is (failure == "stop")
