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
from contextlib import contextmanager
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
UNREACHABLE = (780.0, 0.0, 300.0)

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
def admin(
    robotstudio_gateway: Gateway,
) -> Iterator[Client]:
    """The admin, holding control, with RAPID stopped."""

    client = robotstudio_gateway.client("admin")

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


def test_gripper_position_without_smartgripper(
    robotstudio_gateway: Gateway,
) -> None:
    #
    # The virtual controller has no SmartGripper, so no position
    # signal (the real YuMi has hand_ActualPosition_L/_R).
    #
    for arm in ("left", "right"):
        detail = expect(robotstudio_gateway.client("diagnostics").get(f"/robot/arms/{arm}/gripper"), 404)["detail"]

        assert detail == f"The controller has no SmartGripper on the {arm} arm"


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
    admin: Client,
) -> None:
    path = f"/robot/rapid/tasks/{TEST_TASK}/modules/{TEST_MODULE}"

    try:
        rapid = expect(admin.put(path, json={"source": TEST_SOURCE}), 200)

        assert TEST_MODULE in loaded(rapid, TEST_TASK)

        rapid = expect(admin.put(path, json={"source": TEST_SOURCE}), 200)

        assert loaded(rapid, TEST_TASK).count(TEST_MODULE) == 1

    finally:
        rapid = expect(admin.delete(path), 200)

    assert TEST_MODULE not in loaded(rapid, TEST_TASK)

    expect(admin.delete(path), 404)


def test_unknown_task(
    admin: Client,
) -> None:
    expect(
        admin.put(
            f"/robot/rapid/tasks/T_NOPE/modules/{TEST_MODULE}",
            json={"source": TEST_SOURCE},
        ),
        404,
    )


def test_modules_cannot_change_while_running(
    admin: Client,
) -> None:
    rapid = expect(admin.post("/robot/rapid/start", json={"reset_program_pointer": True}), 200)

    assert rapid["execution_state"] == "running"

    expect(
        admin.put(
            f"/robot/rapid/tasks/{TEST_TASK}/modules/{TEST_MODULE}",
            json={"source": TEST_SOURCE},
        ),
        409,
    )

    rapid = expect(admin.post("/robot/rapid/stop"), 200)

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


def save_program(
    admin: Client,
) -> dict[str, dict[str, str]]:
    """The program modules of the arm tasks: task -> module -> source."""

    controller = Controller()

    rapid = expect(admin.get("/robot/rapid"), 200)

    return {
        task: {
            module: controller.save_module(task, module)
            for module in loaded(rapid, task)
        }
        for task in TASKS
    }


def replace_program(
    admin: Client,
    program: dict[str, dict[str, str]],
) -> None:
    """Unload the arm tasks' modules and load these (RAPID stopped)."""

    rapid = expect(admin.get("/robot/rapid"), 200)

    for task in TASKS:
        for module in loaded(rapid, task):
            path = f"/robot/rapid/tasks/{task}/modules/{module}"

            #
            # Right after a stop the virtual controller sometimes
            # still refuses an unload ("current PGM state") although
            # it reports RAPID stopped; it accepts it moments later.
            #
            deadline = time.monotonic() + 5

            while (
                (response := admin.delete(path)).status_code == 409
                and "PGM state" in response.text
                and time.monotonic() < deadline
            ):
                time.sleep(0.5)

            expect(response, 200)

        for module, source in program[task].items():
            expect(
                admin.put(f"/robot/rapid/tasks/{task}/modules/{module}", json={"source": source}),
                200,
            )


def program_sources(
    program: dict[str, list[Path]],
) -> dict[str, dict[str, str]]:
    return {
        task: dict(module_source(path) for path in paths)
        for task, paths in program.items()
    }


