"""Offline Mac UI grid refresh tests; all motion and camera calls are doubles."""

import copy
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest

from cup_commands import CupSession, WorkspaceCheckError
from cup_inventory import SelectionError
from handover_plan import handover_jobs, right_via_pose
from placement_records import combine
from recovery_journal import RecoveryJournal
from robot_client import RobotClient, RobotCommandError
from test_cup_selection import RobotDouble, detection, inventory
from test_support_grid import grid_fixture


CLEAR = {1: "clear", 2: "clear", 3: "clear"}
FULL = {1: "occupied", 2: "occupied", 3: "occupied"}
SOURCES = [(100, 450), (200, 500)]


def observed_scene(grid, sequence, sources, states):
    """Use the same combined evidence shape as a real full placement capture."""
    snapshot = grid.snapshot()
    slots = []
    for place in snapshot["locations"]:
        state = states[place["id"]]
        u = place["id"] * 30
        slots.append({
            "id": place["id"], "state": state, "reason": "synthetic depth evidence",
            "reason_code": {"clear": "clear", "occupied": "raised_depth",
                            "unknown": "table_depth"}[state],
            "valid_fraction": 1.0, "table_fraction": 1.0,
            "visible_fraction": 1.0, "floor_visible_fraction": 1.0,
            "polygon_px": [[u, 200], [u + 20, 200], [u + 20, 220], [u, 220]],
            "cup_count": 0, "assigned_cups": [],
            "evidence": ["raised_depth"] if state == "occupied" else [],
            "reserved": place.get("reserved", False),
        })
    objects = []
    for u, x in sources:
        objects.extend([detection("CUP", u, x=x), detection("CUP BASE", u, x=x)])
    result = inventory(capture=f"frame-{sequence}", sequence=sequence, objects=objects)
    result["images"] = {"rgb": f"/offline/grid-{sequence}.png"}
    result["placement_observation"] = combine(snapshot, [slots], [sequence])
    return result


@pytest.fixture
def make_rig(tmp_path):
    def create(*, sources=SOURCES, states=CLEAR, park=True):
        grid = grid_fixture(tmp_path)
        capture = Mock(return_value=(observed_scene(grid, 10, sources, states), 99))
        robot = RobotDouble()
        robot.read_control_status = Mock(return_value={"execution_state": "running",
            "controller_state": "motoron", "operation_mode": "AUTO"})
        recovery = RecoveryJournal(tmp_path / "recovery.json")
        save = Mock()
        session = CupSession(capture, save, robot, "cal-1", placing_grid=grid,
                             recovery=recovery, between_cup_check=True,
                             between_cup_grid_check=True,
                             right_park_pose=right_via_pose() if park else None)
        session.scan()
        count = len(handover_jobs(session.inventory["cups"][0],
                                 place=grid.snapshot()["locations"][0],
                                 right_park_pose=session.right_park_pose))
        rig = SimpleNamespace(session=session, capture=capture, robot=robot, grid=grid,
                              recovery=recovery, save=save, transfer_count=count,
                              frames=deque(), allow_idle_frames=False, idle_states=[],
                              captures=[])

        def idle_wait():
            rig.idle_states.append(session.batch_progress["state"])
            if not rig.frames:
                # Continuous runs finish only because this offline harness asks
                # the server to close after its last scripted observation.
                session.finish_batch_when_parked()
            else:
                assert rig.allow_idle_frames, "Unexpected idle wait before remaining transfer"

        session._wait_for_grid = Mock(side_effect=idle_wait)
        return rig
    return create


