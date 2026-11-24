"""Durable admission/receipt races and wire contracts; no real network or robot."""

import copy
from email.parser import Parser
from io import BytesIO
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

import failover_coordinator as module
from failover_coordinator import (CoordinatorClient, CoordinatorConflict, CoordinatorError,
                                  CoordinatorHandler, CoordinatorStore, CoordinatorUnavailable)


RUN = "batch-one"
FIRST, SECOND, THIRD = "a" * 16, "b" * 16, "c" * 16


def steps(identity=FIRST, arm="left", kind="path"):
    return [{"name": f"q{identity}_0_{arm}_{kind}", "type": kind, "group": "g0",
             "body": {"arm": arm, "speed": 100}}]


def queue(identity=FIRST, owner="primary", state="done", started="2026-11-20T10:00:00Z", plan=None):
    plan = steps(identity) if plan is None else plan
    return {"state": state, "length": len(plan), "owner": owner, "started_at": started,
            "message": "DONE" if state == "done" else "", "cancel_requested": False,
            "steps": [{"name": item["name"], "type": item["type"], "group": item["group"],
                       "arm": item["body"]["arm"], "state": state,
                       "message": "DONE" if state == "done" else ""} for item in plan]}


class Gateway:
    def __init__(self):
        self.owner = "primary"
        self.queue = {"state": "idle", "length": 0, "steps": []}
        self.calls = []
        self.arms = {arm: {"accepted": True, "message": "DONE"} for arm in ("left", "right")}

    def get(self, path):
        self.calls.append(path)
        if path == "/control":
            return {"active": self.owner is not None, "client_id": self.owner}
        if path.startswith("/robot/rws/status?arm="):
            return copy.deepcopy(self.arms[path.rsplit("=", 1)[1]])
        assert path == "/robot/rws/queue"
        return copy.deepcopy(self.queue)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "request", Mock(side_effect=AssertionError("Real HTTP forbidden")))
    gateway = Gateway()
    store = CoordinatorStore(tmp_path / "coordinator.json", "http://offline.invalid", "synthetic-credential")
    monkeypatch.setattr(store, "_get", gateway.get)
    return store, gateway


def admit(store):
    return store.start("primary", RUN, {"speed": 100, "phase_speeds": {"travel": 100}})


def prepare(store, identity=FIRST, role="primary", **options):
    return store.prepare_queue(role, RUN, identity, steps(identity, **options))


def test_armed_and_historical_queue_never_admit_a_batch(setup):
    store, gateway = setup
    gateway.queue = queue()
    for role in ("primary", "secondary", "tertiary"):
        assert store.heartbeat(role)["run"] is None
        with pytest.raises(CoordinatorConflict):
            prepare(store, role=role)
    assert store.state("primary")["run"] is None
    assert gateway.calls == [], "No lease, camera or robot access while only armed"
    with pytest.raises(CoordinatorConflict):
        store.start("secondary", RUN, {})
    assert gateway.calls == []


def test_start_is_idempotent_and_never_restarts_a_finished_epoch(setup):
    store, gateway = setup
    first = admit(store)
    assert admit(store) == first
    with pytest.raises(CoordinatorConflict):
        store.start("primary", RUN, {"speed": 250})
    with pytest.raises(CoordinatorConflict):
        store.start("primary", "other-run", {})
    store.finish("primary", RUN, "no_pickable_cups")
    assert admit(store)["run"]["phase"] == "complete"
    store.start("primary", "new-batch", {})
    with pytest.raises(CoordinatorConflict):
        admit(store)


def test_exact_receipt_is_durable_before_successor_replaces_gateway_queue(setup, monkeypatch):
    store, gateway = setup
    admit(store)
    assert prepare(store) == {"state": "pending", "queue": None, "home_cycle": 0}
    gateway.queue = queue()
    gateway.owner = "secondary"
    assert prepare(store, SECOND, role="secondary")["state"] == "pending"
    disk = json.loads(store.path.read_text())
    assert disk["submissions"][FIRST]["state"] == "done"
    assert disk["head"] == SECOND
    gateway.queue = queue(SECOND, "secondary", state="running")
    monkeypatch.setattr(store, "_get", Mock(side_effect=AssertionError("Receipt must already be durable")))
    assert store.receipt("primary", RUN, FIRST) == {"state": "done", "queue": queue(), "home_cycle": 0}
    restarted = CoordinatorStore(store.path, "http://offline.invalid", "synthetic-credential")
    assert restarted.receipt("tertiary", RUN, FIRST)["state"] == "done"


@pytest.mark.parametrize("state", ["running", "failed", "cancelled"])
def test_predecessor_blocks_replacement_even_after_preemption(setup, state):
    store, gateway = setup
    admit(store)
    prepare(store)
    gateway.queue = queue(state=state)
    gateway.owner = "secondary"
    with pytest.raises(CoordinatorConflict):
        prepare(store, SECOND, role="secondary")
    record = json.loads(store.path.read_text())["submissions"][FIRST]
    assert record["state"] == ("pending" if state == "running" else state)
    assert SECOND not in json.loads(store.path.read_text())["submissions"]


def test_unseen_submission_is_unknown_not_assumed_rejected(setup):
    store, gateway = setup
    admit(store)
    prepare(store)
    assert store.receipt("primary", RUN, FIRST) == {"state": "unknown", "queue": None, "home_cycle": 0}
    gateway.owner = "secondary"
    with pytest.raises(CoordinatorConflict):
        prepare(store, SECOND, role="secondary")
    with pytest.raises(CoordinatorConflict):
        store.finish("secondary", RUN, "workspace_empty")
    gateway.queue = queue(state="running")  # A delayed, accepted POST becomes visible.
    assert store.receipt("tertiary", RUN, FIRST)["state"] == "pending"
    gateway.queue = queue()
    assert store.receipt("primary", RUN, FIRST)["state"] == "done"


def test_identical_receipt_polls_do_not_rewrite_persistent_history(setup, monkeypatch):
    store, gateway = setup
    admit(store)
    prepare(store)
    commit = Mock(wraps=store._commit)
    monkeypatch.setattr(store, "_commit", commit)
    for _ in range(5):
        assert store.receipt("primary", RUN, FIRST)["state"] == "unknown"
    assert commit.call_count == 1
    gateway.queue = queue(state="running")
    for _ in range(5):
        assert store.receipt("primary", RUN, FIRST)["state"] == "pending"
    assert commit.call_count == 2
    gateway.queue = queue()
    for _ in range(5):
        assert store.receipt("primary", RUN, FIRST)["state"] == "done"
    assert commit.call_count == 3
    assert json.loads(store.path.read_text())["submissions"][FIRST]["queue"] == queue()


