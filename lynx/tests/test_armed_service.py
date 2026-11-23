"""Combined runtime lifecycle checks without a model, camera or real gateway."""

from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import Mock

import pytest
import requests

from lynx import armed_service as module, picker
from failover_coordinator import (CoordinatorStore, RECOVERY_CAPABILITY, CONTINUOUS_CAPABILITY,
                                  DEVICE_SPEED_CAPABILITY)


class Server:
    def __init__(self, address, store, token):
        self.address, self.store, self.token = address, store, token
        self.started = threading.Event()
        self.finished = threading.Event()
        self.closed = False
        self.poll = None

    def serve_forever(self, poll_interval):
        self.poll = poll_interval
        self.started.set()
        self.finished.wait(3)

    def shutdown(self):
        self.finished.set()

    def server_close(self):
        self.closed = True


@pytest.fixture
def rig(tmp_path, monkeypatch):
    token = tmp_path / "coordinator-token"
    diagnostics = tmp_path / "diagnostics-token"
    token.write_text("synthetic-coordinator-private")
    diagnostics.write_text("synthetic-readonly-private")
    servers = []

    def server(*args):
        value = Server(*args)
        servers.append(value)
        return value

    monkeypatch.setattr(module, "CoordinatorServer", server)
    monkeypatch.setattr(module, "RETRY_DELAY", 0)
    monkeypatch.setattr(module, "output", Mock())
    http = Mock(side_effect=AssertionError("No gateway or camera traffic before a batch"))
    monkeypatch.setattr(requests, "request", http)
    args = ["--live", "--state-dir", str(tmp_path), "--model", "/offline/model",
            "--coordinator-token-file", str(token),
            "--coordinator-gateway-token-file", str(diagnostics)]
    return args, servers, http


def test_combined_startup_serves_coordinator_and_reuses_guarded_picker(rig, monkeypatch, tmp_path):
    args, servers, http = rig
    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}

    def run(command):
        assert servers[0].started.wait(1)
        native = picker.arguments(command)
        assert native.armed and native.live and not native.standby
        assert native.coordinator_url == "http://127.0.0.1:8092"
        assert native.state_dir == tmp_path
        assert native.model == Path("/offline/model")
        assert servers[0].store.state("tertiary") == {
            "run": None, "workers": {}, "head": None,
            "capabilities": [RECOVERY_CAPABILITY, CONTINUOUS_CAPABILITY, DEVICE_SPEED_CAPABILITY],
        }
        return 0

    monkeypatch.setattr(picker, "main", Mock(side_effect=run))
    assert module.main(args) == 0
    assert len(servers) == 1 and servers[0].closed
    assert servers[0].poll == 0.2
    assert servers[0].finished.is_set()
    assert all(signal.getsignal(s) is handler for s, handler in previous.items())
    http.assert_not_called()


def test_startup_failure_retries_same_picker_without_replacing_coordinator_state(rig, monkeypatch, tmp_path):
    args, servers, http = rig
    path = tmp_path / "existing-coordinator.json"
    old = CoordinatorStore(path, "http://offline.invalid", "synthetic-readonly")
    old.start("primary", "completed-batch", {"speed": 100})
    monkeypatch.setattr(old, "_get", lambda _: {"active": True, "client_id": "primary"})
    old.finish("primary", "completed-batch", "no_pickable_cups")
    before = path.read_bytes()
    calls = []

    def run(command):
        calls.append(command)
        assert servers[0].store.state("secondary")["run"]["phase"] == "complete"
        assert path.read_bytes() == before
        return 1 if len(calls) == 1 else 0

    monkeypatch.setattr(picker, "main", run)
    assert module.main(args + ["--coordinator-state-file", str(path)]) == 0
    assert len(calls) == 2 and calls[0] == calls[1]
    assert len(servers) == 1 and servers[0].closed
    assert path.read_bytes() == before
    http.assert_not_called()


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_shutdown_between_native_attempts_does_not_retry_or_stop_robot(rig, monkeypatch, signum):
    args, servers, http = rig

    def interrupted(_command):
        signal.getsignal(signum)(signum, None)
        raise AssertionError("Signal handler should interrupt the launcher")

    native = Mock(side_effect=interrupted)
    monkeypatch.setattr(picker, "main", native)
    assert module.main(args) == 0
    native.assert_called_once()
    assert servers[0].closed
    http.assert_not_called()


def test_picker_handled_shutdown_returns_zero_and_is_not_restarted(rig, monkeypatch):
    args, servers, http = rig
    native = Mock(return_value=0)
    monkeypatch.setattr(picker, "main", native)
    assert module.main(args) == 0
    native.assert_called_once()
    assert servers[0].closed
    http.assert_not_called()


@pytest.mark.parametrize("extra", [["--port", "0"], ["--port", "65536"], ["--depth-frames", "0"],
                                   ["--conf", "nan"], ["--speed", "0"], ["--inference-timeout", "-1"]])
def test_invalid_options_fail_before_listener_or_picker(rig, monkeypatch, extra):
    args, servers, _http = rig
    native = Mock()
    monkeypatch.setattr(picker, "main", native)
    with pytest.raises(SystemExit):
        module.main(args + extra)
    assert servers == []
    native.assert_not_called()


def test_explicit_live_required(rig, monkeypatch):
    args, servers, _http = rig
    with pytest.raises(SystemExit):
        module.main(args[1:])
    assert servers == []


def test_invalid_private_credentials_fail_closed_without_leaking(rig, monkeypatch, tmp_path):
    args, servers, http = rig
    token = tmp_path / "coordinator-token"
    token.unlink()
    token.symlink_to(tmp_path / "diagnostics-token")
    native = Mock()
    monkeypatch.setattr(picker, "main", native)
    assert module.main(args) == 1
    native.assert_not_called()
    assert servers == []
    assert "synthetic-readonly-private" not in str(module.output.call_args_list)
    http.assert_not_called()


def test_coordinator_listener_failure_disarms_only_local_process(rig, monkeypatch):
    args, servers, http = rig
    terminated = threading.Event()
    kill = Mock(side_effect=lambda *_: terminated.set())
    monkeypatch.setattr(module.os, "kill", kill)

    def run(_command):
        assert servers[0].started.wait(1)
        servers[0].finished.set()  # Unexpected listener return, not a shutdown.
        assert terminated.wait(1)
        return 0

    monkeypatch.setattr(picker, "main", run)
    assert module.main(args) == 1
    kill.assert_called_once_with(module.os.getpid(), signal.SIGTERM)
    assert servers[0].closed
    http.assert_not_called()


def test_listener_shutdown_is_bounded_even_if_shutdown_method_hangs(monkeypatch):
    server = Server(None, None, None)
    blocker = threading.Event()
    monkeypatch.setattr(module, "SHUTDOWN_TIMEOUT", 0.01)
    server.shutdown = lambda: blocker.wait(1)
    serving = threading.Thread(target=lambda: server.serve_forever(0.2), daemon=True)
    serving.start()
    assert server.started.wait(1)
    started = time.monotonic()
    try:
        module.close_server(server, serving)
        assert time.monotonic() - started < 0.5
        assert server.closed
    finally:
        blocker.set()
        server.finished.set()
        serving.join(1)


def test_module_import_keeps_native_runtime_free_of_heavy_dependencies():
    code = ("import sys; import lynx.armed_service; "
            "assert not any(n in sys.modules for n in "
            "('numpy','cv2','torch','ultralytics','fastapi','uvicorn')); print('ok')")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "ok"
