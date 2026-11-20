"""Offline Mac UI continuation checks with real plans and grid bookkeeping."""
import copy
import threading
from unittest.mock import Mock, patch

import pytest

from cup_commands import CupSession, WorkspaceCheckError
from cup_inventory import SelectionError
from device_dashboard import DeviceDashboard
from handover_plan import handover_jobs, right_via_pose
from recovery_journal import RecoveryJournal
from robot_client import RobotClient, RobotCommandError
from test_cup_selection import RobotDouble, detection, inventory, TRANSFER_JOB_COUNT
from test_support_grid import grid_fixture
from web_controller import WebController


def scene(sequence, sources):
    objects = []
    for u, x in sources:
        objects.extend([detection("CUP", u, x=x), detection("CUP BASE", u, x=x)])
    result = inventory(capture=f"frame-{sequence}", sequence=sequence, objects=objects)
    result["images"] = {"rgb": f"/offline/{sequence}.png", "annotated": f"/offline/{sequence}-labels.png"}
    return result


@pytest.fixture
def rig(tmp_path):
    original = scene(10, [(100, 450), (200, 500), (300, 550)])
    capture = Mock(return_value=(original, 99))
    robot = RobotDouble()
    recovery = RecoveryJournal(tmp_path / "recovery.json")
    session = CupSession(capture, Mock(), robot, "cal-1", placing_grid=grid_fixture(tmp_path),
                         recovery=recovery, between_cup_check=True)
    session.scan()
    return session, capture, robot, recovery


def pick(session, cup_id=1):
    return session.execute_selection(action="goto", capture_id="frame-10", cup_id=cup_id)


def test_batch_captures_after_each_complete_plan_and_keeps_reviewed_destinations(rig):
    session, capture, robot, recovery = rig
    baseline = copy.deepcopy(session.inventory)
    preview = session.preview_batch(capture_id="frame-10")
    expected = []
    for cup, place in zip(baseline["cups"], preview["placement"]["locations"]):
        expected += handover_jobs(cup, place=place)
    frames = [scene(11, [(200, 500), (300, 550)]), scene(12, [(300, 550)]), scene(13, [])]
    at_capture = []
    def observe(purpose):
        assert purpose == "between_cups"
        count = len(at_capture) + 1
        assert len(robot.jobs) == count * TRANSFER_JOB_COUNT
        assert {j["name"] for j in robot.jobs[-TRANSFER_JOB_COUNT:]} == {j["name"] for j in expected[:TRANSFER_JOB_COUNT]}
        assert "left_park" in [j["name"] for j in robot.jobs[-TRANSFER_JOB_COUNT:]]
        assert session.inventory["cups"][count - 1]["execution"]["state"] == "completed"
        assert not recovery.required()
        # Pending batch reservations survive; camera validation cannot reconcile them.
        slots = session.placing_grid.snapshot()["locations"]
        assert all(slot["occupied"] for slot in slots)
        assert sum(slot.get("reserved", False) for slot in slots) == 3 - count
        at_capture.append(session.placing_grid.path.read_bytes())
        fresh = frames[count - 1]
        # Even invalid grid evidence is irrelevant to this source-only check.
        fresh["placement_observation"] = {"slots": "ignored", "grid_snapshot": None}
        return fresh, 100 + count
    capture.side_effect = observe
    with patch.object(session.placing_grid, "reconcile_observation", side_effect=AssertionError("Grid was rechecked")):
        jobs = session.execute_batch(capture_id="frame-10")
    assert jobs == expected == robot.jobs
    assert not any("home" in j["name"] or "camera" in j["name"] for j in jobs)
    assert capture.call_args_list == [(('scan',),), (('between_cups',),), (('between_cups',),), (('between_cups',),)]
    assert session.inventory["capture"] == "frame-10"
    assert session.inventory["image_capture"] == "frame-13"
    assert session.inventory["frame"]["sequence"] == 13
    assert [c["id"] for c in session.inventory["cups"]] == [1, 2, 3]
    assert [c["status"] for c in session.inventory["cups"]] == ["placed"] * 3
    assert all(c["bbox_xyxy"] is None for c in session.inventory["cups"])
    assert session.inventory["cups"][1]["last_verification"]["capture"] == "frame-11"
    assert session.inventory["cups"][2]["last_verification"]["source"] == "workspace_check"
    assert session.batch_progress["state"] == "completed"
    assert session.scan_usable and not session.motion_uncertain and not recovery.required()
    assert session.placing_grid.path.read_bytes() == at_capture[-1]
    assert session.batch_progress["completion_reason"] == "workspace_empty"
    assert session.batch_progress["revision"] == 3


