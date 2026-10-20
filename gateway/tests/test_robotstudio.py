"""Arm pose, RAPID control and moves on the RobotStudio virtual
controller (run with --robotstudio).

Moves are tested both ways: over RWS with the program in
rapid/ (POST /robot/rws/*), and through the legacy tcp robot
provider with the team's socket server rapid/CommServer.mod
(POST /robot/command). The virtual controller has no communication
task, so that test runs CommServer in T_ROB_R and only moves the
left arm.

The tests change the controller's RAPID program. The program
modules loaded before the run are saved at the start and loaded
again at the end, but the program is left stopped.
"""

import re
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

import pytest
import requests
from requests.auth import HTTPDigestAuth

from gateway.rws.motion import orient_zyx
from gateway.tests.support import ROBOTSTUDIO_RWS_PORT, Client, Gateway, expect


pytestmark = pytest.mark.robotstudio

RAPID_DIR = Path(__file__).resolve().parents[2] / "rapid"

TASKS = ("T_ROB_L", "T_ROB_R")

TEST_TASK = "T_ROB_L"
TEST_MODULE = "GatewayTest"

TEST_SOURCE = f"""MODULE {TEST_MODULE}
    PROC gateway_test()
        TPWrite "Loaded by test_robotstudio.py";
    ENDPROC
ENDMODULE
"""

#
# The arm modules of the program in rapid/, as rapid/rapidctl.py
# loads them.
#
#
# The virtual controller has no SmartGripper option, so the gripper
# routines the arm modules call are stubbed.
#
GRIPPER_STUB = RAPID_DIR / "sim" / "GripperStub.mod"

LEFT_ARM = [
    RAPID_DIR / "SharedVars.mod",
    RAPID_DIR / "LeftArmInstructions.mod",
    RAPID_DIR / "GoHomeLeft.mod",
    GRIPPER_STUB,
]

RWS_PROGRAM = {
    "T_ROB_L": LEFT_ARM,
    "T_ROB_R": [
        RAPID_DIR / "SharedVars.mod",
        RAPID_DIR / "RightArmInstructions.mod",
        RAPID_DIR / "GoHomeRight.mod",
        GRIPPER_STUB,
    ],
}

#
# The team's socket server in place of the right arm.
#
TCP_PROGRAM = {
    "T_ROB_L": LEFT_ARM,
    "T_ROB_R": [
        RAPID_DIR / "SharedVars.mod",
        RAPID_DIR / "CommServer.mod",
    ],
}

#
# With the calibration tilt; reachable for the left arm.
#
TARGET = (325.0, 200.0, 300.0)
TARGET_ROTATION = (90.0, 0.0, 90.0)
TARGET_COMMAND = "325,200,300,90,0,90"

RIGHT_TARGET = (420.0, -150.0, 300.0)

#
# Inside the gateway's default safe zone but out of the arm's reach.
#
UNREACHABLE = (900.0, 0.0, 300.0)

POSITION_TOLERANCE_MM = 1.0


def loaded(
    rapid: dict,
    task: str,
) -> list[str]:
    return next(
        entry["modules"]
        for entry in rapid["tasks"]
        if entry["name"] == task
    )


@pytest.fixture()
def operator(
    robotstudio_gateway: Gateway,
) -> Iterator[Client]:
    """The operator, holding control, with RAPID stopped."""

    client = robotstudio_gateway.client("operator")

    with client.control():
        expect(client.post("/robot/rapid/stop"), 200)

        yield client

        client.post("/robot/rapid/stop")


# ================================================================
# Read endpoints
# ================================================================

def test_health_reports_the_controller(
    robotstudio_gateway: Gateway,
) -> None:
    health = expect(robotstudio_gateway.client(None).get("/health"), 200)

    controller = health["robot_controller"]

    assert controller["ok"] is True and controller["reachable"] is True
    assert controller["controller_state"] in ("motoron", "motoroff")
    assert controller["operation_mode"] == "AUTO"
    assert controller["rapid_execution_state"] in ("running", "stopped")
    assert controller["move_in_progress"] is False

    assert health["status"] == "ok"


