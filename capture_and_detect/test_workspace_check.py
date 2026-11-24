"""Offline source-only scene comparison, independent of motion and grid state."""

from copy import deepcopy

import pytest

from cup_inventory import SelectionError
from workspace_check import check_workspace, refresh_workspace


def cup(identifier=1, *, x=500, y=100, z=50, u=100, v=100, **changes):
    value = {"id": identifier, "x": x, "y": y, "z": z,
             "pixel": {"u": u, "v": v}, "selectable": True,
             "blocked_reason": None, "status": "available"}
    value.update(changes)
    return value


def observation(cups=None, *, capture="scan", sequence=10, **changes):
    value = {"capture": capture, "frame": {"sequence": sequence, "timestamp": "same-second"},
             "calibration_sha256": "cal", "tag_offset_mm": [0, 0, 0],
             "gateway": "http://camera.invalid/api/v1",
             "cups": [cup()] if cups is None else cups}
    value.update(changes)
    return value


def fresh(cups=None, **changes):
    return observation(cups, capture="after-cup", sequence=11, **changes)


def check(previous, current, **options):
    return check_workspace(previous, current, calibration_id="cal", **options)


def test_matches_original_ids_after_renumbering_without_mutation():
    previous = observation([cup(4), cup(9, x=600, u=300)])
    current = fresh([cup(1, x=508, u=103), cup(2, x=597, u=299)])
    before = deepcopy((previous, current))
    result = check(previous, current)
    assert result["matched_cup_ids"] == [4, 9]
    assert result["matched_count"] == 2
    assert result["matches"][4]["id"] == 1
    assert result["matches"][9]["x"] == 597
    assert result["capture"] == "after-cup"
    assert result["frame"] == current["frame"]
    result["matches"][4]["pixel"]["u"] = -1
    result["frame"]["sequence"] = -1
    assert (previous, current) == before


@pytest.mark.parametrize("completion", [
    {"status": "placed", "selectable": False},
    {"status": "replaced", "selectable": False},
    {"execution": {"state": "completed", "action": "goto"}},
])
def test_expected_completed_source_removal_is_allowed(completion):
    previous = observation([cup(1, **completion), cup(2, x=600, u=300)])
    result = check(previous, fresh([cup(1, x=600, u=300)]))
    assert list(result["matches"]) == [2]


def test_hover_completion_does_not_remove_required_source():
    previous = observation([cup(execution={"state": "completed", "action": "hover"})])
    with pytest.raises(SelectionError, match="source cup 1 moved, disappeared"):
        check(previous, fresh([]))


def test_last_cup_check_accepts_empty_source_area_and_ignores_grid():
    previous = observation([cup(status="placed", selectable=False)], placement_observation={"broken": True})
    current = fresh([cup(8, placement_location_ids=[1])], placement_observation={"changed": "ignored"})
    result = check(previous, current)
    assert result["matches"] == {}
    assert result["matched_count"] == 0


def test_grid_observations_do_not_match_sources_or_create_ambiguity():
    previous = observation([cup(3), cup(5, placement_location_ids=[2])])
    current = fresh([cup(2), cup(2, placement_location_ids=[9])])
    assert list(check(previous, current)["matches"]) == [3]


def test_source_seen_only_in_placement_area_is_missing():
    with pytest.raises(SelectionError, match="source cup 1 moved, disappeared"):
        check(observation(), fresh([cup(1, placement_location_ids=[1])]))


def test_excluded_original_cups_need_not_remain_and_excluded_fresh_cups_are_not_added():
    previous = observation([cup(1), cup(2, selectable=False, blocked_reason="missing depth", z=None),
                            cup(3, x=900, u=800, selectable=False)])
    current = fresh([cup(5), cup(6, x=250, u=800),
                     cup(7, x=700, u=900, selectable=False, blocked_reason="occluded")])
    assert list(check(previous, current)["matches"]) == [1]


def test_formerly_blocked_source_cannot_be_automatically_admitted():
    previous = observation([cup(1), cup(2, x=600, u=300, selectable=False, blocked_reason="occluded")])
    with pytest.raises(SelectionError, match="additional source cup"):
        check(previous, fresh([cup(5), cup(6, x=600, u=300)]))


@pytest.mark.parametrize("changes", [
    {"x": 521}, {"y": 121}, {"z": 71}, {"u": 141}, {"v": 141},
])
def test_movement_in_either_coordinates_requires_rescan(changes):
    with pytest.raises(SelectionError, match="source cup 1 moved, disappeared"):
        check(observation(), fresh([cup(**changes)]))


