"""Blended RAPID paths, using only in-memory fake controllers."""

import json

import pytest
from pydantic import ValidationError

from gateway.control.manager import ControlConflict
from gateway.models.rws import RwsPathPoint, RwsPathRequest
from gateway.rws.client import RwsError
from gateway.rws.motion import MotionConflict, MotionSpeed, PATH_VARIABLES
from gateway.security.authentication import get_identity
from gateway.security.identity import Identity
from gateway.tests.test_control_sessions import acquire, fenced
from gateway.tests.test_robot_admin import Controller, motion as fenced_motion
from gateway.tests.test_rws import FakeArmTasks, motion


POINT = {"x": 400, "y": 0, "z": 300, "rx": 180, "ry": 0, "rz": 0}


def points():
    return [RwsPathPoint(**POINT), RwsPathPoint(**{**POINT, "x": 450})]


def add_path_symbols(controller):
    for arm in ("left", "right"):
        for name in PATH_VARIABLES:
            controller.symbols[f"{name}_{arm}"] = "0"
        controller.symbols.update({
            f"move_request_{arm}": "FALSE",
            f"move_speed_{arm}": "[50,500,5000,1000]",
            f"move_acc_{arm}": "100",
            f"move_ramp_{arm}": "100",
        })
    return controller


def test_path_serializes_eight_slots_and_commits_once_after_the_final_selector():
    controller = add_path_symbols(FakeArmTasks())
    route = [
        RwsPathPoint(**POINT, linear=True, speed=35, zone=5),
        RwsPathPoint(**{**POINT, "x": 450, "rx": 0}),
        RwsPathPoint(**{**POINT, "x": 500}, zone=100),
    ]
    response = motion(controller).path(
        "right", route, speed=MotionSpeed(80, 40, 25), zone=12,
        max_acceleration=0.75,
    )
    assert (response.accepted, response.message) == (True, "DONE")
    written = {name: value for _, name, value in controller.writes}
    assert json.loads(written["path_pos_right"]) == [
        [400, 0, 300], [450, 0, 300], [500, 0, 300], *([[0, 0, 0]] * 5),
    ]
    assert json.loads(written["path_rot_right"]) == [
        [0, 1, 0, 0], [1, 0, 0, 0], [0, 1, 0, 0], *([[1, 0, 0, 0]] * 5),
    ]
    assert written["path_lin_right"] == "[TRUE,FALSE,FALSE,FALSE,FALSE,FALSE,FALSE,FALSE]"
    assert json.loads(written["path_speed_right"]) == [35, 80, 80, 80, 80, 80, 80, 80]
    assert json.loads(written["path_zone_right"]) == [5, 12, 0, 0, 0, 0, 0, 0]
    assert written["path_acc_right"] == "0.750000"
    assert written["move_acc_right"] == "40.000000"
    assert written["move_ramp_right"] == "25.000000"
    assert written["go_home_right"] == "FALSE"
    assert written["grip_cmd_right"] == '""'
    assert controller.writes[0] == ("T_ROB_R", "path_len_right", "0")
    assert controller.writes[-2:] == [
        ("T_ROB_R", "path_len_right", "3"),
        ("T_ROB_R", "move_request_right", "TRUE"),
    ]
    assert all(task == "T_ROB_R" and name.endswith("_right") for task, name, _ in controller.writes)
    assert sum(name == "move_request_right" for _, name, _ in controller.writes) == 1


def test_path_disables_previous_acceleration_limit_and_uses_defaults():
    controller = add_path_symbols(FakeArmTasks())
    controller.symbols["path_acc_left"] = "0.5"
    assert motion(controller).path("left", points()).accepted
    assert controller.symbols["path_acc_left"] == "0.000000"
    assert json.loads(controller.symbols["path_speed_left"]) == [50] * 8
    assert json.loads(controller.symbols["path_zone_left"]) == [10, 0, 0, 0, 0, 0, 0, 0]


@pytest.mark.parametrize("payload", [
    {"points": []},
    {"points": [POINT] * 9},
    {"points": [{"x": 400, "y": 0, "z": 300}]},
    {"points": [{**POINT, "rx": float("inf")}]},
    {"points": [{**POINT, "x": float("nan")}]},
    {"points": [{**POINT, "speed": 0}]},
    {"points": [{**POINT, "speed": 1501}]},
    {"points": [{**POINT, "zone": -1}]},
    {"points": [{**POINT, "zone": 101}]},
    {"points": [POINT], "zone": float("nan")},
    {"points": [POINT], "zone": 101},
    {"points": [POINT], "max_acceleration": 0},
    {"points": [POINT], "max_acceleration": 21},
    {"points": [POINT], "acceleration": 19},
    {"points": [POINT], "ramp": 9},
])
def test_path_request_rejects_invalid_points_and_limits(payload):
    with pytest.raises(ValidationError):
        RwsPathRequest(**payload)