@pytest.mark.parametrize("change", ["owner", "name", "arm", "type", "group", "started", "order"])
def test_receipt_requires_exact_owner_signature_and_stable_start(setup, change):
    store, gateway = setup
    admit(store)
    plan = steps() + [{**steps()[0], "name": f"q{FIRST}_1_other", "group": "g1"}]
    store.prepare_queue("primary", RUN, FIRST, plan)
    gateway.queue = queue(state="running", plan=plan)
    assert store.receipt("primary", RUN, FIRST)["state"] == "pending"
    gateway.queue = queue(plan=plan)
    if change == "owner":
        gateway.queue["owner"] = "secondary"
    elif change == "started":
        gateway.queue["started_at"] = "2026-11-20T10:00:01Z"
    elif change == "order":
        gateway.queue["steps"].reverse()
    else:
        replacements = {"name": "other", "arm": "right", "type": "move", "group": "g9"}
        gateway.queue["steps"][0][change] = replacements[change]
    assert store.receipt("tertiary", RUN, FIRST)["state"] == "unknown"
    with pytest.raises(CoordinatorConflict):
        prepare(store, SECOND)
    gateway.queue = queue(plan=plan)
    assert store.receipt("primary", RUN, FIRST)["state"] == "unknown", "An observed identity conflict stays latched"


@pytest.mark.parametrize("change", ["done_message", "done_state", "naive_start", "duplicate_names", "bad_length"])
def test_malformed_gateway_completion_never_becomes_done(setup, change):
    store, gateway = setup
    admit(store)
    prepare(store)
    gateway.queue = queue()
    if change == "done_message": gateway.queue["steps"][0]["message"] = "OK"
    if change == "done_state": gateway.queue["steps"][0]["state"] = "running"
    if change == "naive_start": gateway.queue["started_at"] = "2026-11-20T10:00:00"
    if change == "bad_length": gateway.queue["length"] = 2
    if change == "duplicate_names":
        gateway.queue["steps"] *= 2
        gateway.queue["length"] = 2
    with pytest.raises(CoordinatorError):
        store.receipt("primary", RUN, FIRST)
    assert json.loads(store.path.read_text())["submissions"][FIRST]["state"] == "pending"


def test_done_steps_cannot_regress(setup):
    store, gateway = setup
    admit(store)
    plan = steps() + [{**steps()[0], "name": f"q{FIRST}_1_other"}]
    store.prepare_queue("primary", RUN, FIRST, plan)
    gateway.queue = queue(state="running", plan=plan)
    gateway.queue["steps"][0].update(state="done", message="DONE")
    store.receipt("primary", RUN, FIRST)
    gateway.queue = queue(state="running", plan=plan)
    assert store.receipt("primary", RUN, FIRST)["state"] == "unknown"


def test_prepare_requires_fresh_control_and_submission_ids_are_immutable(setup):
    store, gateway = setup
    admit(store)
    gateway.owner = "secondary"
    with pytest.raises(CoordinatorConflict): prepare(store)
    assert FIRST not in json.loads(store.path.read_text())["submissions"]
    gateway.owner = "primary"
    assert prepare(store) == prepare(store)
    with pytest.raises(CoordinatorConflict): prepare(store, arm="right")
    gateway.owner = "secondary"
    with pytest.raises(CoordinatorConflict): prepare(store, role="secondary")


def test_lease_lost_during_receipt_collection_cannot_prepare(setup, monkeypatch):
    store, gateway = setup
    admit(store)
    count = 0
    def get(path):
        nonlocal count
        if path == "/control":
            count += 1
            if count == 2: gateway.owner = "secondary"
        return gateway.get(path)
    monkeypatch.setattr(store, "_get", get)
    with pytest.raises(CoordinatorConflict): prepare(store)
    assert FIRST not in json.loads(store.path.read_text())["submissions"]


def test_preexisting_matching_history_cannot_authorize_new_submission(setup):
    store, gateway = setup
    gateway.queue = queue()
    admit(store)
    with pytest.raises(CoordinatorConflict): prepare(store)


def test_definite_rejection_is_durable_idempotent_and_may_follow_lost_prepare_response(setup):
    store, gateway = setup
    admit(store)
    prepare(store)
    assert store.reject_queue("primary", RUN, FIRST) == {"state": "not_submitted", "queue": None, "home_cycle": 0}
    assert store.reject_queue("primary", RUN, FIRST)["state"] == "not_submitted"
    assert store.reject_queue("secondary", RUN, SECOND)["state"] == "not_submitted"
    with pytest.raises(CoordinatorConflict): prepare(store, SECOND)
    gateway.owner = "secondary"
    assert prepare(store, THIRD, role="secondary")["state"] == "pending"


@pytest.mark.parametrize("prepared", [False, True], ids=["lost-prepare", "prepared"])
@pytest.mark.parametrize("restart", [False, True], ids=["same-store", "reopened-store"])
def test_historical_non_submission_rejection_is_read_only_and_idempotent(setup, monkeypatch, prepared, restart):
    store, gateway = setup
    admit(store)
    if prepared:
        prepare(store)
    expected = store.reject_queue("primary", RUN, FIRST)
    store.block("primary", RUN, "Retired before dispatch")
    store.start("primary", "new-run", {})
    store.prepare_queue("primary", "new-run", SECOND, steps(SECOND))
    before = store.path.read_bytes()
    gateway.calls.clear()
    if restart:
        store = CoordinatorStore(store.path, "http://offline.invalid", "synthetic-credential")
    monkeypatch.setattr(store, "_get", Mock(side_effect=AssertionError("Historical result needs no gateway access")))
    commit = Mock(side_effect=AssertionError("Historical result must not rewrite the store"))
    monkeypatch.setattr(store, "_commit", commit)

    for _ in range(2):
        assert store.reject_queue("primary", RUN, FIRST) == expected

    assert store.path.read_bytes() == before
    assert store.state("primary")["head"]["submission_id"] == SECOND
    assert gateway.calls == []
    commit.assert_not_called()


@pytest.mark.parametrize("role,run_id", [("secondary", RUN), ("tertiary", RUN),
                                        ("primary", "new-run"), ("primary", "unknown-run")])
def test_historical_non_submission_rejection_still_requires_exact_identity(setup, role, run_id):
    store, gateway = setup
    admit(store)
    prepare(store)
    store.reject_queue("primary", RUN, FIRST)
    store.block("primary", RUN, "Retired before dispatch")
    store.start("primary", "new-run", {})
    before = store.path.read_bytes()
    gateway.calls.clear()

    with pytest.raises(CoordinatorConflict):
        store.reject_queue(role, run_id, FIRST)

    assert store.path.read_bytes() == before
    assert gateway.calls == []


