"""Gateway ownership monitoring tests: fake HTTP only, no robot or camera."""

import json
import secrets
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

import active_device_status as status_module
from active_device_status import ActiveDeviceMonitor, empty_status, monitor_from_options


@pytest.fixture
def credential():
    # An ephemeral test value, never an operator's real gateway credential.
    return secrets.token_urlsafe(24)


class Response:
    def __init__(self, value=None, status=200, raw=None):
        self.status_code = status
        self.raw = raw if raw is not None else json.dumps(value).encode()
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, size):
        for start in range(0, len(self.raw), size):
            yield self.raw[start:start + size]


@pytest.fixture
def gateway(monkeypatch):
    values = {"/control": {"active": True, "client_id": "primary", "priority": 300},
              "/robot/rws/queue": {"state": "idle", "owner": None}}
    calls = []

    def get(url, **options):
        path = url.removeprefix("http://offline.invalid/api/v1")
        calls.append((path, options))
        value = values[path]
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, Response) else Response(value)

    monkeypatch.setattr(status_module.requests, "get", get)
    for method in ("post", "put", "patch", "delete"):
        monkeypatch.setattr(status_module.requests, method,
                            Mock(side_effect=AssertionError("Monitoring must be read-only")))
    return SimpleNamespace(values=values, calls=calls)


def monitor(credential, **options):
    return ActiveDeviceMonitor("http://offline.invalid", credential, **options)


@pytest.mark.parametrize("owner,device,label", [
    ("primary", "cloud", "Cloud"), ("secondary", "pi", "Raspberry Pi"),
    ("tertiary", "lynx", "Lynx"), ("admin", None, "Maintenance"),
    ("operator", None, "Operator"), ("custom-client", None, "Other controller"),
])
def test_current_owner_roles_do_not_guess_a_machine(gateway, credential, owner, device, label):
    gateway.values["/control"]["client_id"] = owner
    instance = monitor(credential)
    assert instance.snapshot()["status"] == "connecting"
    assert gateway.calls == []
    instance._poll_once()
    result = instance.snapshot()
    assert (result["status"], result["device_id"], result["label"], result["client_id"]) == (
        "active", device, label, owner)
    assert result["updated_at"]
    assert [path for path, _ in gateway.calls] == ["/control", "/robot/rws/queue"]
    assert all(options["allow_redirects"] is False and options["stream"] is True
               and options["headers"]["Connection"] == "close"
               and options["timeout"] == (.5, 1.0) for _, options in gateway.calls)
    assert credential not in json.dumps(result)


def test_old_queue_and_current_lease_are_separate(gateway, credential):
    gateway.values["/control"]["client_id"] = "tertiary"
    gateway.values["/robot/rws/queue"] = {
        "state": "running", "owner": "primary", "steps": [{"body": "not public"}],
        "message": credential, "length": 18, "step": 12,
    }
    instance = monitor(credential)
    instance._poll_once()
    result = instance.snapshot()
    assert result["client_id"] == "tertiary"
    assert result["queue"] == {"state": "running", "owner": "primary", "label": "Cloud",
                               "fingerprint": result["queue"]["fingerprint"]}
    assert len(result["queue"]["fingerprint"]) == 64
    assert credential not in json.dumps(result)
    gateway.values["/control"] = {"active": False, "client_id": "primary"}
    instance._poll_once()
    result = instance.snapshot()
    assert result["status"] == "idle"
    assert result["client_id"] is result["device_id"] is result["label"] is None
    assert result["queue"]["owner"] == "primary"


@pytest.mark.parametrize("label", ["MacBook", "Windows PC"])
def test_opted_in_primary_label_covers_control_queue_and_idle_route(gateway, credential, label):
    instance = monitor(credential, primary_label=label)
    assert instance.snapshot()["names"]["cloud"] == label
    gateway.values["/robot/rws/queue"] = {"state": "running", "owner": "primary"}
    instance._poll_once()
    result = instance.snapshot()
    assert result["device_id"] == "cloud"  # Existing UI/device identity remains compatible.
    assert result["label"] == label and result["queue"]["label"] == label
    gateway.values["/control"] = {"active": False}
    instance._poll_once()
    result = instance.snapshot()
    assert result["label"] is None and result["names"]["cloud"] == label
    result["names"]["cloud"] = "mutated caller copy"
    assert instance.snapshot()["names"]["cloud"] == label


