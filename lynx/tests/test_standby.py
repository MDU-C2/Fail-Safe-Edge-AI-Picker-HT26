"""Native finite standby orchestration with offline camera and robot doubles."""

import copy
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from cup_commands import CupSession
from cup_inventory import SelectionError
from handover_plan import right_camera_park_jobs, right_via_pose
from lynx import standby
from lynx.picker import arguments
from lynx.runtime_lock import NativeRuntimeLock
from recovery_journal import RecoveryJournal
from robot_client import RobotClient, RobotCommandError
from test_between_cup_grid import observed_scene, CLEAR, FULL
from test_cup_selection import RobotDouble
from test_support_grid import grid_fixture


def completed_queue(*, parked=False, prefix=""):
    names = ["left_release", "left_park", "right_release", "right_clear"]
    if parked:
        names.append("right_park")
    return {"state": "done", "steps": [{"name": prefix + name, "state": "done"} for name in names]}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    robot = RobotDouble()
    robot.try_begin_standby = Mock(return_value=True)
    robot.wait_for_idle_queue = Mock(return_value={"state": "idle", "steps": []})
    robot.check_lease = Mock()
    robot.end_standby = Mock()
    robot.request_stop = Mock()
    robot.queue_clear = Mock()
    session = SimpleNamespace(robot=robot, between_cup_grid_check=True, continuous_batch=False,
                              motion_uncertain=False, recovery=RecoveryJournal(tmp_path / "recovery.json"),
                              speed=250, phase_speeds=None, batch_progress={"completion_reason": "workspace_empty"},
                              scan=Mock(return_value={"capture": "fresh"}), execute_observed_batch=Mock(return_value=[]))
    def finish_confirmed():
        session.recovery.clear()
        session.motion_uncertain = False
    session._finish_confirmed_motion = Mock(side_effect=finish_confirmed)
    positions = {"left": 25.0, "right": 23.3}
    telemetry = Mock(side_effect=lambda _robot, arm: positions[arm])
    monkeypatch.setattr(RobotClient, "read_gripper_position", telemetry)
    output, sleep = Mock(), Mock()
    return SimpleNamespace(session=session, robot=robot, positions=positions, telemetry=telemetry,
                           output=output, sleep=sleep)


def run(rig):
    return standby.run_standby(rig.session, output=rig.output, sleep=rig.sleep)


def test_waits_for_cloud_then_pi_then_executes_only_one_finite_batch(rig):
    rig.robot.try_begin_standby.side_effect = [False, False, True]
    expected = [object()]
    rig.session.execute_observed_batch.return_value = expected
    assert run(rig) is expected
    assert rig.robot.try_begin_standby.call_args_list == [
        call(expected_client_id="tertiary", expected_priority=100)] * 3
    assert rig.sleep.call_args_list == [call(1.0), call(1.0)]
    rig.session.scan.assert_called_once_with()
    rig.session.execute_observed_batch.assert_called_once_with(capture_id="fresh")
    rig.robot.end_standby.assert_called_once_with()
    assert not rig.robot.jobs
    rig.robot.request_stop.assert_not_called()
    rig.robot.queue_clear.assert_not_called()
    assert "batch is finished" in rig.output.call_args.args[0]


@pytest.mark.parametrize("state", ["failed", "cancelled", "unknown"])
def test_failed_or_unknown_inherited_queue_prevents_capture_parking_and_clear(rig, state):
    rig.robot.wait_for_idle_queue.side_effect = RobotCommandError(f"queue {state}")
    with pytest.raises(RobotCommandError, match=state):
        run(rig)
    assert not rig.robot.jobs
    rig.session.scan.assert_not_called()
    rig.session.execute_observed_batch.assert_not_called()
    rig.telemetry.assert_not_called()
    rig.robot.queue_clear.assert_not_called()
    rig.robot.request_stop.assert_not_called()
    rig.robot.end_standby.assert_called_once_with()