def test_tolerance_is_euclidean_and_inclusive():
    assert check(observation(), fresh([cup(x=512, y=116, u=124, v=132)]))["matched_count"] == 1
    with pytest.raises(SelectionError, match="source cup 1 moved"):
        check(observation(), fresh([cup(x=513, y=116)]))


@pytest.mark.parametrize("changes", [
    {"selectable": False, "blocked_reason": "arm covers the cup"},
    {"z": None, "selectable": False, "blocked_reason": "missing depth"},
    {"pixel": {"u": None, "v": None}, "selectable": False},
])
def test_fresh_blocked_or_partial_match_is_rejected(changes):
    with pytest.raises(SelectionError, match="became blocked or lost usable coordinates"):
        check(observation(), fresh([cup(**changes)]))


def test_partial_nearby_detection_cannot_be_discarded_to_force_a_unique_match():
    current = fresh([cup(1), cup(2, x=None, z=None, u=102, selectable=False, blocked_reason="occluded")])
    with pytest.raises(SelectionError, match="ambiguous match"):
        check(observation(), current)


def test_two_fresh_matches_and_reverse_ambiguity_are_rejected():
    with pytest.raises(SelectionError, match="ambiguous match"):
        check(observation(), fresh([cup(1, x=495), cup(2, x=505)]))
    with pytest.raises(SelectionError, match="ambiguous match"):
        check(observation([cup(1, x=490), cup(2, x=510)]), fresh([cup(5)]))


def test_new_source_and_still_visible_completed_source_require_rescan():
    with pytest.raises(SelectionError, match="additional source cup"):
        check(observation(), fresh([cup(1), cup(2, x=600, u=300)]))
    with pytest.raises(SelectionError, match="additional source cup"):
        check(observation([cup(1, status="placed", selectable=False)]), fresh())


def test_immutable_scan_baseline_prevents_gradual_drift():
    baseline = observation([cup(4)])
    first = fresh([cup(1, x=515)])
    matched = check(baseline, first, baseline_inventory=baseline)["matches"][4]
    previous = deepcopy(first)
    previous["cups"] = [dict(matched, id=4)]
    second = observation([cup(1, x=530)], capture="second", sequence=12)
    # Rolling-only comparison would accept another 15 mm move.
    assert check(previous, second)["matched_count"] == 1
    with pytest.raises(SelectionError, match="source cup 4 moved, disappeared"):
        check(previous, second, baseline_inventory=baseline)


def test_both_latest_observation_and_original_scan_bound_movement():
    baseline = observation([cup(4)])
    previous = observation([cup(4, x=485)], capture="first", sequence=11)
    current = observation([cup(1, x=515)], capture="second", sequence=12)
    with pytest.raises(SelectionError, match="moved since the previous observation"):
        check(previous, current, baseline_inventory=baseline)


def test_original_baseline_uses_current_completion_state():
    baseline = observation([cup(4), cup(9, x=600, u=300)])
    previous = deepcopy(baseline)
    previous["cups"][0].update(status="placed", selectable=False)
    assert list(check(previous, fresh([cup(1, x=600, u=300)]),
                      baseline_inventory=baseline)["matches"]) == [9]


@pytest.mark.parametrize("change", ["missing", "blocked", "unreviewed"])
def test_active_scan_cannot_drop_or_authorize_original_ids(change):
    baseline = observation([cup(4)])
    previous = deepcopy(baseline)
    if change == "missing":
        previous["cups"] = []
    elif change == "blocked":
        previous["cups"][0]["selectable"] = False
    else:
        previous["cups"].append(cup(9, x=600, u=300))
    with pytest.raises(SelectionError, match="active scan"):
        check(previous, fresh(), baseline_inventory=baseline)


@pytest.mark.parametrize("sequence", [0, 9, 10])
def test_sequence_must_advance_even_when_timestamp_or_capture_changes(sequence):
    current = fresh()
    current["frame"].update(sequence=sequence, timestamp="a-later-looking-timestamp")
    with pytest.raises(SelectionError, match="frame did not advance"):
        check(observation(), current)


def test_advancing_sequence_allows_same_second_timestamp():
    assert check(observation(), fresh())["matched_count"] == 1


def test_capture_id_cannot_repeat_previous_or_original_scan():
    with pytest.raises(SelectionError, match="repeated a previous capture"):
        check(observation(), observation(sequence=11))
    previous = observation(capture="latest", sequence=11)
    with pytest.raises(SelectionError, match="repeated a previous capture"):
        check(previous, observation(sequence=12), baseline_inventory=observation())


