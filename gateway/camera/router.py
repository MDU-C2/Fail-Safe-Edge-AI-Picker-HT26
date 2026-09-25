from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Response,
    status,
)

from gateway.camera.service import camera_service
from gateway.models.camera import CameraStatus
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
    "/rgb",
    summary="Get the latest RGB frame",
)
def get_rgb_frame(
    _: Annotated[
        Identity,
        Depends(require_permission("camera.read")),
    ],
) -> Response:
    frame = camera_service.get_rgb_frame()

    if frame is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No RGB frame is currently available",
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
    "/depth",
    summary="Get the latest depth frame",
)
def get_depth_frame(
    _: Annotated[
        Identity,
        Depends(require_permission("camera.read")),
    ],
) -> Response:
    frame = camera_service.get_depth_frame()

    if frame is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No depth frame is currently available",
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