@pytest.fixture()
def load_program(
    admin: Client,
) -> Iterator[LoadProgram]:
    """Load and start a program; restore the saved one afterwards."""

    backup = save_program(admin)

    def load(
        program: dict[str, list[Path]],
    ) -> Client:
        replace_program(admin, program_sources(program))

        expect(admin.post("/robot/rapid/start", json={"reset_program_pointer": True}), 200)

        return admin

    try:
        yield load

    finally:
        admin.post("/robot/rapid/stop")

        replace_program(admin, backup)


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
    admin = load_program(RWS_PROGRAM)

    #
    # Without a body /home homes the left arm.
    #
    assert expect(admin.post("/robot/rws/home"), 200) == DONE

    assert rws_move(admin, TARGET, TARGET_ROTATION) == DONE

    pose = arm_pose(admin)

    assert_position(pose, TARGET)

    rx, ry, rz = TARGET_ROTATION

    assert same_orientation(pose, orient_zyx(rz, ry, rx)), pose

    #
    # Without a rotation the orientation is kept.
    #
    second = (300.0, 150.0, 280.0)

    assert rws_move(admin, second) == DONE

    moved = arm_pose(admin)

    assert_position(moved, second)
    assert same_orientation(moved, tuple(pose[q] for q in ("q1", "q2", "q3", "q4"))), moved

    #
    # The arm program refuses what it cannot reach; the gateway
    # refuses what is outside its safe zone without sending it.
    #
    assert rws_move(admin, UNREACHABLE) == MOVE_FAILED

    assert rws_move(admin, (1500.0, 0.0, 300.0)) == OUTSIDE_SAFE_ZONE

    assert_position(arm_pose(admin), second)


def test_rws_path_moves_through_every_point(
    load_program: LoadProgram,
) -> None:
    operator = load_program(RWS_PROGRAM)

    assert expect(operator.post("/robot/rws/home"), 200) == DONE

    rx, ry, rz = TARGET_ROTATION
    above = (TARGET[0], TARGET[1], TARGET[2] + 40.0)
    points = [
        {**dict(zip(("x", "y", "z"), above)), "rx": rx, "ry": ry, "rz": rz, "speed": 200},
        {**dict(zip(("x", "y", "z"), TARGET)), "rx": rx, "ry": ry, "rz": rz, "linear": True, "speed": 50},
    ]

    #
    # The whole arrays go over RWS in one write each, and RAPID
    # blends through the first point and stops at the last.
    #
    body = {"points": points, "zone": 10, "max_acceleration": 2.0, "acceleration": 60, "ramp": 40}

    assert expect(operator.post("/robot/rws/path", json=body), 200) == DONE

    pose = arm_pose(operator)

    assert_position(pose, TARGET)
    assert same_orientation(pose, orient_zyx(rz, ry, rx)), pose

    #
    # One unreachable point refuses the whole path before moving.
    #
    bad = {"points": [{**points[0]}, {"x": UNREACHABLE[0], "y": UNREACHABLE[1], "z": UNREACHABLE[2],
                                      "rx": rx, "ry": ry, "rz": rz}]}

    assert expect(operator.post("/robot/rws/path", json=bad), 200) == MOVE_FAILED

    assert_position(arm_pose(operator), TARGET)

    #
    # A single move afterwards is still a single move.
    #
    assert rws_move(operator, above) == DONE

    assert_position(arm_pose(operator), above)


def test_rws_moves_the_right_arm(
    load_program: LoadProgram,
) -> None:
    admin = load_program(RWS_PROGRAM)

    left_before = arm_pose(admin)

    assert rws_home(admin, "right") == DONE
    assert rws_move(admin, RIGHT_TARGET, arm="right") == DONE

    assert_position(arm_pose(admin, "right"), RIGHT_TARGET)

    #
    # The left arm was not touched.
    #
    assert_position(arm_pose(admin), tuple(left_before[axis] for axis in "xyz"))


def test_rws_gripper(
    load_program: LoadProgram,
) -> None:
    admin = load_program(RWS_PROGRAM)

    for action in ("grip", "open"):
        result = admin.post("/robot/rws/gripper", json={"arm": "left", "action": action})

        assert expect(result, 200) == DONE


