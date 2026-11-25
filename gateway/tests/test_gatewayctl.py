"""The operator tool gateway/scripts/gatewayctl.py, run as a subprocess.

Against the mock gateways it checks the identity handling (the token
or development client given, and what it says when that client lacks
the permission), the lease, calibration, camera, events, the smoke
test and tokens. With --robotstudio it drives the RAPID program and
the arms of the virtual controller end to end.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from gateway.tests.support import Gateway, expect, token_for


TOOL = Path(__file__).resolve().parents[1] / "scripts" / "gatewayctl.py"


# ================================================================
# Helpers
# ================================================================

def identity(
    gateway: Gateway,
    client: str | None,
) -> list[str]:
    """The tool's arguments for acting as this client."""

    if client is None:
        return []

    if gateway.security_mode == "strict":
        return ["--token", token_for(client)]

    return ["--client", client]


def run_tool(
    gateway: Gateway,
    client: str | None,
    *arguments: str,
    backups: Path | None = None,
    timeout: float = 120,
) -> subprocess.CompletedProcess:
    environment = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
    }

    environment.pop("GATEWAY_TOKEN", None)
    environment.pop("GATEWAY_URL", None)

    if backups:
        environment["GATEWAYCTL_BACKUP_DIR"] = str(backups)

    return subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--url",
            gateway.api_url,
            *identity(gateway, client),
            *arguments,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=timeout,
    )


def succeeds(
    result: subprocess.CompletedProcess,
) -> str:
    assert result.returncode == 0, f"exit {result.returncode}\n{result.stdout}\n{result.stderr}"

    return result.stdout


def fails(
    result: subprocess.CompletedProcess,
) -> str:
    assert result.returncode == 1, f"exit {result.returncode}\n{result.stdout}\n{result.stderr}"

    return result.stderr


# ================================================================
# Offline
# ================================================================

