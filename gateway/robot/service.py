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
            message=(
                f"Mock accepted command "
                f"'{command.command}'"
            ),
        )


robot_service = MockRobotService()