def test_both_arm_poses(
    robotstudio_gateway: Gateway,
) -> None:
    poses = expect(robotstudio_gateway.client("diagnostics").get("/robot/arms"), 200)

    assert set(poses) == {"left", "right"}

    for arm, unit in (("left", "ROB_L"), ("right", "ROB_R")):
        pose = poses[arm]

        assert pose["arm"] == arm and pose["mech_unit"] == unit
        assert pose["tool"] and pose["wobj"]

        norm = sum(pose[q] ** 2 for q in ("q1", "q2", "q3", "q4"))

        assert norm == pytest.approx(1.0, abs=1e-3)


def test_pose_with_explicit_frame(
    robotstudio_gateway: Gateway,
) -> None:
    pose = expect(
        robotstudio_gateway.client().get(
            "/robot/arms/left/pose",
            params={"tool": "tool0", "wobj": "wobj0", "coordinate": "Base"},
        ),
        200,
    )

    assert (pose["tool"], pose["wobj"], pose["coordinate"]) == ("tool0", "wobj0", "Base")


def test_unknown_tool_is_rejected_by_controller(
    robotstudio_gateway: Gateway,
) -> None:
    response = robotstudio_gateway.client().get(
        "/robot/arms/left/pose",
        params={"tool": "no_such_tool"},
    )

    detail = expect(response, 400)["detail"]

    #
    # The controller's build paths are stripped from the message.
    #
    assert detail.startswith("Robot controller: ")
    assert "BUILDAGENTS" not in detail


def test_joints(
    robotstudio_gateway: Gateway,
) -> None:
    for arm in ("left", "right"):
        joints = expect(robotstudio_gateway.client().get(f"/robot/arms/{arm}/joints"), 200)

        assert len(joints["joints"]) == 6


def test_rapid_status(
    robotstudio_gateway: Gateway,
) -> None:
    rapid = expect(robotstudio_gateway.client("diagnostics").get("/robot/rapid"), 200)

    assert rapid["execution_state"] in ("running", "stopped")
    assert {task["name"] for task in rapid["tasks"]} >= set(TASKS)


# ================================================================
# Modules
# ================================================================

def test_load_replace_and_unload_module(
    operator: Client,
) -> None:
    path = f"/robot/rapid/tasks/{TEST_TASK}/modules/{TEST_MODULE}"

    try:
        rapid = expect(operator.put(path, json={"source": TEST_SOURCE}), 200)

        assert TEST_MODULE in loaded(rapid, TEST_TASK)

        rapid = expect(operator.put(path, json={"source": TEST_SOURCE}), 200)

        assert loaded(rapid, TEST_TASK).count(TEST_MODULE) == 1

    finally:
        rapid = expect(operator.delete(path), 200)

    assert TEST_MODULE not in loaded(rapid, TEST_TASK)

    expect(operator.delete(path), 404)


def test_unknown_task(
    operator: Client,
) -> None:
    expect(
        operator.put(
            f"/robot/rapid/tasks/T_NOPE/modules/{TEST_MODULE}",
            json={"source": TEST_SOURCE},
        ),
        404,
    )


def test_modules_cannot_change_while_running(
    operator: Client,
) -> None:
    rapid = expect(operator.post("/robot/rapid/start", json={"reset_program_pointer": True}), 200)

    assert rapid["execution_state"] == "running"

    expect(
        operator.put(
            f"/robot/rapid/tasks/{TEST_TASK}/modules/{TEST_MODULE}",
            json={"source": TEST_SOURCE},
        ),
        409,
    )

    rapid = expect(operator.post("/robot/rapid/stop"), 200)

    assert rapid["execution_state"] == "stopped"
    assert TEST_MODULE not in loaded(rapid, TEST_TASK)


# ================================================================
# Moves, end to end
# ================================================================

