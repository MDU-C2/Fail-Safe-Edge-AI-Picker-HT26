"""Continuous shared picking with fake camera, gateway and durable coordinator.

No test opens a network connection or uses production state. The shared fixture
blocks real HTTP/socket access and all observations are built in temporary grids.
"""

from collections import deque
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cup_commands import CupSession
from cup_inventory import SelectionError
from failover_workflow import FailoverRunner
from handover_plan import right_via_pose
from queued_robot_client import ControlUnavailable, QueueRobotClient
from recovery_journal import RecoveryJournal
from test_between_cup_grid import CLEAR, FULL, observed_scene
from test_failover_workflow import LocalCoordinator, monitor_polls, rig, start
from test_queued_robot_client import API, gateway
from test_support_grid import grid_fixture


FIRST = [(100, 450)]
NEW_CUP = [(200, 500)]


def continuous(rig):
    rig.session._continuous_batch = True
    rig.runner._configured_continuous = True


def homes(rig):
    return [item["steps"][0]["body"]["arm"] for item in rig.gateway.submissions
            if len(item["steps"]) == 1 and item["steps"][0]["type"] == "home"]


def transfers(rig):
    return [item for item in rig.gateway.submissions if len(item["steps"]) > 2]


def script(rig, frames):
    """Build observations after the preceding queue has updated grid state."""
    remaining = deque(frames)
    captures, waits = [], []

    def capture(purpose):
        assert remaining, "Unexpected camera check after the scripted final wait"
        sources, cells = remaining.popleft()
        captures.append({"purpose": purpose, "homes": list(homes(rig)),
                         "phase": (rig.coordinator.state().get("run") or {}).get("phase")})
        sequence = 10 + len(captures)
        return observed_scene(rig.grid, sequence, sources, cells), sequence

    def wait():
        waits.append({"state": rig.session.batch_progress["state"],
                      "phase": rig.coordinator.state()["run"]["phase"],
                      "homes": list(homes(rig)), "transfers": len(transfers(rig))})
        if not remaining:
            rig.session.finish_batch_when_parked()

    rig.capture.side_effect = capture
    rig.session._wait_for_grid = Mock(side_effect=wait)
    return SimpleNamespace(captures=captures, waits=waits, remaining=remaining)


def watch_once(rig):
    continuous(rig)
    start(rig)
    evidence = script(rig, [([], FULL)])
    rig.runner.resume("batch-one")
    assert rig.coordinator.state()["run"]["phase"] == "watching"
    assert homes(rig) == ["right", "left"]
    assert not evidence.remaining


@contextmanager
def successor(rig, directory, *, role="secondary", continuous_default=False):
    """A fresh device with its own model/session state and the shared store."""
    priority = {"primary": 300, "secondary": 200, "tertiary": 100}[role]
    rig.gateway.client_id, rig.gateway.priority, rig.gateway.owner = role, priority, None
    grid = grid_fixture(directory)
    capture = Mock()
    coordinator = LocalCoordinator(rig.store, role)
    robot = QueueRobotClient(API, {})
    robot.QUEUE_POLL = 0.001
    session = CupSession(capture, Mock(), robot, "cal-1",
                         recovery=RecoveryJournal(directory / "recovery.json"), placing_grid=grid,
                         between_cup_check=True, between_cup_grid_check=True,
                         continuous_batch=continuous_default, right_park_pose=right_via_pose())
    runner = FailoverRunner(session, coordinator, role=role, state_dir=directory, output=Mock())
    worker = SimpleNamespace(session=session, robot=robot, runner=runner, capture=capture,
                             grid=grid, coordinator=coordinator, gateway=rig.gateway, store=rig.store)
    try:
        yield worker
    finally:
        runner.close()
        robot.end_standby()