@pytest.mark.parametrize("historical_state", ["missing", "pending", "unknown", "dispatched", "done",
                                             "contradictory-start", "contradictory-queue"])
def test_rejection_cannot_create_or_change_other_historical_receipts(setup, historical_state):
    store, gateway = setup
    admit(store)
    prepare(store)
    gateway.queue = queue()
    assert store.receipt("primary", RUN, FIRST)["state"] == "done"
    store.block("primary", RUN, "Finished previous queue")
    store.start("primary", "new-run", {})
    store.prepare_queue("primary", "new-run", SECOND, steps(SECOND))
    value = json.loads(store.path.read_text())
    if historical_state == "missing":
        del value["submissions"][FIRST]
    elif historical_state in {"pending", "unknown"}:
        value["submissions"][FIRST].update(state=historical_state, queue=None, started_at=None)
    elif historical_state == "dispatched":
        value["submissions"][FIRST].update(state="pending", queue=queue(state="running"))
    elif historical_state == "contradictory-start":
        value["submissions"][FIRST].update(state="not_submitted", queue=None)
    elif historical_state == "contradictory-queue":
        value["submissions"][FIRST].update(state="not_submitted")
    # Exercise historical states independently of admission: a replay must
    # never turn a different or contradictory old record into a non-submission.
    store.path.write_text(json.dumps(value))
    before = store.path.read_bytes()
    gateway.calls.clear()

    with pytest.raises(CoordinatorConflict):
        store.reject_queue("primary", RUN, FIRST)

    assert store.path.read_bytes() == before
    assert store.state("primary")["head"]["submission_id"] == SECOND
    assert gateway.calls == []


@pytest.mark.parametrize("observed", [False, True])
def test_rejection_cannot_erase_an_accepted_submission(setup, observed):
    store, gateway = setup
    admit(store)
    prepare(store)
    gateway.queue = queue(state="running")
    if observed:
        store.receipt("primary", RUN, FIRST)
        gateway.queue = {"state": "idle", "length": 0, "steps": []}
    with pytest.raises(CoordinatorConflict): store.reject_queue("primary", RUN, FIRST)
    assert json.loads(store.path.read_text())["submissions"][FIRST]["state"] == "pending"


def test_missing_rejection_cannot_hide_gateway_submission(setup):
    store, gateway = setup
    admit(store)
    gateway.queue = queue()
    with pytest.raises(CoordinatorConflict): store.reject_queue("primary", RUN, FIRST)


def test_stop_or_block_closes_admission_but_retains_accepted_completion(setup):
    store, gateway = setup
    admit(store)
    prepare(store)
    assert store.stop("secondary", RUN, "operator_stop")["run"]["phase"] == "stopped"
    with pytest.raises(CoordinatorConflict): prepare(store, SECOND)
    gateway.queue = queue()
    assert store.receipt("primary", RUN, FIRST)["state"] == "done"
    assert store.state("tertiary")["run"]["phase"] == "stopped"
    store.start("primary", "new-run", {})
    assert store.block("tertiary", "new-run", "unknown_movement")["run"]["phase"] == "blocked"
    assert all(path.startswith(("/control", "/robot/rws/queue")) for path in gateway.calls)


def test_finalization_is_durable_and_only_admits_home_queues(setup):
    store, gateway = setup
    admit(store)
    assert store.set_phase("primary", RUN, "finalizing")["run"]["phase"] == "finalizing"
    with pytest.raises(CoordinatorConflict): prepare(store)
    right = steps(FIRST, arm="right", kind="home")
    store.prepare_queue("primary", RUN, FIRST, right)
    gateway.queue = queue(plan=right)
    gateway.owner = "secondary"
    assert store.confirm_home("primary", RUN, FIRST, "right")["run"]["confirmed_homes"] == ["right"]
    left = steps(SECOND, arm="left", kind="home")
    store.prepare_queue("secondary", RUN, SECOND, left)
    gateway.queue = queue(SECOND, owner="secondary", plan=left)
    with pytest.raises(CoordinatorConflict): store.finish("secondary", RUN, "workspace_empty")
    store.confirm_home("secondary", RUN, SECOND, "left")
    assert store.finish("secondary", RUN, "workspace_empty")["run"]["phase"] == "complete"
    assert store.state("primary")["run"]["phase"] == "complete"
    with pytest.raises(CoordinatorConflict): prepare(store, THIRD, role="secondary")


def test_home_confirmation_is_idempotent_and_durable_after_old_owner_preemption(setup):
    store, gateway = setup
    admit(store)
    store.set_phase("primary", RUN, "finalizing")
    right = steps(FIRST, arm="right", kind="home")
    store.prepare_queue("primary", RUN, FIRST, right)
    gateway.queue = queue(plan=right)
    gateway.owner = "tertiary"
    first = store.confirm_home("primary", RUN, FIRST, "right")
    assert store.confirm_home("primary", RUN, FIRST, "right") == first
    assert first["run"]["confirmed_homes"] == ["right"]
    assert first["head"]["submission_id"] == FIRST and first["head"]["queue"] == queue(plan=right)
    restarted = CoordinatorStore(store.path, "http://offline.invalid", "synthetic-token")
    assert restarted.state("tertiary")["run"]["home_receipts"] == {"right": FIRST}
    with pytest.raises(CoordinatorConflict):
        store.prepare_queue("tertiary", RUN, SECOND, steps(SECOND, arm="right", kind="home"))


def test_successor_can_adopt_only_current_exact_done_home_under_its_lease(setup):
    store, gateway = setup
    admit(store)
    store.set_phase("primary", RUN, "finalizing")
    right = steps(FIRST, arm="right", kind="home")
    store.prepare_queue("primary", RUN, FIRST, right)
    gateway.queue = queue(plan=right)
    with pytest.raises(CoordinatorConflict): store.confirm_home("secondary", RUN, FIRST, "right")
    gateway.owner = "secondary"
    assert store.confirm_home("secondary", RUN, FIRST, "right")["run"]["confirmed_homes"] == ["right"]


def test_stale_home_receipt_cannot_be_adopted_by_successor(setup):
    store, gateway = setup
    admit(store)
    store.set_phase("primary", RUN, "finalizing")
    right = steps(FIRST, arm="right", kind="home")
    store.prepare_queue("primary", RUN, FIRST, right)
    gateway.queue = queue(plan=right)
    store.receipt("primary", RUN, FIRST)
    gateway.owner = "secondary"
    gateway.queue = queue(SECOND, owner="secondary")
    with pytest.raises(CoordinatorConflict): store.confirm_home("secondary", RUN, FIRST, "right")
    assert store.state("primary")["run"]["confirmed_homes"] == []


