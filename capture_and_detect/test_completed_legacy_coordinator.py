"""Legacy completed history can precede work, never prove a new cup completed."""

import copy
import json
from pathlib import Path

import pytest

import failover_coordinator as module
from failover_coordinator import CoordinatorConflict, CoordinatorError, CoordinatorStore
from test_failover_coordinator import FIRST, SECOND, RUN, admit, prepare, queue, setup, steps
from test_inspected_unknown_coordinator import abandon, unknown_receipt


def completed_legacy_queue():
    return json.loads((Path(__file__).parent / "test_fixtures" /
                       "legacy_completed_duplicate_queue.json").read_text())


def test_completed_legacy_baseline_admits_new_identity_without_rewriting_history(setup):
    store, gateway = setup
    admit(store)
    gateway.queue = completed_legacy_queue()
    original = copy.deepcopy(gateway.queue)
    with pytest.raises(CoordinatorError):
        module._queue(original)  # Receipt and Home validation remain strict.
    assert prepare(store) == {"state": "pending", "queue": None, "home_cycle": 0}
    assert gateway.queue == original
    saved = json.loads(store.path.read_text())["submissions"][FIRST]
    assert saved["state"] == "pending" and saved["queue"] is None
    assert saved["started_at"] is None
    assert gateway.calls == ["/control", "/robot/rws/queue", "/control"]
    # Only the new prefixed queue can subsequently establish completion.
    gateway.queue = queue()
    assert store.receipt("primary", RUN, FIRST)["state"] == "done"


@pytest.mark.parametrize("change", ["running", "failed", "cancelled", "unfinished_step",
    "missing_done", "mixed_prefix", "all_prefixed", "missing_owner", "bad_time"])
def test_only_fully_confirmed_unprefixed_legacy_history_is_a_baseline(setup, change):
    store, gateway = setup
    admit(store)
    gateway.queue = completed_legacy_queue()
    if change in {"running", "failed", "cancelled"}:
        gateway.queue["state"] = change
    elif change == "unfinished_step":
        gateway.queue["steps"][0].update(state="pending", message=None)
    elif change == "missing_done":
        gateway.queue["steps"][0]["message"] = None
    elif change == "mixed_prefix":
        gateway.queue["steps"][0]["name"] = f"q{SECOND}_0_new"
    elif change == "all_prefixed":
        for step in gateway.queue["steps"]:
            step["name"] = f"q{SECOND}_" + step["name"]
    elif change == "missing_owner":
        gateway.queue["owner"] = ""
    else:
        gateway.queue["started_at"] = "yesterday"
    before = store.path.read_bytes()
    with pytest.raises(CoordinatorError):
        prepare(store)
    assert store.path.read_bytes() == before


def test_legacy_completion_cannot_resolve_an_earlier_admitted_cup(setup):
    store, gateway = setup
    admit(store)
    prepare(store)
    gateway.queue = queue(state="running")
    original = store.receipt("primary", RUN, FIRST)["queue"]
    gateway.queue = completed_legacy_queue()
    with pytest.raises(CoordinatorConflict, match="not safely resolved"):
        prepare(store, SECOND)
    record = json.loads(store.path.read_text())["submissions"][FIRST]
    assert record["state"] == "unknown" and record["queue"] == original
    assert record["identity_conflict"] is True
    assert SECOND not in json.loads(store.path.read_text())["submissions"]


def test_legacy_completion_does_not_supply_an_unobserved_receipt(setup):
    store, gateway = setup
    admit(store)
    prepare(store)
    gateway.queue = completed_legacy_queue()
    before = store.path.read_bytes()
    with pytest.raises(CoordinatorError):
        store.receipt("primary", RUN, FIRST)
    assert store.path.read_bytes() == before


def test_unsubmitted_result_can_be_recorded_against_legacy_completed_history(setup):
    store, gateway = setup
    admit(store)
    gateway.queue = completed_legacy_queue()
    original = copy.deepcopy(gateway.queue)
    prepare(store)
    result = store.reject_queue("primary", RUN, FIRST)
    assert result["state"] == "not_submitted" and result["queue"] is None
    assert gateway.queue == original


def test_inspected_abandonment_keeps_legacy_completed_observation_and_unknown_original(setup):
    store, gateway = setup
    legacy = completed_legacy_queue()
    original = unknown_receipt(store, gateway, current=legacy)
    result = abandon(store, gateway)
    assert result["state"] == "unknown" and result["queue"] == original["queue"]
    assert result["abandonment"]["observed_queue"] == gateway.queue == legacy
    assert store.state("primary")["head"] is None
    # All persisted original-queue validation remains strict after restart;
    # only the separate inspected observation uses legacy compatibility.
    restarted = CoordinatorStore(store.path, "http://offline.invalid", "synthetic-token")
    assert restarted.receipt("primary", RUN, FIRST) == result


def test_coordinated_submission_itself_still_requires_unique_names(setup):
    store, gateway = setup
    admit(store)
    gateway.queue = completed_legacy_queue()
    before = store.path.read_bytes()
    with pytest.raises(CoordinatorError):
        store.prepare_queue("primary", RUN, FIRST, steps() * 2)
    assert store.path.read_bytes() == before and gateway.calls == []
