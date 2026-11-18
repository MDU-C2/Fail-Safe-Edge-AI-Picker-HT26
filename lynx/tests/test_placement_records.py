"""Placement scan bookkeeping retains the actual amount of camera evidence."""
import copy

import pytest

from cup_inventory import SelectionError
from lynx.placement_records import combine


def snapshot():
    return {"fingerprint": "a" * 64, "cell_size_mm": 90,
            "locations": [{"id": 2, "x": 400, "y": -200, "z": 230,
                           "occupied": True, "reserved": True,
                           "reservation_id": "existing-batch"}]}


def slot(state="clear", **changes):
    record = {"id": 2, "state": state, "reason": "Native depth reason.",
              "reason_code": "clear" if state == "clear" else "table_depth",
              "polygon_px": [[1, 1], [6, 1], [6, 5], [1, 5]],
              "valid_fraction": 0.999, "table_fraction": 0.998,
              "visible_fraction": 0.9, "floor_visible_fraction": 0.8,
              "assigned_cups": [], "cup_count": 0, "evidence": []}
    record.update(changes)
    return record


@pytest.mark.parametrize("state", ["clear", "occupied", "unknown"])
def test_one_observation_preserves_native_reason_metrics_and_untouched_snapshot(state):
    grid = snapshot()
    observations = [[slot(state)]]
    original_grid, original_observations = copy.deepcopy(grid), copy.deepcopy(observations)
    result = combine(grid, observations, [12])
    assert result["frame_count"] == 1 and result["frame_sequences"] == [12]
    assert result["slots"] == observations[0]
    assert result["grid_snapshot"] == original_grid
    assert result["applied"] is False and result["released_ids"] == []
    assert result["message"] == "Placement area checked against one fresh depth observation."
    assert (grid, observations) == (original_grid, original_observations)
    result["slots"][0]["reason"] = "changed output"
    result["grid_snapshot"]["locations"][0]["occupied"] = False
    assert (grid, observations) == (original_grid, original_observations)


def test_three_observations_keep_any_obstacle_and_count_only_last_frame_cups():
    cup = {"object_indices": [0, 1]}
    observations = [[slot("occupied", assigned_cups=[cup], cup_count=1, evidence=["cup"])],
                    [slot("unknown", valid_fraction=0.6)], [slot()]]
    result = combine(snapshot(), observations, [5, 10, 15])
    assert result["frame_count"] == 3
    assert result["slots"][0]["state"] == "occupied"
    assert result["slots"][0]["evidence"] == ["cup"]
    assert result["slots"][0]["valid_fraction"] == 0.6
    assert result["slots"][0]["assigned_cups"] == []
    assert result["slots"][0]["cup_count"] == 0
    assert result["cup_summary"] == {"assigned_count": 0, "location_ids": [],
        "min_assigned_count": 0, "max_assigned_count": 1, "stable_cell_counts": False}


def test_three_observations_need_all_clear_and_single_unknown_keeps_specific_reason():
    unknown = slot("unknown", reason="Depth sample below the table.", reason_code="table_depth")
    combined = combine(snapshot(), [[slot()], [unknown], [slot()]], [5, 10, 15])
    assert combined["slots"][0]["state"] == "unknown"
    assert combined["slots"][0]["reason_code"] == "inconsistent_frames"
    single = combine(snapshot(), [[unknown]], [10])
    assert single["slots"][0]["reason"] == "Depth sample below the table."
    assert single["slots"][0]["reason_code"] == "table_depth"


@pytest.mark.parametrize("observations,sequences", [
    ([], []), ([[slot()]] * 2, [1, 2]), ([[slot()]] * 4, [1, 2, 3, 4]),
    ([[slot()]], []), ([[slot()]], [1, 2, 3]), ([[slot()]], [True]),
    ([[slot()]], [-1]), ([[slot()]], [1.0]),
    ([[slot()]] * 3, [1, 1, 2]), ([[slot()]] * 3, [3, 2, 1]),
])
def test_unsupported_observation_counts_and_nonadvancing_sequences_rejected(observations, sequences):
    with pytest.raises(SelectionError, match="one or three distinct advancing"):
        combine(snapshot(), observations, sequences)


@pytest.mark.parametrize("count", [1, 3])
def test_every_observation_must_match_snapshot_location_ids(count):
    observations = [[slot()] for _ in range(count)]
    observations[-1][0]["id"] = 3
    with pytest.raises(SelectionError, match="geometry changed"):
        combine(snapshot(), observations, list(range(1, count + 1)))


def test_three_observations_must_have_the_same_camera_polygon():
    observations = [[slot()] for _ in range(3)]
    observations[1][0]["polygon_px"][0][0] = 2
    with pytest.raises(SelectionError, match="geometry changed"):
        combine(snapshot(), observations, [1, 2, 3])


@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("fraction", [None, True, "1", -0.01, 1.01, float("nan"), float("inf")])
def test_every_observation_requires_finite_bounded_valid_fraction(count, fraction):
    observations = [[slot()] for _ in range(count)]
    observations[0][0]["valid_fraction"] = fraction
    with pytest.raises(SelectionError, match="invalid valid_fraction"):
        combine(snapshot(), observations, list(range(1, count + 1)))


@pytest.mark.parametrize("count", [1, 3])
def test_missing_valid_fraction_cannot_authorize_clearance(count):
    observations = [[slot()] for _ in range(count)]
    del observations[0][0]["valid_fraction"]
    with pytest.raises(SelectionError, match="invalid valid_fraction"):
        combine(snapshot(), observations, list(range(1, count + 1)))


@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("evidence", [None, 123, "cup", {"cup": True}, [1], [[]]])
def test_malformed_evidence_cannot_bypass_single_or_three_view_validation(count, evidence):
    observations = [[slot()] for _ in range(count)]
    observations[0][0]["evidence"] = evidence
    with pytest.raises(SelectionError, match="list of strings"):
        combine(snapshot(), observations, list(range(1, count + 1)))


@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("fraction", [0, 1])
def test_supported_legacy_evidence_and_fraction_boundaries_are_preserved(count, fraction):
    observations = [[slot("unknown", valid_fraction=fraction)] for _ in range(count)]
    for frame in observations:
        del frame[0]["evidence"]
        del frame[0]["assigned_cups"]
    result = combine(snapshot(), observations, list(range(1, count + 1)))
    assert result["slots"][0]["valid_fraction"] == fraction
    assert result["slots"][0]["state"] == "unknown"
    assert "evidence" not in result["slots"][0]
    assert "cup_summary" not in result
