"""History polling, storage deadlines and external state changes; no robot access."""

import json
from unittest.mock import Mock

import pytest
import requests

import failover_coordinator as module
from test_failover_coordinator import Gateway, FIRST, RUN, steps


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "request", Mock(side_effect=AssertionError("Real HTTP forbidden")))
    result = module.CoordinatorStore(tmp_path / "state.json", "http://offline.invalid", "test-token")
    result._get = Gateway().get
    result.start("primary", RUN, {})
    result.prepare_queue("primary", RUN, FIRST, steps())
    result.reject_queue("primary", RUN, FIRST)
    result.state("primary")  # Validate the final durable bytes once.
    return result


def test_status_and_heartbeats_reuse_validated_history_without_parsing_it(store, monkeypatch):
    before = store.path.read_bytes()
    monkeypatch.setattr(module, "_decode", Mock(side_effect=AssertionError("Repeated history parse")))
    monkeypatch.setattr(store, "_validate_state", Mock(side_effect=AssertionError("Repeated history validation")))
    for role in ("primary", "secondary", "tertiary"):
        snapshot = store.heartbeat(role, RUN, "waiting")
        assert snapshot["workers"][role]["status"] == "waiting"
        assert snapshot["head"]["submission_id"] == FIRST
        snapshot["run"]["phase"] = "corrupted caller copy"
        snapshot["head"]["state"] = "done"
        assert store.state(role)["run"]["phase"] == "active"
        assert store.state(role)["head"]["state"] == "not_submitted"
    assert len(store.state("primary")["workers"]) == 3
    assert store.path.read_bytes() == before


def test_same_size_external_corruption_is_revalidated_for_cached_status(store):
    original = store.path.read_bytes()
    corrupted = original.replace(b'"state":"not_submitted"', b'"state":"bad_submitted"')
    assert len(corrupted) == len(original) and corrupted != original
    store.path.write_bytes(corrupted)
    with pytest.raises(module.CoordinatorError):
        store.state("primary")
    with pytest.raises(module.CoordinatorError):
        store.heartbeat("secondary", RUN)
    store.path.write_bytes(original)
    assert store.state("primary")["head"]["state"] == "not_submitted"


def test_peer_commit_invalidates_cached_status_and_checks_current_run(store):
    peer = module.CoordinatorStore(store.path, "http://offline.invalid", "test-token")
    peer.stop("primary", RUN, "test")
    assert store.state("primary")["run"]["phase"] == "stopped"
    peer.start("primary", "next-run", {})
    assert store.state("secondary")["run"]["id"] == "next-run"
    with pytest.raises(module.CoordinatorConflict):
        store.heartbeat("primary", RUN)


def test_failed_transaction_cannot_modify_cached_durable_public_result(store, monkeypatch):
    original = store.state("primary")
    before = store.path.read_bytes()
    monkeypatch.setattr(module, "_write_state", Mock(side_effect=OSError("synthetic write failure")))
    with pytest.raises(module.CoordinatorUnavailable):
        store.stop("primary", RUN, "must not be cached")
    assert store.state("primary") == original
    assert store.path.read_bytes() == before


def test_directory_fsync_failure_revalidates_replaced_file_instead_of_serving_old_cache(store, monkeypatch):
    before, digest = store.state("primary"), store._validated_digest
    original_fsync = module.os.fsync
    barriers = []
    def fail_directory_barrier(descriptor):
        if module.stat.S_ISDIR(module.os.fstat(descriptor).st_mode):
            barriers.append("directory")
            raise OSError("synthetic directory fsync failure after rename")
        barriers.append("file")
        original_fsync(descriptor)
    monkeypatch.setattr(module.os, "fsync", fail_directory_barrier)
    with pytest.raises(module.CoordinatorUnavailable):
        store.stop("primary", RUN, "new state was replaced but its directory barrier failed")
    assert barriers == ["file", "directory"]
    assert json.loads(store.path.read_text())["run"]["phase"] == "stopped"
    assert store._validated_digest == digest  # The failed commit did not update the cache.
    validate = Mock(wraps=store._validate_state)
    monkeypatch.setattr(store, "_validate_state", validate)
    current = store.state("primary")
    assert before["run"]["phase"] == "active" and current["run"]["phase"] == "stopped"
    assert current["run"]["reason"].startswith("new state was replaced")
    assert store._validated_digest != digest
    validate.assert_called_once()
    assert store.state("secondary") == current
    validate.assert_called_once()  # The newly validated actual bytes can now be cached.
    restarted = module.CoordinatorStore(store.path, "http://offline.invalid", "test-token")
    assert restarted.state("primary") == current


