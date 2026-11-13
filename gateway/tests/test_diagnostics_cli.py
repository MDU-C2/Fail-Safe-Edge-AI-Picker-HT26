"""The read-only diagnostics tool in diagnostics/gateway_diag.py."""

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from rich.console import Console

from gateway.tests.support import Gateway, token_for


TOOL = Path(__file__).resolve().parents[2] / "diagnostics" / "gateway_diag.py"

spec = importlib.util.spec_from_file_location("gateway_diag", TOOL)
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


# ================================================================
# Helpers
# ================================================================

def diagnostics_client(
    gateway: Gateway,
    token: str | None = None,
) -> "diag.Gateway":
    return diag.Gateway(
        url=gateway.api_url,
        token=token or token_for("diagnostics"),
        client="diagnostics",
        verify=gateway.verify,
        timeout=10,
    )


def ca_arguments(
    gateway: Gateway,
) -> list[str]:
    """--ca with the test CA for an https gateway."""

    return ["--ca", gateway.verify] if isinstance(gateway.verify, str) else []


def run_tool(
    gateway: Gateway,
    *arguments: str,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--url",
            gateway.api_url,
            "--token",
            token_for("diagnostics"),
            *ca_arguments(gateway),
            *arguments,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "COLUMNS": "140"},
        timeout=60,
    )


def render(
    renderable,
) -> str:
    console = Console(
        file=io.StringIO(),
        width=140,
        height=50,
        record=True,
    )
    console.print(renderable)

    return console.export_text()


# ================================================================
# Polling
# ================================================================

def test_collect_reads_every_endpoint_as_diagnostics(
    gateway: Gateway,
) -> None:
    results = diag.collect(diagnostics_client(gateway))

    assert results["health"].ok
    assert results["health"].data["security_mode"] == gateway.security_mode

    for name in ("control", "camera", "robot"):
        assert results[name].ok, (name, results[name])

    assert results["calibration"].status in (200, 404)

    #
    # The mock gateways point RWS at a closed port, so the
    # RWS-backed endpoints are skipped rather than timed out.
    #
    assert results["health"].data["robot_controller"]["reachable"] is False
    assert "arms" not in results
    assert "rapid" not in results


def test_rejected_token_is_shown(
    strict_gateway: Gateway,
) -> None:
    gateway = diagnostics_client(strict_gateway, token="not-a-token")
    results = diag.collect(gateway)

    assert results["control"].status == 401
    assert "AUTH REJECTED" in render(diag.header_panel(gateway, results, diag.View()))


def test_status_json_and_exit_code(
    gateway: Gateway,
) -> None:
    completed = run_tool(gateway, "status", "--json")
    report = json.loads(completed.stdout)

    #
    # Degraded: the mock gateway has no robot controller.
    #
    assert completed.returncode == 1, completed.stderr
    assert report["health"]["data"]["status"] == "degraded"
    assert report["intrinsics"]["status"] == 200


def test_status_dashboard_renders(
    gateway: Gateway,
) -> None:
    completed = run_tool(gateway, "status")

    assert completed.returncode == 1, completed.stderr
    assert "camera" in completed.stdout
    assert "control & calibration" in completed.stdout
    assert "holder" in completed.stdout


def test_unreachable_gateway_exits_2() -> None:
    gateway = diag.Gateway(
        url="http://127.0.0.1:9/api/v1",
        token=None,
        client="diagnostics",
        verify=True,
        timeout=2,
    )
    results = diag.collect(gateway)

    assert diag.exit_code(results) == 2
    assert "UNREACHABLE" in render(diag.header_panel(gateway, results, diag.View()))


def test_get_prints_json(
    gateway: Gateway,
) -> None:
    completed = run_tool(gateway, "get", "camera/status")

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["connected"] is True


def test_lease_holder_is_shown(
    gateway: Gateway,
) -> None:
    with gateway.client("primary").control():
        results = diag.collect(diagnostics_client(gateway))

    text = render(diag.control_panel(results))

    assert "primary" in text
    assert "300" in text


# ================================================================
# Events
# ================================================================