def test_two_cycles_refresh_new_cups_and_grid_release_without_new_confirmation(rig):
    continuous(rig)
    evidence = script(rig, [
        (FIRST, CLEAR), (FIRST, CLEAR),  # Preview, then the admitted fresh scan.
        ([], FULL), ([], FULL),
        (NEW_CUP, FULL), (NEW_CUP, {**FULL, 2: "clear"}),
        ([], FULL), ([], FULL),
    ])
    preview = rig.session.scan()
    jobs = rig.runner.execute_primary("goto", preview["capture"], [], None)

    state = rig.coordinator.state()
    assert state["run"]["phase"] == "watching"
    assert state["run"]["settings"]["continuous_batch"] is True
    assert state["run"]["confirmed_homes"] == ["right", "left"]
    assert len(transfers(rig)) == 2
    assert homes(rig) == ["right", "left", "right", "left"]
    assert rig.session.batch_progress["home_cycles"] == 2
    assert rig.session.batch_progress["completion_reason"] == "session_closed"
    assert [wait["state"] for wait in evidence.waits] == [
        "waiting_for_cups", "waiting_for_cups", "waiting_for_grid",
        "waiting_for_cups", "waiting_for_cups"]
    assert [len(item["homes"]) for item in evidence.captures] == [0, 0, 0, 2, 2, 2, 2, 4]
    assert [job["body"]["points"][-1]["x"] for job in jobs if job["name"] == "approach_cup"] == [450, 500]
    assert [cup["execution"]["placement"]["id"] for cup in rig.session.inventory["cups"]
            if cup.get("status") == "placed"] == [1, 2]
    assert not evidence.remaining
    assert not rig.session.recovery.required() and not rig.session.motion_uncertain
    assert not any(method == "DELETE" or path == "/robot/rapid/stop"
                   for method, path, _ in rig.gateway.calls)


def test_repeated_empty_frames_keep_same_home_proof_and_do_not_finish_session(rig):
    continuous(rig)
    start(rig)
    evidence = script(rig, [([], FULL), ([], FULL), ([], FULL)])
    rig.runner.resume("batch-one")

    assert homes(rig) == ["right", "left"]
    assert transfers(rig) == []
    assert [capture["homes"] for capture in evidence.captures] == [[], ["right", "left"], ["right", "left"]]
    assert [wait["phase"] for wait in evidence.waits] == ["watching"] * 3
    assert rig.coordinator.state()["run"]["confirmed_homes"] == ["right", "left"]
    assert rig.session.batch_progress["arms_home"] is True


def test_full_grid_waits_with_cup_present_and_never_homes_before_placement(rig):
    continuous(rig)
    start(rig)
    evidence = script(rig, [(FIRST, FULL), (FIRST, FULL),
                            (FIRST, {**FULL, 3: "clear"}), ([], FULL)])
    rig.runner.resume("batch-one")

    assert [wait["state"] for wait in evidence.waits] == [
        "waiting_for_grid", "waiting_for_grid", "waiting_for_cups"]
    assert all(wait["homes"] == [] and wait["transfers"] == 0 for wait in evidence.waits[:2])
    assert [capture["homes"] for capture in evidence.captures] == [[], [], [], []]
    assert len(transfers(rig)) == 1
    assert homes(rig) == ["right", "left"]
    assert rig.coordinator.state()["run"]["phase"] == "watching"


@pytest.mark.parametrize("role", ["secondary", "tertiary"])
def test_watching_takeover_scans_fresh_and_inherits_continuous_setting(rig, tmp_path, role):
    watch_once(rig)
    with successor(rig, tmp_path / role, role=role) as worker:
        evidence = script(worker, [([], FULL), (NEW_CUP, {**FULL, 1: "clear"}), ([], FULL)])
        worker.runner.resume("batch-one")
        assert evidence.captures[0] == {"purpose": "scan", "homes": ["right", "left"], "phase": "watching"}
        assert evidence.waits[0]["state"] == "waiting_for_cups"
        assert evidence.waits[0]["homes"] == ["right", "left"]
        assert worker.session.batch_progress["continuous"] is True
        assert worker.session.batch_progress["home_cycles"] == 1
        assert worker.session.continuous_batch is False  # Its standalone default is restored.
        assert len(transfers(worker)) == 1
        assert homes(worker) == ["right", "left", "right", "left"]
        assert worker.coordinator.state()["run"]["phase"] == "watching"


def test_finalizing_takeover_finishes_only_missing_home_before_watching(rig, tmp_path, monkeypatch):
    continuous(rig)
    start(rig, phase="finalizing")
    confirm = rig.coordinator.confirm_home

    def disconnect_after_right(*args, **kwargs):
        result = confirm(*args, **kwargs)
        if args[-1] == "right":
            raise ControlUnavailable("Owner disconnected after measured right Home")
        return result

    monkeypatch.setattr(rig.coordinator, "confirm_home", disconnect_after_right)
    monkeypatch.setattr(rig.runner._closed, "wait", lambda _delay: rig.runner._closed.set())
    rig.runner.resume("batch-one")
    assert homes(rig) == ["right"]
    rig.capture.assert_not_called()
    assert rig.coordinator.state()["run"]["confirmed_homes"] == ["right"]

    with successor(rig, tmp_path / "after-home-crash") as worker:
        evidence = script(worker, [([], FULL), ([], FULL)])
        worker.runner.resume("batch-one")
        assert evidence.captures[0] == {"purpose": "scan", "homes": ["right", "left"], "phase": "watching"}
        assert homes(worker) == ["right", "left"]
        assert transfers(worker) == []
        assert worker.coordinator.state()["run"]["phase"] == "watching"
        assert not worker.session.recovery.required()