def test_cached_memory_contains_only_public_head_and_not_receipt_history(store):
    old_ids = [FIRST]
    for index in range(12):
        identity = f"{index:016x}"
        store.prepare_queue("primary", RUN, identity, steps(identity))
        store.reject_queue("primary", RUN, identity)
        old_ids.append(identity)
    public = store.state("primary")
    history = json.loads(store.path.read_text())
    assert len(history["submissions"]) == 13
    assert set(store._cached_public) == {"run", "head", "capabilities"}
    assert store._cached_public == {key: value for key, value in public.items() if key != "workers"}
    encoded = json.dumps(store._cached_public)
    assert old_ids[-1] in encoded
    assert all(identity not in encoded for identity in old_ids[:-1])
    # Also guard against adding a second full-document cache beside the small
    # public cache. The store may retain only the fingerprint and live workers.
    dictionaries = [value for value in vars(store).values() if isinstance(value, dict)]
    while dictionaries:
        value = dictionaries.pop()
        assert "submissions" not in value and "runs" not in value
        dictionaries.extend(item for item in value.values() if isinstance(item, dict))
    blobs = [value for value in vars(store).values() if isinstance(value, (bytes, bytearray))]
    assert blobs == [store._validated_digest] and len(blobs[0]) == 32


def test_cached_public_history_does_not_replace_fresh_mutation_document(store):
    with store._transaction() as value:
        value["run"]["reason"] = "uncommitted"
        value["submissions"][FIRST]["state"] = "corrupt"
    assert store.state("primary")["run"]["reason"] == ""
    assert store.state("primary")["head"]["state"] == "not_submitted"
    with store._transaction() as value:
        assert value["submissions"][FIRST]["state"] == "not_submitted"


@pytest.mark.parametrize("action", ["start", "prepare_queue", "reject_queue", "receipt", "stop", "confirm_home"])
def test_slow_durable_metadata_gets_its_own_budget_without_retry(monkeypatch, action):
    calls = []
    class Response:
        status_code = 200
        def iter_content(self, _size):
            yield b'{"state":"pending","queue":null}'
        def close(self): pass
    def slow_request(method, url, **options):
        calls.append((method, url, options))
        # A real embedded commit measured 3.44s: the old 3s budget loses its ACK.
        if options["timeout"][1] < 3.44:
            raise requests.ReadTimeout("synthetic slow durable commit")
        return Response()
    monkeypatch.setattr(requests, "request", slow_request)
    client = module.CoordinatorClient("http://offline.invalid", "test-token", "primary")
    assert client._call(action, {"run_id":RUN}, get=action == "receipt")["state"] == "pending"
    assert len(calls) == 1 and calls[0][2]["timeout"] == (1.0, 15)
    assert calls[0][2]["headers"]["Connection"] == "close"


def test_metadata_timeout_is_not_retried_and_identifies_operation(monkeypatch):
    request = Mock(side_effect=requests.ReadTimeout("synthetic timeout"))
    monkeypatch.setattr(requests, "request", request)
    client = module.CoordinatorClient("http://offline.invalid", "test-token", "primary", durable_timeout=15)
    with pytest.raises(module.CoordinatorUnavailable, match="prepare_queue.*15s") as error:
        client.prepare_queue(RUN, FIRST, steps())
    assert isinstance(error.value.__cause__.__cause__, requests.ReadTimeout)
    assert request.call_count == 1


def test_status_requests_allow_an_in_progress_commit_but_connect_quickly(monkeypatch):
    request = Mock(side_effect=requests.ReadTimeout("synthetic unavailable status"))
    monkeypatch.setattr(requests, "request", request)
    client = module.CoordinatorClient("http://offline.invalid", "test-token", "primary")
    with pytest.raises(module.CoordinatorUnavailable):
        client.state()
    assert request.call_args.kwargs["timeout"] == (1.0, 15)
    assert request.call_count == 1
