import asyncio
import socket

from gateway.events.manager import event_manager
from gateway.models.commands import (
    CommandResponse,
    RobotCommand,
)
from gateway.models.status import RobotStatus


class MockRobotService:
    """Development robot implementation.

    No physical robot is contacted.
    """

    def __init__(self) -> None:
        self._state = "idle"

    def get_status(self) -> RobotStatus:
        return RobotStatus(
            connected=True,
            state=self._state,
        )

    async def execute(
        self,
        command: RobotCommand,
    ) -> CommandResponse:
        if command.command == "start":
            self._state = "running"

        elif command.command == "stop":
            self._state = "idle"

        await event_manager.publish(
            "robot.status",
            {
                "connected": True,
                "state": self._state,
            },
        )

        return CommandResponse(
            accepted=True,
            message=f"Mock accepted command '{command.command}'",
        )


class RobotService:
    """Temporary TCP interface to the real robot.

    Sends the command text received by the gateway directly to the
    robot TCP server and waits for a text response.
    """

    def __init__(
        self,
        host: str = "192.168.125.1",
        port: int = 5001,
    ) -> None:
        self._host = host
        self._port = port

    def get_status(self) -> RobotStatus:
        return RobotStatus(
            connected=False,
            state="unknown",
        )

    async def execute(
        self,
        command: RobotCommand,
    ) -> CommandResponse:
        response = await asyncio.to_thread(
            self._send_command,
            command.command,
        )

        await event_manager.publish(
            "robot.status",
            {
                "connected": response.accepted,
                "state": (
                    "command_completed"
                    if response.accepted
                    else "command_failed"
                ),
            },
        )

        return response

    def _send_command(
        self,
        command: str,
    ) -> CommandResponse:
        try:
            with socket.create_connection(
                (self._host, self._port),
                timeout=5,
            ) as sock:
                sock.settimeout(120)

                sock.sendall(
                    command.encode()
                )

                response = (
                    sock.recv(1024)
                    .decode()
                    .strip()
                )

        except OSError as error:
            return CommandResponse(
                accepted=False,
                message=f"Robot connection error: {error}",
            )

        return CommandResponse(
            accepted=response == "DONE",
            message=response,
        )


# Choose which implementation is currently used.

# Development:
# robot_service = MockRobotService()

# Real robot:
robot_service = RobotService()