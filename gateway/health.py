import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter

from gateway.camera.service import camera_service
from gateway.config import get_settings
from gateway.control.router import get_control_manager
from gateway.models.status import (
    CameraHealth,
    HealthResponse,
    RobotControllerHealth,
)
from gateway.rws.client import RwsError, rws_client
from gateway.rws.motion import rws_motion


VERSION = "0.3.0"

#
# The health check must answer quickly even when the controller is
# off the network, so RWS gets much shorter (connect, read) timeouts
# than usual. A controller on the LAN connects in milliseconds.
#
RWS_PROBE_TIMEOUT_SECONDS = (0.5, 2.0)

STARTED = time.monotonic()

router = APIRouter(
    tags=["system"],
)


# ================================================================
# Checks
# ================================================================

def camera_health() -> CameraHealth:
    settings = get_settings()

    status = camera_service.get_status()

    age = (
        None
        if status.timestamp is None
        else (datetime.now(timezone.utc) - status.timestamp).total_seconds()
    )

    return CameraHealth(
        ok=(
            status.connected
            and age is not None
            and age <= settings.camera_max_frame_age_seconds
        ),
        provider=settings.camera_provider,
        connected=status.connected,
        frame_age_seconds=None if age is None else round(age, 3),
        max_frame_age_seconds=settings.camera_max_frame_age_seconds,
        error=status.error,
    )


def robot_controller_health() -> RobotControllerHealth:
    moving = rws_motion.is_moving()

    try:
        #
        # The first call finds out whether the controller is there
        # at all; the others are only made if it is.
        #
        controller_state = rws_client.controller_state(RWS_PROBE_TIMEOUT_SECONDS)
        operation_mode = rws_client.operation_mode(RWS_PROBE_TIMEOUT_SECONDS)
        execution_state = rws_client.execution_state(RWS_PROBE_TIMEOUT_SECONDS)

    except RwsError as error:
        return RobotControllerHealth(
            ok=False,
            reachable=error.status_code is not None,
            move_in_progress=moving,
            error=error.message,
        )

    return RobotControllerHealth(
        ok=True,
        reachable=True,
        controller_state=controller_state,
        operation_mode=operation_mode,
        rapid_execution_state=execution_state,
        move_in_progress=moving,
    )


# ================================================================
# Endpoint
# ================================================================

@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Gateway, camera and robot controller health",
    description=(
        "No authentication. status is ok when the camera delivers "
        "current frames and the robot controller answers over Robot "
        "Web Services, otherwise degraded; the details say which "
        "part is down and why. Always 200 while the gateway runs."
    ),
)
async def health() -> HealthResponse:
    settings = get_settings()

    camera = camera_health()

    robot_controller = await asyncio.to_thread(
        robot_controller_health
    )

    return HealthResponse(
        status=(
            "ok"
            if camera.ok and robot_controller.ok
            else "degraded"
        ),
        version=VERSION,
        uptime_seconds=round(time.monotonic() - STARTED, 1),
        security_mode=settings.security_mode,
        robot_provider=settings.robot_provider,
        control_lease_active=get_control_manager().get() is not None,
        camera=camera,
        robot_controller=robot_controller,
    )