@pytest.mark.parametrize("system,label", [("Darwin", "MacBook"), ("Windows", "Windows PC")])
def test_only_explicit_live_local_failover_changes_primary_machine_label(
        gateway, credential, monkeypatch, tmp_path, system, label):
    monkeypatch.setenv("GATEWAY_TOKEN", credential)
    monkeypatch.setattr(status_module.platform, "system", lambda: system)
    config = tmp_path / "devices.json"
    local = {"backend": "local", "gateway": "http://offline.invalid", "state_dir": "unused",
             "failover": {"coordinator_url": "http://coordinator.invalid", "token_file": "private-file"}}
    config.write_text(json.dumps({"devices": {"mac": local}}))
    args = SimpleNamespace(device_config=config)
    instance = monitor_from_options(args, mode="live")
    assert instance.snapshot()["names"]["cloud"] == label
    assert "names" not in monitor_from_options(args, mode="dry_run").snapshot()
    del local["failover"]
    config.write_text(json.dumps({"devices": {"mac": local}}))
    assert "names" not in monitor_from_options(args, mode="live").snapshot()
    assert gateway.calls == []


@pytest.mark.parametrize("control", [
    {}, {"active": "true"}, {"active": True}, {"active": True, "client_id": None},
    {"active": True, "client_id": ["primary"]},
    {"active": True, "client_id": "<unsafe>"},
])
def test_malformed_control_never_becomes_an_owner(gateway, credential, control):
    gateway.values["/control"] = control
    instance = monitor(credential)
    instance._poll_once()
    assert instance.snapshot() == empty_status("unavailable", "Gateway status is unavailable.")
    assert len(gateway.calls) == 1


@pytest.mark.parametrize("queue", [
    {"state": "unexpected", "owner": "primary"},
    {"state": "running", "owner": None},
    {"state": "running", "owner": {"client_id": "primary"}},
])
def test_malformed_queue_does_not_infer_execution_owner(gateway, credential, queue):
    gateway.values["/robot/rws/queue"] = queue
    instance = monitor(credential)
    instance._poll_once()
    assert instance.snapshot()["status"] == "unavailable"


def test_queue_404_is_optional_but_other_errors_clear_status(gateway, credential):
    instance = monitor(credential)
    gateway.values["/robot/rws/queue"] = Response({}, status=404)
    instance._poll_once()
    assert instance.snapshot()["status"] == "active"
    assert instance.snapshot()["queue"] is None
    gateway.values["/robot/rws/queue"] = Response({}, status=503)
    instance._poll_once()
    assert instance.snapshot()["status"] == "unavailable"


def test_failed_queue_is_history_not_current_lease_owner(gateway, credential):
    gateway.values["/control"] = {"active": False}
    gateway.values["/robot/rws/queue"] = {"state": "failed", "owner": "secondary"}
    instance = monitor(credential)
    instance._poll_once()
    result = instance.snapshot()
    assert result["status"] == "idle"
    assert result["device_id"] is None
    assert result["queue"] == {"state": "failed", "owner": "secondary", "label": "Raspberry Pi",
                               "fingerprint": result["queue"]["fingerprint"]}


def test_snapshot_is_cached_isolated_and_never_returns_stale_owner(gateway, credential, monkeypatch):
    instance = monitor(credential)
    instance._poll_once()
    result = instance.snapshot()
    result["queue"]["owner"] = "changed"
    assert instance.snapshot()["queue"]["owner"] is None
    assert len(gateway.calls) == 2
    monkeypatch.setattr(status_module.time, "monotonic", lambda: instance._observed + 6)
    stale = instance.snapshot()
    assert stale["status"] == "unavailable"
    assert stale["client_id"] is stale["queue"] is None
    assert stale["updated_at"] == result["updated_at"]
    assert len(gateway.calls) == 2


def test_transport_exception_is_sanitized_and_recovery_updates_owner(gateway, credential):
    gateway.values["/control"] = requests.ConnectionError("private detail " + credential)
    instance = monitor(credential)
    instance._poll_once()
    assert instance.snapshot() == empty_status("unavailable", "Gateway status is unavailable.")
    gateway.values["/control"] = {"active": True, "client_id": "secondary"}
    instance._poll_once()
    assert instance.snapshot()["device_id"] == "pi"