def test_individual_picks_keep_ids_and_use_refreshed_geometry(rig):
    session, capture, robot, _ = rig
    capture.side_effect = [(scene(11, [(204, 505), (300, 550)]), 101),
                           (scene(12, [(300, 550)]), 102)]
    pick(session)
    assert session.inventory["cups"][1]["id"] == 2
    assert session.inventory["cups"][1]["x"] == 505
    second = pick(session, 2)
    approach = next(j for j in second if j["name"] == "approach_cup")
    assert approach["body"]["points"][-1]["x"] == 505
    assert len(robot.jobs) == TRANSFER_JOB_COUNT * 2
    assert capture.call_count == 3
    assert session.inventory["images"]["annotated"] == "/offline/12.png"


def test_stale_camera_frame_stops_batch_without_fabricating_motion_failure(rig):
    session, capture, robot, recovery = rig
    capture.return_value = (scene(10, [(200, 500), (300, 550)]), 101)
    with pytest.raises(WorkspaceCheckError, match="Placement completed.*Run scan again"):
        session.execute_batch(capture_id="frame-10")
    assert len(robot.jobs) == TRANSFER_JOB_COUNT
    assert session.inventory["cups"][0]["status"] == "placed"
    assert session.batch_progress["completed_cups"] == [1]
    assert session.batch_progress["state"] == "interrupted"
    assert session.inventory["workspace_check"]["state"] == "failed"
    assert not session.scan_usable and not session.motion_uncertain and not recovery.required()
    with pytest.raises(SelectionError, match="Run scan"):
        pick(session, 2)
    assert len(robot.jobs) == TRANSFER_JOB_COUNT


def test_individual_picks_refresh_moved_sources_without_automatically_picking_more(rig):
    session, capture, robot, recovery = rig
    capture.side_effect = [(scene(11, [(200, 500), (310, 565)]), 101),
                           (scene(12, [(320, 580)]), 102)]
    pick(session)
    pick(session, 2)
    assert len(robot.jobs) == TRANSFER_JOB_COUNT * 2
    assert not session.motion_uncertain and not recovery.required()
    assert session.scan_usable
    assert next(c for c in session.inventory["cups"] if c["id"] == 3)["x"] == 580


def test_repeated_latest_capture_identity_is_rejected_even_with_new_sequence(rig):
    session, capture, _, _ = rig
    first, repeated = scene(11, [(200, 500), (300, 550)]), scene(12, [(300, 550)])
    repeated["capture"] = first["capture"]
    capture.side_effect = [(first, 101), (repeated, 102)]
    pick(session)
    with pytest.raises(WorkspaceCheckError, match="repeated a previous capture"):
        pick(session, 2)


def test_batch_follows_far_move_and_new_addition_using_current_geometry(rig):
    session, capture, robot, recovery = rig
    capture.return_value = (scene(10, [(100, 450), (200, 500)]), 100)
    session.scan()
    capture.side_effect = [(scene(11, [(250, 590), (350, 650)]), 101),
                           (scene(12, [(350, 650)]), 102), (scene(13, []), 103)]
    jobs = session.execute_batch(capture_id="frame-10")
    approaches = [job["body"]["points"][-1] for job in jobs if job["name"] == "approach_cup"]
    assert [point["x"] for point in approaches] == [450, 590, 650]
    placements = [job["body"]["points"][-1]["x"] for job in jobs if job["name"] == "right_place"]
    assert placements == [420, 500, 580]
    assert session.batch_progress["completed_cups"] == [1, 3, 4]
    assert session.batch_progress["cup_ids"] == [1, 3, 4]
    assert session.batch_progress["completion_reason"] == "workspace_empty"
    assert len(robot.jobs) == 3 * TRANSFER_JOB_COUNT
    assert session.scan_usable and not session.motion_uncertain and not recovery.required()


