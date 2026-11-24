"""Compact durable history stays strict, compatible and atomic; no live I/O."""

import copy
import json
from unittest.mock import Mock

import pytest
import requests

import failover_coordinator as module
from test_failover_coordinator import FIRST, RUN, SECOND, Gateway, queue, steps


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "request", Mock(side_effect=AssertionError("Real HTTP forbidden")))
    store = module.CoordinatorStore(tmp_path / "state.json", "http://offline.invalid", "synthetic-token")
    gateway = Gateway()
    monkeypatch.setattr(store, "_get", gateway.get)
    store.start("primary", RUN, {
        "continuous_batch": True,
        "device_speeds": {"primary": 100, "secondary": 75.5, "tertiary": 50},
        "note": 'Café — "cup"', "optional": None,
    })
    store.prepare_queue("primary", RUN, FIRST, steps())
    gateway.queue = queue()
    assert store.receipt("primary", RUN, FIRST)["state"] == "done"
    store.prepare_queue("primary", RUN, SECOND, steps(SECOND))
    store.reject_queue("primary", RUN, SECOND)
    return store, module._decode(store.path.read_bytes())


def _no_temporary_files(store):
    assert list(store.path.parent.glob("." + store.path.name + "*.tmp")) == []


def test_old_pretty_history_is_read_without_rewriting_then_compacts_losslessly(history, monkeypatch):
    store, before = history
    pretty = (json.dumps(before, indent=2, ensure_ascii=False) + "\n").encode()
    store.path.write_bytes(pretty)
    restarted = module.CoordinatorStore(store.path, "http://offline.invalid", "synthetic-token")
    assert restarted.state("secondary")["head"]["state"] == "not_submitted"
    assert restarted.receipt("tertiary", RUN, FIRST)["queue"] == before["submissions"][FIRST]["queue"]
    assert store.path.read_bytes() == pretty

    finished = "2026-11-23T15:00:00+00:00"
    monkeypatch.setattr(module, "_now", lambda: finished)
    restarted.stop("primary", RUN, 'Operator stopped — "checked"')
    expected = copy.deepcopy(before)
    for run in (expected["run"], expected["runs"][RUN]):
        run.update(phase="stopped", reason='Operator stopped — "checked"', finished_at=finished)
    raw = store.path.read_bytes()
    assert module._decode(raw) == expected
    assert raw == json.dumps(expected, allow_nan=False, separators=(",", ":")).encode()
    assert len(raw) < len(pretty)
    assert b"\n" not in raw
    assert module.CoordinatorStore(store.path, "http://offline.invalid", "synthetic-token").state(
        "primary")["run"]["phase"] == "stopped"
    _no_temporary_files(store)


def test_max_state_limit_applies_to_exact_compact_bytes_before_touching_file(history, monkeypatch):
    store, value = history
    raw = module._json_bytes(value, limit=module.MAX_STATE)
    monkeypatch.setattr(module, "MAX_STATE", len(raw))
    store._commit(value)  # The exact boundary is allowed.
    assert store.path.read_bytes() == raw
    writer = Mock(side_effect=AssertionError("Oversize state reached disk writer"))
    monkeypatch.setattr(module, "_write_state", writer)
    monkeypatch.setattr(module, "MAX_STATE", len(raw) - 1)
    with pytest.raises(module.CoordinatorError, match="oversized"):
        store._commit(value)
    writer.assert_not_called()
    assert store.path.read_bytes() == raw
    _no_temporary_files(store)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf"), {1: "one", "1": "duplicate"}])
def test_nonfinite_or_duplicate_encoded_keys_are_rejected_before_write(history, monkeypatch, invalid):
    store, value = history
    before = store.path.read_bytes()
    value["workers"] = {"synthetic": invalid}
    writer = Mock(side_effect=AssertionError("Invalid JSON reached disk writer"))
    monkeypatch.setattr(module, "_write_state", writer)
    with pytest.raises(module.CoordinatorError):
        store._commit(value)
    writer.assert_not_called()
    assert store.path.read_bytes() == before
    _no_temporary_files(store)


@pytest.mark.parametrize("invalid_field", ['"invalid":NaN,', '"schema_version":1,'])
def test_strict_decoder_rejects_invalid_pretty_history_without_rewriting(history, invalid_field):
    store, value = history
    raw = ("{\n" + invalid_field + json.dumps(value, indent=2)[1:]).encode()
    store.path.write_bytes(raw)
    with pytest.raises(module.CoordinatorError, match="Invalid JSON"):
        module.CoordinatorStore(store.path, "http://offline.invalid", "synthetic-token")
    assert store.path.read_bytes() == raw
    _no_temporary_files(store)


@pytest.mark.parametrize("failure", ["create", "file_fsync", "replace"])
def test_failure_before_replacement_preserves_old_complete_history_and_removes_temp(history, monkeypatch, failure):
    store, before = history
    raw = store.path.read_bytes()
    def fail(*_args, **_kwargs):
        raise OSError("synthetic " + failure + " failure")
    if failure == "create":
        monkeypatch.setattr(module.tempfile, "NamedTemporaryFile", fail)
    elif failure == "file_fsync":
        original = module.os.fsync
        def fail_file(descriptor):
            if module.stat.S_ISREG(module.os.fstat(descriptor).st_mode):
                fail()
            return original(descriptor)
        monkeypatch.setattr(module.os, "fsync", fail_file)
    else:
        monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(module.CoordinatorUnavailable):
        store.stop("primary", RUN, "must not be acknowledged")
    assert store.path.read_bytes() == raw
    assert module._decode(raw) == before
    assert store.state("primary")["run"]["phase"] == "active"
    _no_temporary_files(store)


def test_commit_replaces_only_after_file_barrier_then_persists_directory(history, monkeypatch):
    store, before = history
    old_raw = store.path.read_bytes()
    events = []
    original_fsync, original_replace = module.os.fsync, module.os.replace
    def record_fsync(descriptor):
        kind = "directory" if module.stat.S_ISDIR(module.os.fstat(descriptor).st_mode) else "file"
        events.append(kind)
        if kind == "file":
            assert store.path.read_bytes() == old_raw
        else:
            assert module._decode(store.path.read_bytes())["run"]["phase"] == "stopped"
        return original_fsync(descriptor)
    def record_replace(source, target):
        assert events == ["file"]
        assert store.path.read_bytes() == old_raw
        candidate = module._decode(module.Path(source).read_bytes())
        assert candidate["submissions"] == before["submissions"]
        assert candidate["run"]["phase"] == "stopped"
        events.append("replace")
        return original_replace(source, target)
    monkeypatch.setattr(module.os, "fsync", record_fsync)
    monkeypatch.setattr(module.os, "replace", record_replace)
    assert store.stop("primary", RUN, "operator stop")["run"]["phase"] == "stopped"
    assert events == ["file", "replace", "directory"]
    _no_temporary_files(store)