def test_gateway_cannot_reflect_credential_as_an_owner(gateway, credential):
    gateway.values["/control"]["client_id"] = credential
    instance = monitor(credential)
    instance._poll_once()
    assert instance.snapshot()["status"] == "unavailable"
    assert credential not in json.dumps(instance.snapshot())


def test_windows_setx_credential_source_never_requires_picker_import(credential, monkeypatch):
    monkeypatch.delenv("GATEWAY_TOKEN", raising=False)
    registry = Mock()
    registry.OpenKey.return_value.__enter__ = Mock(return_value="test-key")
    registry.OpenKey.return_value.__exit__ = Mock(return_value=None)
    registry.QueryValueEx.return_value = (credential, 1)
    with patch.dict("sys.modules", {"winreg": registry}), patch.object(status_module.os, "name", "nt"):
        assert status_module._environment_token() == credential
    registry.QueryValueEx.assert_called_once_with("test-key", "GATEWAY_TOKEN")


@pytest.mark.parametrize("response", [
    Response({}, status=302), Response(raw=b"not json"), Response([]),
    Response(raw=b" " * (status_module.MAX_RESPONSE + 1)),
])
def test_redirect_invalid_or_oversized_response_is_closed(gateway, credential, response):
    gateway.values["/control"] = response
    instance = monitor(credential)
    instance._poll_once()
    assert instance.snapshot()["status"] == "unavailable"
    assert response.closed


def test_background_start_close_are_idempotent(gateway, credential):
    instance = monitor(credential)
    polled = threading.Event()
    original = instance._poll_once

    def poll():
        original()
        polled.set()

    instance._poll_once = poll
    instance.start()
    thread = instance._thread
    instance.start()
    assert polled.wait(timeout=2)
    assert instance._thread is thread
    instance.close()
    instance.close()
    instance.start()
    assert instance._thread is thread
    assert not thread.is_alive()
    assert instance.snapshot()["status"] == "unavailable"


def test_standalone_options_use_environment_without_network(gateway, credential, monkeypatch):
    monkeypatch.setenv("GATEWAY_TOKEN", credential)
    args = SimpleNamespace(gateway="http://offline.invalid", device_config=None)
    instance = monitor_from_options(args, mode="live")
    assert isinstance(instance, ActiveDeviceMonitor)
    assert instance._headers["Authorization"] == "Bearer " + credential
    assert gateway.calls == []
    assert monitor_from_options(args, mode="demo") is None
    monkeypatch.delenv("GATEWAY_TOKEN")
    monkeypatch.setattr(status_module, "_environment_token", lambda: None)
    assert monitor_from_options(args, mode="live") is None


def test_managed_local_options_use_gateway_file_but_prefer_environment(
        gateway, credential, monkeypatch, tmp_path):
    monkeypatch.delenv("GATEWAY_TOKEN", raising=False)
    state = tmp_path / "local-state"
    state.mkdir()
    (state / "gateway-token").write_text(credential)
    config = tmp_path / "devices.json"
    config.write_text(json.dumps({"devices": {"mac": {
        "backend": "local", "gateway": "http://offline.invalid", "state_dir": "local-state"}}}))
    args = SimpleNamespace(gateway=None, device_config=config)
    instance = monitor_from_options(args, mode="live")
    assert instance._headers["Authorization"] == "Bearer " + credential
    replacement = secrets.token_urlsafe(24)
    monkeypatch.setenv("GATEWAY_TOKEN", replacement)
    with patch("device_service.read_gateway_token", side_effect=AssertionError("Unneeded file read")):
        instance = monitor_from_options(args, mode="dry_run")
    assert instance._headers["Authorization"] == "Bearer " + replacement
    assert gateway.calls == []