def test_old_finite_run_stays_finite_on_a_continuous_primary(rig):
    start(rig)  # Old settings intentionally omit continuous_batch.
    continuous(rig)
    evidence = script(rig, [(FIRST, CLEAR), ([], FULL)])
    rig.runner.resume("batch-one")

    assert rig.coordinator.state()["run"]["phase"] == "complete"
    assert rig.session.batch_progress["continuous"] is False
    assert rig.session.continuous_batch is True
    assert homes(rig) == ["right", "left"]
    assert len(transfers(rig)) == 1
    assert len(evidence.captures) == 2
    rig.session._wait_for_grid.assert_not_called()


@pytest.mark.parametrize("acknowledge", [False, True])
def test_stopped_watching_run_does_not_resume_after_local_recovery(rig, monkeypatch, acknowledge):
    watch_once(rig)
    rig.runner.stop_batch()
    assert rig.session.request_stop()["stopped"] is True
    if acknowledge:
        rig.session.acknowledge_recovery()
        assert not rig.session.stop_requested
        assert rig.session.scan_usable is False
    rig.capture.reset_mock()
    rig.gateway.calls.clear()
    callback = Mock()
    monitor_polls(rig, monkeypatch, callback)

    callback.assert_not_called()
    rig.capture.assert_not_called()
    assert rig.gateway.calls == []
    assert rig.coordinator.state()["run"]["phase"] == "stopped"


def test_continuous_start_requires_coordinator_capability_before_admission(rig, monkeypatch):
    continuous(rig)
    state = rig.coordinator.state
    monkeypatch.setattr(rig.coordinator, "state", lambda: {**state(), "capabilities": []})
    with pytest.raises(SelectionError, match="continuous-picking update"):
        rig.runner.execute_primary("goto", "preview", [], None)
    assert state()["run"] is None
    assert rig.gateway.calls == []
    rig.capture.assert_not_called()


def test_stop_while_watching_prevents_the_next_camera_check_and_admission(rig):
    continuous(rig)
    start(rig)
    evidence = script(rig, [([], FULL), (NEW_CUP, CLEAR)])

    def stop_at_home():
        assert rig.coordinator.state()["run"]["phase"] == "watching"
        assert homes(rig) == ["right", "left"]
        rig.runner.stop_batch()
        assert rig.session.request_stop()["stopped"] is True

    rig.session._wait_for_grid.side_effect = stop_at_home
    with pytest.raises(SelectionError, match="Software stop requested"):
        rig.runner.resume("batch-one")
    assert len(evidence.captures) == 1
    assert len(evidence.remaining) == 1
    assert homes(rig) == ["right", "left"]
    assert transfers(rig) == []
    assert rig.coordinator.state()["run"]["phase"] == "stopped"
    assert rig.session.stop_requested and rig.session.recovery.required()


def test_status_monitor_can_schedule_watching_takeover_without_owning_robot(rig, monkeypatch):
    watch_once(rig)
    rig.capture.reset_mock()
    rig.gateway.calls.clear()
    callback = Mock()
    monitor_polls(rig, monkeypatch, callback)
    assert callback.call_count == 4
    assert all(call.args == ("batch-one",) for call in callback.call_args_list)
    assert rig.gateway.calls == []
    rig.capture.assert_not_called()


def test_old_home_receipt_on_reconnecting_primary_cannot_confirm_a_later_cycle(rig, tmp_path):
    watch_once(rig)
    old_pending = rig.runner.pending_path.read_bytes()
    first_homes = rig.coordinator.state()["run"]["home_receipts"].copy()
    with successor(rig, tmp_path / "second-cycle") as worker:
        script(worker, [(NEW_CUP, CLEAR), ([], FULL)])
        worker.runner.resume("batch-one")
        second_homes = worker.coordinator.state()["run"]["home_receipts"].copy()
        assert first_homes != second_homes

    with successor(rig, tmp_path / "reconnected-primary", role="primary", continuous_default=True) as worker:
        worker.runner.pending_path.write_bytes(old_pending)
        evidence = script(worker, [([], FULL)])
        worker.runner.resume("batch-one")
        assert len(evidence.captures) == 1
        assert homes(worker) == ["right", "left", "right", "left"]
        assert worker.coordinator.state()["run"]["home_receipts"] == second_homes
        assert worker.coordinator.state()["run"]["phase"] == "watching"
        assert not worker.session.recovery.required()