def supply_frames(rig, frames, *, allow_idle_frames=False):
    """Build each snapshot only after the preceding motion/bookkeeping finishes."""
    rig.frames = deque(frames)
    rig.allow_idle_frames = allow_idle_frames
    sequence = 10

    def capture(purpose):
        nonlocal sequence
        assert purpose == "between_cups_grid"
        completed = [cup for cup in rig.session.inventory["cups"]
                     if cup.get("execution", {}).get("state") == "completed"]
        transfers = transfer_jobs(rig)
        assert len(transfers) == len(completed) * rig.transfer_count
        assert all(len(cup["execution"]["completed_steps"]) == rig.transfer_count
                   and not cup["execution"]["pending_jobs"] for cup in completed)
        assert not rig.recovery.required()
        if rig.session.right_park_pose is not None:
            assert transfers[-1]["name"] == "right_park"
        rig.captures.append({"transfers": len(completed), "homes": len(home_jobs(rig)),
                             "jobs": copy.deepcopy(rig.robot.jobs)})
        assert rig.frames, "Unexpected capture beyond scripted observations"
        value = rig.frames.popleft()
        if isinstance(value, Exception):
            raise value
        sources, states = value
        sequence += 1
        return observed_scene(rig.grid, sequence, sources, states), 100 + sequence

    rig.capture.side_effect = capture


def run_batch(rig):
    return rig.session.execute_batch(capture_id="frame-10")


def placement_ids(rig):
    return [cup["execution"]["placement"]["id"] for cup in rig.session.inventory["cups"]
            if cup.get("execution", {}).get("state") == "completed"]


def home_jobs(rig):
    return [job for job in rig.robot.jobs if job["endpoint"] == RobotClient.HOME_ENDPOINT]


def transfer_jobs(rig):
    return [job for job in rig.robot.jobs if job["endpoint"] != RobotClient.HOME_ENDPOINT]


def assert_complete(rig, count, *, reason="session_closed", homes=1):
    assert len(transfer_jobs(rig)) == count * rig.transfer_count
    assert len(home_jobs(rig)) == homes * 2
    assert rig.session.batch_progress["state"] == "completed"
    assert rig.session.batch_progress["completion_reason"] == reason
    assert rig.session.scan_usable
    assert not rig.session.motion_uncertain and not rig.recovery.required()
    assert not any("home" in job["name"].lower() for job in transfer_jobs(rig))
    assert not any(place.get("reserved") for place in rig.grid.snapshot()["locations"])


@pytest.mark.parametrize("park", [False, True])
def test_removed_grid_cup_allows_same_cell_to_be_reused_repeatedly(make_rig, park):
    rig = make_rig(sources=SOURCES + [(300, 550)], states={**FULL, 1: "clear"}, park=park)
    supply_frames(rig, [(SOURCES[1:] + [(300, 550)], {**FULL, 1: "clear"}),
                        ([(300, 550)], {**FULL, 1: "clear"}), ([], FULL)])
    with patch.object(rig.grid, "reserve", wraps=rig.grid.reserve) as reserve:
        run_batch(rig)
    assert_complete(rig, 3)
    assert placement_ids(rig) == [1, 1, 1]
    assert [[place["id"] for place in entry.args[0]["locations"]]
            for entry in reserve.call_args_list] == [[1], [1], [1]]
    assert rig.session.batch_progress["completed_cups"] == [1, 2, 3]
    assert [cup["last_verification"]["placement_checks"][0]["capture"]
            for cup in rig.session.inventory["cups"]] == ["frame-10", "frame-11", "frame-12"]
    assert [cup["last_verification"]["placement_checks"][0]["source"]
            for cup in rig.session.inventory["cups"]] == ["saved_scan", "workspace_check", "workspace_check"]
    assert rig.session.inventory["capture"] == "frame-10"
    assert rig.session.inventory["image_capture"] == "frame-13"
    assert rig.session.inventory["workspace_check"]["grid_checked"] is True
    rig.session._wait_for_grid.assert_called_once_with()


