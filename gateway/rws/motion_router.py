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
    RwsGripperRequest,
    RwsMotionRequest,
    RwsMoveRequest,
    RwsPathRequest,
)
from gateway.robot.service import COMMAND_STATES
from gateway.rws.client import RwsError
from gateway.rws.errors import to_http_exception
from gateway.rws.motion import (
    MotionConflict,
    MotionSpeed,
    MotionTimeout,
    PathPoint,
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


def motion_speed(
    request: RwsMotionRequest,
) -> MotionSpeed:
    """The request's speed; what it leaves out is the default."""

    default = rws_motion.default_speed

    return MotionSpeed(
        speed=(
            default.speed
            if request.speed is None
            else request.speed
        ),
        acceleration=(
            default.acceleration
            if request.acceleration is None
            else request.acceleration
        ),
        ramp=(
            default.ramp
            if request.ramp is None
            else request.ramp
        ),
    )


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
        "Writes the target into the arm's RAPID task (rapid/) "
        "and waits for the move to finish. arm is left (default) or "
        "right; tool0 in wobj0, mm and degrees; without rx/ry/rz the "
        "orientation is kept. A target outside the gateway's safe "
        "zone (ERR outside safe zone, nothing is sent) or out of "
        "reach (ERR move failed) gives accepted=false. 409 if RAPID "
        "is not running or the arm is busy, 504 on timeout. With "
        "wait=false it answers STARTED once the job is written; poll "
        "GET /robot/rws/status. speed is the TCP speed in mm/s "
        "(default 50, RAPID v50), acceleration and ramp are RAPID "
        "AccSet's in % (default 100); they also apply to later "
        "legacy /robot/command moves of that arm."
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
        motion_speed(request),
    )


@router.post(
    "/path",
    response_model=CommandResponse,
    summary="Move an arm through several points without stopping",
    description=(
        "One job with 1 to 8 points (tool0 in wobj0, mm and degrees, "
        "rx/ry/rz required). The arm blends through the middle points "
        "(zone, mm, per point or the request's, default 10; 0 stops "
        "there) and stops only at the last. linear=true moves in a "
        "straight line to the point (MoveL), otherwise a joint move. "
        "speed per point or the request's, in mm/s; acceleration and "
        "ramp as for /move. max_acceleration limits the TCP "
        "acceleration in m/s2 (RAPID PathAccLim), e.g. while carrying "
        "a cup. Every point must be in the safe zone (ERR outside safe "
        "zone, nothing is sent) and reachable (ERR move failed, the arm "
        "does not move). 409 if the RAPID program has no path "
        "variables (upload rapid/ again). wait as for /move."
    ),
)
async def path(
    request: RwsPathRequest,
    _: Annotated[
        Identity,
        Depends(require_control("robot.control")),
    ],
) -> CommandResponse:
    points = [
        PathPoint(
            position=(point.x, point.y, point.z),
            rotation=(point.rx, point.ry, point.rz),
            linear=point.linear,
            speed=point.speed,
            zone=point.zone,
        )
        for point in request.points
    ]

    return await run(
        rws_motion.path,
        request.arm,
        points,
        request.zone,
        request.max_acceleration,
        request.wait,
        motion_speed(request),
    )


@router.post(
    "/home",
    response_model=CommandResponse,
    summary="Move an arm to its home position over Robot Web Services",
    description=(
        "Body optional; arm is left (default) or right. ERR move "
        "failed if the arm stopped before reaching home (blocked). "
        "speed, acceleration and ramp as for /move."
    ),
)
async def home(
    _: Annotated[
        Identity,
        Depends(require_control("robot.control")),
    ],
    request: RwsMotionRequest | None = None,
) -> CommandResponse:
    request = request or RwsMotionRequest()

    return await run(
        rws_motion.home,
        request.arm,
        request.wait,
        motion_speed(request),
    )


@router.post(
    "/gripper",
    response_model=CommandResponse,
    summary="Close, open or set the opening of an arm's gripper over Robot Web Services",
    description=(
        "action grip closes until the fingers hold something, with "
        "optional force in N (0 < force <= 20, default 20, the "
        "SmartGripper's maximum); open opens fully; move goes to "
        "position, the opening between the fingers in mm (0 closed "
        "to 25 fully open), from either side and without holding "
        "force. position only with move, force only with grip (422 "
        "otherwise). 409 if the RAPID program predates move or "
        "force (upload rapid/ again). wait as for /move. The last "
        "force also applies to later legacy GRIP commands of that "
        "arm."
    ),
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
        request.position,
        request.force,
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