def test_left_home_cannot_be_prepared_before_right_measured_confirmation(setup):
    store, gateway = setup
    admit(store)
    store.set_phase("primary", RUN, "finalizing")
    with pytest.raises(CoordinatorConflict):
        store.prepare_queue("primary", RUN, FIRST, steps(FIRST, kind="home"))
    with pytest.raises(CoordinatorConflict): store.finish("primary", RUN, "workspace_empty")


def test_concurrent_prepares_have_one_durable_winner(setup):
    store, gateway = setup
    admit(store)
    peer = CoordinatorStore(store.path, "http://offline.invalid", "synthetic-credential")
    peer._get = gateway.get
    barrier, outcomes = threading.Barrier(2), []
    def contender(instance, identity):
        barrier.wait()
        try:
            prepare(instance, identity)
            outcomes.append("prepared")
        except CoordinatorConflict:
            outcomes.append("conflict")
    threads = [threading.Thread(target=contender, args=(instance, identity))
               for instance, identity in ((store, FIRST), (peer, SECOND))]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=5)
    assert sorted(outcomes) == ["conflict", "prepared"]
    assert len(json.loads(store.path.read_text())["submissions"]) == 1


def test_receipt_write_failure_cannot_authorize_successor(setup, monkeypatch):
    store, gateway = setup
    admit(store)
    prepare(store)
    gateway.queue = queue()
    monkeypatch.setattr(module, "_write_state", Mock(side_effect=OSError("synthetic storage failure")))
    with pytest.raises(CoordinatorUnavailable): prepare(store, SECOND)
    value = json.loads(store.path.read_text())
    assert value["head"] == FIRST and value["submissions"][FIRST]["state"] == "pending"


def test_corrupted_receipt_evidence_is_not_loaded_as_done(setup):
    store, gateway = setup
    admit(store)
    prepare(store)
    value = json.loads(store.path.read_text())
    value["submissions"][FIRST]["state"] = "done"
    store.path.write_text(json.dumps(value))
    with pytest.raises(CoordinatorError): store.state("primary")


class Response:
    def __init__(self, value=None, status=200):
        self.status_code, self.raw, self.closed = status, json.dumps(value).encode(), False
    def iter_content(self, _size): yield self.raw
    def close(self): self.closed = True


@pytest.mark.parametrize("status,error", [(200, None), (503, CoordinatorUnavailable)])
def test_gateway_evidence_reads_release_connection_after_each_request(tmp_path, monkeypatch, status, error):
    response = Response({"state": "idle", "length": 0, "steps": []}, status)
    request = Mock(return_value=response)
    monkeypatch.setattr(requests, "request", request)
    store = CoordinatorStore(tmp_path / "coordinator.json", "http://offline.invalid", "synthetic-credential")
    if error:
        with pytest.raises(error):
            store._get("/robot/rws/queue")
    else:
        assert store._get("/robot/rws/queue")["state"] == "idle"
    assert request.call_count == 1
    assert request.call_args.args == ("GET", "http://offline.invalid/api/v1/robot/rws/queue")
    assert request.call_args.kwargs["headers"]["Connection"] == "close"
    assert response.closed


@pytest.mark.parametrize("status,error", [(401, CoordinatorError), (403, CoordinatorError),
                                         (409, CoordinatorConflict), (500, CoordinatorUnavailable),
                                         (503, CoordinatorUnavailable), (302, CoordinatorError)])
def test_client_classifies_failures_without_retry_or_exposing_server_text(monkeypatch, status, error):
    response = Response({"error": "PRIVATE DETAIL"}, status)
    request = Mock(return_value=response)
    monkeypatch.setattr(requests, "request", request)
    client = CoordinatorClient("http://offline.invalid:8092", "synthetic-credential", "primary")
    with pytest.raises(error) as caught: client.prepare_queue(RUN, FIRST, steps())
    assert "PRIVATE DETAIL" not in str(caught.value)
    assert request.call_count == 1 and response.closed
    assert request.call_args.kwargs["allow_redirects"] is False


def test_client_transport_failure_is_uncertain_and_never_retried(monkeypatch):
    request = Mock(side_effect=requests.Timeout("PRIVATE DETAIL"))
    monkeypatch.setattr(requests, "request", request)
    client = CoordinatorClient("http://offline.invalid:8092", "synthetic-credential", "secondary")
    with pytest.raises(CoordinatorUnavailable) as caught: client.prepare_queue(RUN, FIRST, steps())
    assert "PRIVATE DETAIL" not in str(caught.value) and request.call_count == 1


def handler(store, *, path="/v1/state", body=None, token="synthetic-token", role="primary", extra_headers=""):
    raw = json.dumps(body).encode() if body is not None else b""
    instance = CoordinatorHandler.__new__(CoordinatorHandler)
    instance.server = SimpleNamespace(store=store, token="synthetic-token")
    instance.headers = Parser().parsestr(
        f"Authorization: Bearer {token}\nX-Failover-Role: {role}\nContent-Length: {len(raw)}\n{extra_headers}\n")
    instance.path, instance.rfile, instance.wfile = path, BytesIO(raw), BytesIO()
    instance.request_version, instance.requestline = "HTTP/1.1", ""
    instance.command, instance.close_connection = ("POST" if body is not None else "GET"), False
    instance._dispatch(post=body is not None)
    return instance.wfile.getvalue().decode()


def test_every_route_requires_authentication_and_no_secrets_enter_error_body():
    store = Mock()
    for path, body in (("/v1/state", None), ("/v1/receipt?run_id=x&submission_id=y", None),
                       ("/v1/start", {"role": "primary", "run_id": RUN, "settings": {}})):
        answer = handler(store, path=path, body=body, token="PRIVATE DETAIL")
        assert "401" in answer.splitlines()[0] and "PRIVATE DETAIL" not in answer
    assert store.mock_calls == []
    answer = handler(store, extra_headers="Authorization: Bearer synthetic-token\n")
    assert "401" in answer.splitlines()[0]


def test_authenticated_routes_dispatch_exact_fields_without_gateway_mutations(setup):
    store, gateway = setup
    answer = handler(store)
    assert "200" in answer.splitlines()[0] and '"run":null' in answer
    answer = handler(store, path="/v1/start", body={"role": "primary", "run_id": RUN, "settings": {}})
    assert "200" in answer.splitlines()[0] and '"phase":"active"' in answer
    assert gateway.calls == []
    answer = handler(store, path="/v1/start", body={"role": "primary", "run_id": "different", "settings": {}})
    assert "409" in answer.splitlines()[0]
    answer = handler(store, path="/v1/start", body={"role": "primary", "run_id": RUN, "settings": {}, "extra": "private"})
    assert "400" in answer.splitlines()[0] and "private" not in answer
    assert "400" in handler(store, role="untrusted").splitlines()[0]