def test_cell_occupied_in_original_scan_can_be_used_when_fresh_scan_clears_it(make_rig):
    rig = make_rig(states={**FULL, 1: "clear"})
    supply_frames(rig, [(SOURCES[1:], {**FULL, 2: "clear"}), ([], FULL)])
    run_batch(rig)
    assert_complete(rig, 2)
    assert placement_ids(rig) == [1, 2]
    second = rig.session.inventory["cups"][1]
    assert second["last_verification"]["placement_visibility"]["state"] == "clear"
    assert second["last_verification"]["placement_checks"][0]["capture"] == "frame-11"
    rig.session._wait_for_grid.assert_called_once_with()


@pytest.mark.parametrize("blocked_state", ["occupied", "unknown"])
def test_pending_destination_is_reallocated_when_fresh_view_is_not_clear(make_rig, blocked_state):
    rig = make_rig()
    supply_frames(rig, [(SOURCES[1:], {1: "occupied", 2: blocked_state, 3: "clear"}),
                        ([], {**FULL, 2: blocked_state})])
    with patch.object(rig.grid, "reserve", wraps=rig.grid.reserve) as reserve:
        run_batch(rig)
    assert_complete(rig, 2)
    assert placement_ids(rig) == [1, 3]
    assert [[place["id"] for place in entry.args[0]["locations"]]
            for entry in reserve.call_args_list] == [[1, 2], [3]]
    unavailable = rig.grid.snapshot()["locations"][1]
    assert unavailable["occupied"] and not unavailable.get("reserved", False)
    assert bool(unavailable.get("vision_blocked", False)) == (blocked_state == "unknown")
    rig.session._wait_for_grid.assert_called_once_with()


def test_full_grid_rechecks_without_moving_then_resumes_when_cell_is_cleared(make_rig):
    rig = make_rig()
    supply_frames(rig, [(SOURCES[1:], FULL), (SOURCES[1:], FULL),
                        (SOURCES[1:], {**FULL, 2: "clear"}), ([], FULL)])
    stationary_jobs = []

    def wait():
        if not rig.frames:
            rig.session.finish_batch_when_parked()
            return
        assert rig.session.batch_progress["state"] == "waiting_for_grid"
        assert rig.session.batch_progress["completed_cups"] == [1]
        assert rig.session.batch_progress["waiting_cup_ids"] == [2]
        assert rig.session.batch_progress["pending_cups"] == []
        assert not rig.recovery.required() and not rig.session.motion_uncertain
        assert len(rig.robot.jobs) == rig.transfer_count
        assert not any(place.get("reserved") for place in rig.grid.snapshot()["locations"])
        stationary_jobs.append(copy.deepcopy(rig.robot.jobs))

    rig.session._wait_for_grid.side_effect = wait
    with patch.object(rig.grid, "reserve", wraps=rig.grid.reserve) as reserve:
        run_batch(rig)
    assert_complete(rig, 2)
    assert placement_ids(rig) == [1, 2]
    assert reserve.call_count == 2
    assert rig.session._wait_for_grid.call_count == 3
    assert stationary_jobs[0] == stationary_jobs[1]
    assert rig.capture.call_args_list == [call("scan")] + [call("between_cups_grid")] * 4
    assert rig.session.inventory["cups"][1]["last_verification"]["placement_checks"][0]["capture"] == "frame-13"


def test_stop_while_waiting_keeps_completed_cup_and_prevents_capture_or_next_motion(make_rig):
    rig = make_rig()
    supply_frames(rig, [(SOURCES[1:], FULL)])
    rig.robot.request_stop = Mock(return_value={"stopped": True})

    def wait_then_stop():
        assert rig.session.batch_progress["state"] == "waiting_for_grid"
        rig.session.request_stop()
        rig.session._check_stop()

    rig.session._wait_for_grid.side_effect = wait_then_stop
    with pytest.raises(SelectionError, match="Software stop requested"):
        run_batch(rig)
    assert len(rig.robot.jobs) == rig.transfer_count
    assert rig.capture.call_args_list == [call("scan"), call("between_cups_grid")]
    assert rig.session.batch_progress["state"] == "interrupted"
    assert rig.session.batch_progress["completed_cups"] == [1]
    assert rig.session.inventory["cups"][0]["status"] == "placed"
    assert rig.session.stop_requested and rig.session.motion_uncertain and rig.recovery.required()
    assert not rig.session.scan_usable
    assert not any(place.get("reserved") for place in rig.grid.snapshot()["locations"])
    rig.robot.request_stop.assert_called_once_with()


