"""ASGI composition and dashboard access checks; no native worker or hardware."""

import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pytest

from lynx import app as lynx_app
from web_app import create_app


GATEWAY_TOKEN = "private-gateway-test-token-" + "g" * 32
DASHBOARD_TOKEN = "private-dashboard-test-token-" + "d" * 32
BASE_ARGS = ["--state-dir", "/state", "--model", "/model", "--worker", "/worker"]
REMOTE_ORIGIN = "http://192.0.2.10:8080"


def basic(token=DASHBOARD_TOKEN):
    return "Basic " + base64.b64encode(("lynx:" + token).encode()).decode()


def fake_gateway(events=None):
    events = [] if events is None else events

    @asynccontextmanager
    async def lifespan(app):
        events.append("gateway started")
        try:
            yield
        finally:
            events.append("gateway closed")

    app = FastAPI(lifespan=lifespan)
    app.state.calls = []

    @app.api_route("/resource", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def resource(request: Request):
        auth = request.headers.get("authorization")
        if auth != "Bearer " + GATEWAY_TOKEN:
            raise HTTPException(401, "Gateway bearer token required")
        app.state.calls.append((request.method, auth))
        return {"component": "gateway", "method": request.method}

    @app.get("/health")
    def health():
        return {"component": "gateway", "status": "ok"}

    return app


def fake_dashboard(events=None, fail_startup=False):
    events = [] if events is None else events

    @asynccontextmanager
    async def lifespan(app):
        events.append("dashboard started")
        if fail_startup:
            raise RuntimeError("dashboard startup failed")
        try:
            yield
        finally:
            events.append("dashboard closed")

    app = FastAPI(lifespan=lifespan)

    @app.get("/api/state")
    def state():
        return {"component": "dashboard"}

    return app


def test_lifespans_start_gateway_first_and_close_it_last():
    events = []
    app = lynx_app.compose(fake_gateway(events), fake_dashboard(events))
    with TestClient(app) as client:
        assert events == ["gateway started", "dashboard started"]
        assert client.get("/api/v1/health").json()["component"] == "gateway"
        assert client.get("/api/state").json()["component"] == "dashboard"
    assert events == ["gateway started", "dashboard started", "dashboard closed", "gateway closed"]


def test_dashboard_startup_failure_still_closes_gateway():
    events = []
    app = lynx_app.compose(fake_gateway(events), fake_dashboard(events, fail_startup=True))
    with pytest.raises(RuntimeError, match="dashboard startup failed"):
        with TestClient(app):
            pass
    assert events == ["gateway started", "dashboard started", "gateway closed"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_dry_run_blocks_gateway_mutations_even_with_valid_bearer(method):
    gateway = fake_gateway()
    app = lynx_app.compose(gateway, fake_dashboard(), live=False)
    with TestClient(app) as client:
        response = client.request(method, "/api/v1/resource", headers={"Authorization": "Bearer " + GATEWAY_TOKEN})
    assert response.status_code == 403
    assert "dry-run" in response.json()["detail"]
    assert gateway.state.calls == []


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_dry_run_forwards_read_methods_to_gateway_bearer_checks(method):
    gateway = fake_gateway()
    app = lynx_app.compose(gateway, fake_dashboard(), dashboard_token=DASHBOARD_TOKEN)
    with TestClient(app) as client:
        assert client.request(method, "/api/v1/resource").status_code == 401
        assert client.request(method, "/api/v1/resource", headers={"Authorization": basic()}).status_code == 401
        assert client.request(method, "/api/v1/resource", headers={"Authorization": "Bearer " + GATEWAY_TOKEN}).status_code == 200
    assert gateway.state.calls == [(method, "Bearer " + GATEWAY_TOKEN)]


def test_live_gateway_preserves_its_authentication_and_mutation_dispatch():
    gateway = fake_gateway()
    app = lynx_app.compose(gateway, fake_dashboard(), live=True, dashboard_token=DASHBOARD_TOKEN)
    with TestClient(app) as client:
        assert client.post("/api/v1/resource", headers={"Authorization": basic()}).status_code == 401
        response = client.post("/api/v1/resource", headers={"Authorization": "Bearer " + GATEWAY_TOKEN})
    assert response.status_code == 200
    assert gateway.state.calls == [("POST", "Bearer " + GATEWAY_TOKEN)]


@pytest.mark.parametrize("authorization", [None, "Bearer " + GATEWAY_TOKEN, basic(GATEWAY_TOKEN), "Basic malformed"])
def test_remote_dashboard_requires_its_own_basic_password(authorization):
    app = lynx_app.compose(fake_gateway(), fake_dashboard(), dashboard_token=DASHBOARD_TOKEN)
    with TestClient(app) as client:
        headers = {} if authorization is None else {"Authorization": authorization}
        response = client.get("/api/state", headers=headers)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == 'Basic realm="Lynx picker"'
        assert response.headers["Cache-Control"] == "no-store"
        assert client.get("/api/state", headers={"Authorization": basic()}).status_code == 200
        assert client.get("/api/v1/health").status_code == 200


def test_gateway_like_prefix_does_not_bypass_dashboard_auth():
    app = lynx_app.compose(fake_gateway(), fake_dashboard(), dashboard_token=DASHBOARD_TOKEN)
    with TestClient(app) as client:
        assert client.get("/api/v1extra/api/state").status_code == 401
        assert client.post("/api/v1").status_code == 403


def test_unauthenticated_dashboard_websocket_is_refused_before_dispatch():
    messages, reached = [], []

    async def downstream(scope, receive, send):
        reached.append(True)

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        messages.append(message)

    gate = lynx_app.AccessGate(downstream, live=False, dashboard_token=DASHBOARD_TOKEN)
    asyncio.run(gate({"type": "websocket", "path": "/events", "headers": []}, receive, send))
    assert messages == [{"type": "websocket.close", "code": 1008}]
    assert not reached


class DashboardController:
    def __init__(self):
        self.calls = []
        self.started = self.closed = False

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def state(self):
        return {"csrf_token": "test-csrf-capability"}

    def submit(self, operation, payload):
        self.calls.append((operation, payload))
        return {"accepted": True}


@pytest.fixture
def remote_dashboard():
    controller = DashboardController()
    dashboard = create_app(controller, allowed_origin=REMOTE_ORIGIN)
    app = lynx_app.compose(fake_gateway(), dashboard, dashboard_token=DASHBOARD_TOKEN)
    with TestClient(app, base_url=REMOTE_ORIGIN, headers={"Authorization": basic()}) as client:
        assert controller.started
        yield client, controller
    assert controller.closed


def test_remote_origin_requires_both_basic_auth_and_csrf(remote_dashboard):
    client, controller = remote_dashboard
    state = client.get("/api/state")
    assert state.status_code == 200
    headers = {"Origin": REMOTE_ORIGIN, "X-CSRF-Token": state.json()["csrf_token"]}
    response = client.post("/api/scan", headers=headers, json={"request_id": "one"})
    assert response.status_code == 202
    assert controller.calls == [("scan", {"request_id": "one"})]
    for name in ("Content-Security-Policy", "X-Content-Type-Options", "Referrer-Policy"):
        assert response.headers[name]
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("headers", [
    {},
    {"Origin": REMOTE_ORIGIN},
    {"X-CSRF-Token": "test-csrf-capability"},
    {"Origin": REMOTE_ORIGIN, "X-CSRF-Token": "wrong"},
    {"Origin": "http://attacker.invalid", "X-CSRF-Token": "test-csrf-capability"},
    {"Origin": "http://127.0.0.1:8080", "X-CSRF-Token": "test-csrf-capability"},
    {"Origin": "null", "X-CSRF-Token": "test-csrf-capability"},
])
def test_remote_commands_cannot_bypass_origin_or_csrf(remote_dashboard, headers):
    client, controller = remote_dashboard
    assert client.post("/api/scan", headers=headers, json={}).status_code == 403
    assert controller.calls == []


def test_remote_basic_auth_does_not_disable_host_or_read_origin_checks(remote_dashboard):
    client, _ = remote_dashboard
    assert client.get("/api/state", headers={"Host": "attacker.invalid"}).status_code == 403
    assert client.get("/api/state", headers={"Origin": "http://attacker.invalid"}).status_code == 403
    assert client.get("/api/state", headers={"Host": "127.0.0.1:8080", "Origin": REMOTE_ORIGIN}).status_code == 403


@pytest.mark.parametrize("origin", [
    "file:///tmp", "http://host/path", "http://user:password@host", "http://host?query=1",
    "http://host#fragment", "https://", "not-an-origin",
])
def test_remote_origin_must_be_an_exact_http_origin(origin):
    with pytest.raises(ValueError, match="exact http"):
        create_app(DashboardController(), allowed_origin=origin)


def test_default_arguments_are_dry_run_and_low_resource():
    args = lynx_app.arguments(BASE_ARGS)
    assert not args.live and not args.mock
    assert args.host == "127.0.0.1"
    assert args.threads == 1
    assert args.camera_fps == 2.0
    assert (args.camera_width, args.camera_height) == (960, 540)
    assert args.inference_timeout <= 15
    assert args.model_headroom_mib == 48


def test_remote_bind_requires_explicit_dashboard_origin():
    with pytest.raises(SystemExit):
        lynx_app.arguments(BASE_ARGS + ["--host", "0.0.0.0"])
    args = lynx_app.arguments(BASE_ARGS + ["--host", "0.0.0.0", "--dashboard-origin", REMOTE_ORIGIN])
    assert args.host == "0.0.0.0"


def test_localhost_binding_is_normalized_to_internal_ipv4_address():
    assert lynx_app.arguments(BASE_ARGS + ["--host", "localhost"]).host == "127.0.0.1"


@pytest.mark.parametrize("host", ["::1", "192.0.2.10", "lynx.example"])
def test_bind_must_also_serve_internal_ipv4_client(host):
    with pytest.raises(SystemExit):
        lynx_app.arguments(BASE_ARGS + ["--host", host, "--dashboard-origin", REMOTE_ORIGIN])


@pytest.mark.parametrize("extra", [
    ["--live", "--mock"], ["--port", "80"], ["--port", "65536"],
    ["--threads", "0"], ["--threads", "5"], ["--conf", "0"], ["--conf", "1"], ["--conf", "1.1"],
    ["--conf", "nan"], ["--inference-timeout", "0"], ["--inference-timeout", "16"],
    ["--inference-timeout", "inf"], ["--camera-fps", ".1"], ["--camera-fps", "5"],
    ["--camera-width", "640", "--camera-height", "480"],
    ["--capture-budget-mib", "31"], ["--capture-budget-mib", "4097"],
    ["--grid-table-z", "nan"], ["--grid-cell-size", "19"], ["--grid-cell-size", "201"],
    ["--model-headroom-mib", "31"], ["--model-headroom-mib", "129"],
])
def test_invalid_arguments_fail_before_hardware_initialization(extra):
    with pytest.raises(SystemExit):
        lynx_app.arguments(BASE_ARGS + extra)


@pytest.fixture
def configured_state(tmp_path, monkeypatch):
    # configure intentionally sets process-level defaults; isolate those mutations.
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.delitem(sys.modules, "gateway.main", raising=False)
    from gateway.config import Settings, get_settings
    monkeypatch.setattr(Settings, "model_config", dict(Settings.model_config))
    get_settings.cache_clear()
    monkeypatch.setattr(lynx_app.shutil, "disk_usage", lambda path: SimpleNamespace(free=1024**3))
    monkeypatch.setattr(os.path, "ismount", lambda path: True)
    registry = {"coordinator": {
        "token_hash": hashlib.sha256(GATEWAY_TOKEN.encode()).hexdigest(),
        "permissions": ["robot.read", "robot.control", "camera.read"],
        "control_priority": 50,
    }}
    for name in ("calibration.json", "placing-grid.json"):
        (tmp_path / name).write_text("{}")
    (tmp_path / "auth_clients.json").write_text(json.dumps(registry))
    (tmp_path / "gateway-token").write_text(GATEWAY_TOKEN)
    (tmp_path / "dashboard-token").write_text(DASHBOARD_TOKEN)
    args = lynx_app.arguments(["--state-dir", str(tmp_path), "--model", "/model", "--worker", "/worker", "--mock"])
    try:
        yield args, tmp_path, registry
    finally:
        get_settings.cache_clear()


def test_configuration_uses_separate_tokens_and_mock_loopback_only(configured_state):
    args, directory, _ = configured_state
    state, token, dashboard = lynx_app.configure(args)
    assert state == directory.resolve()
    assert token == GATEWAY_TOKEN and dashboard == DASHBOARD_TOKEN
    assert token != dashboard
    assert os.environ["GATEWAY_SECURITY_MODE"] == "strict"
    assert os.environ["GATEWAY_CONTROL_REQUIRE_SESSION"] == "true"
    assert os.environ["GATEWAY_API_PREFIX"] == ""
    assert os.environ["GATEWAY_ROBOT_HOST"] == "127.0.0.1"
    assert os.environ["GATEWAY_CAMERA_PROVIDER"] == "mock"
    assert os.environ["GATEWAY_ROBOT_PROVIDER"] == "mock"


@pytest.mark.parametrize("password", [GATEWAY_TOKEN, "short"])
def test_configuration_refuses_shared_or_short_dashboard_password(configured_state, password):
    args, directory, _ = configured_state
    (directory / "dashboard-token").write_text(password)
    with pytest.raises(ValueError, match="distinct dashboard token"):
        lynx_app.configure(args)


def test_configuration_rejects_coordinator_without_control_permission(configured_state):
    args, directory, registry = configured_state
    registry["coordinator"]["permissions"].remove("robot.control")
    (directory / "auth_clients.json").write_text(json.dumps(registry))
    with pytest.raises(ValueError, match="required registry permissions"):
        lynx_app.configure(args)


def test_live_configuration_requires_explicit_robot_address(configured_state):
    args, _, _ = configured_state
    args.mock, args.live = False, True
    os.environ.pop("GATEWAY_ROBOT_HOST", None)
    with pytest.raises(ValueError, match="GATEWAY_ROBOT_HOST"):
        lynx_app.configure(args)


@pytest.mark.parametrize("failure_stage", ["attach", "intrinsics", "storage"])
def test_factory_releases_native_model_if_later_initialization_fails(tmp_path, monkeypatch, failure_stage):
    import capture_and_detect
    import placing_grid
    from lynx import camera, inference

    resources, workers = [], []

    class Worker:
        def __init__(self, *args, **kwargs):
            self.closed = False
            workers.append(self)

        def close(self):
            self.closed = True

    class Camera:
        def __init__(self, *args, **kwargs):
            pass

        def intrinsics(self):
            if failure_stage == "intrinsics":
                raise ValueError("fake intrinsics failure")
            return {}

    class Store:
        def __init__(self, *args, **kwargs):
            raise OSError("fake storage failure")

    def attach(resource):
        if failure_stage == "attach":
            raise RuntimeError("fake attach failure")
        resources.append(resource)

    monkeypatch.setattr(camera, "LocalCameraClient", Camera)
    monkeypatch.setattr(inference, "NativeWorker", Worker)
    monkeypatch.setattr(capture_and_detect, "Calibration", lambda path: object())
    monkeypatch.setattr(capture_and_detect, "CaptureStore", Store)
    monkeypatch.setattr(placing_grid, "PlacingGrid", lambda path: SimpleNamespace(snapshot=lambda: {}))
    controller = SimpleNamespace(event=lambda message: None, attach_resource=attach)
    factory = lynx_app.hardware_factory(lynx_app.arguments(BASE_ARGS + ["--mock"]), tmp_path, GATEWAY_TOKEN,
                                       object(), object(), lambda: object())

    with pytest.raises((ValueError, OSError, RuntimeError), match="fake .* failure"):
        factory(controller)

    assert len(workers) == 1 and workers[0].closed


@pytest.fixture
def memory_factory(tmp_path, monkeypatch):
    import capture_and_detect
    import placing_grid
    from lynx import camera, inference, resources

    calls = []

    def worker(*args, **kwargs):
        calls.append("worker")
        raise RuntimeError("fake native worker boundary")

    def trim():
        calls.append("trim")
        return "Heap trim unavailable; checking memory without it."

    monkeypatch.setattr(camera, "LocalCameraClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(inference, "NativeWorker", worker)
    monkeypatch.setattr(resources, "trim_unused_heap", trim)
    monkeypatch.setattr(capture_and_detect, "Calibration", lambda path: object())
    monkeypatch.setattr(placing_grid, "PlacingGrid", lambda path: SimpleNamespace(snapshot=lambda: {}))

    def factory(extra=()):
        return lynx_app.hardware_factory(lynx_app.arguments(BASE_ARGS + list(extra)), tmp_path, GATEWAY_TOKEN,
                                         object(), object(), lambda: object())

    return factory, calls


@pytest.mark.parametrize("issue", ["shared", "host", "unknown"])
def test_memory_guard_refuses_worker_and_surfaces_dashboard_error(memory_factory, monkeypatch, issue):
    from lynx import resources
    from web_controller import WebController

    factory, calls = memory_factory
    mib = resources.MIB
    leaf = resources.CgroupMemory(Path("/sys/fs/cgroup/apps/picker"), None, 100*mib, "unlimited")
    parent = resources.CgroupMemory(Path("/sys/fs/cgroup/apps"), 256*mib,
                                   (240 if issue == "shared" else 192)*mib, "finite")
    snapshot = resources.MemorySnapshot((leaf, parent), None if issue == "unknown" else
                                        (32 if issue == "host" else 200)*mib, False, ())
    monkeypatch.setattr(resources, "memory_snapshot", lambda proc: snapshot)
    monkeypatch.setattr(lynx_app.sys, "platform", "linux")
    controller = WebController(factory(), mode="dry_run", llm_enabled=False)
    try:
        controller._initialize()
        state = controller.state()
        assert state["ready"] is False and state["busy"] is False
        assert "Native model was not started" in state["error"]
        assert ("could not be verified" if issue == "unknown" else "48 MiB") in state["error"]
        assert calls == ["trim"]
    finally:
        controller.close()


@pytest.mark.parametrize("platform,extra", [("linux", ["--mock"]), ("darwin", [])])
def test_mock_and_nonlinux_skip_trim_and_guard(memory_factory, monkeypatch, platform, extra):
    from lynx import resources

    factory, calls = memory_factory
    monkeypatch.setattr(lynx_app.sys, "platform", platform)
    monkeypatch.setattr(resources, "require_model_headroom", lambda required: pytest.fail("guard should be skipped"))
    controller = SimpleNamespace(event=lambda message: None)
    with pytest.raises(RuntimeError, match="fake native worker boundary"):
        factory(extra)(controller)
    assert calls == ["worker"]


def test_real_linux_checks_selected_reserve_after_trim_before_worker(memory_factory, monkeypatch):
    from lynx import resources

    factory, calls = memory_factory
    events = []

    def guard(required):
        calls.append(("guard", required))
        assert calls == ["trim", ("guard", 64)]
        return SimpleNamespace(summary=lambda mib: "Memory snapshot checked")

    monkeypatch.setattr(lynx_app.sys, "platform", "linux")
    monkeypatch.setattr(resources, "require_model_headroom", guard)
    controller = SimpleNamespace(event=events.append)
    with pytest.raises(RuntimeError, match="fake native worker boundary"):
        factory(["--model-headroom-mib", "64"])(controller)
    assert calls == ["trim", ("guard", 64), "worker"]
    assert events[0].startswith("Heap trim unavailable")
    assert events[1] == "Memory snapshot checked"
