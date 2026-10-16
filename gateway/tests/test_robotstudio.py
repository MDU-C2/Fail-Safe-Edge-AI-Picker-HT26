"""Arm pose, RAPID control and moves on the RobotStudio virtual
controller (run with --robotstudio).

Moves are tested both ways: over RWS with the program in
gateway/rapid (POST /robot/rws/*), and through the legacy tcp robot
provider with the socket server fixture in tests/rapid
(POST /robot/command).

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

RAPID_DIR = Path(__file__).resolve().parents[1] / "rapid"
FIXTURE_DIR = Path(__file__).resolve().parent / "rapid"

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
# The program in gateway/rapid, as upload_rapid.py loads it.
#
RWS_PROGRAM = {
    "T_ROB_L": [RAPID_DIR / "LegacyShared.mod", RAPID_DIR / "LegacyMotion.mod"],
    "T_ROB_R": [RAPID_DIR / "LegacyShared.mod", RAPID_DIR / "RightArmIdle.mod"],
}

#
# The same with the legacy socket server in the right task.
#
TCP_PROGRAM = {
    "T_ROB_L": [RAPID_DIR / "LegacyShared.mod", RAPID_DIR / "LegacyMotion.mod"],
    "T_ROB_R": [RAPID_DIR / "LegacyShared.mod", FIXTURE_DIR / "LegacyCommServer.mod"],
}

#
# Inside the safe zone, with the calibration tilt.
#
TARGET = (325.0, 200.0, 300.0)
TARGET_ROTATION = (90.0, 0.0, 90.0)
TARGET_COMMAND = "325,200,300,90,0,90"

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
    """Direct RWS access, only to save and restore the program."""

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


def left_pose(
    client: Client,
) -> dict:
    return expect(
        client.get(
            "/robot/arms/left/pose",
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
) -> dict:
    body = dict(zip(("x", "y", "z"), position))

    if rotation is not None:
        body.update(zip(("rx", "ry", "rz"), rotation))

    return expect(client.post("/robot/rws/move", json=body), 200)


DONE = {"accepted": True, "message": "DONE"}


def test_rws_moves_the_virtual_robot(
    load_program: LoadProgram,
) -> None:
    operator = load_program(RWS_PROGRAM)

    assert expect(operator.post("/robot/rws/home"), 200) == DONE

    assert rws_move(operator, TARGET, TARGET_ROTATION) == DONE

    pose = left_pose(operator)

    assert_position(pose, TARGET)

    rx, ry, rz = TARGET_ROTATION

    assert same_orientation(pose, orient_zyx(rz, ry, rx)), pose

    #
    # Without a rotation the orientation is kept.
    #
    second = (300.0, 150.0, 280.0)

    assert rws_move(operator, second) == DONE

    moved = left_pose(operator)

    assert_position(moved, second)
    assert same_orientation(moved, tuple(pose[q] for q in ("q1", "q2", "q3", "q4"))), moved

    #
    # The safe zone is checked by the RAPID program.
    #
    result = rws_move(operator, (1000.0, 0.0, 0.0))

    assert result == {"accepted": False, "message": "ERR outside safe zone"}

    assert_position(left_pose(operator), second)


def test_rws_refuses_a_second_move_while_moving(
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
    # Homing from TARGET takes about two seconds.
    #
    time.sleep(0.5)

    response = operator.post("/robot/rws/move", json=dict(zip(("x", "y", "z"), TARGET)))

    homing.join()

    assert expect(response, 409)["detail"] == "A move is already in progress"
    assert expect(responses["home"], 200) == DONE


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

    assert result == DONE

    assert send_command(operator, TARGET_COMMAND) == DONE

    assert_position(left_pose(operator), TARGET)

    result = send_command(operator, "1000,0,0")

    assert result == {"accepted": False, "message": "ERR outside safe zone"}