def stub_gripper(
    controller: "Controller",
    arm: str,
) -> tuple[float, float]:
    """Opening (mm) and force (N) the arm's last SmartGripper call
    asked for, as rapid/sim/GripperStub.mod keeps them."""

    task = "T_ROB_L" if arm == "left" else "T_ROB_R"

    return (
        float(controller.read_symbol(f"{task}/GripperStub", "stub_pos")),
        float(controller.read_symbol(f"{task}/GripperStub", "stub_force")),
    )


def test_rws_gripper_position_and_force(
    load_program: LoadProgram,
) -> None:
    operator = load_program(RWS_PROGRAM)
    controller = Controller()

    def gripper(
        **body,
    ) -> dict:
        return expect(operator.post("/robot/rws/gripper", json=body), 200)

    #
    # move reaches g_MoveTo with the opening, in both directions.
    #
    assert gripper(arm="left", action="move", position=12.5) == DONE
    assert stub_gripper(controller, "left")[0] == pytest.approx(12.5)

    assert gripper(arm="left", action="move", position=3) == DONE
    assert stub_gripper(controller, "left")[0] == pytest.approx(3)

    assert gripper(arm="left", action="move", position=25) == DONE
    assert stub_gripper(controller, "left")[0] == pytest.approx(25)

    #
    # grip reaches g_GripIn with the force; without one, 20 N.
    #
    assert gripper(arm="left", action="grip", force=4.5) == DONE
    assert stub_gripper(controller, "left") == (pytest.approx(0), pytest.approx(4.5))
    assert float(controller.read_symbol("T_ROB_L", "grip_force_left")) == pytest.approx(4.5)

    assert gripper(arm="left", action="grip") == DONE
    assert stub_gripper(controller, "left")[1] == pytest.approx(20)

    #
    # The right arm has its own; the left one is not touched.
    #
    assert gripper(arm="right", action="move", position=18, wait=False)["message"] == "STARTED"

    assert wait_for_status(operator, "right") == DONE
    assert stub_gripper(controller, "right")[0] == pytest.approx(18)
    assert stub_gripper(controller, "left")[0] == pytest.approx(0)

    #
    # The gripper does not move the arm.
    #
    before = arm_pose(operator)

    assert gripper(arm="left", action="move", position=10) == DONE

    assert_position(arm_pose(operator), tuple(before[axis] for axis in "xyz"))


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
    admin = load_program(RWS_PROGRAM)
    controller = Controller()

    low = TARGET
    high = (TARGET[0], TARGET[1], TARGET[2] + 60.0)

    assert rws_move(admin, low) == DONE

    #
    # 60 mm: about 3 s at 20 mm/s, well under 1 s at 400 mm/s.
    #
    slow = timed_move(admin, high, speed=20)

    assert controller.read_symbol("T_ROB_L", "move_speed_left").startswith("[20,")

    fast = timed_move(admin, low, speed=400, acceleration=50, ramp=40)

    assert slow > 2.5, slow
    assert fast < slow / 2, (slow, fast)

    assert_position(arm_pose(admin), low)

    assert controller.read_symbol("T_ROB_L", "move_speed_left").startswith("[400,")
    assert float(controller.read_symbol("T_ROB_L", "move_acc_left")) == 50
    assert float(controller.read_symbol("T_ROB_L", "move_ramp_left")) == 40

    #
    # Without a speed: the default, v50 and full acceleration.
    #
    assert rws_home(admin) == DONE

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
    admin = load_program(RWS_PROGRAM)

    assert rws_move(admin, TARGET) == DONE

    responses = {}

    homing = threading.Thread(
        target=lambda: responses.update(home=admin.post("/robot/rws/home")),
    )

    homing.start()

    #
    # Homing from TARGET takes several seconds at the default
    # 50 mm/s.
    #
    time.sleep(0.5)

    response = admin.post("/robot/rws/move", json=dict(zip(("x", "y", "z"), TARGET)))

    #
    # The other arm is independent.
    #
    right = rws_home(admin, "right")

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
    admin = load_program(RWS_PROGRAM)

    assert rws_home(admin) == DONE
    assert rws_home(admin, "right") == DONE

    #
    # Both arms started at once, then polled, as
    # rapid/tests/handover.py does over the socket.
    #
    started = {"accepted": True, "message": "STARTED"}

    body = {"arm": "left", "wait": False, **dict(zip(("x", "y", "z"), TARGET))}

    assert expect(admin.post("/robot/rws/move", json=body), 200) == started

    body = {"arm": "right", "wait": False, **dict(zip(("x", "y", "z"), RIGHT_TARGET))}

    assert expect(admin.post("/robot/rws/move", json=body), 200) == started

    assert expect(admin.get("/robot/rws/status", params={"arm": "left"}), 200)["message"] == "BUSY"

    #
    # A new job for a busy arm is refused (CommServer: ERR busy).
    #
    response = admin.post("/robot/rws/gripper", json={"arm": "left", "action": "grip", "wait": False})

    assert expect(response, 409)["detail"] == "The left arm is busy with an earlier job"

    assert wait_for_status(admin, "left") == DONE
    assert wait_for_status(admin, "right") == DONE

    assert_position(arm_pose(admin), TARGET)
    assert_position(arm_pose(admin, "right"), RIGHT_TARGET)

    body = {"arm": "left", "wait": False, **dict(zip(("x", "y", "z"), UNREACHABLE))}

    assert expect(admin.post("/robot/rws/move", json=body), 200) == started

    assert wait_for_status(admin, "left") == MOVE_FAILED


