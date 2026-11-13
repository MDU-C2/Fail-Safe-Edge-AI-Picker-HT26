"""Session fences and watchdog behavior. Fake RWS only; no sockets or robot."""
import asyncio
from datetime import timedelta
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.control.manager import ControlConflict, ControlManager
from gateway.control import router as control
from gateway.robot import router as robot
from gateway.rws import motion_router, rapid_router
from gateway.rws.client import RwsError
from gateway.security.authentication import get_identity
from gateway.security.identity import Identity
from gateway.tests.test_robot_admin import Controller, motion


def acquire(manager, name="operator", priority=100):
    return asyncio.run(manager.acquire(name, priority)).lease


def until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "Watchdog condition was not reached"
        time.sleep(0.005)


@pytest.fixture
def fenced(monkeypatch):
    manager = ControlManager(5, require_session=True)
    controller = Controller()
    controller.program_modules = lambda task: []
    original_tasks = controller.tasks
    controller.tasks = lambda: [dict(task, name=f"T_ROB_{index}", taskstate="normal")
                               for index, task in enumerate(original_tasks())]
    mover = motion(controller)
    monkeypatch.setattr(control, "control_manager", manager)
    monkeypatch.setattr(rapid_router, "rws_client", controller)
    monkeypatch.setattr(rapid_router, "rws_motion", mover)
    monkeypatch.setattr(motion_router, "rws_motion", mover)
    app = FastAPI()
    app.dependency_overrides[get_identity] = lambda: Identity("operator", True, frozenset({"robot.read", "robot.control"}), 100)
    for router in (control.router, motion_router.router, rapid_router.router, robot.router):
        app.include_router(router)
    with TestClient(app) as client:
        yield manager, controller, mover, client
    manager.close()


def test_optional_mode_still_rejects_an_explicit_stale_session():
    manager = ControlManager(5)
    lease = acquire(manager)
    assert manager.renew("operator")[0]
    assert not manager.renew("operator", "stale")[0]
    assert not manager.release("operator", "stale")[0]
    assert not manager.owns_control("operator", "stale")
    assert manager.owns_control("operator", lease.session_id)


def test_required_sessions_fence_same_token_after_reacquisition(fenced):
    manager, _, _, client = fenced
    first = client.post("/control/acquire").json()["lease"]
    session = first["session_id"]
    assert len(session) == 32
    assert first["session_required"] and first["recovery_required"]
    assert client.post("/control/renew").status_code == 409
    assert client.post("/control/release").status_code == 409
    headers = {"X-Control-Session": session}
    renewal = client.post("/control/renew", headers=headers).json()["lease"]
    assert renewal["session_id"] == session
    assert client.post("/control/release", headers=headers).status_code == 200
    second = client.post("/control/acquire").json()["lease"]["session_id"]
    assert second != session
    for endpoint in ("/control/renew", "/control/release", "/robot/rapid/stop"):
        assert client.post(endpoint, headers=headers).status_code == 409
    assert client.post("/control/renew", headers={"X-Control-Session": second}).status_code == 200


def test_utc_clock_changes_do_not_change_monotonic_expiry(monkeypatch):
    manager = ControlManager(60)
    lease = acquire(manager)
    monkeypatch.setattr(manager, "_now", lambda: lease.expires_at + timedelta(days=5))
    assert manager.get().session_id == lease.session_id
    monkeypatch.setattr(manager, "_now", lambda: lease.acquired_at - timedelta(days=5))
    with manager._lock:
        from dataclasses import replace
        manager._lease = replace(manager._lease, deadline=time.monotonic() - 1)
    assert manager.get() is None


def test_watchdog_expires_without_any_api_read_and_confirms_stop():
    manager, controller = ControlManager(0.06, require_session=True), Controller()
    generation = controller.stop_generation()
    stopped = threading.Event()
    def stop():
        controller.stop_and_verify()
        stopped.set()
    manager.start_watchdog(controller.invalidate_pending_dispatch, stop)
    try:
        acquire(manager)
        assert stopped.wait(2), "No API request is needed to notice expiry"
        until(lambda: not manager._stop_in_progress)
        assert controller.execution == "stopped"
        assert controller.stop_generation() != generation
        assert manager.recovery_required
        assert "confirmed" in manager.recovery_reason
        assert manager.capabilities()["watchdog_enabled"]
        assert not any(action in ("start", "resetpp") for _, action, _ in controller.calls)
    finally:
        manager.close()


