"""POST /robot/command with the tcp provider against a fake CommServer.

FakeCommServer answers like the team's rapid/CommServer.mod: one
message per connection, jobs answer STARTED at once and STATUS
reports BUSY until the arm is done. The gateway passes every reply
through unchanged; only accepted and the event state come from it.
"""

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from gateway.tests.conftest import mock_settings
from gateway.tests.support import Client, Gateway, expect, free_port


MOVE_SECONDS = 0.5


class FakeCommServer:
    def __init__(self) -> None:
        self.port = free_port()
        self.received: list[str] = []

        #
        # When each arm's current job finishes; "L" or "R".
        #
        self._done_at = {"L": 0.0, "R": 0.0}
        self._ok = {"L": False, "R": False}

        self._server = socket.create_server(("127.0.0.1", self.port))
        self._server.settimeout(0.2)

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._server.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()

            except TimeoutError:
                continue

            with client:
                message = client.recv(1024).decode()

                self.received.append(message)

                client.sendall(self._reply(message).encode())

    def _reply(
        self,
        message: str,
    ) -> str:
        arm = "L"

        if message.startswith("R,"):
            arm = "R"
            message = message[2:]

        busy = time.monotonic() < self._done_at[arm]

        if message == "STATUS":
            if busy:
                return "BUSY"

            return "DONE" if self._ok[arm] else "ERR move failed"

        if busy:
            return "ERR busy"

        if message in ("HOME", "GRIP", "OPEN"):
            return self._start(arm, ok=True)

        try:
            values = [float(value) for value in message.split(",")]

        except ValueError:
            return "ERR bad format"

        if len(values) not in (3, 6):
            return "ERR bad format"

        #
        # Stands in for an unreachable target: the job starts but
        # STATUS reports the failure.
        #
        return self._start(arm, ok=values[0] < 1000)

    def _start(
        self,
        arm: str,
        ok: bool,
    ) -> str:
        self._done_at[arm] = time.monotonic() + MOVE_SECONDS
        self._ok[arm] = ok

        return "STARTED"


@pytest.fixture(scope="module")
def comm_server() -> Iterator[FakeCommServer]:
    server = FakeCommServer()

    server.start()

    yield server

    server.stop()


@pytest.fixture(scope="module")
def tcp_gateway(
    tmp_path_factory: pytest.TempPathFactory,
    comm_server: FakeCommServer,
) -> Iterator[Gateway]:
    gateway = Gateway(
        tmp_path_factory.mktemp("tcp"),
        "strict",
        {
            **mock_settings(),
            "GATEWAY_ROBOT_PROVIDER": "tcp",
            "GATEWAY_ROBOT_PORT": str(comm_server.port),
        },
    )

    gateway.start()

    yield gateway

    gateway.stop()


def send(
    client: Client,
    command: str,
) -> dict:
    return expect(
        client.post("/robot/command", json={"command": command}),
        200,
    )


def wait_until_done(
    client: Client,
    prefix: str = "",
) -> dict:
    deadline = time.monotonic() + 5

    while (result := send(client, prefix + "STATUS"))["message"] == "BUSY":
        assert result["accepted"]
        assert time.monotonic() < deadline

        time.sleep(0.05)

    return result


def test_job_starts_and_status_reports_done(
    tcp_gateway: Gateway,
    comm_server: FakeCommServer,
) -> None:
    operator = tcp_gateway.client("operator")

    with operator.control():
        assert send(operator, "400,150,300") == {"accepted": True, "message": "STARTED"}

        assert send(operator, "STATUS") == {"accepted": True, "message": "BUSY"}

        assert wait_until_done(operator) == {"accepted": True, "message": "DONE"}

    assert "400,150,300" in comm_server.received


def test_refusals_are_not_accepted(
    tcp_gateway: Gateway,
) -> None:
    operator = tcp_gateway.client("operator")

    with operator.control():
        assert send(operator, "HOME")["message"] == "STARTED"

        assert send(operator, "GRIP") == {"accepted": False, "message": "ERR busy"}

        wait_until_done(operator)

        assert send(operator, "1,2") == {"accepted": False, "message": "ERR bad format"}

        assert send(operator, "1000,0,0")["message"] == "STARTED"

        assert wait_until_done(operator) == {"accepted": False, "message": "ERR move failed"}


def test_right_arm_prefix_is_passed_through(
    tcp_gateway: Gateway,
    comm_server: FakeCommServer,
) -> None:
    operator = tcp_gateway.client("operator")

    with operator.control():
        #
        # The arms are independent: the left one can start while
        # the right one moves.
        #
        assert send(operator, "R,420,-150,300,180,0,0")["message"] == "STARTED"
        assert send(operator, "OPEN")["message"] == "STARTED"

        assert wait_until_done(operator, "R,")["message"] == "DONE"
        assert wait_until_done(operator)["message"] == "DONE"

    assert "R,420,-150,300,180,0,0" in comm_server.received


def test_unreachable_server_is_reported(
    tmp_path: Path,
) -> None:
    gateway = Gateway(
        tmp_path,
        "strict",
        {
            **mock_settings(),
            "GATEWAY_ROBOT_PROVIDER": "tcp",
            "GATEWAY_ROBOT_PORT": str(free_port()),
        },
    )

    gateway.start()

    try:
        operator = gateway.client("operator")

        with operator.control():
            result = send(operator, "STATUS")

    finally:
        gateway.stop()

    assert result["accepted"] is False
    assert result["message"].startswith("Robot connection error")
