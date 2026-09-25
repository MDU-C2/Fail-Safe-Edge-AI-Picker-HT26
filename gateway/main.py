from contextlib import asynccontextmanager

from fastapi import FastAPI

from gateway.camera.factory import create_camera_producer
from gateway.camera.router import router as camera_router
from gateway.config import get_settings
from gateway.control.router import (
    configure_control_manager,
    router as control_router,
)
from gateway.events.router import router as events_router
from gateway.models.status import HealthResponse
from gateway.robot.router import router as robot_router


# ================================================================
# Configuration
# ================================================================

settings = get_settings()


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

    try:
        yield

    finally:
        #
        # Stop the camera and release its resources
        # when the gateway shuts down.
        #
        await camera_producer.stop()


# ================================================================
# FastAPI application
# ================================================================

app = FastAPI(
    title=settings.app_name,
    version="0.3.0",
    lifespan=lifespan,
)


# ================================================================
# Control manager
# ================================================================

configure_control_manager(
    lease_seconds=settings.control_lease_seconds,
)


# ================================================================
# System endpoints
# ================================================================

@app.get(
    f"{settings.api_prefix}/health",
    response_model=HealthResponse,
    tags=["system"],
)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        security_mode=settings.security_mode,
    )


# ================================================================
# API routers
# ================================================================

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