def test_rws_move_needs_running_rapid(
    load_program: LoadProgram,
) -> None:
    admin = load_program(RWS_PROGRAM)

    expect(admin.post("/robot/rapid/stop"), 200)

    detail = expect(admin.post("/robot/rws/home"), 409)["detail"]

    assert detail == "RAPID is not running"


# ================================================================
# Motion queue (C++ gateway only)
# ================================================================
#
# The model tiers act here, not the admin: queue_program loads the
# RWS program with the admin's lease and gives it back, so primary
# and secondary can take turns. Test gateways lease for 2 s.
#

QUEUE_TIMEOUT_SECONDS = 120

SECOND = (300.0, 150.0, 280.0)

#
# TARGET <-> SECOND at this speed take about 6 s each, long enough
# for a lease to lapse and change hands meanwhile.
#
SLOW_MM_S = 10.0


def queue_move(
    name: str,
    position: tuple[float, float, float],
    *,
    arm: str = "left",
    rotation: tuple[float, float, float] | None = None,
    speed: float | None = None,
    group: str | None = None,
) -> dict:
    body: dict = {"arm": arm, **dict(zip(("x", "y", "z"), position))}

    if rotation is not None:
        body.update(zip(("rx", "ry", "rz"), rotation))

    if speed is not None:
        body["speed"] = speed

    step = {"name": name, "type": "move", "body": body}

    if group is not None:
        step["group"] = group

    return step


QUEUE_HOME = {"name": "home", "type": "home"}

SLOW_QUEUE = [
    queue_move("to target", TARGET),
    queue_move("slow 1", SECOND, speed=SLOW_MM_S),
    queue_move("slow 2", TARGET, speed=SLOW_MM_S),
    queue_move("slow 3", SECOND, speed=SLOW_MM_S),
]


def wait_for_queue(
    client: Client,
) -> dict:
    """The queue once it no longer runs."""

    deadline = time.monotonic() + QUEUE_TIMEOUT_SECONDS

    while (queue := expect(client.get("/robot/rws/queue"), 200))["state"] == "running":
        assert time.monotonic() < deadline, queue

        time.sleep(0.2)

    return queue