def test_help_lists_every_command() -> None:
    result = subprocess.run(
        [sys.executable, str(TOOL), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )

    output = succeeds(result)

    for command in (
        "status", "arms", "move", "home", "grip", "open", "gripper-to", "reset-home",
        "rapid", "camera", "calibration", "control", "events", "smoke", "token",
    ):
        assert command in output

    #
    # The table of which client a command needs.
    #
    assert "robot.program + the control lease" in output


def test_token_new_prints_a_matching_entry(
    strict_gateway: Gateway,
) -> None:
    output = succeeds(run_tool(strict_gateway, None, "token", "new", "camera_tool", "--priority", "20", "--permissions", "camera.read"))

    token = output.splitlines()[1]

    entry = json.loads(output.split("restart the gateway after adding it):", 1)[1])

    assert entry == {
        "camera_tool": {
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "permissions": ["camera.read"],
            "control_priority": 20,
        }
    }


# ================================================================
# Identity and permissions
# ================================================================

def test_no_or_unknown_token_says_what_to_give(
    strict_gateway: Gateway,
) -> None:
    for arguments in ([], ["--token", "not-a-token"]):
        result = subprocess.run(
            [sys.executable, str(TOOL), "--url", strict_gateway.api_url, *arguments, "control", "show"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={key: value for key, value in os.environ.items() if key != "GATEWAY_TOKEN"},
            timeout=30,
        )

        error = fails(result)

        assert "HTTP 401" in error
        assert "--token" in error


def test_common_options_work_before_or_after_the_command(
    strict_gateway: Gateway,
) -> None:
    environment = {key: value for key, value in os.environ.items() if key not in ("GATEWAY_TOKEN", "GATEWAY_URL")}

    for arguments in (
        ["--url", strict_gateway.api_url, "--token", token_for("diagnostics"), "control", "show"],
        ["control", "show", "--url", strict_gateway.api_url, "--token", token_for("diagnostics")],
        ["--url", strict_gateway.api_url, "control", "show", "--token", token_for("diagnostics")],
    ):
        result = subprocess.run(
            [sys.executable, str(TOOL), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            timeout=30,
        )

        assert succeeds(result).strip() == "free", arguments

    #
    # One given before the command is not reset by the command's
    # own (suppressed) default.
    #
    result = subprocess.run(
        [sys.executable, str(TOOL), "--token", token_for("diagnostics"), "--url", strict_gateway.api_url, "status"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=30,
    )

    assert "control free" in result.stdout, result.stderr


def test_a_client_without_the_permission_is_told_which_one_is_needed(
    gateway: Gateway,
) -> None:
    error = fails(run_tool(gateway, "diagnostics", "rapid", "stop"))

    assert "HTTP 403" in error
    assert "robot.control" in error

    error = fails(run_tool(gateway, "diagnostics", "calibration", "put", str(TOOL)))

    assert "is not a readable JSON file" in error


def test_status_and_unreachable_controller(
    gateway: Gateway,
) -> None:
    #
    # The mock gateways have no controller, so health is degraded.
    #
    result = run_tool(gateway, "diagnostics", "status")

    assert result.returncode == 1, result.stderr
    assert "robot   unreachable" in result.stdout
    assert "control free" in result.stdout

    error = fails(run_tool(gateway, "diagnostics", "rapid", "status"))

    assert "503" in error
    assert "unreachable" in error


# ================================================================
# Lease and calibration
# ================================================================

def test_control_show_and_release(
    gateway: Gateway,
) -> None:
    assert succeeds(run_tool(gateway, "diagnostics", "control", "show")).strip() == "free"

    primary = gateway.client("primary")

    expect(primary.post("/control/acquire"), 200)

    assert "held by primary" in succeeds(run_tool(gateway, "diagnostics", "control", "show"))

    succeeds(run_tool(gateway, "primary", "control", "release"))

    assert succeeds(run_tool(gateway, "diagnostics", "control", "show")).strip() == "free"


def test_calibration_put_and_get(
    gateway: Gateway,
    tmp_path: Path,
) -> None:
    calibration = {"camera_to_base": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], "note": "gatewayctl"}

    upload = tmp_path / "calibration.json"
    upload.write_text(json.dumps(calibration), encoding="utf-8")

    succeeds(run_tool(gateway, "primary", "calibration", "put", str(upload)))

    #
    # The lease is released afterwards.
    #
    assert expect(gateway.client("diagnostics").get("/control"), 200)["active"] is False

    download = tmp_path / "back.json"

    succeeds(run_tool(gateway, "diagnostics", "calibration", "get", "-o", str(download)))

    assert json.loads(download.read_text(encoding="utf-8")) == calibration

    assert json.loads(succeeds(run_tool(gateway, "diagnostics", "calibration", "get"))) == calibration


def test_lease_held_by_a_higher_client_is_waited_for_then_refused(
    strict_gateway: Gateway,
    tmp_path: Path,
) -> None:
    upload = tmp_path / "calibration.json"
    upload.write_text("{}", encoding="utf-8")

    admin = strict_gateway.client("admin")

    with admin.control():
        error = fails(run_tool(strict_gateway, "primary", "calibration", "put", str(upload)))

    assert "Could not take control" in error


def test_admin_takes_control_from_a_model(
    strict_gateway: Gateway,
    tmp_path: Path,
) -> None:
    upload = tmp_path / "calibration.json"
    upload.write_text("{}", encoding="utf-8")

    primary = strict_gateway.client("primary")

    expect(primary.post("/control/acquire"), 200)

    try:
        output = succeeds(run_tool(strict_gateway, "admin", "calibration", "put", str(upload)))

        assert "Took control from primary" in output

    finally:
        primary.post("/control/release")


# ================================================================
# Camera, events, smoke test
# ================================================================

def test_camera_rgb_and_capture(
    gateway: Gateway,
    tmp_path: Path,
) -> None:
    cv2 = pytest.importorskip("cv2")

    image = tmp_path / "frame.png"

    succeeds(run_tool(gateway, "diagnostics", "camera", "rgb", "-o", str(image)))

    frame = cv2.imread(str(image))

    assert frame is not None and frame.shape == (480, 640, 3)

    output = succeeds(run_tool(gateway, "diagnostics", "camera", "capture", "-o", str(tmp_path / "out"), "--count", "2", "--interval", "0.1"))

    assert "Saved [2]" in output

    for folder in ("rgb", "depth"):
        assert len(list((tmp_path / "out" / folder).iterdir())) == 2

    depth = cv2.imread(str(next((tmp_path / "out" / "depth").iterdir())), cv2.IMREAD_UNCHANGED)

    assert depth.dtype.name == "uint16"


def test_events_prints_what_happens(
    gateway: Gateway,
) -> None:
    results = []

    def watch() -> None:
        results.append(run_tool(gateway, "diagnostics", "events", "--events", "control", "--count", "1", "--json", timeout=30))

    thread = threading.Thread(target=watch)

    thread.start()

    #
    # Until the tool is subscribed; then make an event.
    #
    time.sleep(3)

    primary = gateway.client("primary")

    with primary.control():
        thread.join(30)

    output = succeeds(results[0])

    event = json.loads(output.splitlines()[0])

    assert event["type"] == "control.acquired"
    assert event["data"]["client_id"] == "primary"


def test_smoke_test_passes_on_a_working_gateway(
    gateway: Gateway,
) -> None:
    output = succeeds(run_tool(gateway, "primary", "smoke", "--write-calibration", "--depth-seconds", "5"))

    assert "all checks passed" in output
    assert "control lease released" in output

    output = succeeds(run_tool(gateway, "diagnostics", "smoke", "--read-only"))

    assert "control lease" not in output


# ================================================================
# RobotStudio, end to end
# ================================================================

@pytest.mark.robotstudio
@pytest.mark.cpp_only
def test_rapid_program_and_arms_on_the_virtual_controller(
    robotstudio_gateway: Gateway,
    tmp_path: Path,
) -> None:
    from gateway.tests.test_robotstudio import Controller, loaded, replace_program, save_program

    admin_client = robotstudio_gateway.client("admin")

    with admin_client.control():
        expect(admin_client.post("/robot/rapid/stop"), 200)

        saved = save_program(admin_client)

    def tool(client: str, *arguments: str) -> str:
        return succeeds(run_tool(robotstudio_gateway, client, *arguments, backups=tmp_path / "backups", timeout=180))

    try:
        #
        # The program in rapid/, with the gripper stub (no
        # SmartGripper on the virtual controller); backs up first.
        #
        output = tool("admin", "-y", "rapid", "upload", "--gripper-stub")

        assert "communication: not on this controller, skipped" in output
        assert "RAPID running" in output

        backups = list((tmp_path / "backups").iterdir())

        assert len(backups) == 1

        output = tool("diagnostics", "rapid", "status")

        assert re.search(r"T_ROB_L\s+\w+\s+.*LeftArm", output)
        assert "RAPID running" in output

        #
        # A model client moves the arm; only one value changes.
        #
        output = tool("primary", "home")

        assert "-> DONE" in output

        output = tool("primary", "move", "--x", "325", "--y", "200", "--z", "300", "--speed", "200")

        assert "-> DONE" in output
        assert "After: left arm at x=325.0 y=200.0 z=300.0" in output

        output = tool("primary", "move", "--z", "280", "--no-wait")

        assert "-> STARTED" in output
        assert "STATUS -> DONE" in output
        assert "x=325.0 y=200.0 z=280.0" in output.split("After:", 1)[1]

        output = tool("primary", "grip", "--force", "5", "--arm", "right")

        assert "-> DONE" in output

        assert float(Controller().read_symbol("T_ROB_R/GripperStub", "stub_force")) == 5

        #
        # Out of the gateway's safe zone: refused, exit 1.
        #
        result = run_tool(robotstudio_gateway, "primary", "move", "--x", "900", "--y", "0", "--z", "300")

        assert result.returncode == 1
        assert "ERR outside safe zone" in result.stdout

        #
        # Reset: the variables back to SharedVars' declared values.
        #
        output = tool("admin", "rapid", "reset")

        assert "back to their declared values" in output
        assert "RAPID running" in output

        assert Controller().read_symbol("T_ROB_L", "target_pos_left") == "[0,0,0]"

        #
        # Not the admin: refused, with the permission named.
        #
        error = fails(run_tool(robotstudio_gateway, "primary", "rapid", "reset"))

        assert "robot.program" in error

        output = tool("admin", "-y", "reset-home", "--speed", "300")

        assert "Done: both arms home, both grippers closed" in output

        #
        # Backup, delete one module, restore the backup.
        #
        output = tool("diagnostics", "rapid", "backup", "--task", "T_ROB_L")

        backup = Path(re.search(r"^Backup: (.+)$", output, re.MULTILINE).group(1).strip())

        assert (backup / "T_ROB_L" / "LeftArm.mod").is_file()

        source = (backup / "T_ROB_L" / "SharedVars.mod").read_text(encoding="iso-8859-1")

        assert re.search(r"^\s*MODULE\s+SharedVars", source, re.MULTILINE)

        #
        # GoHomeLeft.mod declares MODULE HomeLeft; backups are named
        # after the module.
        #
        output = tool("admin", "-y", "rapid", "delete", "--task", "T_ROB_L", "HomeLeft")

        assert "T_ROB_L: unloaded HomeLeft" in output

        rapid = expect(admin_client.get("/robot/rapid"), 200)

        assert "HomeLeft" not in loaded(rapid, "T_ROB_L")
        assert rapid["execution_state"] == "stopped"

        output = tool("admin", "-y", "rapid", "restore", str(backup))

        assert "T_ROB_L: loaded HomeLeft.mod" in output
        assert "RAPID running" in output

        output = tool("admin", "rapid", "restart")

        assert "RAPID running (program pointer at main)" in output

        #
        # RAPID stopped mid-job (a Stop or a collision): a plain start
        # would continue that motion, so it is refused; from main the
        # job is dropped and the arm stays where it stopped.
        #
        diagnostics = robotstudio_gateway.client("diagnostics")

        with admin_client.control():
            moving = {"arm": "left", "x": 325, "y": 200, "z": 300, "speed": 10, "wait": False}

            assert expect(admin_client.post("/robot/rws/move", json=moving), 200)["message"] == "STARTED"

            time.sleep(1.5)

            assert expect(admin_client.post("/robot/rapid/stop"), 200)["execution_state"] == "stopped"

        assert expect(diagnostics.get("/robot/rws/status", params={"arm": "left"}), 200)["message"] == "BUSY"

        stopped_at = expect(diagnostics.get("/robot/arms/left/pose"), 200)

        error = fails(run_tool(robotstudio_gateway, "primary", "rapid", "start"))

        assert "left arm has an unfinished job" in error
        assert "--main" in error

        assert expect(diagnostics.get("/robot/rapid"), 200)["execution_state"] == "stopped"

        output = tool("primary", "rapid", "start", "--main")

        assert "RAPID running (program pointer at main)" in output

        assert expect(diagnostics.get("/robot/rws/status", params={"arm": "left"}), 200)["message"] != "BUSY"

        time.sleep(1.0)

        now = expect(diagnostics.get("/robot/arms/left/pose"), 200)

        assert all(abs(now[axis] - stopped_at[axis]) < 1.0 for axis in "xyz"), (stopped_at, now)

        output = tool("primary", "rapid", "stop")

        assert "RAPID stopped" in output

    finally:
        with admin_client.control():
            admin_client.post("/robot/rapid/stop")

            replace_program(admin_client, saved)
