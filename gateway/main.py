from contextlib import asynccontextmanager

from fastapi import FastAPI

from gateway.calibration.router import router as calibration_router
from gateway.camera.factory import create_camera_producer
from gateway.camera.router import router as camera_router
from gateway.config import get_settings
from gateway.control.router import (
    configure_control_manager,
    get_control_manager,
    router as control_router,
)
from gateway.events.router import router as events_router
from gateway.health import (
    VERSION,
    router as health_router,
)
from gateway.robot.router import router as robot_router
from gateway.rws.arms_router import router as arms_router
from gateway.rws.motion_router import router as motion_router
from gateway.rws.rapid_router import router as rapid_router
from gateway.rws.client import rws_client
from gateway.security.authentication import get_client_registry


# ================================================================
# Configuration
# ================================================================

settings = get_settings()


# ================================================================
# Client registry
# ================================================================

#
# Load the registered clients now so that a missing or invalid
# auth clients file stops the gateway at startup instead of
# failing on the first request.
#
if settings.security_mode == "strict":
    get_client_registry()


# ================================================================
# Camera producer
# ================================================================

camera_producer = create_camera_producer(
    settings
)


# ================================================================
# Application lifecycle
# ================================================================

@asynccontextmanager
async def lifespan(
    app: FastAPI,
):
    #
    # Start the configured camera producer when
    # the gateway starts.
    #
    await camera_producer.start()
    manager = get_control_manager()
    manager.start_watchdog(rws_client.invalidate_pending_dispatch, rws_client.stop_and_verify)

    try:
        yield

    finally:
        #
        # Stop the camera and release its resources
        # when the gateway shuts down.
        #
        await camera_producer.stop()
        manager.close()


# ================================================================
# FastAPI application
# ================================================================

app = FastAPI(
    title=settings.app_name,
    version=VERSION,
    lifespan=lifespan,
)


# ================================================================
# Control manager
# ================================================================

configure_control_manager(
    lease_seconds=settings.control_lease_seconds,
    require_session=settings.control_require_session,
)


# ================================================================
# API routers
# ================================================================

app.include_router(
    health_router,
    prefix=settings.api_prefix,
)

app.include_router(
    control_router,
    prefix=settings.api_prefix,
)

app.include_router(
    robot_router,
    prefix=settings.api_prefix,
)

app.include_router(
    events_router,
    prefix=settings.api_prefix,
)

app.include_router(
    camera_router,
    prefix=settings.api_prefix,
)

app.include_router(
    calibration_router,
    prefix=settings.api_prefix,
)

app.include_router(
    arms_router,
    prefix=settings.api_prefix,
)

app.include_router(
    rapid_router,
    prefix=settings.api_prefix,
)

app.include_router(
    motion_router,
    prefix=settings.api_prefix,
)
