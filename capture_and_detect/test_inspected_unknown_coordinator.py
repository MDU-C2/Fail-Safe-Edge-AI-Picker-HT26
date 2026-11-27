"""Inspected archival retirement never turns overwritten robot work into success."""

import copy
import json
from unittest.mock import Mock

import pytest
import requests

import failover_coordinator as module
from failover_coordinator import (CoordinatorClient, CoordinatorConflict, CoordinatorError,
                                  CoordinatorStore, CoordinatorUnavailable, validate_abandonment)
from test_failover_coordinator import (FIRST, SECOND, RUN, admit, handler, prepare, queue,
                                       setup, steps)


def legacy_queue(state="done"):
    plan = [{"name": f"legacy_{index}_left_handover", "type": "path", "group": f"g{index}",
             "body": {"arm": "left", "speed": 100}} for index in range(18)]
    return queue(owner="primary", state=state, started="2026-11-25T10:00:00Z", plan=plan)


def unknown_receipt(store, gateway, *, owner="primary", current=None):
    admit(store)
    gateway.owner = owner
    prepare(store, role=owner)
    gateway.queue = queue(owner=owner, state="running", started="2026-11-23T10:00:00Z")
    store.receipt(owner, RUN, FIRST)
    gateway.queue = legacy_queue() if current is None else copy.deepcopy(current)
    old = store.receipt(owner, RUN, FIRST)
    assert old["state"] == "unknown" and old["queue"]["state"] == "running"
    store.block(owner, RUN, "Original cup completion is unknown")
    gateway.owner = "primary"
    gateway.calls.clear()
    return old


def abandon(store, gateway, **changes):
    values = {"role": "primary", "run_id": RUN, "submission_id": FIRST, "acknowledged": True,
              "expected_gateway_queue": copy.deepcopy(gateway.queue)}
    values.update(changes)
    return store.abandon(**values)


@pytest.mark.parametrize("owner", ["primary", "secondary", "tertiary"])
@pytest.mark.parametrize("terminal", ["idle", "done", "failed", "cancelled"])
def test_explicit_abandonment_preserves_unknown_evidence_and_different_gateway_queue(setup, owner, terminal):
    store, gateway = setup
    current = {"state": "idle", "length": 0, "steps": []} if terminal == "idle" else legacy_queue(terminal)
    original = unknown_receipt(store, gateway, owner=owner, current=current)
    gateway.arms["left"] = {"accepted": False, "message": "ERR move failed"}
    before = json.loads(store.path.read_text())
    assert module.ABANDONMENT_CAPABILITY in store.state("primary")["capabilities"]
    result = abandon(store, gateway)
    proof = validate_abandonment(result["abandonment"], run_id=RUN, submission_id=FIRST,
                                 queue=original["queue"])
    assert result["state"] == "unknown" and result["queue"] == original["queue"]
    assert "recovery" not in result
    assert proof["observed_queue"] == gateway.queue == current
    assert proof["arm_status"] == gateway.arms
    after = json.loads(store.path.read_text())
    assert after["submissions"][FIRST] == {**before["submissions"][FIRST], "abandonment": proof}
    assert after["submissions"][FIRST]["identity_conflict"] is True
    assert after["run"] == {**before["run"], "phase": "stopped", "abandonment": proof}
    assert after["runs"][RUN] == after["run"] and after["head"] is None
    assert gateway.calls == ["/control", "/robot/rws/queue", "/robot/rws/status?arm=left",
                             "/robot/rws/status?arm=right", "/robot/rws/queue", "/control"]
    assert store.start("primary", RUN, before["run"]["settings"])["run"]["phase"] == "stopped"
    store.start("primary", "fresh-scan-required", {})
    restarted = CoordinatorStore(store.path, "http://offline.invalid", "synthetic-token")
    assert restarted.receipt("tertiary", RUN, FIRST) == result
    assert restarted.state("secondary")["run"]["id"] == "fresh-scan-required"
    assert gateway.queue == current


