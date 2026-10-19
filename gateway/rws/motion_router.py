import asyncio
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    status,
)

from gateway.control.router import require_control
from gateway.events.manager import event_manager
from gateway.models.commands import CommandResponse
from gateway.models.rws import (
    RwsArmRequest,
    RwsGripperRequest,
    RwsMoveRequest,
)
from gateway.robot.service import COMMAND_STATES
from gateway.rws.client import RwsError
from gateway.rws.errors import to_http_exception
from gateway.rws.motion import (
    MotionConflict,
    MotionTimeout,
    rws_motion,
)
from gateway.security.authentication import require_permission
from gateway.security.identity import Identity

router = APIRouter(
    prefix="/robot/rws",
    tags=["rws motion"],
)


# ================================================================
# Helpers
# ================================================================

async def call(
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

    return response


async def run(
    function,
    *args,
) -> CommandResponse:
    response = await call(
        function,
        *args,
    )

    #
    # Same event as the legacy tcp robot provider:
    # command_started / command_completed / command_failed.
    #
    await event_manager.publish(
        "robot.status",
        {
            "connected": True,
            "state": COMMAND_STATES.get(
                response.message,
                "command_failed",
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
    summary="Move an arm over Robot Web Services",
    description=(
        "Writes the target into the arm's RAPID task (gateway/rapid) "
        "and waits for the move to finish. arm is left (default) or "
        "right; tool0 in wobj0, mm and degrees; without rx/ry/rz the "
        "orientation is kept. A target outside the gateway's safe "
        "zone (ERR outside safe zone, nothing is sent) or out of "
        "reach (ERR move failed) gives accepted=false. 409 if RAPID "
        "is not running or the arm is busy, 504 on timeout. With "
        "wait=false it answers STARTED once the job is written; poll "
        "GET /robot/rws/status."
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
        request.arm,
        (request.x, request.y, request.z),
        rotation,
        request.wait,
    )


@router.post(
    "/home",
    response_model=CommandResponse,
    summary="Move an arm to its home position over Robot Web Services",
    description=(
        "Body optional; arm is left (default) or right. ERR move "
        "failed if the arm stopped before reaching home (blocked)."
    ),
)
async def home(
    _: Annotated[
        Identity,
        Depends(require_control("robot.control")),
    ],
    request: RwsArmRequest | None = None,
) -> CommandResponse:
    request = request or RwsArmRequest()

    return await run(
        rws_motion.home,
        request.arm,
        request.wait,
    )


@router.post(
    "/gripper",
    response_model=CommandResponse,
    summary="Close (grip) or open an arm's gripper over Robot Web Services",
)
async def gripper(
    request: RwsGripperRequest,
    _: Annotated[
        Identity,
        Depends(require_control("robot.control")),
    ],
) -> CommandResponse:
    return await run(
        rws_motion.gripper,
        request.arm,
        request.action,
        request.wait,
    )


@router.get(
    "/status",
    response_model=CommandResponse,
    summary="State of an arm's last job",
    description=(
        "Same answers as the team's CommServer STATUS: BUSY while "
        "the arm works on a job (also one started by another "
        "client), then DONE or ERR move failed (accepted=false). "
        "For jobs started with wait=false."
    ),
)
async def job_status(
    _: Annotated[
        Identity,
        Depends(require_permission("robot.read")),
    ],
    arm: Annotated[
        Literal["left", "right"],
        Query(),
    ] = "left",
) -> CommandResponse:
    return await call(
        rws_motion.status,
        arm,
    )
