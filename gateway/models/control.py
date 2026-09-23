from datetime import datetime

from pydantic import BaseModel


class ControlLeaseResponse(BaseModel):
    active: bool
    client_id: str | None = None
    priority: int | None = None
    acquired_at: datetime | None = None
    expires_at: datetime | None = None


class ControlActionResponse(BaseModel):
    success: bool
    message: str
    lease: ControlLeaseResponse