def test_watchdog_stop_failure_stays_latched_and_does_not_claim_stop():
    manager = ControlManager(0.05, require_session=True)
    attempted = threading.Event()
    def fail():
        attempted.set()
        raise RwsError(None, "Offline fake controller")
    manager.start_watchdog(lambda: None, fail)
    try:
        acquire(manager)
        assert attempted.wait(2)
        until(lambda: "UNCONFIRMED" in manager.recovery_reason)
        assert manager.recovery_required
    finally:
        manager.close()


def test_clean_release_does_not_force_a_reset_after_every_completed_cup():
    manager = ControlManager(5, require_session=True)
    lease = acquire(manager)
    authorization = manager.authorize("operator", lease.session_id)
    manager.clear_recovery(authorization)
    assert manager.release("operator", lease.session_id)[0]
    assert not manager.recovery_required
    next_lease = acquire(manager)
    manager.authorize("operator", next_lease.session_id, require_clear=True)()


def test_release_during_action_latches_recovery_and_invalidates_dispatch():
    manager = ControlManager(5, require_session=True)
    lease = acquire(manager)
    authorization = manager.authorize("operator", lease.session_id)
    manager.clear_recovery(authorization)
    with authorization.operation():
        assert manager.release("operator", lease.session_id)[0]
        with pytest.raises(ControlConflict):
            authorization()
    assert manager.recovery_required
    assert "outstanding robot action" in manager.recovery_reason


def test_startup_latch_blocks_motion_and_start_but_allows_verified_reset(fenced):
    manager, controller, _, client = fenced
    session = client.post("/control/acquire").json()["lease"]["session_id"]
    headers = {"X-Control-Session": session}
    for endpoint, payload in (("/robot/rws/home", {}), ("/robot/rws/gripper", {"action": "open"}),
                              ("/robot/rws/move", {"x": 400, "y": 0, "z": 400}), ("/robot/rapid/start", {})):
        assert client.post(endpoint, json=payload, headers=headers).status_code == 409
    assert controller.calls == []
    assert client.post("/robot/rapid/stop", headers=headers).status_code == 200
    assert client.post("/robot/rapid/reset", json={"start": False}, headers=headers).status_code == 200
    assert not manager.recovery_required
    assert controller.execution == "stopped"
    assert not any(action == "start" for _, action, _ in controller.calls)


def test_failed_reset_cannot_clear_gateway_recovery(fenced):
    manager, controller, _, client = fenced
    session = client.post("/control/acquire").json()["lease"]["session_id"]
    controller.bad_read = "move_request_right"
    assert client.post("/robot/rapid/reset", json={"start": False}, headers={"X-Control-Session": session}).status_code == 409
    assert manager.recovery_required


def test_fenced_mode_refuses_untracked_socket_and_background_jobs(fenced):
    manager, controller, _, client = fenced
    session = client.post("/control/acquire").json()["lease"]["session_id"]
    headers = {"X-Control-Session": session}
    assert client.post("/robot/rws/home", json={"wait": False}, headers=headers).status_code == 409
    assert client.post("/robot/command", json={"command": "HOME"}, headers=headers).status_code == 409
    assert client.put("/robot/rapid/tasks/T_ROB_L/modules/Test", json={"source": "MODULE Test\nENDMODULE\n"}, headers=headers).status_code == 409
    assert client.delete("/robot/rapid/tasks/T_ROB_L/modules/Test", headers=headers).status_code == 409
    assert not controller.calls


@pytest.mark.parametrize("action, options", [
    ("grip", {}),
    ("grip", {"force": 4.5}),
    ("move", {"position": 12.5}),
])
def test_release_while_staging_never_posts_move_request(fenced, action, options):
    manager, controller, mover, _ = fenced
    lease = acquire(manager)
    authorization = manager.authorize("operator", lease.session_id)
    manager.clear_recovery(authorization)
    controller.symbols["move_request_left"] = "FALSE"
    controller.symbols.update(grip_pos_left="25", grip_force_left="20")
    controller.write_hook = lambda: manager.release("operator", lease.session_id)
    with authorization.operation(), pytest.raises(ControlConflict):
        mover.gripper("left", action, authorization=authorization, **options)
    assert not any(name == "move_request_left" and value == "TRUE" for _, name, value in controller.calls)
    assert manager.recovery_required


