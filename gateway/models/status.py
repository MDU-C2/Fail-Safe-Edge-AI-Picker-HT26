from typing import Literal

from pydantic import BaseModel


class CameraHealth(BaseModel):
    #
    # Connected and the newest frame is recent enough to be served.
    #
    ok: bool

    provider: str
    connected: bool

    frame_age_seconds: float | None = None
    max_frame_age_seconds: float

    error: str | None = None


class RobotControllerHealth(BaseModel):
    #
    # Robot Web Services answered. Motors off or RAPID stopped is
    # still ok: that is the robot's state, not a gateway fault.
    #
    ok: bool

    reachable: bool

    controller_state: str | None = None
    operation_mode: str | None = None
    rapid_execution_state: str | None = None

    move_in_progress: bool

    error: str | None = None


class HealthResponse(BaseModel):
    #
    # ok when camera and robot controller are both ok, else degraded.
    # The endpoint answers 200 either way while the gateway runs.
    #
    status: Literal["ok", "degraded"]

    version: str
    uptime_seconds: float

    security_mode: str
    robot_provider: str

    control_lease_active: bool

    camera: CameraHealth
    robot_controller: RobotControllerHealth


class RobotStatus(BaseModel):
    connected: bool
    state: str