@contextmanager
def take_control(
    client: Client,
) -> Iterator[None]:
    """Acquire the lease once it is free (its holder stopped
    renewing), renew it until preempted or the block ends, then
    release it."""

    deadline = time.monotonic() + 10

    while client.post("/control/acquire").status_code != 200:
        assert time.monotonic() < deadline, "the lease did not become free"

        time.sleep(0.2)

    stop = threading.Event()

    def renew() -> None:
        while not stop.wait(0.5):
            if client.post("/control/renew").status_code != 200:
                return

    thread = threading.Thread(target=renew, daemon=True)

    thread.start()

    try:
        yield

    finally:
        stop.set()
        thread.join()

        client.post("/control/release")


def job_states(
    queue: dict,
) -> list[str]:
    return [step["state"] for step in queue["steps"]]


@pytest.fixture()
def queue_program(
    robotstudio_gateway: Gateway,
) -> Iterator[Gateway]:
    """The RWS program running, both arms home and the lease free;
    the saved program back afterwards (RAPID stopped)."""

    admin = robotstudio_gateway.client("admin")

    with admin.control():
        expect(admin.post("/robot/rapid/stop"), 200)

        backup = save_program(admin)

    try:
        #
        # Inside the try, so the saved program comes back also when
        # this one does not load or start.
        #
        with admin.control():
            replace_program(admin, program_sources(RWS_PROGRAM))

            expect(admin.post("/robot/rapid/start", json={"reset_program_pointer": True}), 200)

            assert rws_home(admin) == DONE
            assert rws_home(admin, "right") == DONE

        yield robotstudio_gateway

    finally:
        wait_for_queue(admin)

        #
        # The admin outranks whoever holds the lease now.
        #
        with admin.control():
            admin.delete("/robot/rws/queue")
            admin.post("/robot/rapid/stop")

            replace_program(admin, backup)


@pytest.mark.cpp_only
def test_queue_runs_every_job_and_refuses_others_meanwhile(
    queue_program: Gateway,
) -> None:
    primary = queue_program.client("primary")
    diagnostics = queue_program.client("diagnostics")

    steps = [
        queue_move("approach", TARGET, rotation=TARGET_ROTATION),
        {"name": "grip", "type": "gripper", "body": {"action": "grip", "force": 10}},
        queue_move("left aside", SECOND, group="both"),
        queue_move("right out", RIGHT_TARGET, arm="right", group="both"),
        {"name": "open", "type": "gripper", "body": {"action": "open"}},
        {"name": "home left", "type": "home", "group": "home"},
        {"name": "home right", "type": "home", "body": {"arm": "right"}, "group": "home"},
    ]

    with primary.control():
        queue = expect(primary.post("/robot/rws/queue", json={"steps": steps}), 202)

        assert queue["state"] == "running"
        assert queue["owner"] == "primary"
        assert queue["length"] == len(steps)
        assert [step["name"] for step in queue["steps"]] == [step["name"] for step in steps]
        assert queue["steps"][3] == {
            "name": "right out",
            "type": "move",
            "arm": "right",
            "group": "both",
            "state": "pending",
            "message": None,
        }

        #
        # Nothing gets in between its jobs, not even its owner.
        #
        detail = expect(primary.post("/robot/rws/move", json=dict(zip(("x", "y", "z"), TARGET))), 409)["detail"]

        assert detail.startswith("A queue is running (owner primary)")

        detail = expect(primary.post("/robot/rws/queue", json={"steps": [QUEUE_HOME]}), 409)["detail"]

        assert detail.startswith("A queue is running (owner primary, job ")

        assert expect(diagnostics.get("/robot/rws/queue"), 200)["state"] == "running"

        controller = expect(diagnostics.get("/health"), 200)["robot_controller"]

        assert controller["queue_state"] == "running"
        assert controller["move_in_progress"] is True

        queue = wait_for_queue(primary)

    assert queue["state"] == "done", queue
    assert queue["step"] is None
    assert job_states(queue) == ["done"] * len(steps)
    assert {step["message"] for step in queue["steps"]} == {"DONE"}
    assert queue["finished_at"] is not None


