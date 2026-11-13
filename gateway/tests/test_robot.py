"""Robot status and commands against the mock robot.

POST /robot/command (the legacy CommServer path) exists only in the
Python gateway; its tests are marked legacy_command and skipped with
--impl cpp, where test_command_route_is_absent checks it is gone.
"""

import pytest
import requests

from gateway.tests import support
from gateway.tests.support import Gateway, expect


def test_status(
    gateway: Gateway,
) -> None:
    status = expect(gateway.client("diagnostics").get("/robot/status"), 200)

    assert status["connected"] is True
    assert status["state"] in ("idle", "running")


@pytest.mark.legacy_command
def test_command_needs_lease(
    gateway: Gateway,
) -> None:
    expect(
        gateway.client("admin").post("/robot/command", json={"command": "start"}),
        409,
    )


@pytest.mark.legacy_command
def test_command_needs_control_permission(
    gateway: Gateway,
) -> None:
    expect(
        gateway.client("diagnostics").post("/robot/command", json={"command": "start"}),
        403,
    )


@pytest.mark.legacy_command
def test_command_with_lease(
    gateway: Gateway,
) -> None:
    admin = gateway.client("admin")

    with admin.control():
        result = expect(admin.post("/robot/command", json={"command": "start"}), 200)

        assert result["accepted"]
        assert expect(admin.get("/robot/status"), 200)["state"] == "running"

        expect(admin.post("/robot/command", json={"command": "stop"}), 200)

        assert expect(admin.get("/robot/status"), 200)["state"] == "idle"


@pytest.mark.legacy_command
def test_command_validation(
    gateway: Gateway,
) -> None:
    admin = gateway.client("admin")

    with admin.control():
        expect(admin.post("/robot/command", json={"command": ""}), 422)
        expect(admin.post("/robot/command", json={"command": "x" * 65}), 422)
        expect(admin.post("/robot/command", json={}), 422)


@pytest.mark.legacy_command
def test_command_is_deprecated_but_works(
    gateway: Gateway,
) -> None:
    admin = gateway.client("admin")

    with admin.control():
        response = admin.post("/robot/command", json={"command": "stop"})

        assert expect(response, 200)["accepted"]

    assert response.headers["Deprecation"].startswith("@")
    assert response.headers["Link"].count('rel="successor-version"') == 2

    #
    # rws/move relative to .../robot/command is .../robot/rws/move.
    #
    assert "<rws/move>" in response.headers["Link"]

    assert "POST /robot/command is deprecated" in gateway.log()

    openapi = requests.get(f"{gateway.url}/openapi.json", timeout=10, verify=gateway.verify).json()

    assert openapi["paths"]["/api/v1/robot/command"]["post"]["deprecated"] is True


def test_command_route_is_absent(
    gateway: Gateway,
) -> None:
    """The C++ gateway leaves the legacy route out entirely, also
    from its schema."""

    if support.implementation != "cpp":
        pytest.skip("the Python gateway keeps the deprecated route")

    admin = gateway.client("admin")

    with admin.control():
        expect(admin.post("/robot/command", json={"command": "start"}), 404)

    openapi = requests.get(f"{gateway.url}/openapi.json", timeout=10, verify=gateway.verify).json()

    assert "/api/v1/robot/command" not in openapi["paths"]