def test_event_feed_receives_events(
    development_gateway: Gateway,
) -> None:
    gateway = diagnostics_client(development_gateway)
    feed = diag.EventFeed(
        url=gateway.events_url(),
        client="diagnostics",
        events=diag.EVENT_FAMILIES,
        verify=gateway.verify,
    )
    feed.start()

    try:
        deadline = time.monotonic() + 10

        while feed.state != "connected" or not feed.events_received:
            assert time.monotonic() < deadline, feed.state
            time.sleep(0.05)

        development_gateway.client("primary").post("/control/acquire")
        development_gateway.client("primary").post("/control/release")

        def types() -> list[str]:
            return [event.message["type"] for event in feed.events_received]

        while "control.released" not in types():
            assert time.monotonic() < deadline, types()
            time.sleep(0.05)

        assert types()[0] == "subscription.updated"
        assert "control.acquired" in types()

        text = render(diag.events_panel(feed, "development", 12))

        assert "control.acquired" in text
        assert "client_id=primary" in text

    finally:
        feed.stop.set()


def test_event_feed_receives_events_in_strict_mode(
    strict_gateway: Gateway,
) -> None:
    gateway = diagnostics_client(strict_gateway)
    feed = diag.EventFeed(
        url=gateway.events_url(),
        client="diagnostics",
        events=diag.EVENT_FAMILIES,
        verify=gateway.verify,
        token=gateway.token,
    )
    feed.start()

    try:
        deadline = time.monotonic() + 10

        while feed.state != "connected" or not feed.events_received:
            assert time.monotonic() < deadline, feed.state
            time.sleep(0.05)

        admin = strict_gateway.client("admin")
        admin.post("/control/acquire")
        admin.post("/control/release")

        def types() -> list[str]:
            return [event.message["type"] for event in feed.events_received]

        while "control.released" not in types():
            assert time.monotonic() < deadline, types()
            time.sleep(0.05)

        assert "control.acquired" in types()

    finally:
        feed.stop.set()


@pytest.mark.parametrize("token", [None, "not-a-token"], ids=["no-token", "unknown-token"])
def test_event_feed_is_refused_in_strict_mode_without_valid_token(
    strict_gateway: Gateway,
    token: str | None,
) -> None:
    gateway = diagnostics_client(strict_gateway)
    feed = diag.EventFeed(
        url=gateway.events_url(),
        client="diagnostics",
        events=diag.EVENT_FAMILIES,
        verify=gateway.verify,
        token=token,
    )
    feed.start()

    try:
        deadline = time.monotonic() + 10

        while not feed.state.startswith("refused"):
            assert time.monotonic() < deadline, feed.state
            time.sleep(0.05)

    finally:
        feed.stop.set()


def test_events_command_with_unknown_token_is_refused(
    strict_gateway: Gateway,
) -> None:
    completed = subprocess.run(
        [sys.executable, str(TOOL), "--url", strict_gateway.api_url, "--token", "not-a-token", *ca_arguments(strict_gateway), "events"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "COLUMNS": "140"},
        timeout=60,
    )

    assert completed.returncode == 2
    assert "refused" in completed.stderr


# ================================================================
# Rendering with a controller (no RWS in the mock gateways)
# ================================================================

def pose(
    x: float,
) -> dict:
    return {
        "arm": "left",
        "mech_unit": "ROB_L",
        "tool": "tool0",
        "wobj": "wobj0",
        "coordinate": "Base",
        "x": x,
        "y": 100.0,
        "z": 300.0,
        #
        # 180 degrees about x.
        #
        "q1": 0.0,
        "q2": 1.0,
        "q3": 0.0,
        "q4": 0.0,
        "cf1": 0,
        "cf4": 0,
        "cf6": 0,
        "cfx": 4,
        "arm_angle": 0.0,
    }