def test_remote_service_tokens_are_never_used_as_gateway_tokens(gateway, credential, monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_TOKEN", credential)
    config = tmp_path / "devices.json"
    config.write_text(json.dumps({"devices": {"mac": {
        "url": "http://offline.invalid", "token_file": "do-not-read"}}}))
    args = SimpleNamespace(gateway=None, device_config=config)
    assert monitor_from_options(args, mode="live") is None
    assert gateway.calls == []


@pytest.mark.parametrize("contents", ["not json", "null", '{"devices":null}', '{"devices":{"mac":null}}'])
def test_unavailable_monitor_configuration_does_not_block_ui(contents, tmp_path):
    path = tmp_path / "devices.json"
    path.write_text(contents)
    assert monitor_from_options(SimpleNamespace(device_config=path), mode="live") is None


def test_endpoint_only_reads_cache_and_preserves_controller_state():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from web_app import create_app
    controller = Mock()
    controller.state.return_value = {"devices": {"active_id": "mac"}}
    monitor_instance = Mock()
    expected = empty_status("active", "Lynx currently holds robot control.")
    expected.update(device_id="lynx", label="Lynx", client_id="tertiary")
    monitor_instance.snapshot.return_value = expected
    app = create_app(controller, assistant=Mock(), device_monitor=monitor_instance)
    with TestClient(app, base_url="http://127.0.0.1:8080") as client:
        with patch("requests.get", side_effect=AssertionError("Endpoint must be cached")):
            result = client.get("/api/active_device")
        assert result.status_code == 200
        assert result.json() == expected
        assert client.get("/api/state").json()["devices"]["active_id"] == "mac"
        assert client.get("/api/active_device", headers={"Origin": "https://elsewhere.invalid"}).status_code == 403
    monitor_instance.start.assert_called_once()
    monitor_instance.close.assert_called_once()
    controller.start.assert_called_once()
    controller.close.assert_called_once()
    controller.submit.assert_not_called()


def test_endpoint_without_monitor_reports_unconfigured():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from web_app import create_app
    with TestClient(create_app(Mock(), assistant=Mock()), base_url="http://127.0.0.1:8080") as client:
        assert client.get("/api/active_device").json() == empty_status()


def test_controller_start_failure_also_closes_monitor():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from web_app import create_app
    controller, monitor_instance = Mock(), Mock()
    controller.start.side_effect = RuntimeError("fake startup failure")
    with pytest.raises(RuntimeError, match="fake startup failure"):
        with TestClient(create_app(controller, assistant=Mock(), device_monitor=monitor_instance)):
            pass
    monitor_instance.close.assert_called_once()


def test_main_wires_and_closes_monitor_when_server_startup_fails(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")
    from web_app import main
    controller, monitor_instance = Mock(), Mock()
    with patch("web_app.WebController", return_value=controller), \
            patch("web_app.hardware_factory", return_value=Mock()), \
            patch("web_app.create_app", return_value=Mock()) as create, \
            patch("active_device_status.monitor_from_options", return_value=monitor_instance) as factory, \
            patch("uvicorn.run", side_effect=RuntimeError("fake server failure")):
        with pytest.raises(RuntimeError, match="fake server failure"):
            main(["--gateway", "http://offline.invalid", "--output", str(tmp_path)])
    assert factory.call_args.kwargs["mode"] == "dry_run"
    assert create.call_args.kwargs["device_monitor"] is monitor_instance
    monitor_instance.close.assert_called_once()
    controller.close.assert_called_once()


def test_queue_fingerprint_binds_same_owner_replacement_without_exposing_jobs(gateway, credential):
    gateway.values["/control"] = {"active": False}
    queue = {"state": "cancelled", "owner": "primary", "started_at": "2026-11-23T10:00:00Z",
             "steps": [{"name": "approach", "state": "skipped", "message": credential}]}
    gateway.values["/robot/rws/queue"] = queue
    instance = monitor(credential)
    instance._poll_once()
    original = instance.snapshot()["queue"]
    assert len(original["fingerprint"]) == 64
    assert all(char in "0123456789abcdef" for char in original["fingerprint"])
    assert credential not in json.dumps(original)
    assert "approach" not in json.dumps(original)
    # JSON object key ordering and cached read times are not queue identity.
    gateway.values["/robot/rws/queue"] = dict(reversed(list(queue.items())))
    instance._poll_once()
    assert instance.snapshot()["queue"] == original
    assert instance.snapshot()["queue"] == original
    gateway.values["/robot/rws/queue"] = {**queue, "started_at": "2026-11-23T10:02:00Z"}
    instance._poll_once()
    replacement = instance.snapshot()["queue"]
    assert replacement["state"] == original["state"]
    assert replacement["owner"] == original["owner"]
    assert replacement["fingerprint"] != original["fingerprint"]
    # Changed completion evidence also invalidates an existing inspection.
    gateway.values["/robot/rws/queue"] = {**queue, "steps": [{"name": "approach", "state": "failed"}]}
    instance._poll_once()
    assert instance.snapshot()["queue"]["fingerprint"] not in {
        original["fingerprint"], replacement["fingerprint"]}
    assert all(path in {"/control", "/robot/rws/queue"} for path, _ in gateway.calls)