def test_handler_rejects_oversized_or_ambiguous_request_lengths():
    store = Mock()
    answer = handler(store, path="/v1/start", body={}, extra_headers="Content-Length: 999999999\n")
    assert "400" in answer.splitlines()[0] and store.mock_calls == []


def test_gateway_reads_are_only_get_and_no_redirect_credentials(monkeypatch, tmp_path):
    response = Response({"active": True, "client_id": "primary"})
    request = Mock(return_value=response)
    monkeypatch.setattr(requests, "request", request)
    store = CoordinatorStore(tmp_path / "state.json", "http://offline.invalid/api/v1", "synthetic-token")
    assert request.call_count == 0
    store._lease("primary")
    args, options = request.call_args
    assert args == ("GET", "http://offline.invalid/api/v1/control")
    assert options["allow_redirects"] is False and "json" not in options


@pytest.mark.parametrize("change", ["type", "arm", "state"])
def test_non_scalar_gateway_fields_are_rejected_without_changing_receipt(setup, change):
    store, gateway = setup
    admit(store)
    prepare(store)
    gateway.queue = queue()
    gateway.queue["steps"][0][change] = ["unexpected"]
    with pytest.raises(CoordinatorError): store.receipt("primary", RUN, FIRST)
    assert json.loads(store.path.read_text())["submissions"][FIRST]["state"] == "pending"


def test_heartbeat_cannot_reopen_terminal_batch(setup):
    store, gateway = setup
    admit(store)
    store.stop("primary", RUN, "operator_stop")
    for role in ("primary", "secondary", "tertiary"):
        status = store.heartbeat(role, RUN, "armed")
        assert status["run"]["phase"] == "stopped"
        assert status["workers"][role]["run_id"] == RUN
    assert gateway.calls == []


def test_heartbeats_are_volatile_timestamped_and_never_rewrite_state(setup, monkeypatch):
    store, gateway = setup
    admit(store)
    before = store.path.read_bytes()
    write = Mock(side_effect=AssertionError("Heartbeats must not write persistent state"))
    monkeypatch.setattr(module, "_write_state", write)
    for role in ("primary", "secondary", "tertiary"):
        for status in ("armed", "waiting", "active", "active"):
            current = store.heartbeat(role, RUN, status)
            assert current["workers"][role]["status"] == status
            assert module._timestamp(current["workers"][role]["last_seen"]).tzinfo is not None
    assert store.path.read_bytes() == before and write.call_count == 0
    assert len(store.state("primary")["workers"]) == 3
    restarted = CoordinatorStore(store.path, "http://offline.invalid", "synthetic-token")
    assert restarted.state("primary")["workers"] == {}
    assert restarted.state("primary")["run"]["id"] == RUN


def test_no_prepared_queue_can_be_hidden_by_new_run_after_unknown_stop(setup):
    store, gateway = setup
    admit(store)
    prepare(store)
    store.stop("primary", RUN, "operator_stop")
    with pytest.raises(CoordinatorConflict): store.start("primary", "replacement-run", {})
    assert store.state("primary")["run"]["id"] == RUN


def test_old_receipt_remains_available_after_new_batch_start(setup):
    store, gateway = setup
    admit(store)
    prepare(store)
    gateway.queue = queue()
    store.finish("primary", RUN, "no_pickable_cups")
    store.start("primary", "next-batch", {})
    assert store.receipt("secondary", RUN, FIRST)["queue"] == queue()


def inspected_failure(store, gateway, *, state="failed", role="primary"):
    admit(store)
    gateway.owner = role
    prepare(store, role=role)
    gateway.queue = queue(owner=role, state=state)
    receipt = store.receipt(role, RUN, FIRST)
    store.block(role, RUN, "left_park_failed")
    gateway.owner = "primary"
    gateway.queue = {"state": "idle", "length": 0, "steps": []}
    return receipt


@pytest.mark.parametrize("state", ["failed", "cancelled"])
@pytest.mark.parametrize("owner", ["primary", "secondary", "tertiary"])
def test_inspected_recovery_preserves_failure_and_permits_only_new_batch(setup, state, owner):
    store, gateway = setup
    original = inspected_failure(store, gateway, state=state, role=owner)
    gateway.arms["left"] = {"accepted": False, "message": "ERR move failed"}
    before = json.loads(store.path.read_text())
    assert module.RECOVERY_CAPABILITY in store.state("primary")["capabilities"]
    receipt = store.recover("primary", RUN, FIRST, acknowledged=True)
    assert receipt["state"] == state and receipt["queue"] == original["queue"]
    recovery = module.validate_recovery(receipt["recovery"], run_id=RUN, submission_id=FIRST,
                                        queue=original["queue"])
    assert recovery["arm_status"] == gateway.arms
    after = json.loads(store.path.read_text())
    original_item = before["submissions"][FIRST]
    assert after["submissions"][FIRST] == {**original_item, "recovery": recovery}
    assert after["run"]["reason"] == before["run"]["reason"] == "left_park_failed"
    assert after["run"]["phase"] == "stopped" and after["head"] is None
    assert store.start("primary", RUN, before["run"]["settings"])["run"]["phase"] == "stopped"
    store.start("primary", "fresh-scan-batch", {})
    restarted = CoordinatorStore(store.path, "http://offline.invalid", "synthetic-token")
    assert restarted.receipt("tertiary", RUN, FIRST) == receipt
    assert restarted.state("secondary")["run"]["id"] == "fresh-scan-batch"


def test_recovery_retry_revalidates_gateway_without_rewriting_acknowledgement(setup, monkeypatch):
    store, gateway = setup
    inspected_failure(store, gateway)
    receipt = store.recover("primary", RUN, FIRST, acknowledged=True)
    commit = Mock(wraps=store._commit)
    monkeypatch.setattr(store, "_commit", commit)
    gateway.calls.clear()
    assert store.recover("primary", RUN, FIRST, acknowledged=True) == receipt
    assert commit.call_count == 0
    assert gateway.calls == ["/control", "/robot/rws/queue", "/robot/rws/status?arm=left",
                             "/robot/rws/status?arm=right", "/robot/rws/queue", "/control"]
    gateway.owner = "secondary"
    with pytest.raises(CoordinatorConflict): store.recover("primary", RUN, FIRST, acknowledged=True)
    gateway.owner = "primary"
    gateway.queue = queue(SECOND, state="running")
    with pytest.raises(CoordinatorConflict): store.recover("primary", RUN, FIRST, acknowledged=True)
    gateway.queue = {"state": "idle", "length": 0, "steps": []}
    store.start("primary", "new-batch", {})
    with pytest.raises(CoordinatorConflict): store.recover("primary", RUN, FIRST, acknowledged=True)


