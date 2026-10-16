import asyncio
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)

from gateway.control.router import require_control
from gateway.events.manager import event_manager
from gateway.models.commands import CommandResponse
from gateway.models.rws import RwsMoveRequest
from gateway.rws.client import RwsError
from gateway.rws.errors import to_http_exception
from gateway.rws.motion import (
    MotionConflict,
    MotionTimeout,
    rws_motion,
)
from gateway.security.identity import Identity

router = APIRouter(
    prefix="/robot/rws",
    tags=["rws motion"],
)


# ================================================================
# Helpers
# ================================================================

async def run(
    function,
    *args,
) -> CommandResponse:
    try:
        response = await asyncio.to_thread(
            function,
            *args,
        )

    except RwsError as error:
        raise to_http_exception(error) from error

    except MotionConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    except MotionTimeout as error:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(error),
        ) from error

    #
    # Same event as the legacy tcp robot provider.
    #
    await event_manager.publish(
        "robot.status",
        {
            "connected": True,
            "state": (
                "command_completed"
                if response.accepted
                else "command_failed"
            ),
        },
    )

    return response


# ================================================================
# Endpoints
# ================================================================

@router.post(
    "/move",
    response_model=CommandResponse,
    summary="Move the left arm over Robot Web Services",
    description=(
        "Writes the target into the RAPID motion program "
        "(gateway/rapid) and waits for the move to finish. "
        "tool0 in wobj0, mm and degrees; without rx/ry/rz the "
        "orientation is kept. A target outside the program's safe "
        "zone or out of reach gives accepted=false. 409 if RAPID is "
        "not running or a move is in progress, 504 on timeout."
    ),
)
async def move(
    request: RwsMoveRequest,
    _: Annotated[
        Identity,
        Depends(require_control("robot.control")),
    ],
) -> CommandResponse:
    rotation = (
        None
        if request.rx is None
        else (request.rx, request.ry, request.rz)
    )

    return await run(
        rws_motion.move,
        (request.x, request.y, request.z),
        rotation,
    )


@router.post(
    "/home",
    response_model=CommandResponse,
    summary="Move the left arm to its home position over Robot Web Services",
)
async def home(
    _: Annotated[
        Identity,
        Depends(require_control("robot.control")),
    ],
) -> CommandResponse:
    return await run(rws_motion.home)