def test_removed_cup_is_dropped_and_unused_destination_released(rig):
    session, capture, robot, _ = rig
    capture.side_effect = [(scene(11, [(300, 550)]), 101), (scene(12, []), 102)]
    session.execute_batch(capture_id="frame-10")
    assert session.batch_progress["completed_cups"] == [1, 3]
    assert session.batch_progress["cup_ids"] == [1, 3]
    assert len(robot.jobs) == 2 * TRANSFER_JOB_COUNT
    slots = session.placing_grid.snapshot()["locations"]
    assert [slot["occupied"] for slot in slots] == [True, True, False]
    assert not any(slot.get("reserved") for slot in slots)


def test_new_cup_after_last_original_cup_extends_the_active_batch(rig):
    session, capture, robot, _ = rig
    capture.return_value = (scene(10, [(100, 450)]), 100)
    session.scan()
    capture.side_effect = [(scene(11, [(300, 550)]), 101), (scene(12, []), 102)]
    session.execute_batch(capture_id="frame-10")
    assert session.batch_progress["completed_cups"] == [1, 2]
    assert len(robot.jobs) == 2 * TRANSFER_JOB_COUNT


def test_capacity_finishes_cleanly_and_keeps_unpicked_sources_available(rig):
    session, capture, robot, recovery = rig
    capture.side_effect = [(scene(11, [(200, 500), (300, 550), (400, 650)]), 101),
                           (scene(12, [(300, 550), (400, 650)]), 102),
                           (scene(13, [(400, 650)]), 103)]
    session.execute_batch(capture_id="frame-10")
    assert len(robot.jobs) == 3 * TRANSFER_JOB_COUNT
    assert session.batch_progress["state"] == "completed"
    assert session.batch_progress["completion_reason"] == "grid_full"
    assert session.batch_progress["excluded_cup_ids"] == [4]
    assert [cup["id"] for cup in session.inventory["cups"] if cup["selectable"]] == [4]
    assert not session.motion_uncertain and not recovery.required()


def test_ambiguous_fresh_sources_are_skipped_while_clear_cup_continues(rig):
    session, capture, robot, recovery = rig
    capture.side_effect = [(scene(11, [(198, 499), (202, 501), (300, 550)]), 101),
                           (scene(12, [(198, 499), (202, 501)]), 102)]
    session.execute_batch(capture_id="frame-10")
    assert session.batch_progress["completed_cups"] == [1, 3]
    assert len(robot.jobs) == 2 * TRANSFER_JOB_COUNT
    assert session.batch_progress["completion_reason"] == "no_pickable_cups"
    assert not session.motion_uncertain and not recovery.required()


def test_empty_workspace_releases_pending_reservations_without_more_motion(rig):
    session, capture, robot, _ = rig
    capture.return_value = (scene(11, []), 101)
    session.execute_batch(capture_id="frame-10")
    assert len(robot.jobs) == TRANSFER_JOB_COUNT
    assert session.batch_progress["completion_reason"] == "workspace_empty"
    assert [slot["occupied"] for slot in session.placing_grid.snapshot()["locations"]] == [True, False, False]


def test_stop_during_replan_cannot_start_the_refreshed_queue(rig):
    session, capture, robot, recovery = rig
    capture.return_value = (scene(11, [(200, 500), (300, 550)]), 101)
    real = session.placing_grid.replan
    def stopped(*args, **kwargs):
        result = real(*args, **kwargs)
        session.latch_stop()
        return result
    with patch.object(session.placing_grid, "replan", side_effect=stopped):
        with pytest.raises(SelectionError, match="Software stop"):
            session.execute_batch(capture_id="frame-10")
    assert len(robot.jobs) == TRANSFER_JOB_COUNT
    assert session.motion_uncertain and recovery.required() and not session.scan_usable


