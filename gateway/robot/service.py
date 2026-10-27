import asyncio
import socket

from gateway.config import Settings, get_settings
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


#
# Replies of the team's CommServer (rapid/CommServer.mod), one
# message per connection:
#
#   "x,y,z[,rx,ry,rz]", "HOME", "GRIP", "OPEN"  ->  STARTED
#   "STATUS"                                    ->  BUSY / DONE / ERR move failed
#   anything refused                            ->  ERR <reason>
#
# An "R," prefix addresses the right arm. Jobs answer at once, so
# the client polls STATUS until it is no longer BUSY. Older servers
# answered DONE when the move was finished instead.
#
COMMAND_STATES = {
    "STARTED": "command_started",
    "BUSY": "busy",
    "DONE": "command_completed",
}


class RobotService:
    """Temporary TCP interface to the real robot.

    Sends the command text received by the gateway directly to the
    robot TCP server and returns its text response unchanged; the
    reply decides accepted (anything but ERR ...) and the state of
    the robot.status event.
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
        response, connected = await asyncio.to_thread(
            self._send_command,
            command.command,
        )

        await event_manager.publish(
            "robot.status",
            {
                "connected": connected,
                "state": COMMAND_STATES.get(
                    response.message,
                    "command_failed",
                ),
            },
        )

        return response

    def _send_command(
        self,
        command: str,
    ) -> tuple[CommandResponse, bool]:
        """The robot's reply, and whether the server answered at all."""

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
            return (
                CommandResponse(
                    accepted=False,
                    message=f"Robot connection error: {error}",
                ),
                False,
            )

        return (
            CommandResponse(
                accepted=response in COMMAND_STATES,
                message=response,
            ),
            True,
        )


def create_robot_service(
    settings: Settings,
) -> MockRobotService | RobotService:
    if settings.robot_provider == "mock":
        return MockRobotService()

    if settings.robot_provider == "tcp":
        return RobotService(
            host=settings.robot_host,
            port=settings.robot_port,
        )

    raise RuntimeError(
        "Unknown robot provider: "
        f"{settings.robot_provider}"
    )


robot_service = create_robot_service(
    get_settings()
)