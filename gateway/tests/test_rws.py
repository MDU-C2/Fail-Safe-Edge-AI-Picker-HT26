"""Arm pose, RAPID and RWS move endpoints without a robot controller.

The mock gateways point RWS at a closed port, so these check what
the gateway itself enforces (permissions, lease, validation) and
that an unreachable controller gives 503. Behaviour against a
controller is in test_robotstudio.py.
"""

import threading
import time

import pytest

from gateway.rws import rapid_router
from gateway.rws.client import RwsError
from gateway.rws.motion import (
    MotionConflict,
    RwsMotion,
    orient_zyx,
)
from gateway.tests.support import Gateway, expect


MODULE_SOURCE = """MODULE GatewayTest
ENDMODULE
"""


@pytest.mark.parametrize(
    "path",
    [
        "/robot/arms",
        "/robot/arms/left/pose",
        "/robot/arms/right/joints",
        "/robot/rapid",
    ],
)
def test_unreachable_controller_gives_503(
    gateway: Gateway,
    path: str,
) -> None:
    response = gateway.client("diagnostics").get(path)

    detail = expect(response, 503)["detail"]

    assert detail.startswith("Robot controller unreachable")


@pytest.mark.parametrize(
    "path, params",
    [
        ("/robot/arms/middle/pose", {}),
        ("/robot/arms/middle/joints", {}),
        ("/robot/arms/left/pose", {"coordinate": "World"}),
        ("/robot/arms/left/pose", {"tool": "../tool0"}),
        ("/robot/arms/left/pose", {"wobj": "a" * 33}),
    ],
)
def test_pose_parameters_are_validated(
    gateway: Gateway,
    path: str,
    params: dict,
) -> None:
    expect(gateway.client("diagnostics").get(path, params=params), 422)


@pytest.mark.parametrize(
    "method, path",
    [
        ("POST", "/robot/rapid/start"),
        ("POST", "/robot/rapid/stop"),
        ("PUT", "/robot/rapid/tasks/T_ROB_L/modules/GatewayTest"),
        ("DELETE", "/robot/rapid/tasks/T_ROB_L/modules/GatewayTest"),
    ],
)
def test_rapid_control_needs_permission_and_lease(
    gateway: Gateway,
    method: str,
    path: str,
) -> None:
    body = {"source": MODULE_SOURCE} if method == "PUT" else None

    expect(gateway.client("diagnostics").request(method, path, json=body), 403)
    expect(gateway.client("operator").request(method, path, json=body), 409)


@pytest.mark.parametrize(
    "path, source",
    [
        ("/robot/rapid/tasks/T_ROB_L/modules/Other", MODULE_SOURCE),
        ("/robot/rapid/tasks/T_ROB_L/modules/GatewayTest", "PROC main()\nENDPROC\n"),
        ("/robot/rapid/tasks/T_ROB_L/modules/GatewayTest", MODULE_SOURCE + "! ☃\n"),
        ("/robot/rapid/tasks/T_ROB_L/modules/GatewayTest", ""),
        ("/robot/rapid/tasks/T-ROB/modules/GatewayTest", MODULE_SOURCE),
        ("/robot/rapid/tasks/T_ROB_L/modules/1Module", MODULE_SOURCE),
    ],
)
def test_module_upload_is_validated_before_contacting_controller(
    gateway: Gateway,
    path: str,
    source: str,
) -> None:
    operator = gateway.client("operator")

    with operator.control():
        expect(operator.put(path, json={"source": source}), 422)


def test_control_with_lease_reaches_controller(
    gateway: Gateway,
) -> None:
    operator = gateway.client("operator")

    with operator.control():
        expect(operator.post("/robot/rapid/stop"), 503)


# ================================================================
# Moves over RWS
# ================================================================

@pytest.mark.parametrize(
    "path, body",
    [
        ("/robot/rws/move", {"x": 300, "y": 150, "z": 280}),
        ("/robot/rws/move", {"arm": "right", "x": 420, "y": -150, "z": 300}),
        ("/robot/rws/home", None),
        ("/robot/rws/home", {"arm": "right"}),
        ("/robot/rws/home", {"wait": False}),
        ("/robot/rws/gripper", {"action": "grip"}),
    ],
)
def test_rws_moves_need_permission_and_lease(
    gateway: Gateway,
    path: str,
    body: dict | None,
) -> None:
    expect(gateway.client("diagnostics").post(path, json=body), 403)
    expect(gateway.client("operator").post(path, json=body), 409)

    operator = gateway.client("operator")

    with operator.control():
        detail = expect(operator.post(path, json=body), 503)["detail"]

    assert detail.startswith("Robot controller unreachable")


@pytest.mark.parametrize(
    "body",
    [
        {"x": 300, "y": 150},
        {"x": 300, "y": 150, "z": "high"},
        {"x": 300, "y": 150, "z": 280, "rx": 90},
        {"x": 300, "y": 150, "z": 280, "rx": 90, "ry": 0},
        {"arm": "middle", "x": 300, "y": 150, "z": 280},
    ],
)
def test_rws_move_is_validated(
    gateway: Gateway,
    body: dict,
) -> None:
    operator = gateway.client("operator")

    with operator.control():
        expect(operator.post("/robot/rws/move", json=body), 422)


