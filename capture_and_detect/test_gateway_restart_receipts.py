"""A gateway restart loses its queue, never the coordinator's durable receipt."""

import copy
from unittest.mock import Mock

import pytest

from failover_coordinator import CoordinatorConflict, CoordinatorStore
from test_between_cup_grid import CLEAR, observed_scene
from test_failover_coordinator import FIRST, RUN, SECOND, admit, prepare, queue, setup, steps
from test_failover_workflow import rig, start
from test_queued_robot_client import API, gateway, move


@pytest.mark.parametrize("successor", ["primary", "secondary", "tertiary"])
def test_reopened_done_receipt_survives_idle_gateway_and_only_new_identity_is_pending(setup, monkeypatch, successor):
    store, fake_gateway = setup
    admit(store)
    prepare(store)
    fake_gateway.queue = queue()
    done = store.receipt("primary", RUN, FIRST)
    before = store.path.read_bytes()

    # Restart removes only the gateway's volatile queue and lease. Reopen the
    # coordinator too, so this proof cannot come from its in-memory cache.
    fake_gateway.queue = {"state": "idle", "length": 0, "steps": []}
    fake_gateway.owner = None
    store = CoordinatorStore(store.path, "http://offline.invalid", "synthetic-token")
    monkeypatch.setattr(store, "_get", Mock(side_effect=AssertionError("Durable receipt queried gateway")))
    assert store.receipt(successor, RUN, FIRST) == done
    assert store.path.read_bytes() == before

    fake_gateway.owner = successor
    monkeypatch.setattr(store, "_get", fake_gateway.get)
    if successor == "primary":
        assert store.prepare_queue(successor, RUN, FIRST, steps()) == done
    else:
        with pytest.raises(CoordinatorConflict, match="already been used"):
            store.prepare_queue(successor, RUN, FIRST, steps())
    assert store.path.read_bytes() == before
    assert store.prepare_queue(successor, RUN, SECOND, steps(SECOND))["state"] == "pending"
    assert store.receipt("primary", RUN, FIRST) == done
    assert store.state(successor)["head"]["submission_id"] == SECOND


@pytest.mark.parametrize("phase", ["complete", "stopped"])
def test_idle_gateway_after_restart_does_not_replay_inactive_completed_cup(rig, monkeypatch, phase):
    start(rig)
    rig.robot.try_begin_standby(expected_client_id="primary", expected_priority=300)
    rig.robot.bind_batch(rig.coordinator, "batch-one", rig.runner.pending_path)
    job = move("already_completed_cup")
    rig.robot.execute_jobs([job])
    record = rig.robot._read_pending(rig.runner.pending_path)
    rig.session.recovery.begin({
        "failover": {"run_id": "batch-one", "submission_id": record["submission_id"]},
        "execution": {"completed_steps": [job], "pending_jobs": {}}, "stop_requested": False,
    })
    rig.session.motion_uncertain = True
    if phase == "complete":
        rig.coordinator.finish("batch-one", "no_pickable_cups")
    else:
        rig.coordinator.stop("batch-one", "operator_stop")
    rig.runner._release()
    rig.gateway.queue = {"state": "idle", "length": 0, "steps": []}
    reopened = CoordinatorStore(rig.store.path, API, "synthetic-token")
    monkeypatch.setattr(reopened, "_get", Mock(side_effect=AssertionError("No gateway request expected")))
    rig.coordinator.store = reopened
    rig.gateway.calls.clear()

    rig.runner.resume("batch-one")
    assert rig.gateway.calls == []
    rig.capture.assert_not_called()
    assert not rig.session.recovery.required() and not rig.session.motion_uncertain
    assert rig.coordinator.receipt("batch-one", record["submission_id"])["state"] == "done"
    assert rig.coordinator.state()["run"]["phase"] == phase


def test_active_batch_after_idle_gateway_restart_picks_fresh_cup_once_without_replaying_receipt(rig, monkeypatch):
    start(rig)
    rig.robot.try_begin_standby(expected_client_id="primary", expected_priority=300)
    rig.robot.bind_batch(rig.coordinator, "batch-one", rig.runner.pending_path)
    rig.robot.execute_jobs([move("already_completed_cup")])
    record = rig.robot._read_pending(rig.runner.pending_path)
    receipt = rig.coordinator.receipt("batch-one", record["submission_id"])
    old_submission = copy.deepcopy(rig.gateway.submissions[0])
    rig.runner._release()

    rig.gateway.queue = {"state": "idle", "length": 0, "steps": []}
    rig.gateway.owner = None
    reopened = CoordinatorStore(rig.store.path, API, "synthetic-token")
    monkeypatch.setattr(reopened, "_get", lambda path: rig.gateway.get(API + path).json())
    rig.coordinator.store = reopened
    scans = []
    def capture(_purpose):
        scans.append(None)
        already_picked = any(len(item["steps"]) > 2 for item in rig.gateway.submissions)
        sequence = 20 + len(scans)
        return observed_scene(rig.grid, sequence, [] if already_picked else [(200, 500)], CLEAR), sequence
    rig.capture.side_effect = capture

    rig.runner.resume("batch-one")
    assert rig.coordinator.receipt("batch-one", record["submission_id"]) == receipt
    submissions = rig.gateway.submissions
    assert submissions[0] == old_submission
    assert len(submissions) == 4  # Original completed cup, new cup, right Home, left Home.
    assert len([item for item in submissions if len(item["steps"]) > 2]) == 1
    assert [item["steps"][0]["body"]["arm"] for item in submissions[2:]] == ["right", "left"]
    assert len({item["steps"][0]["name"] for item in submissions}) == 4
    assert rig.coordinator.state()["run"]["phase"] == "complete"
    assert not rig.session.recovery.required()
    assert not any(method == "DELETE" or path == "/robot/rapid/stop" for method, path, _ in rig.gateway.calls)