class Controller:
    """Direct RWS access, to save and restore the program and to
    read RAPID variables the gateway has no endpoint for."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.auth = HTTPDigestAuth("Default User", "robotics")

        self._base = f"http://127.0.0.1:{ROBOTSTUDIO_RWS_PORT}"

    def save_module(
        self,
        task: str,
        module: str,
    ) -> str:
        name = f"gateway_backup_{task}_{module}"

        self._session.post(
            f"{self._base}/rw/rapid/modules/{module}",
            params={"task": task, "action": "save", "json": "1"},
            data={"name": name, "path": "$TEMP"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        ).raise_for_status()

        response = self._session.get(
            f"{self._base}/fileservice/$TEMP/{name}.mod",
            timeout=30,
        )

        response.raise_for_status()

        return response.content.decode("iso-8859-1")

    def read_symbol(
        self,
        task: str,
        name: str,
    ) -> str:
        response = self._session.get(
            f"{self._base}/rw/rapid/symbol/data/RAPID/{task}/{name}",
            params={"json": "1"},
            timeout=30,
        )

        response.raise_for_status()

        return response.json()["_embedded"]["_state"][0]["value"]


def module_source(
    path: Path,
) -> tuple[str, str]:
    """Return the declared module name and the source."""

    text = path.read_text(encoding="utf-8")

    #
    # The module names inside the files can differ from the file
    # names, so read them from the file.
    #
    name = re.search(r"^\s*MODULE\s+(\w+)", text, re.MULTILINE).group(1)

    #
    # In simulation the socket server binds to 127.0.0.1.
    #
    text = re.sub(
        r'CONST string SERVER_IP := "[^"]*";',
        'CONST string SERVER_IP := "127.0.0.1";',
        text,
    )

    return name, text


LoadProgram = Callable[[dict[str, list[Path]]], Client]


@pytest.fixture()
def load_program(
    operator: Client,
) -> Iterator[LoadProgram]:
    """Load and start a program; restore the saved one afterwards."""

    controller = Controller()

    rapid = expect(operator.get("/robot/rapid"), 200)

    backup = {
        task: {
            module: controller.save_module(task, module)
            for module in loaded(rapid, task)
        }
        for task in TASKS
    }

    def replace_program(
        program: dict[str, dict[str, str]],
    ) -> None:
        rapid = expect(operator.get("/robot/rapid"), 200)

        for task in TASKS:
            for module in loaded(rapid, task):
                expect(operator.delete(f"/robot/rapid/tasks/{task}/modules/{module}"), 200)

            for module, source in program[task].items():
                expect(
                    operator.put(f"/robot/rapid/tasks/{task}/modules/{module}", json={"source": source}),
                    200,
                )

    def load(
        program: dict[str, list[Path]],
    ) -> Client:
        replace_program(
            {
                task: dict(module_source(path) for path in paths)
                for task, paths in program.items()
            }
        )

        expect(operator.post("/robot/rapid/start", json={"reset_program_pointer": True}), 200)

        return operator

    try:
        yield load

    finally:
        operator.post("/robot/rapid/stop")

        replace_program(backup)


def arm_pose(
    client: Client,
    arm: str = "left",
) -> dict:
    return expect(
        client.get(
            f"/robot/arms/{arm}/pose",
            params={"tool": "tool0", "wobj": "wobj0"},
        ),
        200,
    )


def assert_position(
    pose: dict,
    expected: tuple[float, float, float],
) -> None:
    for axis, value in zip(("x", "y", "z"), expected):
        assert pose[axis] == pytest.approx(value, abs=POSITION_TOLERANCE_MM), pose


def same_orientation(
    pose: dict,
    quaternion: tuple[float, ...],
) -> bool:
    #
    # q and -q are the same orientation.
    #
    dot = sum(
        pose[name] * value
        for name, value in zip(("q1", "q2", "q3", "q4"), quaternion)
    )

    return abs(dot) == pytest.approx(1.0, abs=1e-3)


def rws_move(
    client: Client,
    position: tuple[float, float, float],
    rotation: tuple[float, float, float] | None = None,
    arm: str = "left",
) -> dict:
    body = {"arm": arm, **dict(zip(("x", "y", "z"), position))}

    if rotation is not None:
        body.update(zip(("rx", "ry", "rz"), rotation))

    return expect(client.post("/robot/rws/move", json=body), 200)


def rws_home(
    client: Client,
    arm: str = "left",
) -> dict:
    return expect(client.post("/robot/rws/home", json={"arm": arm}), 200)


DONE = {"accepted": True, "message": "DONE"}
MOVE_FAILED = {"accepted": False, "message": "ERR move failed"}
OUTSIDE_SAFE_ZONE = {"accepted": False, "message": "ERR outside safe zone"}


def test_rws_moves_the_virtual_robot(
    load_program: LoadProgram,
) -> None:
    operator = load_program(RWS_PROGRAM)

    #
    # Without a body /home homes the left arm.
    #
    assert expect(operator.post("/robot/rws/home"), 200) == DONE

    assert rws_move(operator, TARGET, TARGET_ROTATION) == DONE

    pose = arm_pose(operator)

    assert_position(pose, TARGET)

    rx, ry, rz = TARGET_ROTATION

    assert same_orientation(pose, orient_zyx(rz, ry, rx)), pose

    #
    # Without a rotation the orientation is kept.
    #
    second = (300.0, 150.0, 280.0)

    assert rws_move(operator, second) == DONE

    moved = arm_pose(operator)

    assert_position(moved, second)
    assert same_orientation(moved, tuple(pose[q] for q in ("q1", "q2", "q3", "q4"))), moved

    #
    # The arm program refuses what it cannot reach; the gateway
    # refuses what is outside its safe zone without sending it.
    #
    assert rws_move(operator, UNREACHABLE) == MOVE_FAILED

    assert rws_move(operator, (1500.0, 0.0, 300.0)) == OUTSIDE_SAFE_ZONE

    assert_position(arm_pose(operator), second)


def test_rws_moves_the_right_arm(
    load_program: LoadProgram,
) -> None:
    operator = load_program(RWS_PROGRAM)

    left_before = arm_pose(operator)

    assert rws_home(operator, "right") == DONE
    assert rws_move(operator, RIGHT_TARGET, arm="right") == DONE

    assert_position(arm_pose(operator, "right"), RIGHT_TARGET)

    #
    # The left arm was not touched.
    #
    assert_position(arm_pose(operator), tuple(left_before[axis] for axis in "xyz"))


def test_rws_gripper(
    load_program: LoadProgram,
) -> None:
    operator = load_program(RWS_PROGRAM)

    for action in ("grip", "open"):
        result = operator.post("/robot/rws/gripper", json={"arm": "left", "action": action})

        assert expect(result, 200) == DONE


def timed_move(
    client: Client,
    position: tuple[float, float, float],
    **speed: float,
) -> float:
    body = {"arm": "left", **dict(zip(("x", "y", "z"), position)), **speed}

    started = time.monotonic()

    assert expect(client.post("/robot/rws/move", json=body), 200) == DONE

    return time.monotonic() - started


def test_rws_move_speed_and_acceleration(
    load_program: LoadProgram,
) -> None:
    operator = load_program(RWS_PROGRAM)
    controller = Controller()

    low = TARGET
    high = (TARGET[0], TARGET[1], TARGET[2] + 60.0)

    assert rws_move(operator, low) == DONE

    #
    # 60 mm: about 3 s at 20 mm/s, well under 1 s at 400 mm/s.
    #
    slow = timed_move(operator, high, speed=20)

    assert controller.read_symbol("T_ROB_L", "move_speed_left").startswith("[20,")

    fast = timed_move(operator, low, speed=400, acceleration=50, ramp=40)

    assert slow > 2.5, slow
    assert fast < slow / 2, (slow, fast)

    assert_position(arm_pose(operator), low)

    assert controller.read_symbol("T_ROB_L", "move_speed_left").startswith("[400,")
    assert float(controller.read_symbol("T_ROB_L", "move_acc_left")) == 50
    assert float(controller.read_symbol("T_ROB_L", "move_ramp_left")) == 40

    #
    # Without a speed: the default, v50 and full acceleration.
    #
    assert rws_home(operator) == DONE

    assert controller.read_symbol("T_ROB_L", "move_speed_left") == "[50,500,5000,1000]"
    assert float(controller.read_symbol("T_ROB_L", "move_acc_left")) == 100
    assert float(controller.read_symbol("T_ROB_L", "move_ramp_left")) == 100

    #
    # The right arm keeps its own.
    #
    assert controller.read_symbol("T_ROB_R", "move_speed_right") == "[50,500,5000,1000]"


def test_rws_refuses_a_second_job_on_a_busy_arm(
    load_program: LoadProgram,
) -> None:
    operator = load_program(RWS_PROGRAM)

    assert rws_move(operator, TARGET) == DONE

    responses = {}

    homing = threading.Thread(
        target=lambda: responses.update(home=operator.post("/robot/rws/home")),
    )

    homing.start()

    #
    # Homing from TARGET takes several seconds at the default
    # 50 mm/s.
    #
    time.sleep(0.5)

    response = operator.post("/robot/rws/move", json=dict(zip(("x", "y", "z"), TARGET)))

    #
    # The other arm is independent.
    #
    right = rws_home(operator, "right")

    homing.join()

    assert expect(response, 409)["detail"] == "A left arm job is already in progress"
    assert expect(responses["home"], 200) == DONE
    assert right == DONE


def wait_for_status(
    client: Client,
    arm: str,
) -> dict:
    deadline = time.monotonic() + 30

    while (status := expect(client.get("/robot/rws/status", params={"arm": arm}), 200))["message"] == "BUSY":
        assert time.monotonic() < deadline

        time.sleep(0.1)

    return status


def test_rws_jobs_without_waiting_like_commserver(
    load_program: LoadProgram,
) -> None:
    operator = load_program(RWS_PROGRAM)

    assert rws_home(operator) == DONE
    assert rws_home(operator, "right") == DONE

    #
    # Both arms started at once, then polled, as
    # rapid/tests/handover.py does over the socket.
    #
    started = {"accepted": True, "message": "STARTED"}

    body = {"arm": "left", "wait": False, **dict(zip(("x", "y", "z"), TARGET))}

    assert expect(operator.post("/robot/rws/move", json=body), 200) == started

    body = {"arm": "right", "wait": False, **dict(zip(("x", "y", "z"), RIGHT_TARGET))}

    assert expect(operator.post("/robot/rws/move", json=body), 200) == started

    assert expect(operator.get("/robot/rws/status", params={"arm": "left"}), 200)["message"] == "BUSY"

    #
    # A new job for a busy arm is refused (CommServer: ERR busy).
    #
    response = operator.post("/robot/rws/gripper", json={"arm": "left", "action": "grip", "wait": False})

    assert expect(response, 409)["detail"] == "The left arm is busy with an earlier job"

    assert wait_for_status(operator, "left") == DONE
    assert wait_for_status(operator, "right") == DONE

    assert_position(arm_pose(operator), TARGET)
    assert_position(arm_pose(operator, "right"), RIGHT_TARGET)

    body = {"arm": "left", "wait": False, **dict(zip(("x", "y", "z"), UNREACHABLE))}

    assert expect(operator.post("/robot/rws/move", json=body), 200) == started

    assert wait_for_status(operator, "left") == MOVE_FAILED


def test_rws_move_needs_running_rapid(
    load_program: LoadProgram,
) -> None:
    operator = load_program(RWS_PROGRAM)

    expect(operator.post("/robot/rapid/stop"), 200)

    detail = expect(operator.post("/robot/rws/home"), 409)["detail"]

    assert detail == "RAPID is not running"


def send_command(
    client: Client,
    command: str,
) -> dict:
    return expect(client.post("/robot/command", json={"command": command}), 200)


def run_command(
    client: Client,
    command: str,
) -> dict:
    """Start a job like rapid/tests/handover.py and wait for it."""

    started = send_command(client, command)

    if started["message"] != "STARTED":
        return started

    deadline = time.monotonic() + 30

    while (status := send_command(client, "STATUS"))["message"] == "BUSY":
        assert time.monotonic() < deadline

        time.sleep(0.1)

    return status


def test_legacy_tcp_commands_move_the_virtual_robot(
    load_program: LoadProgram,
) -> None:
    operator = load_program(TCP_PROGRAM)

    #
    # The socket server needs a moment after start to listen.
    #
    deadline = time.monotonic() + 15

    while True:
        result = send_command(operator, "HOME")

        if result["accepted"] or time.monotonic() > deadline:
            break

        assert result["message"].startswith("Robot connection error"), result

        time.sleep(0.5)

    assert result == {"accepted": True, "message": "STARTED"}

    while (status := send_command(operator, "STATUS"))["message"] == "BUSY":
        time.sleep(0.1)

    assert status == DONE

    assert run_command(operator, TARGET_COMMAND) == DONE

    assert_position(arm_pose(operator), TARGET)

    assert run_command(operator, "1,2") == {"accepted": False, "message": "ERR bad format"}

    assert run_command(operator, "900,0,300") == MOVE_FAILED

    assert_position(arm_pose(operator), TARGET)