def test_server_close_while_grid_full_finishes_without_stop_or_further_capture(make_rig):
    rig = make_rig()
    supply_frames(rig, [(SOURCES[1:], FULL)])
    rig.robot.request_stop = Mock()
    rig.session._wait_for_grid.side_effect = rig.session.finish_batch_when_parked
    run_batch(rig)
    assert_complete(rig, 1, homes=0)
    assert rig.capture.call_args_list == [call("scan"), call("between_cups_grid")]
    assert rig.session.batch_progress["completed_cups"] == [1]
    assert not rig.session.stop_requested
    rig.robot.request_stop.assert_not_called()


def test_source_removed_while_grid_full_homes_and_keeps_watching(make_rig):
    rig = make_rig()
    supply_frames(rig, [(SOURCES[1:], FULL), ([], FULL)], allow_idle_frames=True)
    run_batch(rig)
    assert_complete(rig, 1)
    assert rig.session.batch_progress["pending_cups"] == []
    assert rig.session.batch_progress["waiting_cup_ids"] == []
    assert rig.session._wait_for_grid.call_count == 2
    assert rig.capture.call_args_list == [call("scan")] + [call("between_cups_grid")] * 2


@pytest.mark.parametrize("wait_first", [False, True])
def test_camera_failure_after_completed_motion_needs_scan_without_repeating_transfer(make_rig, wait_first):
    rig = make_rig()
    frames = ([(SOURCES[1:], FULL)] if wait_first else []) + [RuntimeError("camera offline")]
    supply_frames(rig, frames)
    rig.session._wait_for_grid.side_effect = None
    with pytest.raises(WorkspaceCheckError, match="camera offline"):
        run_batch(rig)
    assert len(rig.robot.jobs) == rig.transfer_count
    assert rig.session.inventory["cups"][0]["status"] == "placed"
    assert rig.session.batch_progress["completed_cups"] == [1]
    assert rig.session.batch_progress["state"] == "interrupted"
    assert not rig.session.motion_uncertain and not rig.recovery.required()
    assert not rig.session.scan_usable
    assert rig.session._wait_for_grid.call_count == int(wait_first)
    assert rig.capture.call_count == 2 + int(wait_first)
    with pytest.raises(SelectionError, match="Run scan"):
        rig.session.execute_selection(action="goto", capture_id="frame-10", cup_id=2)
    assert len(rig.robot.jobs) == rig.transfer_count


@pytest.mark.parametrize("job_name", ["left_park", "right_park"])
def test_failed_motion_does_not_capture_or_release_pending_reservations(make_rig, job_name):
    rig = make_rig()
    rig.robot.error = RobotCommandError("park failed")
    rig.robot.error_at = job_name
    with pytest.raises(RobotCommandError, match="park failed"):
        run_batch(rig)
    rig.capture.assert_called_once_with("scan")
    assert rig.robot.jobs[-1]["name"] == job_name
    assert sum(job["name"] == "approach_cup" for job in rig.robot.jobs) == 1
    assert rig.session.batch_progress["completed_cups"] == []
    assert rig.session.motion_uncertain and rig.recovery.required() and not rig.session.scan_usable
    assert all(place.get("reserved") for place in rig.grid.snapshot()["locations"][:2])
    rig.session._wait_for_grid.assert_not_called()


@pytest.mark.parametrize("remaining,homes", [([], 1),
    ([(198, 499), (202, 501)], 0)])