def test_rws_status_needs_read_permission_only(
    gateway: Gateway,
) -> None:
    #
    # No lease: any reader may poll. Mock gateways cannot reach
    # the controller.
    #
    detail = expect(
        gateway.client("diagnostics").get("/robot/rws/status", params={"arm": "right"}),
        503,
    )["detail"]

    assert detail.startswith("Robot controller unreachable")

    expect(gateway.client("diagnostics").get("/robot/rws/status", params={"arm": "middle"}), 422)


@pytest.mark.parametrize(
    "path, body",
    [
        ("/robot/rws/home", {"arm": "middle"}),
        ("/robot/rws/gripper", {"action": "squeeze"}),
        ("/robot/rws/gripper", {}),
    ],
)
def test_rws_home_and_gripper_are_validated(
    gateway: Gateway,
    path: str,
    body: dict,
) -> None:
    operator = gateway.client("operator")

    with operator.control():
        expect(operator.post(path, json=body), 422)


@pytest.mark.parametrize(
    "angles, expected",
    [
        ((0, 0, 0), (1.0, 0.0, 0.0, 0.0)),
        ((90, 0, 90), (0.5, 0.5, 0.5, 0.5)),
        #
        # Read from OrientZYX(60, 20, 30) on the virtual controller.
        #
        ((60, 20, 30), (0.846279, 0.136873, 0.272703, 0.436703)),
    ],
)
def test_orient_zyx_matches_rapid(
    angles: tuple[float, float, float],
    expected: tuple[float, float, float, float],
) -> None:
    assert orient_zyx(*angles) == pytest.approx(expected, abs=1e-6)


class FiveTaskController:
    """The task list of the real YuMi: two background tasks without
    an active field and a non-motion communication task."""

    def execution_state(self) -> str:
        return "running"

    def controller_state(self) -> str:
        return "motoron"

    def operation_mode(self) -> str:
        return "MANR"

    def tasks(self) -> list[dict[str, str]]:
        return [
            {"name": "T_ROB_R", "taskstate": "init", "excstate": "star", "active": "On"},
            {"name": "T_ROB_L", "taskstate": "init", "excstate": "star", "active": "On"},
            {"name": "YuMiLibBG_R", "taskstate": "init", "excstate": "star"},
            {"name": "YuMiLibBG_L", "taskstate": "init", "excstate": "star"},
            {"name": "communication", "taskstate": "init", "excstate": "star", "active": "Off"},
        ]

    def program_modules(
        self,
        task: str,
    ) -> list[str]:
        return ["SharedVars"]


def test_rapid_status_handles_background_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rapid_router,
        "rws_client",
        FiveTaskController(),
    )

    tasks = {
        task.name: task.active
        for task in rapid_router.read_status().tasks
    }

    assert tasks == {
        "T_ROB_R": True,
        "T_ROB_L": True,
        "YuMiLibBG_R": True,
        "YuMiLibBG_L": True,
        "communication": False,
    }


# ================================================================
# RWS motion handshake against a fake controller
# ================================================================

class FakeArmTasks:
    """The variables of gateway/rapid/SharedVars.mod in the two arm
    tasks. An arm finishes a job job_seconds after move_request is
    set; a target with x >= 800 is out of reach."""

    def __init__(
        self,
        job_seconds: float = 0.0,
    ) -> None:
        self.job_seconds = job_seconds

        self.state = "running"
        self.writes: list[tuple[str, str, str]] = []

        self.symbols = {
            f"{name}_{arm}": value
            for arm in ("left", "right")
            for name, value in (
                ("move_request", "FALSE"),
                ("move_ok", "FALSE"),
                ("go_home", "FALSE"),
                ("use_rot", "FALSE"),
                ("grip_cmd", '""'),
                ("target_pos", "[0,0,0]"),
                ("target_rot", "[1,0,0,0]"),
            )
        }

    def execution_state(self) -> str:
        return self.state

    def read_symbol(
        self,
        task: str,
        name: str,
    ) -> str:
        if name not in self.symbols:
            raise RwsError(400, "Symbol not found")

        return self.symbols[name]

    def write_symbol(
        self,
        task: str,
        name: str,
        value: str,
    ) -> None:
        self.writes.append((task, name, value))

        self.symbols[name] = value

        if name.startswith("move_request_") and value == "TRUE":
            arm = name.removeprefix("move_request_")

            threading.Timer(self.job_seconds, self._finish, (arm,)).start()

    def _finish(
        self,
        arm: str,
    ) -> None:
        x = float(self.symbols[f"target_pos_{arm}"].strip("[]").split(",")[0])

        self.symbols[f"move_ok_{arm}"] = (
            "TRUE"
            if self.symbols[f"go_home_{arm}"] == "TRUE"
            or self.symbols[f"grip_cmd_{arm}"] != '""'
            or x < 800
            else "FALSE"
        )

        self.symbols[f"grip_cmd_{arm}"] = '""'
        self.symbols[f"move_request_{arm}"] = "FALSE"