@pytest.mark.parametrize("payload, symbol, expected", [
    ({"action": "move", "position": 12.5}, "grip_pos_left", "12.500000"),
    ({"action": "grip", "force": 4.5}, "grip_force_left", "4.500000"),
    ({"action": "grip"}, "grip_force_left", "20.000000"),
])
def test_fenced_gripper_route_preserves_position_and_force(fenced, payload, symbol, expected):
    manager, controller, _, client = fenced
    session = client.post("/control/acquire").json()["lease"]["session_id"]
    manager.clear_recovery(manager.authorize("operator", session))
    controller.symbols.update(move_request_left="FALSE", grip_pos_left="25", grip_force_left="5")

    def complete_job():
        if controller.calls[-1][1:] == ("move_request_left", "TRUE"):
            controller.symbols["move_request_left"] = "FALSE"

    controller.write_hook = complete_job
    headers = {"X-Control-Session": session}
    response = client.post("/robot/rws/gripper", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": True, "message": "DONE"}
    assert controller.symbols[symbol] == expected
    assert controller.calls[-1][1:] == ("move_request_left", "TRUE")

    assert manager.release("operator", session)[0]
    acquire(manager)
    before = list(controller.calls)
    assert client.post("/robot/rws/gripper", json=payload, headers=headers).status_code == 409
    assert controller.calls == before


def test_final_dispatch_serializes_with_preemption_and_is_followed_by_stop(fenced):
    manager, controller, mover, _ = fenced
    initial = acquire(manager, "autonomy", 50)
    authorization = manager.authorize("autonomy", initial.session_id)
    manager.clear_recovery(authorization)
    controller.symbols["move_request_left"] = "FALSE"
    dispatched, finish_dispatch, preempted, stopped = (threading.Event() for _ in range(4))
    original_write = controller.write_symbol
    def write(task, name, value):
        original_write(task, name, value)
        if name == "move_request_left" and value == "TRUE":
            dispatched.set()
            assert finish_dispatch.wait(2)
    controller.write_symbol = write
    def stop():
        controller.stop_and_verify()
        stopped.set()
    manager.start_watchdog(controller.invalidate_pending_dispatch, stop)
    errors = []
    def command():
        try:
            with authorization.operation():
                mover.gripper("left", "grip", authorization=authorization)
        except (ControlConflict, RwsError) as error:
            errors.append(error)
    def preempt():
        acquire(manager, "operator", 100)
        preempted.set()
    worker = threading.Thread(target=command)
    worker.start()
    assert dispatched.wait(2)
    takeover = threading.Thread(target=preempt)
    takeover.start()
    assert not preempted.wait(0.03), "Revocation must serialize against the final flag write"
    finish_dispatch.set()
    assert preempted.wait(2)
    assert stopped.wait(2)
    worker.join(2)
    takeover.join(2)
    assert not worker.is_alive() and errors
    assert controller.execution == "stopped"
    assert manager.recovery_required
    assert controller.calls[-1][1] == "stop"


def test_stop_generation_change_while_staging_fences_final_request():
    controller = Controller()
    controller.symbols["move_request_left"] = "FALSE"
    controller.write_hook = controller.invalidate_pending_dispatch
    with pytest.raises(RwsError, match="software stop"):
        motion(controller).gripper("left", "grip")
    assert not any(name == "move_request_left" and value == "TRUE" for _, name, value in controller.calls)


def test_slow_unrelated_http_read_does_not_hold_up_lease_expiration():
    manager, controller = ControlManager(0.06, require_session=True), Controller()
    invalidated = threading.Event()
    def invalidate():
        controller.invalidate_pending_dispatch()
        invalidated.set()
    manager.start_watchdog(invalidate, controller.stop_and_verify)
    lease = acquire(manager)
    authorization = manager.authorize("operator", lease.session_id)
    generation = controller.stop_generation()
    errors, dispatched = [], []
    started = threading.Event()
    def dispatch():
        started.set()
        try:
            with controller.dispatch_guard(generation, authorization):
                dispatched.append(True)
        except (ControlConflict, RwsError) as error:
            errors.append(error)
    try:
        with controller._lock:
            worker = threading.Thread(target=dispatch)
            worker.start()
            assert started.wait(1)
            assert invalidated.wait(1), "Waiting for HTTP must not prevent lease invalidation"
            assert manager.get() is None
        worker.join(2)
        assert not worker.is_alive() and errors and not dispatched
    finally:
        manager.close()


def test_reset_waits_until_watchdog_stop_attempt_has_settled():
    manager = ControlManager(0.05, require_session=True)
    stopping, finish = threading.Event(), threading.Event()
    def stop():
        stopping.set()
        assert finish.wait(2)
    manager.start_watchdog(lambda: None, stop)
    try:
        acquire(manager)
        assert stopping.wait(1)
        with pytest.raises(ControlConflict, match="watchdog stop"):
            manager.ensure_reset_allowed()
        finish.set()
        until(lambda: not manager._stop_in_progress)
        manager.ensure_reset_allowed()
        assert manager.recovery_required
    finally:
        finish.set()
        manager.close()
