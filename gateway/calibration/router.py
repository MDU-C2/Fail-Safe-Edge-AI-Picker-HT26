import json
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    status,
)

from gateway.calibration.service import calibration_service
from gateway.control.router import get_control_manager
from gateway.events.manager import event_manager
from gateway.models.calibration import CalibrationResponse
from gateway.security.authentication import require_permission
from gateway.security.identity import Identity

MAX_CALIBRATION_BYTES = 1_000_000

router = APIRouter(
    prefix="/calibration",
    tags=["calibration"],
)


@router.get(
    "",
    response_model=CalibrationResponse,
    summary="Get the current calibration",
)
def get_calibration(
    _: Annotated[
        Identity,
        Depends(require_permission("camera.read")),
    ],
) -> CalibrationResponse:
    calibration = calibration_service.get()

    if calibration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No calibration has been uploaded",
        )

    return calibration


@router.post(
    "",
    response_model=CalibrationResponse,
    summary="Upload a new calibration",
)
async def post_calibration(
    calibration: Annotated[
        dict[str, Any],
        Body(),
    ],
    identity: Annotated[
        Identity,
        Depends(require_permission("robot.control")),
    ],
) -> CalibrationResponse:
    manager = get_control_manager()

    if not manager.owns_control(identity.client_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Client does not own the active control lease",
        )

    if len(json.dumps(calibration)) > MAX_CALIBRATION_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Calibration is larger than "
                f"{MAX_CALIBRATION_BYTES} bytes"
            ),
        )

    stored = calibration_service.set(
        calibration,
        identity.client_id,
    )

    await event_manager.publish(
        "calibration.updated",
        {
            "updated_at": stored.updated_at.isoformat(),
            "updated_by": stored.updated_by,
        },
    )

    return stored
