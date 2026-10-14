import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from gateway.config import get_settings
from gateway.models.calibration import CalibrationResponse


class CalibrationService:
    """
    Stores the calibration JSON posted by the controlling client.

    The calibration is kept in memory and written to a file, so it
    is still available after the gateway restarts.
    """

    def __init__(
        self,
        filename: str,
    ) -> None:
        self._path = Path(filename)

        self._lock = Lock()

        self._current: CalibrationResponse | None = self._load()

    def _load(self) -> CalibrationResponse | None:
        if not self._path.is_file():
            return None

        try:
            return CalibrationResponse.model_validate_json(
                self._path.read_text(encoding="utf-8")
            )

        except ValueError as error:
            raise RuntimeError(
                f"Calibration file is not valid: "
                f"{self._path}: {error}"
            ) from error

    def get(self) -> CalibrationResponse | None:
        with self._lock:
            return self._current

    def set(
        self,
        calibration: dict[str, Any],
        client_id: str,
    ) -> CalibrationResponse:
        stored = CalibrationResponse(
            calibration=calibration,
            updated_at=datetime.now(timezone.utc),
            updated_by=client_id,
        )

        with self._lock:
            #
            # Write to a temporary file first and then replace the
            # old one, so a crash never leaves a half-written file.
            #
            self._path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            temporary = self._path.with_suffix(
                self._path.suffix + ".tmp"
            )

            temporary.write_text(
                stored.model_dump_json(indent=2),
                encoding="utf-8",
            )

            os.replace(
                temporary,
                self._path,
            )

            self._current = stored

        return stored


calibration_service = CalibrationService(
    get_settings().calibration_file
)