@pytest.mark.parametrize("count", [1, 8])
def test_path_accepts_capacity_boundaries_and_always_stops_at_end(count):
    controller = add_path_symbols(FakeArmTasks())
    assert motion(controller).path("left", [RwsPathPoint(**POINT)] * count).accepted
    assert controller.symbols["path_len_left"] == str(count)
    assert json.loads(controller.symbols["path_zone_left"])[count - 1] == 0


@pytest.mark.parametrize("name", sorted(PATH_VARIABLES | {
    "go_home", "grip_cmd", "move_speed", "move_acc", "move_ramp",
}))
def test_missing_path_protocol_symbol_rejects_before_any_write(name):
    controller = add_path_symbols(FakeArmTasks())
    del controller.symbols[f"{name}_left"]
    with pytest.raises(MotionConflict, match=f"no {name}_left"):
        motion(controller).path("left", points())
    assert controller.writes == []


@pytest.mark.parametrize("index, changes", [
    (0, {"x": -1}),
    (1, {"z": 601}),
    (0, {"x": 5, "zone": 10}),
    (0, {"y": 495, "zone": 10}),
    (0, {"z": 5, "zone": 10}),
])
def test_every_target_and_blend_envelope_is_checked_before_staging(index, changes):
    controller = add_path_symbols(FakeArmTasks())
    route = [POINT, POINT]
    route[index] = {**POINT, **changes}
    result = motion(controller).path("left", [RwsPathPoint(**point) for point in route])
    assert (result.accepted, result.message) == (False, "ERR outside safe zone")
    assert controller.writes == []


def test_final_point_at_box_edge_ignores_requested_blend_radius():
    controller = add_path_symbols(FakeArmTasks())
    endpoint = RwsPathPoint(**{**POINT, "x": 0}, zone=100)
    assert motion(controller).path("left", [endpoint]).accepted
    assert json.loads(controller.symbols["path_zone_left"])[0] == 0


@pytest.mark.parametrize("job", ["move", "home", "gripper"])
def test_non_path_job_clears_stale_path_selector_without_affecting_other_arm(job):
    controller = add_path_symbols(FakeArmTasks())
    controller.symbols["path_len_left"] = "3"
    controller.symbols["path_len_right"] = "7"
    mover = motion(controller)
    args = {"move": ((400, 0, 300), None), "home": (), "gripper": ("open",)}[job]
    assert getattr(mover, job)("left", *args).accepted
    assert controller.writes[0] == ("T_ROB_L", "path_len_left", "0")
    assert controller.symbols["path_len_right"] == "7"


def test_legacy_controller_remains_usable_for_single_moves():
    controller = FakeArmTasks()
    assert motion(controller).move("left", (400, 0, 300), None).accepted
    assert not any(name.startswith("path_") for _, name, _ in controller.writes)


@pytest.mark.parametrize("failed_symbol", sorted(PATH_VARIABLES | {"move_acc", "move_ramp"}))
def test_failed_staging_never_dispatches_or_retries(failed_symbol):
    controller = add_path_symbols(FakeArmTasks())
    original_write = controller.write_symbol
    attempts = []

    def fail(task, name, value):
        attempts.append(name)
        if name == f"{failed_symbol}_left":
            raise RwsError(500, "Offline staging failure")
        original_write(task, name, value)

    controller.write_symbol = fail
    mover = motion(controller)
    with pytest.raises(RwsError, match="staging failure"):
        mover.path("left", points())
    assert "move_request_left" not in attempts
    assert attempts.count(f"{failed_symbol}_left") == 1
    assert not mover.is_moving()


def test_uncertain_final_dispatch_is_never_retried():
    controller = add_path_symbols(FakeArmTasks())
    original_write = controller.write_symbol
    dispatched = []

    def uncertain(task, name, value):
        original_write(task, name, value)
        if name == "move_request_left":
            dispatched.append(value)
            raise RwsError(None, "Lost response after dispatch")

    controller.write_symbol = uncertain
    with pytest.raises(RwsError, match="Lost response"):
        motion(controller).path("left", points())
    assert dispatched == ["TRUE"]


def test_failed_array_write_leaves_previous_path_disabled():
    controller = add_path_symbols(FakeArmTasks())
    controller.symbols["path_len_left"] = "7"
    original_write = controller.write_symbol

    def fail(task, name, value):
        if name == "path_rot_left":
            raise RwsError(500, "Offline staging failure")
        original_write(task, name, value)

    controller.write_symbol = fail
    with pytest.raises(RwsError, match="staging failure"):
        motion(controller).path("left", points())
    assert controller.symbols["path_len_left"] == "0"
    assert not any(name == "move_request_left" for _, name, _ in controller.writes)


