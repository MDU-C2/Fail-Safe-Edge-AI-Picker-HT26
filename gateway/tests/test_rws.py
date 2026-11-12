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
    MotionSpeed,
    PathPoint,
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
        "/robot/arms/left/gripper",
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
        ("/robot/arms/middle/gripper", {}),
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
        ("/robot/rws/path", {"points": [{"x": 400, "y": 100, "z": 300, "rx": 180, "ry": 0, "rz": 180}]}),
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
        {"x": 300, "y": 150, "z": 280, "speed": 0},
        {"x": 300, "y": 150, "z": 280, "speed": 2000},
        {"x": 300, "y": 150, "z": 280, "speed": "fast"},
        {"x": 300, "y": 150, "z": 280, "acceleration": 10},
        {"x": 300, "y": 150, "z": 280, "acceleration": 150},
        {"x": 300, "y": 150, "z": 280, "ramp": 5},
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
        ("/robot/rws/home", {"speed": -50}),
        ("/robot/rws/home", {"acceleration": 0}),
        ("/robot/rws/gripper", {"action": "squeeze"}),
        ("/robot/rws/gripper", {}),
        ("/robot/rws/gripper", {"action": "move"}),
        ("/robot/rws/gripper", {"action": "move", "position": 26}),
        ("/robot/rws/gripper", {"action": "move", "position": -1}),
        ("/robot/rws/gripper", {"action": "move", "position": 10, "force": 5}),
        ("/robot/rws/gripper", {"action": "open", "position": 10}),
        ("/robot/rws/gripper", {"action": "grip", "position": 10}),
        ("/robot/rws/gripper", {"action": "open", "force": 5}),
        ("/robot/rws/gripper", {"action": "grip", "force": 0}),
        ("/robot/rws/gripper", {"action": "grip", "force": 21}),
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