def test_unknown_without_conflict_flag_stays_frozen_after_inspected_abandonment(setup, monkeypatch):
    store, gateway = setup
    original = unknown_receipt(store, gateway)
    disk = json.loads(store.path.read_text())
    del disk["submissions"][FIRST]["identity_conflict"]
    store.path.write_text(json.dumps(disk))
    result = abandon(store, gateway)
    monkeypatch.setattr(store, "_get", Mock(side_effect=AssertionError("Abandoned receipt must not refresh")))
    assert store.receipt("primary", RUN, FIRST) == result
    assert result["state"] == "unknown" and result["queue"] == original["queue"]


def legacy_failed_duplicate_queue():
    plan = [{"name": "right_release" if index in {15, 17} else f"legacy_step_{index}",
             "type": "path", "group": f"g{index}", "body": {"arm": "right", "speed": 100}}
            for index in range(19)]
    current = queue(state="failed", started="2026-11-25T08:57:55Z", plan=plan)
    for index, step in enumerate(current["steps"]):
        step.update(state="done" if index < 4 else "failed" if index == 4 else "skipped",
                    message="DONE" if index < 4 else "RAPID stopped" if index == 4 else None)
    return current


@pytest.mark.parametrize("prior_state", ["pending", "unknown"])
def test_duplicate_labels_in_current_failed_queue_cannot_hide_original_receipt(setup, prior_state):
    store, gateway = setup
    admit(store)
    prepare(store)
    gateway.queue = queue(state="running", started="2026-11-23T14:49:11Z")
    original = store.receipt("primary", RUN, FIRST)
    store.block("primary", RUN, "Original cup completion is unknown")
    disk = json.loads(store.path.read_text())
    disk["submissions"][FIRST]["state"] = prior_state
    store.path.write_text(json.dumps(disk))
    previous = copy.deepcopy(disk["submissions"][FIRST])
    gateway.queue = legacy_failed_duplicate_queue()
    current = copy.deepcopy(gateway.queue)
    with pytest.raises(CoordinatorError):
        module._queue(current)  # Normal completion/admission parsing remains strict.
    result = store.receipt("primary", RUN, FIRST)
    assert result["state"] == "unknown" and result["queue"] == original["queue"]
    saved = json.loads(store.path.read_text())
    assert saved["submissions"][FIRST] == {**previous, "state": "unknown", "identity_conflict": True}
    assert saved["head"] == FIRST and saved["run"]["phase"] == "blocked"
    assert gateway.queue == current
    restarted = CoordinatorStore(store.path, "http://offline.invalid", "synthetic-token")
    assert restarted.receipt("secondary", RUN, FIRST) == result
    # Only the separate, explicitly inspected gateway recovery clears the
    # malformed terminal queue; abandonment itself still accepts strict IDLE.
    gateway.queue = {"state": "idle", "length": 0, "steps": []}
    retired = abandon(store, gateway)
    assert retired["state"] == "unknown" and retired["queue"] == original["queue"]
    assert retired["abandonment"]["observed_queue"] == gateway.queue


@pytest.mark.parametrize("invalid", [None, {}, {"state": "idle", "length": 1, "steps": []},
                                     legacy_failed_duplicate_queue()])
def test_invalid_current_queue_never_becomes_original_completion_evidence(setup, invalid):
    store, gateway = setup
    admit(store)
    prepare(store)
    gateway.queue = queue(state="running")
    original = store.receipt("primary", RUN, FIRST)
    gateway.queue = copy.deepcopy(invalid)
    result = store.receipt("primary", RUN, FIRST)
    assert result["state"] == "unknown" and result["queue"] == original["queue"]
    assert json.loads(store.path.read_text())["submissions"][FIRST]["identity_conflict"] is True


def test_malformed_current_queue_without_previously_bound_receipt_still_raises(setup):
    store, gateway = setup
    admit(store)
    prepare(store)
    gateway.queue = legacy_failed_duplicate_queue()
    before = store.path.read_bytes()
    with pytest.raises(CoordinatorError):
        store.receipt("primary", RUN, FIRST)
    assert store.path.read_bytes() == before


