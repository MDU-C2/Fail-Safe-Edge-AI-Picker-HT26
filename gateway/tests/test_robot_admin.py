"""Program reset/collision controls tested entirely with simulated RWS calls."""
from contextlib import contextmanager
import asyncio
import threading
import time

import pytest
from pydantic import ValidationError

from gateway.models.rws import RapidResetRequest
from gateway.rws.client import RwsClient, RwsError
from gateway.rws.motion import RwsMotion, MotionConflict
from gateway.rws import rapid_router
from gateway.control.manager import ControlManager
from gateway.tests.support import Gateway, expect


class Controller(RwsClient):
    def __init__(self):
        super().__init__("offline.invalid", 80, "user", "password", 0.1)
        self.execution = "running"
        self.mode = "AUTO"
        self.motors = "motoron"
        self.collision = "INIT"
        self.calls = []
        self.reads = []
        self.symbols = {f"{name}_{arm}": value for arm in ("left", "right") for name, value in
                        {"move_request": "TRUE", "go_home": "TRUE", "use_rot": "TRUE",
                         "move_ok": "TRUE", "grip_cmd": '"GRIP"'}.items()}
        self.bad_read = None
        self.write_hook = None
        self.start_hook = None
        self.ack_denied = False
        self.normal_running_reads = 0

    def post(self, path, params=None, data=None):
        action = (params or {}).get("action")
        self.calls.append((path, action, data))
        if action == "stop":
            self.execution = "stopped"
        if action == "start":
            if self.start_hook:
                self.start_hook()
            self.execution = "running"
        if action == "acknowledge":
            if self.ack_denied:
                raise RwsError(403, "Local Client privileges required")
            self.collision = "CONFIRMED"

    def get_state(self, path, *args, **kwargs):
        if path == "/rw/panel/coldetstate":
            return [{"coldetstate ": self.collision}]
        raise AssertionError(path)

    def execution_state(self):
        return self.execution

    def operation_mode(self):
        return self.mode

    def controller_state(self):
        return self.motors

    def tasks(self):
        active = self.normal_running_reads > 0
        self.normal_running_reads -= 1
        return [{"type": "norm", "excstate": "star" if active else "stop"},
                {"type": "semistatic", "excstate": "star"}]

    def read_symbol(self, task, name):
        self.reads.append((task, name))
        if name not in self.symbols:
            raise RwsError(400, "Missing symbol")
        return "TRUE" if name == self.bad_read else self.symbols[name]

    def write_symbol(self, task, name, value):
        self.calls.append((task, name, value))
        self.symbols[name] = value
        if self.write_hook:
            self.write_hook()

    @contextmanager
    def mastership(self):
        yield


def motion(controller):
    return RwsMotion(controller, 0.1, (-1000, -1000, -1000), (1000, 1000, 1000))


def test_reset_clears_and_verifies_both_arms_then_leaves_stopped():
    c = Controller()
    motion(c).reset_program()
    assert c.execution == "stopped"
    assert all(value in {"FALSE", '""'} for value in c.symbols.values())
    assert {task for task, _ in c.reads} == {"T_ROB_L", "T_ROB_R"}
    assert len(c.reads) == 20
    assert any(action == "resetpp" for _, action, _ in c.calls)
    assert not any(action == "start" for _, action, _ in c.calls)


def test_reset_readback_failure_does_not_reset_pointer_or_start():
    c = Controller()
    c.bad_read = "move_request_right"
    with pytest.raises(MotionConflict, match="could not verify"):
        motion(c).reset_program(True)
    assert c.execution == "stopped"
    assert not any(action in {"resetpp", "start"} for _, action, _ in c.calls)


def test_missing_program_prevents_all_variable_writes():
    c = Controller()
    del c.symbols["grip_cmd_right"]
    with pytest.raises(MotionConflict, match="Missing"):
        motion(c).reset_program(True)
    assert not any(path.startswith("T_ROB_") for path, _, _ in c.calls)


def test_reset_rejects_active_gateway_job():
    c = Controller()
    m = motion(c)
    m._busy["right"].acquire()
    try:
        with pytest.raises(MotionConflict, match="unwind"):
            m.reset_program()
        assert not m._busy["left"].locked()
    finally:
        m._busy["right"].release()


def test_reset_excludes_new_command_staging():
    c = Controller()
    m = motion(c)

    def attempt():
        with pytest.raises(MotionConflict, match="already in progress"):
            m.home("left")

    c.write_hook = attempt
    m.reset_program()


def test_start_needs_auto_motors_and_cleared_collision():
    for field, value in (("mode", "MANR"), ("motors", "motoroff"), ("collision", "TRIGGERED")):
        c = Controller()
        setattr(c, field, value)
        with pytest.raises(RwsError):
            motion(c).reset_program(True)
        assert not any(action == "start" for _, action, _ in c.calls)


def test_fresh_start_explicit_and_after_reset():
    c = Controller()
    motion(c).reset_program(True)
    actions = [action for _, action, _ in c.calls]
    assert actions.index("resetpp") < actions.index("start")
    assert c.execution == "running"


def test_stop_generation_prevents_delayed_initialization():
    c = Controller()
    generation = c.stop_generation()
    c.stop()
    c.calls.clear()
    with pytest.raises(RwsError, match="software stop"):
        motion(c).reset_program(True, generation)
    assert c.calls == []


