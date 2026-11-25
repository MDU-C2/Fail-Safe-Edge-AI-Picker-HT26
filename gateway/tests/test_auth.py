"""Health endpoint and how each security mode identifies clients."""

import hashlib
from pathlib import Path

import pytest

from gateway.tests.conftest import mock_settings
from gateway.tests.support import Gateway, expect, token_for


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
    admin = gateway.client("admin")

    with admin.control():
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

    client.session.headers["X-Dev-Client"] = "admin"

    expect(client.get("/robot/status"), 401)


def test_development_defaults_to_admin(
    development_gateway: Gateway,
) -> None:
    client = development_gateway.client(None)

    with client.control():
        lease = expect(client.get("/control"), 200)

    assert lease["client_id"] == "admin"


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


# ================================================================
# Clients from GATEWAY_AUTH_CLIENTS (C++ only)
# ================================================================

def bearer(
    gateway: Gateway,
    token: str,
):
    client = gateway.client(None)

    client.session.headers["Authorization"] = f"Bearer {token}"

    return client


@pytest.mark.cpp_only
def test_inline_clients_replace_the_file(
    tmp_path: Path,
) -> None:
    """
    GATEWAY_AUTH_CLIENTS holds the clients on one line (the Lynx app
    environment cannot hold JSON); the clients file is then unused.
    """

    def entry(
        client_id: str,
        priority: int,
        permissions: str,
        token: str,
    ) -> str:
        return f"{client_id}:{priority}:{permissions}:{hashlib.sha256(token.encode()).hexdigest()}"

    gateway = Gateway(
        tmp_path,
        "strict",
        {
            **mock_settings(),
            "GATEWAY_AUTH_CLIENTS": " ".join(
                [
                    entry("lynx-admin", 1000, "*", "inline-admin"),
                    entry("watcher", 10, "robot.read,camera.read", "inline-watcher"),
                ]
            ),
        },
    )

    gateway.start()

    try:
        expect(bearer(gateway, "inline-watcher").get("/robot/status"), 200)
        expect(bearer(gateway, "inline-watcher").post("/control/acquire"), 403)

        admin = bearer(gateway, "inline-admin")

        expect(admin.post("/control/acquire"), 200)

        assert expect(admin.get("/control"), 200)["client_id"] == "lynx-admin"

        expect(admin.post("/control/release"), 200)

        #
        # The file's clients are gone.
        #
        expect(bearer(gateway, token_for("admin")).get("/robot/status"), 401)

    finally:
        gateway.stop()


@pytest.mark.cpp_only
def test_malformed_inline_clients_stop_startup(
    tmp_path: Path,
) -> None:
    gateway = Gateway(
        tmp_path,
        "strict",
        {
            **mock_settings(),
            "GATEWAY_AUTH_CLIENTS": "admin:1000:*",
        },
    )

    with pytest.raises(RuntimeError, match="GATEWAY_AUTH_CLIENTS"):
        gateway.start()


@pytest.mark.cpp_only
def test_cut_inline_clients_stop_startup(
    tmp_path: Path,
) -> None:
    """WeOS keeps only 63 characters of an app environment value; the
    cut line must stop the gateway, not lock every client out."""

    line = f"admin:1000:*:{hashlib.sha256(b'admin-token').hexdigest()}"

    gateway = Gateway(
        tmp_path,
        "strict",
        {
            **mock_settings(),
            "GATEWAY_AUTH_CLIENTS": line[:63],
        },
    )

    with pytest.raises(RuntimeError, match="64 lowercase hex"):
        gateway.start()
