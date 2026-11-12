from datetime import datetime

from pydantic import BaseModel


class ControlLeaseResponse(BaseModel):
    active: bool
    client_id: str | None = None
    priority: int | None = None
    acquired_at: datetime | None = None
    expires_at: datetime | None = None
    session_id: str | None = None
    session_required: bool = False
    watchdog_enabled: bool = False
    recovery_required: bool = False
    recovery_reason: str | None = None


class ControlActionResponse(BaseModel):
    success: bool
    message: str
    lease: ControlLeaseResponse
