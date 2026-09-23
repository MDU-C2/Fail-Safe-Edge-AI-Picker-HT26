from gateway.models.commands import CommandResponse, RobotCommand
from gateway.models.status import RobotStatus


class MockRobotService:
    """Development robot implementation. No physical robot is contacted."""

    def get_status(self) -> RobotStatus:
        return RobotStatus(connected=False, state="mock")

    def execute(self, command: RobotCommand) -> CommandResponse:
        # This is deliberately NOT a real command forwarder.
        # Hardware-specific validation and safety checks belong before any
        # future physical robot transport is called.
        return CommandResponse(
            accepted=True,
            message=f"Mock accepted command '{command.command}'",
        )


robot_service = MockRobotService()