@pytest.mark.parametrize("change, message", [
    ({"calibration_sha256": "different"}, "calibration no longer matches"),
    ({"tag_offset_mm": [1, 0, 0]}, "offsets no longer match"),
    ({"tag_offset_mm": [None, 0, 0]}, "offsets are invalid"),
    ({"gateway": "http://other.invalid/api/v1"}, "gateway no longer matches"),
    ({"gateway": ""}, "gateway identity is missing"),
    ({"capture": " "}, "capture identity is missing"),
    ({"frame": {"sequence": True}}, "frame identity is invalid"),
    ({"frame": {"sequence": -1}}, "frame identity is invalid"),
    ({"cups": None}, "observations are invalid"),
])
def test_invalid_or_changed_observation_metadata_fails(change, message):
    current = fresh()
    current.update(change)
    with pytest.raises(SelectionError, match=message):
        check(observation(), current)


def test_trailing_gateway_slash_is_not_a_change():
    current = fresh()
    current["gateway"] += "/"
    assert check(observation(), current)["matched_count"] == 1


@pytest.mark.parametrize("identifier", [True, 0, "1", None])
def test_invalid_source_id_is_rejected(identifier):
    with pytest.raises(SelectionError, match="identities are ambiguous"):
        check(observation(), fresh([cup(identifier)]))


def test_duplicate_source_ids_are_rejected():
    with pytest.raises(SelectionError, match="identities are ambiguous"):
        check(observation(), fresh([cup(1), cup(1, x=600, u=300)]))


@pytest.mark.parametrize("name, value", [
    ("match_mm", 0), ("match_mm", True), ("match_mm", float("nan")),
    ("match_px", -1), ("match_px", float("inf")),
])
def test_invalid_tolerances_fail(name, value):
    with pytest.raises(SelectionError, match="positive finite number"):
        check(observation(), fresh(), **{name: value})


# Dynamic refresh deliberately differs from the strict compatibility helper:
# geometry may change, but observation identity and candidate clarity still matter.
def refresh(previous, current, **options):
    return refresh_workspace(previous, current, calibration_id="cal", **options)


def test_refresh_retains_only_unique_nearby_ids_and_uses_fresh_geometry():
    previous = observation([cup(4, last_action="hover", execution={"action": "hover", "state": "completed"}),
                            cup(9, x=600, u=300)])
    current = fresh([cup(1, x=508, u=105, confidence=.95), cup(2, x=597, u=299)])
    result = refresh(previous, current)
    assert result["matched_cup_ids"] == [4, 9]
    assert result["matched_count"] == 2
    assert result["added_cup_ids"] == result["removed_cup_ids"] == []
    assert result["state"] == "unchanged"
    assert result["next_cup_id"] == 10
    assert [item["id"] for item in result["cups"]] == [4, 9]
    assert result["cups"][0]["observed_cup_id"] == 1
    assert result["cups"][0]["x"] == 508
    assert result["cups"][0]["execution"]["action"] == "hover"
    assert all(item["selectable"] for item in result["cups"])


def test_refresh_far_move_addition_and_disappearance_rebuild_current_candidates():
    previous = observation([cup(4), cup(9, x=600, u=300)])
    current = fresh([cup(1, x=700, u=500), cup(2, x=780, u=700)])
    result = refresh(previous, current)
    assert result["matched_cup_ids"] == []
    assert result["removed_cup_ids"] == [4, 9]
    assert result["added_cup_ids"] == [10, 11]
    assert result["next_cup_id"] == 12
    assert result["state"] == "refreshed"
    assert [item["x"] for item in result["cups"]] == [700, 780]
    assert all(item["selectable"] for item in result["cups"])


def test_refresh_no_baseline_restriction_allows_incremental_changes():
    first = refresh(observation(), fresh([cup(x=515)]))
    previous = observation(first["cups"], capture=first["capture"], sequence=first["frame"]["sequence"],
                           next_cup_id=first["next_cup_id"])
    result = refresh(previous, observation([cup(x=530)], capture="next", sequence=12))
    assert result["matched_cup_ids"] == [1]
    assert result["cups"][0]["x"] == 530


def test_refresh_monotonic_ids_survive_removal_of_highest_pending_id():
    first = refresh(observation([cup(4)]), fresh([cup(1, x=700, u=500)]))
    assert first["added_cup_ids"] == [5]
    previous = observation(first["cups"], capture="after-cup", sequence=11, next_cup_id=first["next_cup_id"])
    second = refresh(previous, observation([], capture="empty", sequence=12))
    assert second["removed_cup_ids"] == [5]
    assert second["next_cup_id"] == 6
    previous = observation(second["cups"], capture="empty", sequence=12, next_cup_id=second["next_cup_id"])
    third = refresh(previous, observation([cup()], capture="new-source", sequence=13))
    assert third["added_cup_ids"] == [6]
    assert third["next_cup_id"] == 7