def test_transport_failure_does_not_latch_identity_conflict_or_hide_outage(setup, monkeypatch):
    store, gateway = setup
    admit(store)
    prepare(store)
    gateway.queue = queue(state="running")
    store.receipt("primary", RUN, FIRST)
    before = store.path.read_bytes()
    monkeypatch.setattr(store, "_get", Mock(side_effect=CoordinatorUnavailable("Disconnected")))
    with pytest.raises(CoordinatorUnavailable):
        store.receipt("primary", RUN, FIRST)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("acknowledged", [False, None, 1, "yes"])
def test_explicit_boolean_acknowledgement_is_required_before_gateway_reads(setup, acknowledged):
    store, gateway = setup
    unknown_receipt(store, gateway)
    before = store.path.read_bytes()
    with pytest.raises(CoordinatorError):
        abandon(store, gateway, acknowledged=acknowledged)
    assert store.path.read_bytes() == before and gateway.calls == []


@pytest.mark.parametrize("role", ["secondary", "tertiary"])
def test_non_primary_cannot_abandon_even_with_gateway_lease(setup, role):
    store, gateway = setup
    unknown_receipt(store, gateway)
    gateway.owner = role
    before = store.path.read_bytes()
    with pytest.raises(CoordinatorConflict):
        abandon(store, gateway, role=role)
    assert store.path.read_bytes() == before and gateway.calls == []


@pytest.mark.parametrize("expected", [None, {}, [], {"state": "idle", "length": True, "steps": []},
    {"state": "idle", "length": 1, "steps": []}])
def test_malformed_expected_gateway_snapshot_fails_before_reads_or_changes(setup, expected):
    store, gateway = setup
    unknown_receipt(store, gateway)
    before = store.path.read_bytes()
    with pytest.raises(CoordinatorError):
        abandon(store, gateway, expected_gateway_queue=expected)
    assert store.path.read_bytes() == before and gateway.calls == []


@pytest.mark.parametrize("change", ["wrong_run", "wrong_receipt", "active", "finalizing", "watching", "complete",
                                     "newer_head", "queue_missing", "steps_changed", "start_changed"])
def test_wrong_or_unbound_original_history_cannot_be_retired(setup, change):
    store, gateway = setup
    unknown_receipt(store, gateway)
    values = {}
    disk = json.loads(store.path.read_text())
    if change == "wrong_run":
        values["run_id"] = "another-run"
    elif change == "wrong_receipt":
        values["submission_id"] = SECOND
    elif change in {"active", "finalizing", "watching", "complete"}:
        disk["run"]["phase"] = disk["runs"][RUN]["phase"] = change
    elif change == "newer_head":
        disk["head"] = SECOND
        disk["submissions"][SECOND] = {"run_id": RUN, "owner": "primary", "steps": steps(SECOND),
            "state": "pending", "prepared_at": "2026-11-25T09:00:00Z", "started_at": None, "queue": None}
    elif change == "queue_missing":
        disk["submissions"][FIRST].update(queue=None, started_at=None)
    elif change == "steps_changed":
        disk["submissions"][FIRST]["steps"][0]["body"]["arm"] = "right"
    else:
        disk["submissions"][FIRST]["started_at"] = "2026-11-25T09:00:00Z"
    store.path.write_text(json.dumps(disk))
    before = store.path.read_bytes()
    with pytest.raises((CoordinatorConflict, CoordinatorError)):
        abandon(store, gateway, **values)
    assert store.path.read_bytes() == before and gateway.calls == []


@pytest.mark.parametrize("state", ["pending", "done", "failed", "cancelled", "not_submitted"])
def test_operation_is_only_for_unknown_receipts(setup, state):
    store, gateway = setup
    unknown_receipt(store, gateway)
    disk = json.loads(store.path.read_text())
    item = disk["submissions"][FIRST]
    item["state"] = state
    if state in {"done", "failed", "cancelled"}:
        item["queue"] = queue(state=state, started=item["started_at"])
    elif state == "not_submitted":
        item.update(queue=None, started_at=None)
    store.path.write_text(json.dumps(disk))
    before = store.path.read_bytes()
    with pytest.raises(CoordinatorConflict):
        abandon(store, gateway)
    assert store.path.read_bytes() == before and gateway.calls == []