def test_only_empty_workspace_homes_before_continuing_to_watch(make_rig, remaining, homes):
    rig = make_rig()
    supply_frames(rig, [(remaining, FULL)])
    run_batch(rig)
    assert_complete(rig, 1, homes=homes)
    rig.session._wait_for_grid.assert_called_once_with()


def test_removed_sources_release_unused_reservations_from_observed_clear_cells(make_rig):
    rig = make_rig(sources=SOURCES + [(300, 550)])
    supply_frames(rig, [([], {**CLEAR, 1: "occupied"})])
    run_batch(rig)
    assert_complete(rig, 1)
    assert [place["occupied"] for place in rig.grid.snapshot()["locations"]] == [True, False, False]
    assert rig.session.inventory["placement_observation"]["released_ids"] == [2, 3]
    rig.session._wait_for_grid.assert_called_once_with()


@pytest.mark.parametrize("remaining", [SOURCES[1:], []])
def test_single_selection_refreshes_grid_without_home_wait_or_other_picks(make_rig, remaining):
    rig = make_rig()
    supply_frames(rig, [(remaining, FULL)])
    rig.session.execute_selection(action="goto", capture_id="frame-10", cup_id=1)
    assert placement_ids(rig) == [1]
    assert len(rig.robot.jobs) == rig.transfer_count
    assert rig.session.batch_progress is None
    assert rig.session.inventory["workspace_check"]["grid_checked"] is True
    assert [cup["id"] for cup in rig.session.inventory["cups"] if cup["selectable"]] == ([2] if remaining else [])
    assert all(place["occupied"] for place in rig.grid.snapshot()["locations"])
    assert not any(place.get("reserved") for place in rig.grid.snapshot()["locations"])
    assert rig.session.scan_usable and not rig.session.motion_uncertain and not rig.recovery.required()
    rig.session._wait_for_grid.assert_not_called()


def test_repeated_empty_frames_home_both_arms_once_and_keep_watching(make_rig):
    rig = make_rig(sources=SOURCES[:1])
    supply_frames(rig, [([], FULL), ([], FULL), ([], FULL)], allow_idle_frames=True)
    jobs = run_batch(rig)
    assert_complete(rig, 1)
    assert jobs == rig.robot.jobs
    assert [entry["homes"] for entry in rig.captures] == [0, 2, 2]
    assert [job["name"] for job in home_jobs(rig)] == ["workspace_right_home", "workspace_left_home"]
    assert [job["body"] for job in home_jobs(rig)] == [
        {"arm": arm, "wait": True, "speed": min(rig.session.speed, 100),
         "acceleration": 100, "ramp": 100} for arm in ("right", "left")]
    assert rig.idle_states == ["waiting_for_cups"] * 3
    assert rig.session.batch_progress["home_cycles"] == 1
    assert rig.session.batch_progress["arms_home"] is True
    assert rig.session.batch_progress["idle_home"]["state"] == "completed"
    assert len(rig.session.batch_progress["idle_home"]["completed_steps"]) == 2
    assert rig.session.inventory["cups"][0]["status"] == "placed"


def test_new_source_after_home_waits_for_clear_grid_then_resumes_and_homes_on_next_empty_period(make_rig):
    rig = make_rig(sources=SOURCES[:1])
    supply_frames(rig, [([], FULL), (SOURCES[1:], FULL),
                        (SOURCES[1:], {**FULL, 2: "clear"}),
                        ([], FULL), ([], FULL)], allow_idle_frames=True)
    jobs = run_batch(rig)
    assert_complete(rig, 2, homes=2)
    assert jobs == rig.robot.jobs
    assert placement_ids(rig) == [1, 2]
    assert rig.session.batch_progress["completed_cups"] == [1, 2]
    assert [entry["transfers"] for entry in rig.captures] == [1, 1, 1, 2, 2]
    assert [entry["homes"] for entry in rig.captures] == [0, 2, 2, 2, 4]
    assert rig.idle_states == ["waiting_for_cups", "waiting_for_grid",
                               "waiting_for_cups", "waiting_for_cups"]
    assert rig.session.batch_progress["home_cycles"] == 2
    assert rig.session.inventory["cups"][1]["last_verification"]["capture"] == "frame-13"
    approaches = [job["body"]["points"][-1]["x"] for job in transfer_jobs(rig)
                  if job["name"] == "approach_cup"]
    assert approaches == [450, 500]


