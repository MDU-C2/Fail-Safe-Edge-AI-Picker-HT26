"""Robot status and commands against the mock robot."""

import requests

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


def test_command_is_deprecated_but_works(
    gateway: Gateway,
) -> None:
    operator = gateway.client("operator")

    with operator.control():
        response = operator.post("/robot/command", json={"command": "stop"})

        assert expect(response, 200)["accepted"]

    assert response.headers["Deprecation"].startswith("@")
    assert response.headers["Link"].count('rel="successor-version"') == 2

    #
    # rws/move relative to .../robot/command is .../robot/rws/move.
    #
    assert "<rws/move>" in response.headers["Link"]

    assert "POST /robot/command is deprecated" in gateway.log()

    openapi = requests.get(f"{gateway.url}/openapi.json", timeout=10).json()

    assert openapi["paths"]["/api/v1/robot/command"]["post"]["deprecated"] is True