@pytest.mark.cpp_only
def test_running_queue_outlives_its_owner_and_preemption(
    queue_program: Gateway,
) -> None:
    primary = queue_program.client("primary")
    secondary = queue_program.client("secondary")
    diagnostics = queue_program.client("diagnostics")

    expect(primary.post("/control/acquire"), 200)

    expect(primary.post("/robot/rws/queue", json={"steps": SLOW_QUEUE}), 202)

    #
    # primary "crashes": it stops renewing, the lease lapses and the
    # next tier takes it. The queue goes on.
    #
    with take_control(secondary):
        queue = expect(secondary.get("/robot/rws/queue"), 200)

        assert queue["state"] == "running"
        assert queue["owner"] == "primary"

        detail = expect(secondary.post("/robot/rws/queue", json={"steps": [QUEUE_HOME]}), 409)["detail"]

        assert detail.startswith("A queue is running (owner primary")

        expect(secondary.post("/robot/rws/home"), 409)

        #
        # primary comes back and preempts secondary: still the same
        # queue, running.
        #
        expect(primary.post("/control/acquire"), 200)

        assert expect(diagnostics.get("/robot/rws/queue"), 200)["state"] == "running"

        assert expect(diagnostics.get("/control"), 200)["client_id"] == "primary"

    queue = wait_for_queue(diagnostics)

    assert queue["state"] == "done", queue
    assert job_states(queue) == ["done"] * len(SLOW_QUEUE)

    assert_position(arm_pose(diagnostics), SECOND)

    #
    # done refuses nothing: the lease holder queues the next.
    #
    with take_control(primary):
        expect(primary.post("/robot/rws/queue", json={"steps": [QUEUE_HOME]}), 202)

        assert wait_for_queue(primary)["state"] == "done"


@pytest.mark.cpp_only
def test_new_lease_holder_cancels_and_clears_the_queue(
    queue_program: Gateway,
) -> None:
    primary = queue_program.client("primary")
    secondary = queue_program.client("secondary")

    expect(primary.post("/control/acquire"), 200)

    expect(primary.post("/robot/rws/queue", json={"steps": SLOW_QUEUE}), 202)

    with take_control(secondary):
        #
        # The owner no longer holds the lease, so it cannot cancel.
        #
        expect(primary.delete("/robot/rws/queue"), 409)

        cancelling = expect(secondary.delete("/robot/rws/queue"), 200)

        assert cancelling["state"] == "running"
        assert cancelling["cancel_requested"] is True

        queue = wait_for_queue(secondary)

        #
        # The job running at the cancel finished; the rest was
        # never sent.
        #
        states = job_states(queue)
        stop = states.index("skipped")

        assert queue["state"] == "cancelled", queue
        assert queue["step"] == stop
        assert states == ["done"] * stop + ["skipped"] * (len(states) - stop)

        stopped_at = {"to target": TARGET, "slow 1": SECOND, "slow 2": TARGET}

        assert_position(arm_pose(secondary), stopped_at[queue["steps"][stop - 1]["name"]])

        #
        # Part way through the plan: no new queue until cleared,
        # single jobs (recovery) allowed.
        #
        detail = expect(secondary.post("/robot/rws/queue", json={"steps": [QUEUE_HOME]}), 409)["detail"]

        assert "clear it first" in detail

        assert rws_home(secondary) == DONE

        assert expect(secondary.delete("/robot/rws/queue"), 200)["state"] == "idle"

        expect(secondary.post("/robot/rws/queue", json={"steps": [QUEUE_HOME]}), 202)

        assert wait_for_queue(secondary)["state"] == "done"