def test_repeated_unpickable_sources_keep_camera_running_without_home(make_rig):
    rig = make_rig()
    ambiguous = [(198, 499), (202, 501)]
    supply_frames(rig, [(ambiguous, FULL), (ambiguous, FULL)], allow_idle_frames=True)
    run_batch(rig)
    assert_complete(rig, 1, homes=0)
    assert rig.idle_states == ["waiting_for_cups", "waiting_for_cups"]
    assert [entry["homes"] for entry in rig.captures] == [0, 0]
    assert rig.session.batch_progress["home_cycles"] == 0
    assert rig.session.batch_progress["arms_home"] is False


@pytest.mark.parametrize("job_name,home_count", [("workspace_right_home", 1),
                                               ("workspace_left_home", 2)])
def test_failed_empty_workspace_home_prevents_another_capture_or_pick(make_rig, job_name, home_count):
    rig = make_rig(sources=SOURCES[:1])
    supply_frames(rig, [([], FULL), (SOURCES[1:], {**FULL, 2: "clear"})], allow_idle_frames=True)
    rig.robot.error = RobotCommandError("Home unconfirmed")
    rig.robot.error_at = job_name
    with pytest.raises(RobotCommandError, match="Home unconfirmed"):
        run_batch(rig)
    assert len(transfer_jobs(rig)) == rig.transfer_count
    assert len(home_jobs(rig)) == home_count
    assert rig.robot.jobs[-1]["name"] == job_name
    assert rig.capture.call_args_list == [call("scan"), call("between_cups_grid")]
    assert len(rig.frames) == 1
    assert rig.session.batch_progress["completed_cups"] == [1]
    assert rig.session.batch_progress["home_cycles"] == 0
    assert rig.session.batch_progress["state"] == "interrupted"
    assert rig.session.inventory["cups"][0]["status"] == "placed"
    assert rig.session.motion_uncertain and rig.recovery.required() and not rig.session.scan_usable
    rig.session._wait_for_grid.assert_not_called()


def test_home_persistence_failure_after_journal_begin_keeps_recovery_even_without_dispatch(make_rig):
    rig = make_rig(sources=SOURCES[:1])
    supply_frames(rig, [([], FULL)])
    failed = False

    def fail_after_journal(value):
        nonlocal failed
        if (value.get("batch_execution", {}).get("state") == "homing_when_empty"
                and rig.recovery.required() and not failed):
            failed = True
            assert home_jobs(rig) == []
            raise OSError("Home inventory save failed after journal begin")

    rig.save.side_effect = fail_after_journal
    with pytest.raises(OSError, match="save failed after journal begin"):
        run_batch(rig)
    assert failed
    assert len(transfer_jobs(rig)) == rig.transfer_count
    assert home_jobs(rig) == []
    assert rig.capture.call_args_list == [call("scan"), call("between_cups_grid")]
    assert rig.session.batch_progress["completed_cups"] == [1]
    assert rig.session.batch_progress["home_cycles"] == 0
    assert rig.session.motion_uncertain and rig.recovery.required() and not rig.session.scan_usable
    rig.session._wait_for_grid.assert_not_called()


