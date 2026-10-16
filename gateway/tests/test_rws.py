"""Arm pose, RAPID and RWS move endpoints without a robot controller.

The mock gateways point RWS at a closed port, so these check what
the gateway itself enforces (permissions, lease, validation) and
that an unreachable controller gives 503. Behaviour against a
controller is in test_robotstudio.py.
"""

import pytest

from gateway.rws.motion import orient_zyx
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
        ("/robot/rws/home", None),
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
    ],
)
def test_rws_move_is_validated(
    gateway: Gateway,
    body: dict,
) -> None:
    operator = gateway.client("operator")

    with operator.control():
        expect(operator.post("/robot/rws/move", json=body), 422)


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