@pytest.mark.parametrize("position", [None, 1.1, 10.0, 20.0, 22.9])
def test_unknown_or_potentially_holding_gripper_blocks_takeover(rig, position):
    rig.positions["right"] = position
    rig.robot.wait_for_idle_queue.return_value = completed_queue()
    with pytest.raises(RobotCommandError, match="gripper may hold"):
        run(rig)
    assert not rig.robot.jobs
    rig.session.scan.assert_not_called()
    rig.session.execute_observed_batch.assert_not_called()
    rig.robot.end_standby.assert_called_once_with()


@pytest.mark.parametrize("position", [0.0, 1.0, 23.0, 23.3, 25.0])
def test_empty_or_fully_open_grippers_allow_finite_takeover(rig, position):
    rig.positions.update(left=position, right=position)
    run(rig)
    rig.session.scan.assert_called_once_with()
    assert rig.telemetry.call_args_list == [call(rig.robot, "left"), call(rig.robot, "right")]


@pytest.mark.parametrize("prefix", ["", "q0123456789abcdef_17_"])
def test_completed_inherited_cup_finishes_exact_right_park_before_scan_with_journal(rig, prefix):
    rig.robot.wait_for_idle_queue.return_value = completed_queue(prefix=prefix)
    expected = right_camera_park_jobs(speed=rig.session.speed, phase_speeds=rig.session.phase_speeds)
    observations = []
    def after_move(_count):
        assert rig.session.motion_uncertain
        assert rig.session.recovery.required()
        observations.append(copy.deepcopy(rig.robot.jobs[-1]))
    rig.robot.after_move = after_move
    def scan():
        assert rig.robot.jobs == expected
        assert not rig.session.recovery.required() and not rig.session.motion_uncertain
        return {"capture": "fresh"}
    rig.session.scan.side_effect = scan
    run(rig)
    assert observations == expected
    assert not any(job["endpoint"] == RobotClient.HOME_ENDPOINT for job in rig.robot.jobs)


def test_already_parked_inherited_cup_does_not_repeat_parking(rig):
    rig.robot.wait_for_idle_queue.return_value = completed_queue(parked=True)
    run(rig)
    assert not rig.robot.jobs
    rig.session.scan.assert_called_once_with()


@pytest.mark.parametrize("missing", ["left_release", "left_park", "right_release"])
def test_incomplete_cup_history_with_right_clear_does_not_guess_parking(rig, missing):
    queue = completed_queue()
    queue["steps"] = [step for step in queue["steps"] if step["name"] != missing]
    rig.robot.wait_for_idle_queue.return_value = queue
    with pytest.raises(RobotCommandError, match="does not confirm"):
        run(rig)
    assert not rig.robot.jobs
    rig.session.scan.assert_not_called()


def test_unrelated_done_queue_adds_no_takeover_parking(rig):
    rig.robot.wait_for_idle_queue.return_value = {"state": "done", "steps": [{"name": "operator_action", "state": "done"}]}
    run(rig)
    assert not rig.robot.jobs


def test_failed_takeover_parking_preserves_recovery_and_never_scans(rig):
    rig.robot.wait_for_idle_queue.return_value = completed_queue()
    rig.robot.error = RobotCommandError("parking completion unknown")
    with pytest.raises(RobotCommandError, match="parking completion unknown"):
        run(rig)
    assert len(rig.robot.jobs) == 1
    assert rig.session.recovery.required() and rig.session.motion_uncertain
    rig.session.scan.assert_not_called()
    rig.robot.request_stop.assert_not_called()


@pytest.mark.parametrize("phase", [1, 3, 4, 5])
def test_lease_loss_prevents_later_capture_and_motion(rig, phase):
    calls = 0
    def check():
        nonlocal calls
        calls += 1
        if calls >= phase:
            raise RobotCommandError("control lost")
    rig.robot.check_lease.side_effect = check
    with pytest.raises(RobotCommandError, match="control lost"):
        run(rig)
    assert not rig.robot.jobs
    rig.session.execute_observed_batch.assert_not_called()
    if phase < 5:
        rig.session.scan.assert_not_called()
    else:
        rig.session.scan.assert_called_once_with()
    rig.robot.end_standby.assert_called_once_with()
    rig.robot.request_stop.assert_not_called()