@pytest.mark.parametrize("acknowledged", [False, None, 1, "yes"])
def test_recovery_requires_explicit_boolean_inspection(setup, acknowledged):
    store, gateway = setup
    inspected_failure(store, gateway)
    gateway.calls.clear()
    before = store.path.read_bytes()
    with pytest.raises(CoordinatorError): store.recover("primary", RUN, FIRST, acknowledged=acknowledged)
    assert gateway.calls == [] and store.path.read_bytes() == before


@pytest.mark.parametrize("role", ["secondary", "tertiary"])
def test_standby_cannot_acknowledge_recovery(setup, role):
    store, gateway = setup
    inspected_failure(store, gateway)
    gateway.owner = role
    before = store.path.read_bytes()
    with pytest.raises(CoordinatorConflict): store.recover(role, RUN, FIRST, acknowledged=True)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("state", ["pending", "unknown", "done", "not_submitted"])
def test_recovery_never_retires_other_receipt_states(setup, state):
    store, gateway = setup
    admit(store)
    prepare(store)
    if state == "unknown": store.receipt("primary", RUN, FIRST)
    if state == "done":
        gateway.queue = queue()
        store.receipt("primary", RUN, FIRST)
    if state == "not_submitted": store.reject_queue("primary", RUN, FIRST)
    store.stop("primary", RUN, "operator_stop")
    gateway.queue = {"state": "idle", "length": 0, "steps": []}
    before = store.path.read_bytes()
    with pytest.raises(CoordinatorConflict): store.recover("primary", RUN, FIRST, acknowledged=True)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("change", ["active_run", "wrong_submission", "running_step", "no_lease",
                                    "queue_busy", "queue_failed", "arm_busy", "arm_unknown", "arm_bad_boolean"])
def test_recovery_rejects_unsafe_state_without_changing_evidence(setup, change):
    store, gateway = setup
    inspected_failure(store, gateway)
    identity = FIRST
    if change in {"active_run", "running_step"}:
        disk = json.loads(store.path.read_text())
        if change == "active_run":
            disk["run"]["phase"] = disk["runs"][RUN]["phase"] = "active"
        else:
            disk["submissions"][FIRST]["queue"]["steps"][0]["state"] = "running"
        store.path.write_text(json.dumps(disk))
    if change == "wrong_submission": identity = SECOND
    if change == "no_lease": gateway.owner = None
    if change == "queue_busy": gateway.queue = queue(SECOND, state="running")
    if change == "queue_failed": gateway.queue = queue(state="failed")
    if change == "arm_busy": gateway.arms["right"] = {"accepted": True, "message": "BUSY"}
    if change == "arm_unknown": gateway.arms["left"] = {"accepted": False, "message": "ERR unavailable"}
    if change == "arm_bad_boolean": gateway.arms["left"] = {"accepted": 1, "message": "DONE"}
    before = store.path.read_bytes()
    with pytest.raises(CoordinatorConflict): store.recover("primary", RUN, identity, acknowledged=True)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("change", ["queue", "lease"])
def test_recovery_rechecks_queue_and_lease_before_commit(setup, monkeypatch, change):
    store, gateway = setup
    inspected_failure(store, gateway)
    before = store.path.read_bytes()
    def get(path):
        result = gateway.get(path)
        if path == "/robot/rws/status?arm=right":
            if change == "queue": gateway.queue = queue(SECOND, state="running")
            else: gateway.owner = "secondary"
        return result
    monkeypatch.setattr(store, "_get", get)
    with pytest.raises(CoordinatorConflict): store.recover("primary", RUN, FIRST, acknowledged=True)
    assert store.path.read_bytes() == before


def test_failed_recovery_write_keeps_failure_attached(setup, monkeypatch):
    store, gateway = setup
    inspected_failure(store, gateway)
    before = store.path.read_bytes()
    monkeypatch.setattr(module, "_write_state", Mock(side_effect=OSError("synthetic storage failure")))
    with pytest.raises(CoordinatorUnavailable): store.recover("primary", RUN, FIRST, acknowledged=True)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("change", ["ack", "role", "run_id", "submission_id", "digest", "timestamp",
                                    "arms", "extra", "queue_changed", "run_link", "head", "phase"])
def test_corrupted_recovery_metadata_cannot_retire_failure_on_restart(setup, change):
    store, gateway = setup
    inspected_failure(store, gateway)
    store.recover("primary", RUN, FIRST, acknowledged=True)
    disk = json.loads(store.path.read_text())
    recovery = disk["submissions"][FIRST]["recovery"]
    changes = {"ack": ("acknowledged", 1), "role": ("acknowledged_by", "secondary"),
               "run_id": ("run_id", "other-run"), "submission_id": ("submission_id", SECOND),
               "digest": ("queue_sha256", "0" * 64), "timestamp": ("recovered_at", "2026-11-20T10:00:00"),
               "arms": ("arm_status", {"left": {"accepted": True, "message": "DONE"}}),
               "extra": ("unrecognized", True)}
    if change in changes:
        key, changed = changes[change]
        recovery[key] = changed
        disk["run"]["recovery"] = disk["runs"][RUN]["recovery"] = copy.deepcopy(recovery)
    if change == "queue_changed": disk["submissions"][FIRST]["queue"]["message"] = "changed evidence"
    if change == "run_link": del disk["runs"][RUN]["recovery"]
    if change == "head": disk["head"] = FIRST
    if change == "phase": disk["run"]["phase"] = disk["runs"][RUN]["phase"] = "active"
    store.path.write_text(json.dumps(disk))
    with pytest.raises(CoordinatorError): CoordinatorStore(store.path, "http://offline.invalid", "synthetic-token")


def test_recovery_http_contract_and_single_attempt_client(setup, monkeypatch):
    store, gateway = setup
    inspected_failure(store, gateway)
    body = {"role": "primary", "run_id": RUN, "submission_id": FIRST, "acknowledged": True}
    answer = handler(store, path="/v1/recover", body=body)
    assert "200" in answer.splitlines()[0] and '"recovery":' in answer
    assert '"state":"failed"' in answer
    assert "400" in handler(store, path="/v1/recover", body={k: v for k, v in body.items()
                                                            if k != "acknowledged"}).splitlines()[0]
    assert "401" in handler(store, path="/v1/recover", body=body, token="wrong-token").splitlines()[0]
    request = Mock(side_effect=requests.Timeout("PRIVATE DETAIL"))
    monkeypatch.setattr(requests, "request", request)
    client = CoordinatorClient("http://offline.invalid:8092", "synthetic-token", "primary")
    with pytest.raises(CoordinatorUnavailable): client.recover(RUN, FIRST, acknowledged=True)
    assert request.call_count == 1
    assert request.call_args.args == ("POST", "http://offline.invalid:8092/v1/recover")
    assert request.call_args.kwargs["json"] == body