@pytest.mark.parametrize("change", ["same_original", "same_prefix_new_time", "same_owner_time",
                                     "running_queue", "running_step", "missing_lease", "wrong_lease",
                                     "arm_busy", "arm_unknown", "arm_bad_boolean", "stale_snapshot"])
def test_current_gateway_evidence_must_be_different_terminal_and_inspected(setup, change):
    store, gateway = setup
    original = unknown_receipt(store, gateway)
    expected = None
    if change in {"same_original", "same_prefix_new_time"}:
        gateway.queue = copy.deepcopy(original["queue"])
        gateway.queue["state"] = "done"
        for step in gateway.queue["steps"]:
            step.update(state="done", message="DONE")
        if change == "same_prefix_new_time":
            gateway.queue["started_at"] = "2026-11-25T09:00:00Z"
    elif change == "same_owner_time":
        gateway.queue["started_at"] = original["queue"]["started_at"]
    elif change == "running_queue":
        gateway.queue = legacy_queue("running")
    elif change == "running_step":
        gateway.queue = legacy_queue("failed")
        gateway.queue["steps"][0]["state"] = "running"
    elif change == "missing_lease":
        gateway.owner = None
    elif change == "wrong_lease":
        gateway.owner = "secondary"
    elif change == "arm_busy":
        gateway.arms["right"] = {"accepted": True, "message": "BUSY"}
    elif change == "arm_unknown":
        gateway.arms["left"] = {"accepted": False, "message": "ERR unavailable"}
    elif change == "arm_bad_boolean":
        gateway.arms["left"] = {"accepted": 1, "message": "DONE"}
    else:
        expected = copy.deepcopy(gateway.queue)
        gateway.queue["message"] = "Changed since inspection"
    before = store.path.read_bytes()
    options = {} if expected is None else {"expected_gateway_queue": expected}
    with pytest.raises(CoordinatorConflict):
        abandon(store, gateway, **options)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("change", ["queue", "lease"])
def test_queue_and_lease_are_rechecked_before_durable_commit(setup, monkeypatch, change):
    store, gateway = setup
    unknown_receipt(store, gateway)
    before = store.path.read_bytes()
    def get(path):
        result = gateway.get(path)
        if path == "/robot/rws/status?arm=right":
            if change == "queue":
                gateway.queue["message"] = "New terminal evidence"
            else:
                gateway.owner = "secondary"
        return result
    monkeypatch.setattr(store, "_get", get)
    with pytest.raises(CoordinatorConflict):
        abandon(store, gateway)
    assert store.path.read_bytes() == before


def test_retry_keeps_same_acknowledgement_and_requires_exact_evidence(setup, monkeypatch):
    store, gateway = setup
    unknown_receipt(store, gateway)
    first = abandon(store, gateway)
    before = store.path.read_bytes()
    commit = Mock(wraps=store._commit)
    monkeypatch.setattr(store, "_commit", commit)
    assert abandon(store, gateway) == first
    commit.assert_not_called()
    assert store.path.read_bytes() == before
    gateway.arms["left"] = {"accepted": False, "message": "ERR move failed"}
    with pytest.raises(CoordinatorConflict):
        abandon(store, gateway)
    gateway.arms["left"] = first["abandonment"]["arm_status"]["left"]
    original = copy.deepcopy(gateway.queue)
    gateway.queue["message"] = "Different snapshot"
    with pytest.raises(CoordinatorConflict):
        abandon(store, gateway)
    gateway.queue = original
    store.start("primary", "new-work", {})
    with pytest.raises(CoordinatorConflict):
        abandon(store, gateway)