def test_controller_panels_render() -> None:
    def ok(data: object) -> "diag.Result":
        return diag.Result(path="/", status=200, data=data)

    results = {
        "health": ok(
            {
                "status": "ok",
                "version": "0.3.0",
                "uptime_seconds": 4000,
                "security_mode": "strict",
                "robot_provider": "tcp",
                "control_lease_active": False,
                "camera": {"ok": True, "provider": "oak", "connected": True, "frame_age_seconds": 0.03, "max_frame_age_seconds": 5},
                "robot_controller": {
                    "ok": True,
                    "reachable": True,
                    "controller_state": "motoron",
                    "operation_mode": "AUTO",
                    "rapid_execution_state": "running",
                    "move_in_progress": True,
                },
            }
        ),
        "arms": ok({"left": pose(400.0), "right": pose(450.0)}),
        "gripper.left": ok({"arm": "left", "position": 12.5}),
        "gripper.right": diag.Result(path="/", status=404, data={"detail": "no SmartGripper"}),
        "job.left": ok({"accepted": True, "message": "BUSY"}),
        "job.right": ok({"accepted": True, "message": "DONE"}),
        "rapid": ok(
            {
                "execution_state": "running",
                "controller_state": "motoron",
                "operation_mode": "AUTO",
                "tasks": [
                    {"name": "T_ROB_L", "state": "linked", "execution_state": "started", "active": True, "modules": ["LeftArm", "SharedVars"]},
                ],
            }
        ),
    }

    text = render(diag.arms_panel(results)) + render(diag.robot_panel(results)) + render(diag.rapid_panel(results))

    assert "400.0" in text and "450.0" in text
    assert "180.0" in text
    assert "12.5 mm" in text
    assert "BUSY" in text and "DONE" in text
    assert "motoron" in text and "AUTO" in text
    assert "LeftArm, SharedVars" in text


def test_dashboard_shows_both_arms() -> None:
    """The watch layout gives the arms panel a fixed height; no arm may be cut off."""

    def ok(data: object) -> "diag.Result":
        return diag.Result(path="/", status=200, data=data)

    results = {
        "health": ok(
            {
                "status": "ok",
                "security_mode": "strict",
                "camera": {"ok": True},
                "robot_controller": {"ok": True, "reachable": True},
            }
        ),
        "arms": ok({"left": pose(400.0), "right": pose(450.0)}),
    }
    gateway = diag.Gateway(
        url="http://127.0.0.1:9/api/v1",
        token=None,
        client="diagnostics",
        verify=True,
        timeout=1,
    )

    for width in (100, 140):
        console = Console(file=io.StringIO(), width=width, height=40, record=True)
        console.print(diag.dashboard(gateway, results, diag.View(), None, 40))
        text = console.export_text()

        assert "400.0" in text, width
        assert "450.0" in text, width


def test_taller_terminal_shows_more_events() -> None:
    """The events panel gets every line the other panels do not need."""

    def ok(data: object) -> "diag.Result":
        return diag.Result(path="/", status=200, data=data)

    results = {
        "health": ok(
            {
                "status": "ok",
                "security_mode": "development",
                "camera": {"ok": True},
                "robot_controller": {"ok": True, "reachable": True},
            }
        ),
        "arms": ok({"left": pose(400.0), "right": pose(450.0)}),
    }
    gateway = diag.Gateway(
        url="http://127.0.0.1:9/api/v1",
        token=None,
        client="diagnostics",
        verify=True,
        timeout=1,
    )
    feed = diag.EventFeed(url="ws://127.0.0.1:9", client="diagnostics", events=[])
    feed.state = "connected"

    for number in range(200):
        feed.events_received.append(
            diag.ReceivedEvent(
                received_at=diag.datetime.now(diag.timezone.utc),
                message={"type": "control.renewed", "data": {"n": f"event{number:03d}"}},
            )
        )

    def visible(height: int) -> list[str]:
        console = Console(file=io.StringIO(), width=120, height=height, record=True)
        console.print(diag.dashboard(gateway, results, diag.View(), feed, height, 120))
        text = console.export_text()

        assert text.count("\n") <= height + 1
        assert "450.0" in text

        return [line for line in text.splitlines() if "n=event" in line]

    shown = {height: visible(height) for height in (40, 60, 90)}

    assert len(shown[40]) >= diag.MIN_EVENTS_HEIGHT - 2
    assert len(shown[40]) < len(shown[60]) < len(shown[90])

    #
    # 30 more lines of terminal, 30 more events, and always the newest.
    #
    assert len(shown[90]) - len(shown[60]) == 30
    assert all("event199" in lines[-1] for lines in shown.values())