@pytest.mark.cpp_only
def test_failed_queue_blocks_new_queues_until_cleared(
    queue_program: Gateway,
) -> None:
    primary = queue_program.client("primary")

    steps = [
        queue_move("approach", TARGET),
        queue_move("unreachable", UNREACHABLE),
        QUEUE_HOME,
    ]

    with primary.control():
        expect(primary.post("/robot/rws/queue", json={"steps": steps}), 202)

        queue = wait_for_queue(primary)

        assert queue["state"] == "failed", queue
        assert queue["step"] == 1
        assert job_states(queue) == ["done", "failed", "skipped"]
        assert queue["steps"][1]["message"] == "ERR move failed"
        assert queue["message"] == "Job 2 (unreachable): ERR move failed"

        assert_position(arm_pose(primary), TARGET)

        detail = expect(primary.post("/robot/rws/queue", json={"steps": [QUEUE_HOME]}), 409)["detail"]

        assert detail == "The last queue failed at job 2 of 3; clear it first (DELETE /robot/rws/queue)"

        assert rws_home(primary) == DONE

        assert expect(primary.delete("/robot/rws/queue"), 200)["state"] == "idle"

        expect(primary.post("/robot/rws/queue", json={"steps": [QUEUE_HOME]}), 202)

        assert wait_for_queue(primary)["state"] == "done"


# ================================================================
# Gateway watchdog: the arms go home without the gateway
# ================================================================

#
# Short, so the test does not wait long; the Lynx uses 5.
#
HEARTBEAT_TIMEOUT_SECONDS = 3.0

ARMS = ("left", "right")


@contextmanager
def watchdog_gateway(
    directory: Path,
) -> Iterator[Gateway]:
    """A second gateway on the virtual controller, feeding the arm
    tasks' watchdog: each one is a gateway (re)start to RAPID."""

    directory.mkdir()

    gateway = Gateway(
        directory,
        "strict",
        {
            "GATEWAY_CAMERA_PROVIDER": "mock",
            "GATEWAY_ROBOT_PROVIDER": "mock",
            "GATEWAY_ROBOT_HOST": "127.0.0.1",
            "GATEWAY_RWS_PORT": str(ROBOTSTUDIO_RWS_PORT),
            "GATEWAY_RWS_HEARTBEAT_TIMEOUT": str(HEARTBEAT_TIMEOUT_SECONDS),
        },
    )

    gateway.start()

    try:
        yield gateway

    finally:
        gateway.stop()


def arm_symbol(
    controller: Controller,
    name: str,
    arm: str,
) -> str:
    return controller.read_symbol("T_ROB_L" if arm == "left" else "T_ROB_R", f"{name}_{arm}")


def wait_for_auto_home(
    controller: Controller,
    arms: tuple[str, ...] = ARMS,
) -> None:
    """Until the arms went home on their own and are idle again."""

    deadline = time.monotonic() + 30

    while not all(
        arm_symbol(controller, "auto_home", arm) == "TRUE"
        and arm_symbol(controller, "move_request", arm) == "FALSE"
        for arm in arms
    ):
        assert time.monotonic() < deadline, "the arms did not go home on their own"

        time.sleep(0.1)


def watchdog_health(
    gateway: Gateway,
) -> dict:
    return expect(gateway.client("diagnostics").get("/health"), 200)["robot_controller"]["watchdog"]