def test_stop_during_reset_prevents_late_start():
    c = Controller()
    c.write_hook = c.stop
    with pytest.raises(RwsError, match="software stop"):
        motion(c).reset_program(True)
    assert c.execution == "stopped"
    assert not any(action == "start" for _, action, _ in c.calls)


def test_lease_is_rechecked_after_slow_start_preconditions():
    c = Controller()
    authorized = True

    def collision():
        nonlocal authorized
        authorized = False
        return "INIT"

    def check():
        if not authorized:
            raise RwsError(409, "Lease lost")

    c.collision_state = collision
    with pytest.raises(RwsError, match="Lease lost"):
        motion(c).reset_program(True, check_authorization=check)
    assert not any(action == "start" for _, action, _ in c.calls)


def test_same_client_reacquiring_lease_does_not_authorize_old_reset(monkeypatch):
    manager = ControlManager(5)
    asyncio.run(manager.acquire("operator", 100))
    monkeypatch.setattr(rapid_router, "get_control_manager", lambda: manager)
    identity = type("Operator", (), {"client_id": "operator"})()
    check = rapid_router.lease_guard(identity)
    manager.renew("operator")
    check()
    manager.release("operator")
    asyncio.run(manager.acquire("operator", 100))
    with pytest.raises(RwsError, match="lease expired or changed"):
        check()


def test_stop_generation_changes_across_gateway_processes(monkeypatch):
    values = iter((123456, 7891011))
    monkeypatch.setattr("gateway.rws.client.secrets.randbits", lambda bits: next(values))
    old, new = Controller(), Controller()
    with pytest.raises(RwsError, match="software stop"):
        motion(new).reset_program(True, old.stop_generation())
    assert new.calls == []


def test_stop_after_start_dispatch_is_last_controller_command():
    c = Controller()
    entered, release = threading.Event(), threading.Event()
    c.start_hook = lambda: (entered.set(), release.wait(2))
    errors = []

    def reset():
        try:
            motion(c).reset_program(True)
        except RwsError as error:
            errors.append(error)

    worker = threading.Thread(target=reset)
    worker.start()
    assert entered.wait(1)
    stopper = threading.Thread(target=c.stop)
    stopper.start()
    release.set()
    worker.join(2)
    stopper.join(2)
    assert not worker.is_alive() and not stopper.is_alive()
    assert c.execution == "stopped"
    assert c.calls[-1][1] == "stop"
    assert errors


def test_stop_while_fresh_start_waits_for_http_lock_prevents_dispatch():
    c = Controller()
    c.execution = "stopped"
    generation = c.stop_generation()
    ready = threading.Event()
    c.collision_state = lambda: (ready.set(), "INIT")[1]
    errors = []

    def initialize():
        try:
            c.start_fresh(generation, time.monotonic() + 30)
        except RwsError as error:
            errors.append(error)

    c._lock.acquire()
    worker = threading.Thread(target=initialize)
    stopper = threading.Thread(target=c.stop)
    try:
        worker.start()
        assert ready.wait(1)
        stopper.start()
        deadline = time.monotonic() + 1
        while c.stop_generation() == generation and time.monotonic() < deadline:
            time.sleep(0.001)
        assert c.stop_generation() != generation
    finally:
        c._lock.release()
    worker.join(2)
    stopper.join(2)
    assert errors
    assert not worker.is_alive() and not stopper.is_alive()
    assert not any(action == "start" for _, action, _ in c.calls)


def test_stop_during_legacy_resetpp_prevents_late_start():
    c = Controller()
    original_post = c.post

    def post(path, params=None, data=None):
        original_post(path, params, data)
        if (params or {}).get("action") == "resetpp":
            # Announce Stop while resetpp is in flight, before it can acquire
            # the start/stop dispatch lock to send its own HTTP request.
            with c._stop_generation_lock:
                c._stop_generation += 1

    c.post = post
    with pytest.raises(RwsError, match="software stop"):
        c.start(True)
    assert not any(action == "start" for _, action, _ in c.calls)


def test_waits_for_normal_tasks_and_ignores_background_tasks():
    c = Controller()
    c.normal_running_reads = 2
    motion(c).reset_program()
    assert c.normal_running_reads < 0


def test_collision_acknowledgement_does_not_start_or_enable_motors():
    c = Controller()
    c.execution = "stopped"
    c.collision = "TRIGGERED"
    assert c.acknowledge_collision() == "CONFIRMED"
    assert c.calls == [("/rw/panel/opmode", "acknowledge", {"opmode": "coldet"})]


def test_collision_acknowledgement_rejection_has_no_fallback():
    c = Controller()
    c.execution = "stopped"
    c.collision = "TRIGGERED"
    c.ack_denied = True
    with pytest.raises(RwsError, match="Local Client"):
        c.acknowledge_collision()
    assert len(c.calls) == 1


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_start_requires_strict_boolean(value):
    with pytest.raises(ValidationError):
        RapidResetRequest(start=value)


def test_start_requires_cancellation_token():
    with pytest.raises(ValidationError):
        RapidResetRequest(start=True)


@pytest.mark.parametrize("path", ["/robot/rapid/reset", "/robot/rapid/collision/ack"])
def test_new_controls_require_permission_and_lease(gateway: Gateway, path):
    expect(gateway.client("diagnostics").post(path), 403)
    expect(gateway.client("operator").post(path), 409)
    operator = gateway.client("operator")
    with operator.control():
        expect(operator.post(path), 503)


def test_collision_read_is_read_only_and_unreachable_gives_503(gateway: Gateway):
    expect(gateway.client("diagnostics").get("/robot/rapid/collision"), 503)
