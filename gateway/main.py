from fastapi import FastAPI
from gateway.events.router import router as events_router
from gateway.config import get_settings
from gateway.control.router import (
    configure_control_manager,
    router as control_router,
)
from gateway.models.status import HealthResponse
from gateway.robot.router import router as robot_router

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
)

configure_control_manager(
    lease_seconds=settings.control_lease_seconds,
)


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