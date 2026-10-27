from datetime import datetime
from pydantic import BaseModel


class CameraStatus(BaseModel):
    connected: bool

    rgb_available: bool
    depth_available: bool

    sequence: int | None = None
    timestamp: datetime | None = None

    rgb_width: int | None = None
    rgb_height: int | None = None
    rgb_format: str | None = None

    depth_width: int | None = None
    depth_height: int | None = None
    depth_format: str | None = None

    #
    # Why the camera is disconnected, while it is; cleared when it
    # connects again.
    #
    error: str | None = None


class CameraIntrinsics(BaseModel):
    width: int
    height: int

    fx: float
    fy: float

    cx: float
    cy: float

    matrix: list[list[float]]