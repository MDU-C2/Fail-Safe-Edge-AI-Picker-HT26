"""Arm pose, RAPID control and robot commands on the RobotStudio
virtual controller (run with --robotstudio).

The tests change the controller's RAPID program. The program
modules loaded before the run are saved at the start and loaded
again at the end, but the program is left stopped.
"""

import re
import time
from pathlib import Path
from typing import Iterator

import pytest
import requests
from requests.auth import HTTPDigestAuth

from gateway.tests.support import ROBOTSTUDIO_RWS_PORT, Client, Gateway, expect


pytestmark = pytest.mark.robotstudio

RAPID_DIR = Path(__file__).resolve().parents[1] / "rapid"

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
# The CommServer program from gateway/rapid, as upload_rapid.py
# loads it.
#
LEGACY_PROGRAM = {
    "T_ROB_L": ["LegacyShared", "LegacyMotion"],
    "T_ROB_R": ["LegacyShared", "LegacyCommServer"],
}

#
# Inside the CommServer safe zone, with the calibration tilt.
#
TARGET = (325.0, 200.0, 300.0)
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
# CommServer program, end to end
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


def legacy_source(
    module: str,
) -> tuple[str, str]:
    """Return the declared module name and the source."""

    text = (RAPID_DIR / f"{module}.mod").read_text(encoding="utf-8")

    #
    # The module names inside the files differ from the file
    # names (e.g. MODULE CommServer), so read them from the file.
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


@pytest.fixture()
def legacy_program(
    operator: Client,
) -> Iterator[Client]:
    """Load and start the CommServer program, restore afterwards."""

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

    replace_program(
        {
            task: dict(legacy_source(module) for module in modules)
            for task, modules in LEGACY_PROGRAM.items()
        }
    )

    try:
        expect(operator.post("/robot/rapid/start", json={"reset_program_pointer": True}), 200)

        yield operator

    finally:
        operator.post("/robot/rapid/stop")

        replace_program(backup)


def send_command(
    client: Client,
    command: str,
) -> dict:
    return expect(client.post("/robot/command", json={"command": command}), 200)


def test_commands_move_the_virtual_robot(
    legacy_program: Client,
) -> None:
    #
    # The socket server needs a moment after start to listen.
    #
    deadline = time.monotonic() + 15

    while True:
        result = send_command(legacy_program, "HOME")

        if result["accepted"] or time.monotonic() > deadline:
            break

        assert result["message"].startswith("Robot connection error"), result

        time.sleep(0.5)

    assert result == {"accepted": True, "message": "DONE"}

    result = send_command(legacy_program, TARGET_COMMAND)

    assert result == {"accepted": True, "message": "DONE"}

    pose = expect(
        legacy_program.get(
            "/robot/arms/left/pose",
            params={"tool": "tool0", "wobj": "wobj0"},
        ),
        200,
    )

    for axis, expected in zip(("x", "y", "z"), TARGET):
        assert pose[axis] == pytest.approx(expected, abs=POSITION_TOLERANCE_MM), pose

    result = send_command(legacy_program, "1000,0,0")

    assert result == {"accepted": False, "message": "ERR outside safe zone"}