def verified_homes(store, gateway, *, continuous=True, role="primary"):
    store.start("primary", RUN, {"continuous_batch": continuous})
    gateway.owner = role
    store.set_phase(role, RUN, "finalizing")
    for identity, arm in ((FIRST, "right"), (SECOND, "left")):
        plan = steps(identity, arm=arm, kind="home")
        store.prepare_queue(role, RUN, identity, plan)
        gateway.queue = queue(identity, owner=role, plan=plan)
        store.confirm_home(role, RUN, identity, arm)
    return store.state(role)


@pytest.mark.parametrize("role", ["primary", "secondary", "tertiary"])
def test_continuous_watching_is_durable_after_both_measured_homes(setup, role):
    store, gateway = setup
    verified_homes(store, gateway, role=role)
    watching = store.watch(role, RUN)
    assert watching["run"]["phase"] == "watching"
    assert watching["run"]["confirmed_homes"] == ["right", "left"]
    assert watching["run"]["home_receipts"] == {"right": FIRST, "left": SECOND}
    assert watching["head"]["submission_id"] == SECOND
    assert module.CONTINUOUS_CAPABILITY in watching["capabilities"]
    saved = store.path.read_bytes()
    assert store.watch(role, RUN) == watching
    assert store.path.read_bytes() == saved
    restarted = CoordinatorStore(store.path, "http://offline.invalid", "synthetic-token")
    assert restarted.state(role) == watching
    assert store.heartbeat(role, RUN, "watching")["run"]["phase"] == "watching"
    with pytest.raises(CoordinatorConflict): store.start("primary", "other-run", {})
    with pytest.raises(CoordinatorConflict): store.set_phase(role, RUN, "finalizing")
    with pytest.raises(CoordinatorConflict): store.finish(role, RUN, "workspace_empty")
    with pytest.raises(CoordinatorConflict):
        store.prepare_queue(role, RUN, THIRD, steps(THIRD, arm="right", kind="home"))
    assert store.path.read_bytes() == saved


def test_next_cup_atomically_resets_home_cycle_and_old_receipts_stay_durable(setup):
    store, gateway = setup
    verified_homes(store, gateway)
    store.watch("primary", RUN)
    before = json.loads(store.path.read_text())
    assert store.prepare_queue("primary", RUN, SECOND, steps(SECOND, arm="left", kind="home"))["state"] == "done"
    assert store.state("primary")["run"]["phase"] == "watching", "Old retries cannot start a new cycle"
    assert prepare(store, THIRD)["state"] == "pending"
    after = json.loads(store.path.read_text())
    assert after["run"]["phase"] == "active"
    assert after["run"]["home_cycle"] == 1
    assert after["run"]["confirmed_homes"] == [] and after["run"]["home_receipts"] == {}
    assert after["submissions"][THIRD]["home_cycle"] == 1 and after["head"] == THIRD
    assert after["submissions"][FIRST] == before["submissions"][FIRST]
    assert after["submissions"][SECOND] == before["submissions"][SECOND]
    assert store.receipt("tertiary", RUN, FIRST)["state"] == "done"
    assert store.prepare_queue("primary", RUN, THIRD, steps(THIRD))["state"] == "pending"
    assert store.state("primary")["run"]["home_cycle"] == 1, "Retry never advances the cycle twice"


def test_delayed_previous_cycle_home_cannot_satisfy_new_home_barrier(setup):
    store, gateway = setup
    verified_homes(store, gateway)
    store.watch("primary", RUN)
    prepare(store, THIRD)
    gateway.queue = queue(THIRD)
    store.set_phase("primary", RUN, "finalizing")
    for old_identity, arm in ((FIRST, "right"), (SECOND, "left")):
        with pytest.raises(CoordinatorConflict): store.confirm_home("primary", RUN, old_identity, arm)
    assert store.state("primary")["run"]["confirmed_homes"] == []
    for identity, arm in (("d" * 16, "right"), ("e" * 16, "left")):
        plan = steps(identity, arm=arm, kind="home")
        store.prepare_queue("primary", RUN, identity, plan)
        gateway.queue = queue(identity, plan=plan)
        store.confirm_home("primary", RUN, identity, arm)
    assert store.watch("primary", RUN)["run"]["home_cycle"] == 1


@pytest.mark.parametrize("change", ["lease", "gateway_failed", "write_failed"])
def test_next_cup_cannot_reset_home_proof_without_durable_permit(setup, monkeypatch, change):
    store, gateway = setup
    verified_homes(store, gateway)
    store.watch("primary", RUN)
    saved = store.path.read_bytes()
    if change == "lease": gateway.owner = "secondary"
    if change == "gateway_failed": gateway.queue = queue(SECOND, state="failed")
    if change == "write_failed":
        monkeypatch.setattr(module, "_write_state", Mock(side_effect=OSError("disk full")))
    with pytest.raises(CoordinatorError): prepare(store, THIRD)
    assert store.path.read_bytes() == saved


@pytest.mark.parametrize("phase", ["active", "finalizing", "blocked", "stopped"])
def test_watch_cannot_start_without_home_or_reopen_a_terminal_run(setup, phase):
    store, gateway = setup
    store.start("primary", RUN, {"continuous_batch": True})
    if phase == "finalizing": store.set_phase("primary", RUN, "finalizing")
    if phase == "blocked": store.block("primary", RUN, "failure")
    if phase == "stopped": store.stop("primary", RUN, "operator_stop")
    before = store.path.read_bytes()
    with pytest.raises(CoordinatorConflict): store.watch("primary", RUN)
    assert store.path.read_bytes() == before


def test_finite_batch_never_enters_continuous_watching(setup):
    store, gateway = setup
    verified_homes(store, gateway, continuous=False)
    with pytest.raises(CoordinatorConflict): store.watch("primary", RUN)
    assert store.finish("primary", RUN, "workspace_empty")["run"]["phase"] == "complete"
    with pytest.raises(CoordinatorConflict): store.watch("primary", RUN)


def test_stop_and_block_end_watching_without_reopening_recovered_runs(setup):
    store, gateway = setup
    verified_homes(store, gateway)
    store.watch("primary", RUN)
    store.stop("primary", RUN, "operator_stop")
    with pytest.raises(CoordinatorConflict): store.watch("primary", RUN)
    with pytest.raises(CoordinatorConflict): prepare(store, THIRD)
    assert store.start("primary", RUN, {"continuous_batch": True})["run"]["phase"] == "stopped"
    store.start("primary", "next-run", {"continuous_batch": True})
    store.prepare_queue("primary", "next-run", THIRD, steps(THIRD))
    gateway.queue = queue(THIRD, state="failed")
    store.receipt("primary", "next-run", THIRD)
    store.block("primary", "next-run", "failure")
    gateway.queue = {"state": "idle", "length": 0, "steps": []}
    store.recover("primary", "next-run", THIRD, acknowledged=True)
    with pytest.raises(CoordinatorConflict): store.watch("primary", "next-run")
    assert store.start("primary", "next-run", {"continuous_batch": True})["run"]["phase"] == "stopped"


