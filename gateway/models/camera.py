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