def test_reset_clears_optional_path_selector_and_supports_a_mixed_program():
    controller = Controller()
    controller.symbols["path_len_left"] = "8"
    fenced_motion(controller).reset_program()
    assert controller.symbols["path_len_left"] == "0"
    assert "path_len_right" not in controller.symbols
    assert controller.execution == "stopped"
    assert ("T_ROB_L", "path_len_left", "0") in controller.calls
    assert not any(name == "path_len_right" for _, name, _ in controller.calls)


def test_reset_does_not_restart_if_path_selector_cannot_be_verified():
    controller = Controller()
    controller.symbols["path_len_left"] = "8"
    controller.bad_read = "path_len_left"
    with pytest.raises(MotionConflict, match="could not verify path_len_left"):
        fenced_motion(controller).reset_program(start=True)
    assert controller.execution == "stopped"
    assert not any(action in {"resetpp", "start"} for _, action, _ in controller.calls)


@pytest.mark.parametrize("revoke_on", ["path_pos_left", "path_len_left"])
def test_revoked_lease_while_staging_prevents_path_dispatch(fenced, revoke_on):
    manager, controller, mover, _ = fenced
    add_path_symbols(controller)
    lease = acquire(manager)
    authorization = manager.authorize("operator", lease.session_id)
    manager.clear_recovery(authorization)

    def revoke():
        if controller.calls[-1][1] == revoke_on and controller.calls[-1][2] != "0":
            manager.release("operator", lease.session_id)

    controller.write_hook = revoke
    with authorization.operation(), pytest.raises(ControlConflict):
        mover.path("left", points(), authorization=authorization)
    assert not any(name == "move_request_left" for _, name, _ in controller.calls)
    assert manager.recovery_required


@pytest.mark.parametrize("stop_on", ["path_pos_left", "path_len_left"])
def test_stop_during_path_staging_prevents_dispatch(stop_on):
    controller = add_path_symbols(Controller())

    def stop():
        if controller.calls[-1][1] == stop_on and controller.calls[-1][2] != "0":
            controller.invalidate_pending_dispatch()

    controller.write_hook = stop
    with pytest.raises(RwsError, match="software stop"):
        fenced_motion(controller).path("left", points())
    assert not any(name == "move_request_left" for _, name, _ in controller.calls)


def test_path_route_requires_motion_permission(fenced):
    _, controller, _, client = fenced
    client.app.dependency_overrides[get_identity] = lambda: Identity(
        "diagnostics", True, frozenset({"robot.read"}), 0,
    )
    assert client.post("/robot/rws/path", json={"points": [POINT]}).status_code == 403
    assert controller.calls == []


def test_path_route_preserves_session_fence_parameters_and_completion(fenced):
    manager, controller, _, client = fenced
    add_path_symbols(controller)
    payload = {"arm": "right", "points": [{**POINT, "linear": True}],
               "speed": 45, "acceleration": 50, "ramp": 30, "max_acceleration": 0.6}
    assert client.post("/robot/rws/path", json=payload).status_code == 409
    session = client.post("/control/acquire").json()["lease"]["session_id"]
    headers = {"X-Control-Session": session}
    assert client.post("/robot/rws/path", json=payload, headers=headers).status_code == 409
    manager.clear_recovery(manager.authorize("operator", session))
    assert client.post("/robot/rws/path", json={**payload, "wait": False}, headers=headers).status_code == 409
    assert client.post("/robot/rws/path", json={"points": []}, headers=headers).status_code == 422
    assert controller.calls == []

    def complete():
        if controller.calls[-1][1:] == ("move_request_right", "TRUE"):
            controller.symbols["move_request_right"] = "FALSE"

    controller.write_hook = complete
    response = client.post("/robot/rws/path", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": True, "message": "DONE"}
    assert controller.symbols["path_acc_right"] == "0.600000"
    assert controller.symbols["move_acc_right"] == "50.000000"
    assert controller.symbols["move_ramp_right"] == "30.000000"
    assert json.loads(controller.symbols["path_speed_right"])[0] == 45
    assert controller.symbols["path_lin_right"].startswith("[TRUE,")
    assert controller.calls[-1][1:] == ("move_request_right", "TRUE")
    assert manager.release("operator", session)[0]
    acquire(manager)
    before = list(controller.calls)
    assert client.post("/robot/rws/path", json=payload, headers=headers).status_code == 409
    assert controller.calls == before
