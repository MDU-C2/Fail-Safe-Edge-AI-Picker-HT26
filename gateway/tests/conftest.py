"""
Fixtures for the gateway test suite.

By default every test runs against gateways with the mock camera
and mock robot, and with Robot Web Services pointed at a closed
local port, so no hardware is needed or contacted.

Opt-in hardware:
    --robotstudio  RobotStudio virtual controller on 127.0.0.1
                   (RWS on port 80, RAPID socket server on 5000)
    --oak          the real OAK camera at GATEWAY_CAMERA_IP
    --real-robot   the real YuMi, through an already running gateway
                   (default the Raspberry Pi); small moves only, see
                   test_real_robot.py
    --real-robot-gripper
                   also close and open the real grippers

Without --real-robot the real robot is never used: every robot
address the suite's own gateways get is 127.0.0.1.

Implementation under test (options in implementation.py):
    --impl python  the Python gateway (default)
    --impl cpp     the C++ gateway at --cpp-binary (default
                   gateway/cpp/build/dev/gateway); Linux only, see
                   gateway/cpp/dev.sh pytest
"""

import os
from pathlib import Path
from typing import Iterator

import pytest
import requests
from requests.auth import HTTPDigestAuth

from gateway.tests import support
from gateway.tests.support import (
    ROBOTSTUDIO_RWS_PORT,
    ROBOTSTUDIO_SOCKET_PORT,
    Gateway,
    free_port,
)

#
# Modules that test Python internals rather than a running gateway;
# with --impl cpp they are skipped, and gateway/cpp/tests has their
# C++ counterparts.
#
PYTHON_ONLY = {
    "test_client_registry.py": "gateway/cpp/tests/test_client_registry.cpp",
    "test_control_manager.py": "gateway/cpp/tests/test_control_manager.cpp",
    "test_oak_reconnect.py": "the C++ OAK producer test (not ported yet)",
}

#
# Modules whose routes the C++ gateway does not have yet, and the
# migration step that brings them (gateway/cpp/README.md). Remove a
# line once its tests pass with --impl cpp.
#
NOT_PORTED_TO_CPP = {
    "test_events.py": "M3 (events WebSocket)",
    "test_robot.py": "M4 (POST /robot/command)",
    "test_robot_tcp.py": "M4 (POST /robot/command)",
    "test_failover.py": "M4 (its devices send POST /robot/command)",
    "test_rws.py": "M5 (RWS routes)",
    "test_robotstudio.py": "M5 (RWS routes)",
    "test_oak.py": "M6 (OAK camera)",
    "test_real_robot.py": "M5 (RWS routes)",
    "test_real_robot_legacy.py": "M4 (POST /robot/command)",
}

GATEWAY_FIXTURES = (
    "strict_gateway",
    "development_gateway",
    "robotstudio_gateway",
    "oak_gateway",
)


def pytest_addoption(
    parser: pytest.Parser,
) -> None:
    parser.addoption(
        "--robotstudio",
        action="store_true",
        help="Run tests against the RobotStudio virtual controller on 127.0.0.1 (changes its RAPID program, restored afterwards)",
    )

    parser.addoption(
        "--oak",
        action="store_true",
        help="Run tests against the real OAK camera (no other process may hold it)",
    )

    parser.addoption(
        "--real-robot",
        action="store_true",
        help="MOVES THE REAL YUMI a few mm through the gateway at --real-robot-url (RAPID must be running in AUTO)",
    )

    parser.addoption(
        "--real-robot-gripper",
        action="store_true",
        help="With --real-robot: also close and open both real grippers",
    )

    parser.addoption(
        "--real-robot-url",
        default="http://169.254.125.202:8000/api/v1",
        help="API base URL of the gateway in front of the real robot (default: the Raspberry Pi)",
    )

    parser.addoption(
        "--real-robot-token",
        default=os.environ.get("GATEWAY_TOKEN", ""),
        help="Bearer token of a client with robot.control on that gateway (default: GATEWAY_TOKEN)",
    )

