"""Offline final-clearance target, dependency and coordinated-version regressions."""
import copy

import pytest

from cup_inventory import SelectionError
from handover_plan import (handover_jobs, right_camera_park_jobs, right_camera_park_pose,
                           right_via_pose)
from test_failover_workflow import rig
from test_queued_robot_client import gateway


@pytest.mark.parametrize("speed,placement,expected", [(180, 90, 90), (75, 120, 75), (80, None, 80)])
def test_right_camera_retreat_follows_release_lift_open_with_capped_speed(speed, placement, expected):
    cup = {"x": 500, "y": 100, "z": 50, "rz": 150, "aperture": 100, "selectable": True}
    place = {"id": 2, "x": 350, "y": -260, "z": 230, "occupied": False}
    phases = {"pick_lift": 100, "handover": None, "flip": 100, "placement": placement}
    jobs = handover_jobs(cup, place=place, speed=speed, phase_speeds=phases,
                         right_park_pose=right_camera_park_pose())
    names = [job["name"] for job in jobs]
    assert len(names) == len(set(names))
    right = [job for job in jobs if job["body"]["arm"] == "right"]
    assert [job["name"] for job in right[-4:]] == [
        "right_release", "right_clear", "right_open_after_clear", "right_park"]
    assert right[-4]["body"]["action"] == "move"
    assert right[-3]["body"]["points"][-1]["z"] == place["z"] + 80
    assert right[-3]["body"]["points"][-1]["linear"] is True
    assert right[-2]["body"]["action"] == "open"
    for previous, current in zip(right, right[1:]):
        assert previous["name"] in current["after"]
    park = right[-1]
    target = park["body"]["points"][-1]
    assert {axis: target[axis] for axis in ("x", "y", "z", "rx", "ry", "rz")} == right_camera_park_pose()
    assert target["zone"] == park["body"]["zone"] == 0
    assert target["speed"] == park["body"]["speed"] == expected
    assert target["linear"] is False
    inherited = right_camera_park_jobs(speed=speed, phase_speeds=phases)
    assert len(inherited) == 1 and inherited[0]["body"] == park["body"]
    via = next(job for job in jobs if job["name"] == "right_via")["body"]["points"][-1]
    assert {axis: via[axis] for axis in ("x", "y", "z", "rx", "ry", "rz")} == right_via_pose()
    assert right_via_pose() != right_camera_park_pose()
    assert all(not job["endpoint"].endswith("/home") for job in jobs)


def test_camera_park_pose_returns_a_fresh_copy():
    pose = right_camera_park_pose()
    assert pose == {"x": 200, "y": -380, "z": 250, "rx": 0, "ry": 0, "rz": 0}
    pose["x"] = 900
    assert right_camera_park_pose()["x"] == 200


def test_coordinated_identity_records_and_accepts_matching_parking_pose(rig):
    rig.session._right_park_pose = right_camera_park_pose()
    settings = rig.runner.settings()
    assert settings["identity"]["right_park_pose"] == right_camera_park_pose()
    rig.runner._apply_settings(settings)
    settings["identity"]["right_park_pose"]["x"] += 1
    assert rig.runner.settings()["identity"]["right_park_pose"] == right_camera_park_pose()
    assert not rig.gateway.calls


@pytest.mark.parametrize("kind", ["old_worker", "different_pose"])
def test_coordinated_identity_rejects_missing_or_different_parking_before_io(rig, kind):
    rig.session._right_park_pose = right_camera_park_pose()
    settings = copy.deepcopy(rig.runner.settings())
    if kind == "old_worker":
        del settings["identity"]["right_park_pose"]
    else:
        settings["identity"]["right_park_pose"] = right_via_pose()
    with pytest.raises(SelectionError, match="parking pose"):
        rig.runner._apply_settings(settings)
    assert not rig.gateway.calls
    rig.capture.assert_not_called()
