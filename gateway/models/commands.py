from typing import Any

from pydantic import BaseModel, Field


class RobotCommand(BaseModel):
    command: str = Field(min_length=1, max_length=64)
    parameters: dict[str, Any] = Field(default_factory=dict)


class CommandResponse(BaseModel):
    accepted: bool
    message: str