def motion(
    controller: FakeArmTasks,
) -> RwsMotion:
    return RwsMotion(
        client=controller,
        timeout=5.0,
        safe_zone_min=(0.0, -500.0, 0.0),
        safe_zone_max=(1000.0, 500.0, 600.0),
    )


def test_rws_motion_writes_the_arms_variables() -> None:
    controller = FakeArmTasks()

    result = motion(controller).move("right", (420.0, -150.0, 300.0), (180.0, 0.0, 0.0))

    assert (result.accepted, result.message) == (True, "DONE")

    written = {name: (task, value) for task, name, value in controller.writes}

    assert written["target_pos_right"] == ("T_ROB_R", "[420.000000,-150.000000,300.000000]")
    assert written["use_rot_right"] == ("T_ROB_R", "TRUE")
    assert written["go_home_right"] == ("T_ROB_R", "FALSE")
    assert "target_rot_right" in written

    #
    # move_request is set last, after the job is written.
    #
    assert controller.writes[-1] == ("T_ROB_R", "move_request_right", "TRUE")

    assert not any(name.endswith("_left") for _, name, _ in controller.writes)


def test_rws_motion_home_and_gripper() -> None:
    controller = FakeArmTasks()
    arms = motion(controller)

    assert arms.home("left").message == "DONE"
    assert controller.symbols["go_home_left"] == "TRUE"

    assert arms.gripper("left", "grip").message == "DONE"
    assert ("T_ROB_L", "grip_cmd_left", '"GRIP"') in controller.writes
    assert controller.symbols["go_home_left"] == "FALSE"

    assert arms.gripper("right", "open").message == "DONE"
    assert ("T_ROB_R", "grip_cmd_right", '"OPEN"') in controller.writes


def test_rws_motion_refuses_targets_outside_the_safe_zone() -> None:
    controller = FakeArmTasks()

    result = motion(controller).move("left", (300.0, 150.0, 700.0), None)

    assert (result.accepted, result.message) == (False, "ERR outside safe zone")

    assert controller.writes == []


def test_rws_motion_reports_a_failed_move() -> None:
    result = motion(FakeArmTasks()).move("left", (900.0, 0.0, 300.0), None)

    assert (result.accepted, result.message) == (False, "ERR move failed")


def test_rws_motion_arms_are_independent() -> None:
    controller = FakeArmTasks(job_seconds=0.5)
    arms = motion(controller)

    results = {}

    left = threading.Thread(
        target=lambda: results.update(left=arms.home("left")),
    )

    left.start()

    time.sleep(0.1)

    assert arms.is_moving()

    with pytest.raises(MotionConflict, match="A left arm job is already in progress"):
        arms.home("left")

    started = time.monotonic()

    assert arms.home("right").message == "DONE"

    left.join()

    assert results["left"].message == "DONE"
    assert time.monotonic() - started < 1.0
    assert not arms.is_moving()


def test_rws_motion_refuses_a_busy_or_missing_program() -> None:
    controller = FakeArmTasks()

    controller.symbols["move_request_left"] = "TRUE"

    with pytest.raises(MotionConflict, match="busy with an earlier job"):
        motion(controller).home("left")

    controller.state = "stopped"

    with pytest.raises(MotionConflict, match="RAPID is not running"):
        motion(controller).home("right")

    controller.state = "running"

    del controller.symbols["move_request_right"]

    with pytest.raises(MotionConflict, match="not loaded in T_ROB_R"):
        motion(controller).home("right")


def test_rws_motion_can_start_without_waiting() -> None:
    controller = FakeArmTasks(job_seconds=0.3)
    arms = motion(controller)

    assert arms.status("left").message == "ERR move failed"

    started = arms.move("left", (400.0, 150.0, 320.0), None, wait=False)

    assert (started.accepted, started.message) == (True, "STARTED")
    assert not arms.is_moving()

    assert (arms.status("left").accepted, arms.status("left").message) == (True, "BUSY")

    #
    # Like CommServer's ERR busy.
    #
    with pytest.raises(MotionConflict, match="busy with an earlier job"):
        arms.gripper("left", "grip", wait=False)

    #
    # The other arm is free meanwhile.
    #
    assert arms.home("right", wait=False).message == "STARTED"

    deadline = time.monotonic() + 5

    while arms.status("left").message == "BUSY":
        assert time.monotonic() < deadline

        time.sleep(0.05)

    assert arms.status("left").message == "DONE"

    while arms.status("right").message == "BUSY":
        time.sleep(0.05)

    assert arms.status("right").message == "DONE"


def test_rws_motion_status_reports_a_failed_job() -> None:
    controller = FakeArmTasks()
    arms = motion(controller)

    assert arms.move("left", (900.0, 0.0, 300.0), None, wait=False).message == "STARTED"

    time.sleep(0.2)

    status = arms.status("left")

    assert (status.accepted, status.message) == (False, "ERR move failed")

    del controller.symbols["move_request_left"]

    with pytest.raises(MotionConflict, match="not loaded in T_ROB_L"):
        arms.status("left")