def test_refresh_watermark_never_goes_below_previous_source_or_grid_ids():
    previous = observation([cup(12, placement_location_ids=[2])], next_cup_id=2)
    result = refresh(previous, fresh())
    assert result["added_cup_ids"] == [13]
    assert result["next_cup_id"] == 14
    assert [item["id"] for item in result["cups"]] == [13]


@pytest.mark.parametrize("watermark", [0, True, 1.2, "5", None])
def test_refresh_invalid_watermark_fails(watermark):
    with pytest.raises(SelectionError, match="watermark is invalid"):
        refresh(observation(next_cup_id=watermark), fresh())


def test_refresh_completed_history_stays_without_old_image_regions_or_new_identity_claim():
    original = cup(4, status="placed", selectable=False, cup_detection_id=3, base_detection_id=5,
                   bbox_xyxy=[80, 80, 120, 120], execution={"state": "completed", "action": "goto"})
    previous = observation([original])
    result = refresh(previous, fresh([cup(1)]))
    completed, new = result["cups"]
    assert completed["id"] == 4
    assert completed["status"] == "placed"
    assert completed["selectable"] is False
    assert completed["bbox_xyxy"] is completed["cup_detection_id"] is completed["base_detection_id"] is None
    assert completed["pixel"] == {"u": None, "v": None}
    assert completed["execution"] == original["execution"]
    assert new["id"] == 5 and new["selectable"] is True
    assert result["matched_cup_ids"] == []
    assert result["removed_cup_ids"] == []
    assert result["added_cup_ids"] == [5]


def test_refresh_execution_completion_also_retains_cup_history():
    previous = observation([cup(4, execution={"state": "completed", "action": "goto"})])
    result = refresh(previous, fresh([]))
    assert result["cups"][0]["id"] == 4
    assert result["cups"][0]["status"] == "placed"
    assert result["cups"][0]["selectable"] is False


def test_refresh_removes_unfinished_missing_cups_but_keeps_completed_history():
    previous = observation([cup(4, status="placed", selectable=False), cup(8)])
    result = refresh(previous, fresh([]))
    assert [item["id"] for item in result["cups"]] == [4]
    assert result["removed_cup_ids"] == [8]
    assert result["next_cup_id"] == 9


def test_refresh_grid_detections_neither_enter_queue_nor_create_source_conflicts():
    previous = observation([cup(4), cup(9, placement_location_ids=[1])], placement_observation={"invalid": True})
    current = fresh([cup(1), cup(1, placement_location_ids=[2], bbox_xyxy=[0, 0, 200, 200])],
                    placement_observation={"changed": "ignored"})
    result = refresh(previous, current)
    assert [item["id"] for item in result["cups"]] == [4]
    assert result["cups"][0]["selectable"] is True
    assert result["blocked_cup_ids"] == []
    assert result["next_cup_id"] == 10


def test_refresh_old_to_new_ambiguity_alone_assigns_new_selectable_identity():
    previous = observation([cup(4, x=490, u=90), cup(9, x=510, u=110)])
    result = refresh(previous, fresh([cup(1)]))
    assert result["matched_cup_ids"] == []
    assert result["added_cup_ids"] == [10]
    assert result["removed_cup_ids"] == [4, 9]
    assert result["ambiguous_identity_cup_ids"] == [10]
    assert result["blocked_cup_ids"] == []
    assert result["cups"][0]["selectable"] is True


def test_refresh_one_old_to_two_distinct_new_sources_does_not_guess_old_identity():
    previous = observation([cup(4)])
    current = fresh([cup(1, x=480, u=80), cup(2, x=520, u=120)])
    result = refresh(previous, current)
    assert result["matched_cup_ids"] == []
    assert result["added_cup_ids"] == [5, 6]
    assert result["removed_cup_ids"] == [4]
    assert result["ambiguous_identity_cup_ids"] == [5, 6]
    assert result["blocked_cup_ids"] == []
    assert all(item["selectable"] for item in result["cups"])