def test_grid_cups_are_ignored_and_never_become_new_selections(rig):
    session, capture, _, _ = rig
    fresh = scene(11, [(100, 450), (200, 500), (300, 550)])
    # Coordinates deliberately overlap the old source; tagged grid observations are excluded.
    fresh["cups"][0].update(placement_location_ids=[1], selectable=False, blocked_reason="Placed cup")
    fresh["placement_observation"] = "not consulted"
    capture.return_value = (fresh, 101)
    pick(session)
    assert session.inventory["workspace_check"]["matched_cup_ids"] == [2, 3]
    assert [c["id"] for c in session.inventory["cups"] if c["selectable"]] == [2, 3]
    assert not any(c.get("placement_location_ids") for c in session.inventory["cups"])


def test_camera_failure_needs_scan_but_placement_remains_confirmed(rig):
    session, capture, robot, recovery = rig
    capture.side_effect = RuntimeError("camera offline")
    with pytest.raises(WorkspaceCheckError, match="camera offline"):
        pick(session)
    assert len(robot.jobs) == TRANSFER_JOB_COUNT
    assert session.inventory["cups"][0]["status"] == "placed"
    assert not session.motion_uncertain and not recovery.required() and not session.scan_usable


def test_failed_left_park_never_captures_or_allows_next_cup(rig):
    session, capture, robot, recovery = rig
    robot.error, robot.error_at = RobotCommandError("left park failed"), "left_park"
    with pytest.raises(RobotCommandError, match="left park failed"):
        session.execute_batch(capture_id="frame-10")
    capture.assert_called_once_with("scan")
    assert session.motion_uncertain and recovery.required() and not session.scan_usable


@pytest.fixture(params=["taught_fixture", "right_via"])
def parked_rig(tmp_path, request):
    # Synthetic offline fixture, never saved as an operator-taught/default pose.
    pose = {"x": 380, "y": -350, "z": 600, "rx": 170, "ry": -15, "rz": 25}
    if request.param == "right_via":
        pose = right_via_pose()
    capture = Mock(return_value=(scene(10, [(100, 450), (200, 500), (300, 550)]), 99))
    robot = RobotDouble()
    recovery = RecoveryJournal(tmp_path / "recovery.json")
    session = CupSession(capture, Mock(), robot, "cal-1", placing_grid=grid_fixture(tmp_path),
                         recovery=recovery, between_cup_check=True, right_park_pose=pose)
    session.scan()
    return session, capture, robot, recovery


def test_workspace_capture_waits_for_optional_right_park_to_report_completion(parked_rig):
    session, capture, _, recovery = parked_rig
    robot = RobotClient("http://offline.invalid/api/v1", {})
    session.robot = robot
    park_started, finish_park = threading.Event(), threading.Event()
    calls, errors = [], []

    def perform(job):
        calls.append(copy.deepcopy(job))
        if job["name"] == "right_park":
            park_started.set()
            if not finish_park.wait(3):
                raise AssertionError("Test did not release the simulated parking completion")

    def observe(purpose):
        assert purpose == "between_cups"
        assert finish_park.is_set()
        execution = session.inventory["cups"][0]["execution"]
        assert execution["state"] == "completed"
        assert "right_park" in [job["name"] for job in execution["completed_steps"]]
        assert not execution["pending_jobs"]
        assert not recovery.required()
        return scene(11, [(200, 500), (300, 550)]), 101

    def execute():
        try:
            pick(session)
        except BaseException as error:
            errors.append(error)

    capture.side_effect = observe
    # Retain RobotClient's real two-arm scheduler and completion callbacks;
    # replace only control/network operations with immediate offline results.
    with patch.object(robot, "_take_control"), patch.object(robot, "_renew_loop"), \
            patch.object(robot, "_perform_job", side_effect=perform), \
            patch.object(robot, "read_gripper_position", return_value=None), \
            patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")):
        worker = threading.Thread(target=execute, daemon=True)
        worker.start()
        try:
            assert park_started.wait(2), f"Parking was never reached: {errors!r}"
            assert worker.is_alive()
            capture.assert_called_once_with("scan")
            execution = session.inventory["cups"][0]["execution"]
            assert "right_park" in execution["pending_jobs"]
            assert "right_park" not in [job["name"] for job in execution["completed_steps"]]
            assert recovery.required() and session.motion_uncertain
        finally:
            finish_park.set()
            worker.join(3)
        assert not worker.is_alive()
    assert errors == []
    assert len(calls) == TRANSFER_JOB_COUNT + 1
    assert capture.call_args_list == [(('scan',),), (('between_cups',),)]
    assert session.scan_usable and not session.motion_uncertain and not recovery.required()


