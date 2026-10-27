from datetime import datetime, timezone
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Response,
    status,
)

from gateway.camera.service import CameraFrame, camera_service
from gateway.config import get_settings
from gateway.models.camera import (
    CameraIntrinsics,
    CameraStatus,
)
from gateway.security.authentication import require_permission
from gateway.security.identity import Identity


router = APIRouter(
    prefix="/camera",
    tags=["camera"],
)


@router.get(
    "/status",
    response_model=CameraStatus,
    summary="Get camera status",
)
def get_camera_status(
    _: Annotated[
        Identity,
        Depends(require_permission("camera.read")),
    ],
) -> CameraStatus:
    return camera_service.get_status()


@router.get(
    "/intrinsics",
    response_model=CameraIntrinsics,
    summary="Get the RGB camera intrinsics",
)
def get_camera_intrinsics(
    _: Annotated[
        Identity,
        Depends(require_permission("camera.read")),
    ],
) -> CameraIntrinsics:
    intrinsics = camera_service.get_intrinsics()

    if intrinsics is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Camera intrinsics are not currently available",
        )

    return intrinsics


def frame_response(
    frame: CameraFrame | None,
    name: str,
) -> Response:
    """Serve a frame, or 503 if there is none or it is stale."""

    if frame is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No {name} frame is currently available",
        )

    if not camera_service.is_connected():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Camera is disconnected, no current {name} frame",
        )

    age = (
        datetime.now(timezone.utc)
        - frame.timestamp
    ).total_seconds()

    max_age = get_settings().camera_max_frame_age_seconds

    if age > max_age:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Latest {name} frame is {age:.1f} s old "
                f"(limit {max_age:g} s); the camera has stalled"
            ),
        )

    return Response(
        content=frame.data,
        media_type=frame.content_type,
        headers={
            "X-Frame-Sequence": str(
                frame.sequence
            ),
            "X-Frame-Timestamp": (
                frame.timestamp.isoformat()
            ),
            "X-Frame-Width": str(
                frame.width
            ),
            "X-Frame-Height": str(
                frame.height
            ),
            "X-Pixel-Format": (
                frame.pixel_format
            ),
        },
    )


@router.get(
    "/rgb",
    summary="Get the latest RGB frame",
)
def get_rgb_frame(
    _: Annotated[
        Identity,
        Depends(require_permission("camera.read")),
    ],
) -> Response:
    return frame_response(
        camera_service.get_rgb_frame(),
        "RGB",
    )


@router.get(
    "/depth",
    summary="Get the latest depth frame",
)
def get_depth_frame(
    _: Annotated[
        Identity,
        Depends(require_permission("camera.read")),
    ],
) -> Response:
    return frame_response(
        camera_service.get_depth_frame(),
        "depth",
    )
