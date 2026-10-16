"""Arm pose and RAPID endpoints without a robot controller.

The mock gateways point RWS at a closed port, so these check what
the gateway itself enforces (permissions, lease, validation) and
that an unreachable controller gives 503. Behaviour against a
controller is in test_robotstudio.py.
"""

import pytest

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