def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    if support.implementation == "cpp":
        for item in items:
            module = Path(str(item.fspath)).name

            if module in PYTHON_ONLY:
                item.add_marker(
                    pytest.mark.skip(reason=f"tests Python internals; C++: {PYTHON_ONLY[module]}")
                )

            elif module in NOT_PORTED_TO_CPP:
                item.add_marker(
                    pytest.mark.skip(reason=f"not in the C++ gateway yet: {NOT_PORTED_TO_CPP[module]}")
                )

    for option in ("robotstudio", "oak", "real_robot", "real_robot_gripper"):
        if config.getoption(option):
            continue

        skip = pytest.mark.skip(
            reason=f"needs --{option.replace('_', '-')}",
        )

        for item in items:
            if option in item.keywords:
                item.add_marker(skip)


def mock_settings() -> dict[str, str]:
    return {
        "GATEWAY_CAMERA_PROVIDER": "mock",
        "GATEWAY_ROBOT_PROVIDER": "mock",
        "GATEWAY_ROBOT_HOST": "127.0.0.1",
        #
        # Nothing listens here, so RWS calls fail with 503.
        #
        "GATEWAY_RWS_PORT": str(free_port()),
    }


def run_gateway(
    tmp_path_factory: pytest.TempPathFactory,
    name: str,
    security_mode: str,
    settings: dict[str, str],
) -> Iterator[Gateway]:
    gateway = Gateway(
        tmp_path_factory.mktemp(name),
        security_mode,
        settings,
    )

    gateway.start()

    try:
        yield gateway

    finally:
        gateway.stop()


# ================================================================
# Gateways
# ================================================================

@pytest.fixture(scope="session")
def strict_gateway(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Gateway]:
    yield from run_gateway(
        tmp_path_factory,
        "strict",
        "strict",
        mock_settings(),
    )


@pytest.fixture(scope="session")
def development_gateway(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Gateway]:
    yield from run_gateway(
        tmp_path_factory,
        "development",
        "development",
        mock_settings(),
    )


@pytest.fixture(
    params=[
        "strict",
        "development",
    ],
)
def gateway(
    request: pytest.FixtureRequest,
) -> Iterator[Gateway]:
    """A mock gateway, once in each security mode."""

    gateway = request.getfixturevalue(f"{request.param}_gateway")

    yield gateway

    gateway.release_all()


@pytest.fixture(scope="session")
def robotstudio_gateway(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Gateway]:
    try:
        requests.get(
            f"http://127.0.0.1:{ROBOTSTUDIO_RWS_PORT}/rw/system?json=1",
            auth=HTTPDigestAuth("Default User", "robotics"),
            timeout=5,
        ).raise_for_status()

    except requests.RequestException as error:
        pytest.fail(
            f"RobotStudio virtual controller not reachable on 127.0.0.1:{ROBOTSTUDIO_RWS_PORT}: {error}"
        )

    yield from run_gateway(
        tmp_path_factory,
        "robotstudio",
        "strict",
        {
            "GATEWAY_CAMERA_PROVIDER": "mock",
            "GATEWAY_ROBOT_PROVIDER": "tcp",
            "GATEWAY_ROBOT_HOST": "127.0.0.1",
            "GATEWAY_ROBOT_PORT": str(ROBOTSTUDIO_SOCKET_PORT),
            "GATEWAY_RWS_PORT": str(ROBOTSTUDIO_RWS_PORT),
        },
    )


@pytest.fixture(scope="session")
def oak_gateway(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Gateway]:
    #
    # GATEWAY_CAMERA_IP and the frame size come from gateway/.env.
    #
    settings = mock_settings()

    settings["GATEWAY_CAMERA_PROVIDER"] = "oak"

    yield from run_gateway(
        tmp_path_factory,
        "oak",
        "strict",
        settings,
    )


# ================================================================
# Isolation
# ================================================================

@pytest.fixture(autouse=True)
def release_control(
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    """Give back any control lease a test left behind."""

    yield

    #
    # The session gateways outlive the test, so they can still be
    # reached here. The gateway fixture cleans up after itself.
    #
    for name in GATEWAY_FIXTURES:
        if name in request.fixturenames:
            request.getfixturevalue(name).release_all()