def test_failed_optional_right_park_prevents_capture_and_next_cup(parked_rig):
    session, capture, robot, recovery = parked_rig
    robot.error, robot.error_at = RobotCommandError("right park failed"), "right_park"
    with pytest.raises(RobotCommandError, match="right park failed"):
        session.execute_batch(capture_id="frame-10")
    capture.assert_called_once_with("scan")
    assert len(robot.jobs) == TRANSFER_JOB_COUNT + 1
    assert robot.jobs[-1]["name"] == "right_park"
    first = session.inventory["cups"][0]
    assert first["status"] == "movement_unconfirmed"
    assert first["execution"]["state"] == "interrupted"
    assert first["execution"]["last_completed_step"] == "right_clear"
    assert "right_park" in first["execution"]["pending_jobs"]
    assert session.batch_progress["state"] == "interrupted"
    assert session.batch_progress["completed_cups"] == []
    assert session.motion_uncertain and recovery.required() and not session.scan_usable
    with pytest.raises(SelectionError, match="Previous movement was not confirmed"):
        pick(session, 2)
    assert len(robot.jobs) == TRANSFER_JOB_COUNT + 1
    capture.assert_called_once_with("scan")


def test_parked_batch_continues_directly_via_next_plan_without_home_or_hover_return(parked_rig):
    session, capture, robot, recovery = parked_rig
    baseline = copy.deepcopy(session.inventory)
    preview = session.preview_batch(capture_id="frame-10")
    expected = [handover_jobs(cup, place=place, right_park_pose=session.right_park_pose)
                for cup, place in zip(baseline["cups"][:2], preview["placement"]["locations"][:2])]
    observations = [scene(11, [(200, 500)]), scene(12, [])]
    at_capture = []

    def observe(purpose):
        assert purpose == "between_cups"
        completed = len(at_capture) + 1
        assert len(robot.jobs) == sum(len(plan) for plan in expected[:completed])
        assert robot.jobs[-1]["name"] == "right_park"
        assert robot.jobs[-1]["body"]["points"][0] == expected[completed - 1][-1]["body"]["points"][0]
        at_capture.append(len(robot.jobs))
        return observations[completed - 1], 100 + completed

    capture.side_effect = observe
    jobs = session.execute_batch(capture_id="frame-10")
    assert jobs == robot.jobs == expected[0] + expected[1]
    assert sum(job["name"] == "right_via" for job in jobs) == 2
    first_right_paths = [job for job in jobs[:at_capture[0]]
                         if job["body"]["arm"] == "right" and "points" in job["body"]]
    assert first_right_paths[0]["name"] == "right_via"
    second_right_paths = [job for job in jobs[at_capture[0]:]
                          if job["body"]["arm"] == "right" and "points" in job["body"]]
    assert second_right_paths[0]["name"] == "right_via"
    assert len(second_right_paths[0]["body"]["points"]) == 1
    assert second_right_paths[0] == next(job for job in expected[1] if job["name"] == "right_via")
    assert not any("home" in job["name"] or "camera_return" in job["name"] for job in jobs)
    assert not any(job["endpoint"] == RobotClient.HOME_ENDPOINT for job in jobs)
    assert session.batch_progress["completed_cups"] == [1, 2]
    assert session.batch_progress["completion_reason"] == "workspace_empty"
    assert not session.home_after_cup
    assert session.scan_usable and not session.motion_uncertain and not recovery.required()


