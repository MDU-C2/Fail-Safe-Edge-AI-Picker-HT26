from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    security_mode: str


class RobotStatus(BaseModel):
    connected: bool
    state: str