@pytest.mark.parametrize("during", ["acquire", "queue", "batch"])
def test_process_interruption_releases_lease_without_cancelling_queue(rig, during):
    target = {"acquire": rig.robot.try_begin_standby, "queue": rig.robot.wait_for_idle_queue,
              "batch": rig.session.execute_observed_batch}[during]
    target.side_effect = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run(rig)
    rig.robot.end_standby.assert_called_once_with()
    rig.robot.request_stop.assert_not_called()
    rig.robot.queue_clear.assert_not_called()


@pytest.mark.parametrize("invalid", ["dry_run", "continuous", "no_refresh", "uncertain", "no_journal"])
def test_standby_refuses_ineligible_session_before_acquiring_control(rig, invalid):
    if invalid == "dry_run":
        rig.robot.dry_run = True
    elif invalid == "continuous":
        rig.session.continuous_batch = True
    elif invalid == "no_refresh":
        rig.session.between_cup_grid_check = False
    elif invalid == "uncertain":
        rig.session.motion_uncertain = True
    else:
        rig.session.recovery = None
    with pytest.raises(SelectionError):
        run(rig)
    rig.robot.try_begin_standby.assert_not_called()
    rig.session.scan.assert_not_called()


@pytest.mark.parametrize("initially_full", [False, True])
def test_real_finite_session_waits_for_grid_and_homes_only_after_workspace_empty(tmp_path, monkeypatch, initially_full):
    grid = grid_fixture(tmp_path)
    robot = RobotDouble()
    robot.try_begin_standby = Mock(return_value=True)
    robot.wait_for_idle_queue = Mock(return_value={"state": "idle", "steps": []})
    robot.check_lease = Mock()
    robot.end_standby = Mock()
    robot.read_control_status = Mock(return_value={"execution_state": "running", "controller_state": "motoron", "operation_mode": "AUTO"})
    monkeypatch.setattr(RobotClient, "read_gripper_position", lambda *_: 25.0)
    sources = [(100, 450)]
    frames = deque([(sources, FULL if initially_full else CLEAR)])
    if initially_full:
        frames.extend([(sources, FULL), (sources, {**FULL, 2: "clear"})])
    frames.append(([], FULL))
    purposes = []
    sequence = 0
    def capture(purpose):
        nonlocal sequence
        purposes.append(purpose)
        assert frames, "Finite standby unexpectedly captured after its batch completed"
        sequence += 1
        observed_sources, states = frames.popleft()
        return observed_scene(grid, sequence, observed_sources, states), 100 + sequence
    session = CupSession(capture, Mock(), robot, "cal-1", placing_grid=grid,
                         recovery=RecoveryJournal(tmp_path / "journal.json"), between_cup_check=True,
                         between_cup_grid_check=True, continuous_batch=False, right_park_pose=right_via_pose())
    waits = []
    def wait():
        assert not robot.jobs, "Initial full-grid waiting should keep the robot stationary"
        waits.append(session.batch_progress["state"])
    session._wait_for_grid = wait
    jobs = standby.run_standby(session, output=Mock(), sleep=Mock())
    assert not frames
    assert purposes == ["scan"] + ["between_cups_grid"] * (3 if initially_full else 1)
    assert waits == (["waiting_for_grid", "waiting_for_grid"] if initially_full else [])
    assert session.batch_progress["completion_reason"] == "workspace_empty"
    assert session.batch_progress["state"] == "completed" and session.batch_progress["home_cycles"] == 1
    assert [job["endpoint"] for job in jobs[-2:]] == [RobotClient.HOME_ENDPOINT] * 2
    assert all(job["endpoint"] != RobotClient.HOME_ENDPOINT for job in jobs[:-2])
    assert any(job["name"] == "right_park" for job in jobs[:-2])
    assert not session.recovery.required() and not session.motion_uncertain
    robot.end_standby.assert_called_once_with()