def test_shutdown_during_first_home_save_skips_home_without_claiming_arms_are_home(make_rig):
    rig = make_rig(sources=SOURCES[:1])
    supply_frames(rig, [([], FULL)])

    def close_before_home(value):
        if value.get("batch_execution", {}).get("state") == "homing_when_empty":
            assert not rig.recovery.required()
            assert home_jobs(rig) == []
            rig.session.finish_batch_when_parked()

    rig.save.side_effect = close_before_home
    run_batch(rig)
    assert_complete(rig, 1, homes=0)
    assert rig.session.batch_progress["arms_home"] is False
    assert rig.session.batch_progress["home_cycles"] == 0
    assert rig.capture.call_args_list == [call("scan"), call("between_cups_grid")]
    rig.session._wait_for_grid.assert_not_called()


def test_raw_source_cup_without_supported_base_prevents_home_and_keeps_watching(make_rig):
    rig = make_rig(sources=SOURCES[:1])
    supply_frames(rig, [([], FULL), ([], FULL)], allow_idle_frames=True)
    full_capture = rig.capture.side_effect

    def cup_only_capture(purpose):
        fresh, started = full_capture(purpose)
        assert fresh["cups"] == []
        # Capture retains raw CUP presence when no supported CUP BASE yielded a
        # selectable inventory entry. Placement-area cups are excluded upstream.
        fresh["source_presence"] = {"cup_detections": 1}
        return fresh, started

    rig.capture.side_effect = cup_only_capture
    run_batch(rig)
    assert_complete(rig, 1, homes=0)
    assert rig.idle_states == ["waiting_for_cups", "waiting_for_cups"]
    assert [cup["status"] for cup in rig.session.inventory["cups"]] == ["placed"]
    assert rig.session.inventory["source_presence"] == {"cup_detections": 1}
    assert rig.session.batch_progress["arms_home"] is False
    assert rig.session.batch_progress["home_cycles"] == 0


def test_shutdown_after_first_home_is_journaled_but_before_dispatch_cancels_cleanly(make_rig):
    rig = make_rig(sources=SOURCES[:1])
    supply_frames(rig, [([], FULL)])
    closed = False

    def close_pending_home(value):
        nonlocal closed
        pending = value.get("batch_execution", {}).get("idle_home", {}).get("pending_job")
        if pending and pending["name"] == "workspace_right_home":
            assert not closed
            assert rig.recovery.required() and rig.session.motion_uncertain
            assert home_jobs(rig) == []
            closed = True
            rig.session.finish_batch_when_parked()

    rig.save.side_effect = close_pending_home
    run_batch(rig)
    assert closed
    assert_complete(rig, 1, homes=0)
    assert rig.session.batch_progress["arms_home"] is False
    assert rig.session.batch_progress["home_cycles"] == 0
    assert rig.session.batch_progress["idle_home"]["state"] == "cancelled"
    assert rig.capture.call_args_list == [call("scan"), call("between_cups_grid")]
    rig.session._wait_for_grid.assert_not_called()


def test_stop_at_right_home_completion_prevents_left_home_and_further_capture(make_rig):
    rig = make_rig(sources=SOURCES[:1])
    supply_frames(rig, [([], FULL), (SOURCES[1:], {**FULL, 2: "clear"})], allow_idle_frames=True)
    rig.robot.request_stop = Mock(return_value={"stopped": True})

    def stop_after_right_home(_count):
        if rig.robot.jobs[-1]["name"] == "workspace_right_home":
            assert rig.recovery.required()
            rig.session.request_stop()

    rig.robot.after_move = stop_after_right_home
    with pytest.raises(SelectionError, match="Software stop requested"):
        run_batch(rig)
    assert len(transfer_jobs(rig)) == rig.transfer_count
    assert [job["name"] for job in home_jobs(rig)] == ["workspace_right_home"]
    assert rig.capture.call_args_list == [call("scan"), call("between_cups_grid")]
    assert rig.session.stop_requested and rig.session.motion_uncertain and rig.recovery.required()
    assert not rig.session.scan_usable
    assert rig.session.batch_progress["home_cycles"] == 0
    assert rig.session.batch_progress["completed_cups"] == [1]
    rig.robot.request_stop.assert_called_once_with()
    rig.session._wait_for_grid.assert_not_called()