def test_watch_rechecks_lease_and_does_not_write_on_late_preemption(setup, monkeypatch):
    store, gateway = setup
    verified_homes(store, gateway)
    before = store.path.read_bytes()
    checks = 0
    def get(path):
        nonlocal checks
        if path == "/control":
            checks += 1
            if checks == 2: gateway.owner = "secondary"
        return gateway.get(path)
    monkeypatch.setattr(store, "_get", get)
    with pytest.raises(CoordinatorConflict): store.watch("primary", RUN)
    assert checks == 2 and store.path.read_bytes() == before


@pytest.mark.parametrize("change", ["finite", "missing_home", "wrong_cycle", "boolean_cycle"])
def test_corrupt_watching_evidence_fails_closed_on_restart(setup, change):
    store, gateway = setup
    verified_homes(store, gateway)
    store.watch("primary", RUN)
    disk = json.loads(store.path.read_text())
    run = disk["run"]
    if change == "finite": run["settings"]["continuous_batch"] = False
    if change == "missing_home": run.update(confirmed_homes=["right"], home_receipts={"right": FIRST})
    if change == "wrong_cycle": run["home_cycle"] = 1
    if change == "boolean_cycle": run["home_cycle"] = True
    disk["runs"][RUN] = copy.deepcopy(run)
    store.path.write_text(json.dumps(disk))
    with pytest.raises(CoordinatorError): CoordinatorStore(store.path, "http://offline.invalid", "synthetic-token")


def test_continuous_setting_requires_boolean_and_missing_setting_remains_finite(setup):
    store, gateway = setup
    for invalid in (1, None, "true", []):
        with pytest.raises(CoordinatorError): store.start("primary", RUN, {"continuous_batch": invalid})
    admit(store)
    assert store.finish("primary", RUN, "no_pickable_cups")["run"]["phase"] == "complete"


def test_watch_http_contract_and_client_single_attempt(setup, monkeypatch):
    store, gateway = setup
    verified_homes(store, gateway)
    answer = handler(store, path="/v1/watch", body={"role": "primary", "run_id": RUN})
    assert "200" in answer.splitlines()[0] and '"phase":"watching"' in answer
    assert "400" in handler(store, path="/v1/watch", body={"role": "primary", "run_id": RUN,
                                                            "extra": True}).splitlines()[0]
    request = Mock(side_effect=requests.Timeout("PRIVATE DETAIL"))
    monkeypatch.setattr(requests, "request", request)
    client = CoordinatorClient("http://offline.invalid:8092", "synthetic-token", "primary")
    with pytest.raises(CoordinatorUnavailable): client.watch(RUN)
    assert request.call_count == 1
    assert request.call_args.args == ("POST", "http://offline.invalid:8092/v1/watch")
    assert request.call_args.kwargs["json"] == {"role": "primary", "run_id": RUN}


def test_home_measurement_proof_survives_cycle_change_for_returning_worker(setup):
    store, gateway = setup
    verified_homes(store, gateway)
    for identity in (FIRST, SECOND):
        receipt = store.receipt("tertiary", RUN, identity)
        assert receipt["home_cycle"] == 0 and receipt["home_verified"] is True
    store.watch("primary", RUN)
    prepare(store, THIRD)
    assert store.state("primary")["run"]["home_cycle"] == 1
    gateway.queue = queue(THIRD)
    store.receipt("primary", RUN, THIRD)
    assert "home_verified" not in store.receipt("primary", RUN, THIRD)
    store.set_phase("primary", RUN, "finalizing")
    for identity in (FIRST, SECOND):
        receipt = store.receipt("tertiary", RUN, identity)
        assert receipt["state"] == "done" and receipt["home_verified"] is True and receipt["home_cycle"] == 0
    with pytest.raises(CoordinatorConflict): store.confirm_home("primary", RUN, FIRST, "right")
    assert store.state("primary")["run"]["confirmed_homes"] == []


def test_done_home_without_measurement_does_not_claim_home_verified(setup):
    store, gateway = setup
    store.start("primary", RUN, {"continuous_batch": True})
    store.set_phase("primary", RUN, "finalizing")
    plan = steps(FIRST, arm="right", kind="home")
    store.prepare_queue("primary", RUN, FIRST, plan)
    gateway.queue = queue(plan=plan)
    receipt = store.receipt("tertiary", RUN, FIRST)
    assert receipt["state"] == "done" and "home_verified" not in receipt
    with pytest.raises(CoordinatorConflict): store.watch("primary", RUN)
    store.confirm_home("primary", RUN, FIRST, "right")
    assert store.receipt("tertiary", RUN, FIRST)["home_verified"] is True


def test_legacy_home_confirmation_is_preserved_before_continuous_cycle_reset(setup):
    store, gateway = setup
    verified_homes(store, gateway)
    disk = json.loads(store.path.read_text())
    for identity in (FIRST, SECOND):
        del disk["submissions"][identity]["home_verified"]
        del disk["submissions"][identity]["home_cycle"]
    del disk["run"]["home_cycle"]
    del disk["runs"][RUN]["home_cycle"]
    store.path.write_text(json.dumps(disk))
    store.watch("primary", RUN)
    prepare(store, THIRD)
    for identity in (FIRST, SECOND):
        receipt = store.receipt("tertiary", RUN, identity)
        assert receipt["home_verified"] is True and receipt.get("home_cycle", 0) == 0


@pytest.mark.parametrize("change", ["not_home", "not_done", "false", "integer", "cycle_type"])
def test_invalid_measured_home_marker_is_rejected_on_load(setup, change):
    store, gateway = setup
    admit(store)
    prepare(store)
    gateway.queue = queue(state="failed" if change == "not_done" else "done")
    store.receipt("primary", RUN, FIRST)
    disk = json.loads(store.path.read_text())
    item = disk["submissions"][FIRST]
    item["home_verified"] = True
    if change != "not_home":
        item["steps"][0]["type"] = item["queue"]["steps"][0]["type"] = "home"
    if change == "false": item["home_verified"] = False
    if change == "integer": item["home_verified"] = 1
    if change == "cycle_type": item["home_cycle"] = True
    store.path.write_text(json.dumps(disk))
    with pytest.raises(CoordinatorError): CoordinatorStore(store.path, "http://offline.invalid", "synthetic-token")