POINT = {"x": 400, "y": 100, "z": 300, "rx": 180, "ry": 0, "rz": 180}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"points": []},
        {"points": [POINT] * 9},
        {"points": [{"x": 400, "y": 100, "z": 300}]},
        {"points": [{**POINT, "speed": 0}]},
        {"points": [{**POINT, "speed": 2000}]},
        {"points": [{**POINT, "zone": -1}]},
        {"points": [{**POINT, "zone": 101}]},
        {"points": [POINT], "zone": 200},
        {"points": [POINT], "max_acceleration": 0},
        {"points": [POINT], "max_acceleration": 50},
        {"points": [POINT], "arm": "middle"},
        {"points": [POINT], "ramp": 5},
    ],
)
def test_rws_path_is_validated(
    gateway: Gateway,
    body: dict,
) -> None:
    operator = gateway.client("operator")

    with operator.control():
        expect(operator.post("/robot/rws/path", json=body), 422)


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
    """The variables of rapid/SharedVars.mod in the two arm
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
                ("move_speed", "[50,500,5000,1000]"),
                ("move_acc", "100"),
                ("move_ramp", "100"),
                ("grip_pos", "25"),
                ("grip_force", "20"),
                ("path_len", "0"),
                ("path_pos", "[" + ",".join(["[0,0,0]"] * 8) + "]"),
                ("path_rot", "[" + ",".join(["[1,0,0,0]"] * 8) + "]"),
                ("path_lin", "[" + ",".join(["FALSE"] * 8) + "]"),
                ("path_speed", "[" + ",".join(["50"] * 8) + "]"),
                ("path_zone", "[" + ",".join(["10"] * 8) + "]"),
                ("path_acc", "0"),
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

        #
        # A path job: every point must be in reach, as RAPID's
        # RunPath checks them all first.
        #
        length = int(self.symbols.get(f"path_len_{arm}", "0"))

        if length > 0 and self.symbols[f"go_home_{arm}"] != "TRUE" and self.symbols[f"grip_cmd_{arm}"] == '""':
            points = self.symbols[f"path_pos_{arm}"][2:-2].split("],[")[:length]
            x = max(float(point.split(",")[0]) for point in points)

        self.symbols[f"move_ok_{arm}"] = (
            "TRUE"
            if self.symbols[f"go_home_{arm}"] == "TRUE"
            or self.symbols[f"grip_cmd_{arm}"] != '""'
            or x < 800
            else "FALSE"
        )

        if f"path_len_{arm}" in self.symbols:
            self.symbols[f"path_len_{arm}"] = "0"

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

    #
    # No speed given: the default (v50, AccSet 100, 100).
    #
    assert written["move_speed_right"] == ("T_ROB_R", "[50.000000,500.000000,5000.000000,1000.000000]")
    assert written["move_acc_right"] == ("T_ROB_R", "100.000000")
    assert written["move_ramp_right"] == ("T_ROB_R", "100.000000")


def test_rws_motion_writes_the_speed() -> None:
    controller = FakeArmTasks()
    arms = motion(controller)

    assert arms.move("left", (400.0, 150.0, 320.0), None, speed=MotionSpeed(200.0, 50.0, 30.0)).message == "DONE"

    assert controller.symbols["move_speed_left"] == "[200.000000,500.000000,5000.000000,1000.000000]"
    assert controller.symbols["move_acc_left"] == "50.000000"
    assert controller.symbols["move_ramp_left"] == "30.000000"

    #
    # Every job writes its own speed, so nothing carries over.
    #
    assert arms.home("left").message == "DONE"

    assert controller.symbols["move_speed_left"] == "[50.000000,500.000000,5000.000000,1000.000000]"
    assert controller.symbols["move_acc_left"] == "100.000000"

    assert arms.home("right", speed=MotionSpeed(120.0, 100.0, 100.0)).message == "DONE"

    assert controller.symbols["move_speed_right"].startswith("[120.000000,")

    #
    # The gripper does not touch the speed.
    #
    controller.writes.clear()

    assert arms.gripper("left", "grip").message == "DONE"

    assert not any(name.startswith("move_speed") for _, name, _ in controller.writes)


def test_rws_motion_refuses_a_program_without_speed_variables() -> None:
    controller = FakeArmTasks()

    del controller.symbols["move_speed_left"]

    with pytest.raises(MotionConflict, match="no speed variables"):
        motion(controller).move("left", (400.0, 150.0, 320.0), None)

    assert controller.writes == []

    #
    # The gripper does not need them.
    #
    assert motion(controller).gripper("left", "open").message == "DONE"


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


def test_rws_motion_gripper_position_and_force() -> None:
    controller = FakeArmTasks()
    arms = motion(controller)

    assert arms.gripper("left", "move", position=12.5).message == "DONE"

    assert controller.writes[-3:] == [
        ("T_ROB_L", "grip_cmd_left", '"MOVE"'),
        ("T_ROB_L", "grip_pos_left", "12.500000"),
        ("T_ROB_L", "move_request_left", "TRUE"),
    ]

    assert arms.gripper("right", "grip", force=4.5).message == "DONE"

    assert controller.symbols["grip_force_right"] == "4.500000"

    #
    # Without a force: the SmartGripper default, so an earlier
    # gentle grip does not carry over.
    #
    assert arms.gripper("right", "grip").message == "DONE"

    assert controller.symbols["grip_force_right"] == "20.000000"

    #
    # open does not touch the position or the force.
    #
    controller.writes.clear()

    assert arms.gripper("right", "open").message == "DONE"

    assert not any(name.startswith(("grip_force", "grip_pos")) for _, name, _ in controller.writes)

    with pytest.raises(ValueError):
        arms.gripper("left", "move")

    with pytest.raises(ValueError):
        arms.gripper("left", "open", position=10.0)

    with pytest.raises(ValueError):
        arms.gripper("left", "open", force=10.0)


def test_rws_motion_gripper_on_a_program_without_position_and_force() -> None:
    controller = FakeArmTasks()

    del controller.symbols["grip_pos_left"]
    del controller.symbols["grip_force_left"]

    arms = motion(controller)

    #
    # An older arm loop would take MOVE for an arm move to
    # target_pos, so nothing is written.
    #
    with pytest.raises(MotionConflict, match="no gripper position variables"):
        arms.gripper("left", "move", position=10.0)

    with pytest.raises(MotionConflict, match="no gripper force variables"):
        arms.gripper("left", "grip", force=5.0)

    assert controller.writes == []

    #
    # A plain grip or open works as before (g_GripIn's own
    # default force).
    #
    assert arms.gripper("left", "grip").message == "DONE"
    assert arms.gripper("left", "open").message == "DONE"

    assert not any(name.startswith("grip_force") for _, name, _ in controller.writes)


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


# ================================================================
# Path jobs
# ================================================================

def path_points(
    *positions: tuple[float, float, float],
    **options,
) -> list[PathPoint]:
    return [
        PathPoint(position=position, rotation=(180.0, 0.0, 180.0), **options)
        for position in positions
    ]


def test_rws_motion_path_writes_padded_arrays_and_length_last() -> None:
    controller = FakeArmTasks()

    points = [
        PathPoint((400.0, 100.0, 450.0), (180.0, 0.0, 180.0), speed=250.0),
        PathPoint((500.0, 100.0, 409.0), (180.0, 0.0, 180.0), zone=20.0),
        PathPoint((500.0, 100.0, 234.0), (180.0, 0.0, 180.0), linear=True, speed=40.0),
    ]

    result = motion(controller).path(
        "left",
        points,
        zone=5.0,
        max_acceleration=2.0,
        speed=MotionSpeed(150.0, 60.0, 40.0),
    )

    assert (result.accepted, result.message) == (True, "DONE")

    written = {name: value for _, name, value in controller.writes}

    assert written["path_pos_left"] == (
        "[[400.000000,100.000000,450.000000],[500.000000,100.000000,409.000000],"
        "[500.000000,100.000000,234.000000]," + ",".join(["[0,0,0]"] * 5) + "]"
    )
    assert written["path_lin_left"] == "[FALSE,FALSE,TRUE," + ",".join(["FALSE"] * 5) + "]"

    #
    # Per point speed and zone, else the path's.
    #
    assert written["path_speed_left"].startswith("[250.000000,150.000000,40.000000,150.000000,")
    assert written["path_zone_left"].startswith("[5.000000,20.000000,5.000000,5.000000,")
    assert written["path_acc_left"] == "2.000000"
    assert written["move_acc_left"] == "60.000000"
    assert written["move_ramp_left"] == "40.000000"
    assert written["path_rot_left"].count("],[") == 7

    #
    # path_len is what makes it a path job, so it goes last, then
    # move_request.
    #
    assert [name for _, name, _ in controller.writes[-2:]] == ["path_len_left", "move_request_left"]
    assert written["path_len_left"] == "3"

    assert not any(name.endswith("_right") for _, name, _ in controller.writes)


def test_rws_motion_path_without_limit_writes_zero() -> None:
    controller = FakeArmTasks()

    assert motion(controller).path("right", path_points((420.0, -150.0, 300.0))).message == "DONE"

    assert controller.symbols["path_acc_right"] == "0.000000"
    assert controller.symbols["path_len_right"] == "0"


def test_rws_motion_path_refuses_any_point_outside_the_safe_zone() -> None:
    controller = FakeArmTasks()

    result = motion(controller).path(
        "left",
        path_points((400.0, 100.0, 300.0), (400.0, 100.0, 900.0)),
    )

    assert (result.accepted, result.message) == (False, "ERR outside safe zone")
    assert controller.writes == []


def test_rws_motion_path_reports_an_unreachable_point() -> None:
    result = motion(FakeArmTasks()).path(
        "left",
        path_points((400.0, 100.0, 300.0), (900.0, 0.0, 300.0)),
    )

    assert (result.accepted, result.message) == (False, "ERR move failed")


def test_rws_motion_path_needs_the_path_variables() -> None:
    controller = FakeArmTasks()

    del controller.symbols["path_len_left"]

    with pytest.raises(MotionConflict, match="no path variables"):
        motion(controller).path("left", path_points((400.0, 100.0, 300.0)))

    assert controller.writes == []

    #
    # Single moves still work on such a program; they only clear
    # path_len where it exists.
    #
    assert motion(controller).move("left", (400.0, 150.0, 320.0), None).message == "DONE"

    assert not any(name == "path_len_left" for _, name, _ in controller.writes)


def test_rws_motion_move_clears_a_stale_path_job() -> None:
    controller = FakeArmTasks()

    controller.symbols["path_len_left"] = "3"

    assert motion(controller).move("left", (400.0, 150.0, 320.0), None).message == "DONE"

    assert ("T_ROB_L", "path_len_left", "0") in controller.writes


def test_rws_motion_path_length_is_checked() -> None:
    arms = motion(FakeArmTasks())

    with pytest.raises(ValueError):
        arms.path("left", [])

    with pytest.raises(ValueError):
        arms.path("left", path_points(*[(400.0, 100.0, 300.0)] * 9))
