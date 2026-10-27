import logging
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    Response,
)

from gateway.control.router import require_control
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

logger = logging.getLogger("uvicorn.error")


# ================================================================
# Deprecation of POST /robot/command
# ================================================================

#
# RFC 9745 Deprecation header (deprecated since 2026-10-16, as a
# Unix timestamp) and RFC 8594 successor links. The links are
# relative to the API prefix, like the route itself.
#
DEPRECATION_HEADERS = {
    "Deprecation": "@1792108800",
    "Link": (
        '<rws/move>; rel="successor-version", '
        '<rws/home>; rel="successor-version"'
    ),
}

_deprecation_logged = False


def warn_deprecated_command() -> None:
    """Log the deprecation once per gateway process."""

    global _deprecation_logged

    if _deprecation_logged:
        return

    _deprecation_logged = True

    logger.warning(
        "POST /robot/command is deprecated; "
        "use POST /robot/rws/move and /robot/rws/home"
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
    summary="Send a command to the robot (deprecated)",
    description=(
        "Deprecated: use POST /robot/rws/move and POST "
        "/robot/rws/home, which drive the RAPID program over Robot "
        "Web Services instead of a RAPID socket server. This route "
        "still works unchanged (it sends the command text to the "
        "socket server at GATEWAY_ROBOT_HOST:GATEWAY_ROBOT_PORT) and "
        "marks its responses with Deprecation and Link headers."
    ),
    deprecated=True,
)
async def send_robot_command(
    command: RobotCommand,
    response: Response,
    _: Annotated[
        Identity,
        Depends(require_control("robot.control")),
    ],
) -> CommandResponse:
    response.headers.update(DEPRECATION_HEADERS)

    warn_deprecated_command()

    return await robot_service.execute(command)