@pytest.mark.parametrize("grid_cups_only", [False, True])
def test_initial_empty_workspace_homes_once_and_exits_without_watching_for_more(tmp_path, monkeypatch, grid_cups_only):
    grid = grid_fixture(tmp_path)
    robot = RobotDouble()
    robot.try_begin_standby = Mock(return_value=True)
    robot.wait_for_idle_queue = Mock(return_value={"state": "idle", "steps": []})
    robot.check_lease, robot.end_standby = Mock(), Mock()
    robot.read_control_status = Mock(return_value={"execution_state": "running", "controller_state": "motoron", "operation_mode": "AUTO"})
    monkeypatch.setattr(RobotClient, "read_gripper_position", lambda *_: 25.0)
    initial = observed_scene(grid, 1, [(100, 450)] if grid_cups_only else [], FULL)
    initial["source_presence"] = {"cup_detections": 0}
    if grid_cups_only:
        for cup in initial["cups"]:
            cup.update(selectable=False, blocked_reason="Cup is already on the placement grid.",
                       placement_location_ids=[1])
    capture = Mock(return_value=(initial, 100))
    session = CupSession(capture, Mock(), robot, "cal-1", placing_grid=grid,
                         recovery=RecoveryJournal(tmp_path / "journal.json"), between_cup_check=True,
                         between_cup_grid_check=True, continuous_batch=False, right_park_pose=right_via_pose())
    standby.run_standby(session, output=Mock(), sleep=Mock())
    capture.assert_called_once_with("scan")
    assert [job["endpoint"] for job in robot.jobs] == [RobotClient.HOME_ENDPOINT] * 2
    assert session.batch_progress["completion_reason"] == "workspace_empty"


def test_cli_standby_requires_live_and_rejects_camera_only_mode():
    base = ["--state-dir", "/offline/state", "--model", "/offline/model"]
    for invalid in (["--standby"], ["--standby", "--scan-once"], ["--standby", "--live", "--scan-once"]):
        with pytest.raises(SystemExit):
            arguments(base + invalid)
    args = arguments(base + ["--live", "--standby"])
    assert args.standby and args.live and args.token_file is None


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    """Verify real CLI resource lifetime and token routing without loading a model."""
    from lynx import picker
    state = tmp_path / "state"
    state.mkdir()
    record = SimpleNamespace(state=state, token_paths=[], robots=[], sessions=[], workers=[], model_loads=0)
    def model_load(_path):
        record.model_loads += 1
        return SimpleNamespace(precision="int8", input_size=640)
    monkeypatch.setattr(picker.Model, "load", model_load)
    monkeypatch.setattr(picker, "Calibration", lambda _path: SimpleNamespace(digest="cal", offset=(0, 0, 0)))
    monkeypatch.setattr(picker, "PlacingGrid", lambda path: SimpleNamespace(path=path))
    def token(path):
        record.token_paths.append(path)
        return "offline-token"
    monkeypatch.setattr(picker, "read_token", token)
    monkeypatch.setattr(picker, "CameraClient", lambda *_args, **_kwargs: object())
    class Worker:
        def __init__(self, *_args, **_kwargs):
            self.closed = False
            record.workers.append(self)
        def close(self):
            self.closed = True
    monkeypatch.setattr(picker, "NativePerception", Worker)
    class Robot:
        kind = "ordinary"
        def __init__(self, api, headers, dry_run):
            self.api, self.headers, self.dry_run = api, headers, dry_run
            self.configure_operator_controls = Mock()
            self.check_lease = Mock()
            self.request_stop = Mock()
            record.robots.append(self)
    class QueuedRobot(Robot):
        kind = "queued"
    monkeypatch.setattr(picker, "RobotClient", Robot)
    monkeypatch.setattr(picker, "QueueRobotClient", QueuedRobot)
    monkeypatch.setattr(picker, "CaptureService", lambda *_args, **_kwargs: SimpleNamespace(capture=Mock()))
    class Session:
        def __init__(self, capture, save, robot, calibration, **kwargs):
            self.robot, self.capture = robot, capture
            self.kwargs = kwargs
            record.sessions.append(self)
    monkeypatch.setattr(picker, "CupSession", Session)
    advice = lambda *_args, **_kwargs: SimpleNamespace(summary=lambda *_args: "offline memory advice")
    for name in ("advise_capture_cache", "advise_packaged_native_cache", "advise_loaded_python_cache", "wait_for_model_headroom"):
        monkeypatch.setattr(picker, name, advice)
    monkeypatch.setattr(picker, "trim_unused_heap", lambda: "offline trim")
    monkeypatch.setattr(picker, "terminal_output", Mock())
    record.standby = Mock(return_value=[])
    monkeypatch.setattr(picker, "run_standby", record.standby)
    record.run = lambda extra=(): picker.main(["--state-dir", str(state), "--model", "/offline/model", "--live", "--standby", *extra])
    return record


