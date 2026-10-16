"""Robot status and commands against the mock robot."""

from gateway.tests.support import Gateway, expect


def test_status(
    gateway: Gateway,
) -> None:
    status = expect(gateway.client("diagnostics").get("/robot/status"), 200)

    assert status["connected"] is True
    assert status["state"] in ("idle", "running")


def test_command_needs_lease(
    gateway: Gateway,
) -> None:
    expect(
        gateway.client("operator").post("/robot/command", json={"command": "start"}),
        409,
    )


def test_command_needs_control_permission(
    gateway: Gateway,
) -> None:
    expect(
        gateway.client("diagnostics").post("/robot/command", json={"command": "start"}),
        403,
    )


def test_command_with_lease(
    gateway: Gateway,
) -> None:
    operator = gateway.client("operator")

    with operator.control():
        result = expect(operator.post("/robot/command", json={"command": "start"}), 200)

        assert result["accepted"]
        assert expect(operator.get("/robot/status"), 200)["state"] == "running"

        expect(operator.post("/robot/command", json={"command": "stop"}), 200)

        assert expect(operator.get("/robot/status"), 200)["state"] == "idle"


def test_command_validation(
    gateway: Gateway,
) -> None:
    operator = gateway.client("operator")

    with operator.control():
        expect(operator.post("/robot/command", json={"command": ""}), 422)
        expect(operator.post("/robot/command", json={"command": "x" * 65}), 422)
        expect(operator.post("/robot/command", json={}), 422)
