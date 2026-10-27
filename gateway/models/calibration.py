from datetime import datetime
from typing import Any

from pydantic import BaseModel


class CalibrationResponse(BaseModel):
    calibration: dict[str, Any]

    updated_at: datetime
    updated_by: str