def test_storage_failure_keeps_unknown_attached_and_original_evidence(setup, monkeypatch):
    store, gateway = setup
    unknown_receipt(store, gateway)
    before = store.path.read_bytes()
    monkeypatch.setattr(module, "_write_state", Mock(side_effect=OSError("Synthetic disk failure")))
    with pytest.raises(CoordinatorUnavailable):
        abandon(store, gateway)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("change", ["ack", "role", "run_id", "submission_id", "digest", "observed_digest",
    "timestamp", "arms", "extra", "original_queue", "observed_queue", "run_link", "head", "phase", "state",
    "conflicting_recovery", "same_original_prefix"])
def test_invalid_abandonment_cannot_be_loaded_as_retired_history(setup, change):
    store, gateway = setup
    unknown_receipt(store, gateway)
    abandon(store, gateway)
    disk = json.loads(store.path.read_text())
    item = disk["submissions"][FIRST]
    proof = item["abandonment"]
    edits = {"ack": ("acknowledged", 1), "role": ("acknowledged_by", "secondary"),
             "run_id": ("run_id", "another-run"), "submission_id": ("submission_id", SECOND),
             "digest": ("queue_sha256", "0" * 64), "observed_digest": ("observed_queue_sha256", "0" * 64),
             "timestamp": ("abandoned_at", "2026-11-25T10:00:00"),
             "arms": ("arm_status", {"left": {"accepted": True, "message": "DONE"}}),
             "extra": ("unknown_field", True)}
    if change in edits:
        key, replacement = edits[change]
        proof[key] = replacement
    elif change == "original_queue":
        item["queue"]["message"] = "Erased original evidence"
    elif change == "observed_queue":
        proof["observed_queue"]["message"] = "Erased observation"
    elif change == "same_original_prefix":
        proof["observed_queue"] = queue(FIRST, started="2026-11-25T10:00:00Z")
        proof["observed_queue_sha256"] = module._queue_digest(proof["observed_queue"])
    elif change == "head":
        disk["head"] = FIRST
    elif change == "phase":
        disk["run"]["phase"] = disk["runs"][RUN]["phase"] = "active"
    elif change == "state":
        item["state"] = "pending"
    elif change == "conflicting_recovery":
        item["recovery"] = {}
    disk["run"]["abandonment"] = disk["runs"][RUN]["abandonment"] = copy.deepcopy(proof)
    if change == "run_link":
        del disk["runs"][RUN]["abandonment"]
    store.path.write_text(json.dumps(disk))
    with pytest.raises(CoordinatorError):
        CoordinatorStore(store.path, "http://offline.invalid", "synthetic-token")


def test_http_contract_authentication_exact_fields_and_single_attempt_client(setup, monkeypatch):
    store, gateway = setup
    unknown_receipt(store, gateway)
    body = {"role": "primary", "run_id": RUN, "submission_id": FIRST, "acknowledged": True,
            "expected_gateway_queue": copy.deepcopy(gateway.queue)}
    for missing in ("acknowledged", "expected_gateway_queue"):
        answer = handler(store, path="/v1/abandon", body={key: value for key, value in body.items() if key != missing})
        assert "400" in answer.splitlines()[0]
    assert "401" in handler(store, path="/v1/abandon", body=body, token="wrong-token").splitlines()[0]
    assert gateway.calls == []
    answer = handler(store, path="/v1/abandon", body=body)
    assert "200" in answer.splitlines()[0] and '"abandonment":' in answer and '"state":"unknown"' in answer
    request = Mock(side_effect=requests.Timeout("PRIVATE DETAIL"))
    monkeypatch.setattr(requests, "request", request)
    client = CoordinatorClient("http://offline.invalid:8092", "synthetic-token", "primary")
    with pytest.raises(CoordinatorUnavailable):
        client.abandon(RUN, FIRST, acknowledged=True, expected_gateway_queue=body["expected_gateway_queue"])
    assert request.call_count == 1
    assert request.call_args.args == ("POST", "http://offline.invalid:8092/v1/abandon")
    assert request.call_args.kwargs["json"] == body
