"""Fixed V4 handover poses and compatibility with previously saved offsets."""

from unittest.mock import Mock

import pytest

from cup_commands import CupSession
from cup_inventory import SelectionError
from handover_plan import handover_jobs, handover_settings, normalize_handover_offset


def plan(**settings):
    return handover_jobs({"x": 500, "y": 100, "z": 50},
                         place={"id": 1, "x": 420, "y": -200, "z": 230, "occupied": False},
                         **settings)


@pytest.mark.parametrize("offset", ((0, 0, 0), (2, -3, 4), (-20, 20, -20), (100, 200, 300)))
@pytest.mark.parametrize("speed", (1, 25, 100, 1200))
def test_legacy_offsets_cannot_change_poses_speeds_blends_or_dependencies(offset, speed):
    assert plan(handover_offset=offset, speed=speed) == plan(speed=speed)
    bodies = {job["name"]: job["body"] for job in plan(handover_offset=offset, speed=speed)}
    assert tuple(bodies["right_take"][axis] for axis in "xyz") == (330, -115, 316)
    assert bodies["right_take"]["speed"] == speed
    assert [point["zone"] for point in bodies["right_beside"]["points"]] == [10, 0]


@pytest.mark.parametrize("value", (None, {}, "1,2,3", (), (1, 2), (1, 2, 3, 4),
                                    (True, 0, 0), (0, 0, "1"), (float("nan"), 0, 0),
                                    (0, float("inf"), 0), (0, 0, 10 ** 1000)))
def test_legacy_config_still_requires_a_finite_numeric_triple(value):
    with pytest.raises(SelectionError):
        normalize_handover_offset(value)


def test_reported_motion_settings_use_the_fixed_receiving_pose():
    assert handover_settings((2, -3, 4), speed=100) == {
        "offset_mm": {"x": 0, "y": 0, "z": 0},
        "receive_mm": {"x": 330, "y": -115, "z": 316},
        "approach_speed": 100, "adjusted": False,
    }


def test_session_startup_discards_previously_saved_receiving_adjustment():
    session = CupSession(Mock(), Mock(), Mock(), "cal-1", handover_offset=[2, -3, 4])
    assert session.handover_offset == (0, 0, 0)
    assert session.motion_settings()["handover"]["receive_mm"] == {"x": 330, "y": -115, "z": 316}