@pytest.mark.cpp_only
def test_arms_go_home_when_the_gateway_is_lost_or_restarts(
    queue_program: Gateway,
    tmp_path: Path,
) -> None:
    controller = Controller()
    admin = queue_program.client("admin")

    #
    # queue_program's gateway writes no heartbeat, so far the
    # watchdog is off; both arms are home.
    #
    home = {arm: arm_pose(admin, arm) for arm in ARMS}

    def assert_home() -> None:
        for arm in ARMS:
            assert_position(arm_pose(admin, arm), tuple(home[arm][axis] for axis in "xyz"))

    def move_away() -> None:
        assert rws_move(admin, TARGET) == DONE
        assert rws_move(admin, RIGHT_TARGET, arm="right") == DONE

    def assert_away() -> None:
        assert_position(arm_pose(admin), TARGET)
        assert_position(arm_pose(admin, "right"), RIGHT_TARGET)

    with admin.control():
        #
        # A gateway start sends the arms home (here from home).
        #
        with watchdog_gateway(tmp_path / "first") as first:
            wait_for_auto_home(controller)

            health = watchdog_health(first)

            assert health["enabled"] is True
            assert health["connected"] is True
            assert health["timeout_seconds"] == HEARTBEAT_TIMEOUT_SECONDS
            assert health["error"] is None

            #
            # A job clears auto_home; while the heartbeat runs the
            # arms stay where they were sent.
            #
            move_away()

            for arm in ARMS:
                assert arm_symbol(controller, "auto_home", arm) == "FALSE"

            time.sleep(HEARTBEAT_TIMEOUT_SECONDS + 1.5)

            assert_away()
            assert watchdog_health(first)["auto_home"] == {"left": False, "right": False}

            lost = time.monotonic()

        #
        # The gateway is gone: after the timeout the arms go home.
        #
        wait_for_auto_home(controller)

        assert time.monotonic() - lost >= HEARTBEAT_TIMEOUT_SECONDS - 1.5

        assert_home()

        #
        # Once only: sent away again without a heartbeat, they stay.
        #
        move_away()

        time.sleep(HEARTBEAT_TIMEOUT_SECONDS + 1.5)

        assert_away()

        #
        # A gateway restart sends them home at once, heartbeat or not.
        #
        with watchdog_gateway(tmp_path / "second") as second:
            wait_for_auto_home(controller)

            assert_home()

            deadline = time.monotonic() + 5

            while watchdog_health(second)["auto_home"] != {"left": True, "right": True}:
                assert time.monotonic() < deadline

                time.sleep(0.2)

            #
            # Both arms holding (a handover): neither goes home.
            #
            move_away()

            for arm in ARMS:
                assert expect(admin.post("/robot/rws/gripper", json={"arm": arm, "action": "grip"}), 200) == DONE

        with watchdog_gateway(tmp_path / "third"):
            time.sleep(HEARTBEAT_TIMEOUT_SECONDS / 2)

            for arm in ARMS:
                assert arm_symbol(controller, "holding", arm) == "TRUE"
                assert arm_symbol(controller, "auto_home", arm) == "FALSE"

            assert_away()

            #
            # The right one lets go: a lost gateway now sends both
            # home, the left one with what it holds.
            #
            assert expect(admin.post("/robot/rws/gripper", json={"arm": "right", "action": "open"}), 200) == DONE

        wait_for_auto_home(controller)

        assert_home()

        assert arm_symbol(controller, "holding", "left") == "TRUE"
        assert arm_symbol(controller, "holding", "right") == "FALSE"

        assert expect(admin.post("/robot/rws/gripper", json={"arm": "left", "action": "open"}), 200) == DONE


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


@pytest.mark.legacy_command
def test_legacy_tcp_commands_move_the_virtual_robot(
    load_program: LoadProgram,
) -> None:
    admin = load_program(TCP_PROGRAM)

    #
    # The socket server needs a moment after start to listen.
    #
    deadline = time.monotonic() + 15

    while True:
        result = send_command(admin, "HOME")

        if result["accepted"] or time.monotonic() > deadline:
            break

        assert result["message"].startswith("Robot connection error"), result

        time.sleep(0.5)

    assert result == {"accepted": True, "message": "STARTED"}

    while (status := send_command(admin, "STATUS"))["message"] == "BUSY":
        time.sleep(0.1)

    assert status == DONE

    assert run_command(admin, TARGET_COMMAND) == DONE

    assert_position(arm_pose(admin), TARGET)

    assert run_command(admin, "1,2") == {"accepted": False, "message": "ERR bad format"}

    assert run_command(admin, "900,0,300") == MOVE_FAILED

    assert_position(arm_pose(admin), TARGET)