@pytest.mark.parametrize("override", [False, True])
def test_cli_uses_tertiary_token_queue_client_finite_refresh_and_closes_worker(launcher, override):
    rig = launcher
    supplied = rig.state / "specific-token"
    assert rig.run(["--token-file", str(supplied)] if override else []) == 0
    assert rig.token_paths == [supplied if override else rig.state / "tertiary-token"]
    assert len(rig.robots) == 1 and rig.robots[0].kind == "queued" and not rig.robots[0].dry_run
    assert rig.robots[0].headers == {"Authorization": "Bearer offline-token"}
    assert rig.robots[0].api == "http://127.0.0.1:8000/api/v1"
    settings = rig.sessions[0].kwargs
    assert settings["between_cup_check"] is True and settings["between_cup_grid_check"] is True
    assert settings["continuous_batch"] is False and settings["right_park_pose"] == right_via_pose()
    rig.standby.assert_called_once()
    assert len(rig.workers) == 1 and rig.workers[0].closed
    with NativeRuntimeLock(rig.state):
        pass  # The CLI released exclusion after its worker closed.


def test_cli_duplicate_native_runtime_refused_before_loading_model(launcher):
    with NativeRuntimeLock(launcher.state):
        assert launcher.run() == 1
    assert launcher.model_loads == 0 and not launcher.workers and not launcher.robots
    launcher.standby.assert_not_called()


def test_cli_standby_interrupt_closes_worker_without_sending_robot_stop(launcher):
    launcher.standby.side_effect = KeyboardInterrupt()
    assert launcher.run() == 0
    assert launcher.workers[0].closed
    launcher.robots[0].request_stop.assert_not_called()
    with NativeRuntimeLock(launcher.state):
        pass


def test_concurrent_stop_during_inherited_parking_keeps_real_session_recovery(tmp_path, monkeypatch):
    grid = grid_fixture(tmp_path)
    robot = RobotDouble()
    robot.try_begin_standby = Mock(return_value=True)
    robot.wait_for_idle_queue = Mock(return_value=completed_queue())
    robot.check_lease, robot.end_standby = Mock(), Mock()
    monkeypatch.setattr(RobotClient, "read_gripper_position", lambda *_: 25.0)
    capture = Mock()
    session = CupSession(capture, Mock(), robot, "cal-1", placing_grid=grid,
                         recovery=RecoveryJournal(tmp_path / "journal.json"), between_cup_check=True,
                         between_cup_grid_check=True, continuous_batch=False, right_park_pose=right_via_pose())
    robot.after_move = lambda _count: session.latch_stop()
    with pytest.raises((SelectionError, RobotCommandError), match="[Ss]top"):
        standby.run_standby(session, output=Mock(), sleep=Mock())
    assert len(robot.jobs) == 1 and robot.jobs[0]["name"] == "right_park"
    assert session.stop_requested and session.motion_uncertain and session.recovery.required()
    capture.assert_not_called()
    robot.end_standby.assert_called_once_with()