def test_stop_during_camera_capture_stays_latched_and_preserves_recovery(rig):
    session, capture, robot, recovery = rig
    def stopped(_):
        session.latch_stop()
        return scene(11, [(200, 500), (300, 550)]), 101
    capture.side_effect = stopped
    with pytest.raises(SelectionError, match="Software stop"):
        session.execute_batch(capture_id="frame-10")
    assert len(robot.jobs) == TRANSFER_JOB_COUNT
    assert session.stop_requested and session.motion_uncertain and recovery.required()
    assert not session.scan_usable


def test_failed_recovery_clear_cannot_be_hidden_as_camera_failure(rig):
    session, capture, _, recovery = rig
    with patch.object(recovery, "clear", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            pick(session)
    capture.assert_called_once_with("scan")
    assert session.motion_uncertain and recovery.required() and not session.scan_usable


def test_next_cup_planning_failure_does_not_reopen_cleared_motion_recovery(rig):
    session, capture, robot, recovery = rig
    capture.return_value = (scene(11, [(200, 500), (300, 550)]), 101)
    def plan(*args, **kwargs):
        if len(robot.jobs) == TRANSFER_JOB_COUNT:
            raise SelectionError("Refreshed target cannot be planned")
        return handover_jobs(*args, **kwargs)
    with patch("cup_commands.handover_jobs", side_effect=plan):
        with pytest.raises(SelectionError, match="cannot be planned"):
            session.execute_batch(capture_id="frame-10")
    assert len(robot.jobs) == TRANSFER_JOB_COUNT
    assert session.batch_progress["completed_cups"] == [1]
    assert not session.motion_uncertain and not recovery.required() and not session.scan_usable


@pytest.mark.parametrize("policy", ["disabled", "hover", "dry_run"])
def test_other_execution_modes_keep_snapshot_policy(rig, policy):
    session, capture, robot, _ = rig
    if policy == "disabled":
        session.between_cup_check = False
    if policy == "dry_run":
        robot.dry_run = True
    session.execute_selection(action="hover" if policy == "hover" else "goto", capture_id="frame-10", cup_id=1)
    capture.assert_called_once_with("scan")


def test_ui_publishes_fresh_image_with_original_selection_identity(rig):
    session, capture, _, _ = rig
    capture.return_value = (scene(11, [(200, 500), (300, 550)]), 101)
    pick(session)
    controller = WebController(lambda _: None, mode="live")
    controller.session = session
    controller.publish_session()
    state = controller.state()
    assert state["motion"]["between_cup_check"] is True
    assert state["inventory"]["capture"] == "frame-10"
    assert state["inventory"]["image_capture"] == "frame-11"
    assert state["inventory"]["image_url"] == "/api/image/frame-11"
    assert state["inventory"]["workspace_check"]["grid_checked"] is False
    assert str(controller.image("frame-11")) == "/offline/11.png"
    assert controller.image("frame-10") is None
    # The combined dashboard must proxy the new image identity, while selection stays bound to the scan.
    dashboard = object.__new__(DeviceDashboard)
    dashboard._lock, dashboard._epoch = threading.RLock(), 1
    dashboard.record = {"device_id": "mac"}
    dashboard.state = Mock(return_value=state)
    client = Mock()
    client.image.return_value = b"new-camera-photo"
    dashboard.clients = {"mac": client}
    assert dashboard.image_bytes("frame-11") == b"new-camera-photo"
    assert dashboard.image_bytes("frame-10") is None
    client.image.assert_called_once_with("frame-11")
