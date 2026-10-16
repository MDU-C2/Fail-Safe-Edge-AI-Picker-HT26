"""Health endpoint and how each security mode identifies clients."""

import pytest

from gateway.tests.support import Gateway, expect


def test_health_needs_no_credentials(
    gateway: Gateway,
) -> None:
    health = expect(
        gateway.client(None).get("/health"),
        200,
    )

    assert health["security_mode"] == gateway.security_mode
    assert health["robot_provider"] == "mock"
    assert health["control_lease_active"] is False

    #
    # The mock camera is fine; RWS points at a closed port.
    #
    assert health["camera"]["ok"] is True
    assert health["camera"]["provider"] == "mock"
    assert health["camera"]["frame_age_seconds"] <= health["camera"]["max_frame_age_seconds"]

    controller = health["robot_controller"]

    assert controller["ok"] is False and controller["reachable"] is False
    assert controller["error"].startswith("Robot controller unreachable")

    assert health["status"] == "degraded"


def test_health_shows_the_lease(
    gateway: Gateway,
) -> None:
    operator = gateway.client("operator")

    with operator.control():
        health = expect(gateway.client(None).get("/health"), 200)

    assert health["control_lease_active"] is True


@pytest.mark.parametrize(
    "path",
    [
        "/robot/status",
        "/camera/status",
        "/control",
    ],
)
def test_strict_rejects_missing_token(
    strict_gateway: Gateway,
    path: str,
) -> None:
    response = strict_gateway.client(None).get(path)

    expect(response, 401)

    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_strict_rejects_unknown_token(
    strict_gateway: Gateway,
) -> None:
    client = strict_gateway.client(None)

    client.session.headers["Authorization"] = "Bearer not-a-registered-token"

    expect(client.get("/robot/status"), 401)


def test_strict_ignores_development_header(
    strict_gateway: Gateway,
) -> None:
    client = strict_gateway.client(None)

    client.session.headers["X-Dev-Client"] = "operator"

    expect(client.get("/robot/status"), 401)


def test_development_defaults_to_operator(
    development_gateway: Gateway,
) -> None:
    client = development_gateway.client(None)

    with client.control():
        lease = expect(client.get("/control"), 200)

    assert lease["client_id"] == "operator"


def test_development_rejects_unknown_client(
    development_gateway: Gateway,
) -> None:
    client = development_gateway.client(None)

    client.session.headers["X-Dev-Client"] = "intruder"

    expect(client.get("/robot/status"), 401)


def test_read_only_client_cannot_take_control(
    gateway: Gateway,
) -> None:
    diagnostics = gateway.client("diagnostics")

    expect(diagnostics.get("/robot/status"), 200)
    expect(diagnostics.get("/camera/status"), 200)

    expect(diagnostics.post("/control/acquire"), 403)
