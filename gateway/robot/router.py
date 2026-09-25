from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)

from gateway.control.router import get_control_manager
from gateway.models.commands import (
    CommandResponse,
    RobotCommand,
)
from gateway.models.status import RobotStatus
from gateway.robot.service import robot_service
from gateway.security.authentication import require_permission
from gateway.security.identity import Identity

router = APIRouter(
    prefix="/robot",
    tags=["robot"],
)


@router.get(
    "/status",
    response_model=RobotStatus,
    summary="Get robot status",
)
def get_robot_status(
    _: Annotated[
        Identity,
        Depends(require_permission("robot.read")),
    ],
) -> RobotStatus:
    return robot_service.get_status()


@router.post(
    "/command",
    response_model=CommandResponse,
    summary="Send a command to the robot",
)
async def send_robot_command(
    command: RobotCommand,
    identity: Annotated[
        Identity,
        Depends(require_permission("robot.control")),
    ],
) -> CommandResponse:
    manager = get_control_manager()

    if not manager.owns_control(identity.client_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Client does not own the active control lease",
        )

    return await robot_service.execute(command)