@pytest.mark.parametrize("pair", [
    [cup(1, x=495), cup(2, x=505)],
    [cup(1, x=500, u=100), cup(2, x=600, u=300, cup_detection_id=8)],
    [cup(1, x=500, u=100, bbox_xyxy=[0, 0, 100, 100]),
     cup(2, x=600, u=300, bbox_xyxy=[10, 10, 90, 90])],
    [cup(1, x=500, u=100), cup(1, x=600, u=300)],
])
def test_refresh_conflicting_fresh_targets_are_blocked_while_other_sources_continue(pair):
    if pair[1].get("cup_detection_id") == 8:
        pair = deepcopy(pair)
        pair[0]["cup_detection_id"] = 8
    current = fresh(pair + [cup(3, x=750, u=700)])
    result = refresh(observation([]), current)
    assert result["blocked_cup_ids"] == [1, 2]
    assert [item["selectable"] for item in result["cups"]] == [False, False, True]
    assert all("ambiguous" in item["blocked_reason"] for item in result["cups"][:2])


def test_refresh_partial_nearby_fresh_detection_blocks_a_valid_neighbor():
    current = fresh([cup(1), cup(2, x=None, z=None, u=105, selectable=False, blocked_reason="missing depth")])
    result = refresh(observation(), current)
    assert result["matched_cup_ids"] == []
    assert result["blocked_cup_ids"] == [2, 3]
    assert all(not item["selectable"] for item in result["cups"])


def test_refresh_touching_or_modestly_overlapping_boxes_are_not_duplicate_regions():
    current = fresh([cup(1, x=500, u=100, bbox_xyxy=[0, 0, 100, 100]),
                     cup(2, x=600, u=300, bbox_xyxy=[50, 0, 150, 100]),
                     cup(3, x=750, u=700, bbox_xyxy=[150, 0, 250, 100])])
    result = refresh(observation([]), current)
    assert result["blocked_cup_ids"] == []
    assert all(item["selectable"] for item in result["cups"])


@pytest.mark.parametrize("identifier", [True, 0, "1", None])
def test_refresh_invalid_fresh_identity_blocks_only_that_candidate(identifier):
    result = refresh(observation([]), fresh([cup(identifier), cup(2, x=700, u=500)]))
    assert result["blocked_cup_ids"] == [1]
    assert result["cups"][1]["selectable"] is True


def test_refresh_previously_blocked_cup_can_become_a_new_eligible_source():
    previous = observation([cup(4, selectable=False, blocked_reason="occluded")])
    result = refresh(previous, fresh())
    assert result["matched_cup_ids"] == []
    assert result["removed_cup_ids"] == [4]
    assert result["added_cup_ids"] == [5]
    assert result["cups"][0]["selectable"] is True


def test_refresh_fresh_nonselectable_and_invalid_coordinates_remain_blocked():
    current = fresh([cup(1, x=900, u=500), cup(2, x=600, u=300, selectable=False, blocked_reason="occluded"),
                     cup(3, x=400, u=None)])
    result = refresh(observation([]), current)
    assert len(result["blocked_cup_ids"]) == 3
    assert all(not item["selectable"] for item in result["cups"])
    assert "outside" in result["cups"][0]["blocked_reason"]
    assert result["cups"][1]["blocked_reason"] == "occluded"
    assert "image coordinates" in result["cups"][2]["blocked_reason"]


def test_refresh_never_inherits_motion_facts_for_unmatched_fresh_source():
    current = fresh([cup(1, x=700, u=500, execution={"state": "completed", "action": "goto"},
                         last_verification={"source": "old"}, grip_left_mm=14, grip_right_mm=16)])
    result = refresh(observation(), current)
    new = result["cups"][0]
    assert new["selectable"] is True and new["status"] == "available"
    for key in ("execution", "last_verification", "grip_left_mm", "grip_right_mm"):
        assert key not in new


def test_refresh_results_and_metadata_are_independent_copies():
    previous, current = observation([cup(4, status="placed", selectable=False)]), fresh()
    before = deepcopy((previous, current))
    result = refresh(previous, current)
    result["cups"][0]["pixel"]["u"] = 999
    result["cups"][1]["x"] = 1
    result["frame"]["sequence"] = 0
    assert (previous, current) == before


@pytest.mark.parametrize("change, message", [
    ({"capture": "scan"}, "repeated a previous capture"),
    ({"frame": {"sequence": 10}}, "frame did not advance"),
    ({"frame": {"sequence": 2}}, "frame did not advance"),
    ({"calibration_sha256": "changed"}, "calibration no longer matches"),
    ({"tag_offset_mm": [1, 0, 0]}, "offsets no longer match"),
    ({"gateway": "http://other.invalid/api/v1"}, "gateway no longer matches"),
    ({"cups": [None]}, "observations are invalid"),
])
def test_refresh_metadata_failures_still_require_new_scan(change, message):
    current = fresh()
    current.update(change)
    with pytest.raises(SelectionError, match=message):
        refresh(observation(), current)
