from datetime import datetime, timezone
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Response,
    status,
)
from fastapi.responses import StreamingResponse

from gateway.camera.service import CameraFrame, CameraFrameSet, camera_service
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


def require_current_frame(
    frame: CameraFrame | None,
    name: str,
) -> CameraFrame:
    """Refuse absent, disconnected or stale frames before serving data."""

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

    return frame


def frame_response(
    frame: CameraFrame | None,
    name: str,
) -> Response:
    """Serve a frame, or 503 if there is none or it is stale."""

    frame = require_current_frame(frame, name)

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


def require_current_pair(
    frame_set: CameraFrameSet | None,
) -> CameraFrameSet:
    require_current_frame(
        frame_set.rgb if frame_set is not None else None,
        "RGB",
    )
    require_current_frame(
        frame_set.depth if frame_set is not None else None,
        "depth",
    )

    assert frame_set is not None

    return frame_set


@router.get(
    "/frame",
    summary="Get one synchronized RGB and depth pair",
    description=(
        "Returns raw RGB bytes followed by raw depth bytes in one response. "
        "X-RGB-Length and X-Depth-Length delimit the buffers; each has its "
        "own width, height and pixel-format headers. Both buffers share "
        "X-Frame-Sequence and X-Frame-Timestamp. Set after_sequence and "
        "timeout_s to wait for a newer pair without repeatedly downloading "
        "old images. Returns 204 when the wait expires, or 503 when the "
        "camera is disconnected or its latest frame is stale."
    ),
)
async def get_frame_pair(
    _: Annotated[
        Identity,
        Depends(require_permission("camera.read")),
    ],
    after_sequence: Annotated[int | None, Query(ge=0)] = None,
    timeout_s: Annotated[float, Query(ge=0, le=30)] = 0.0,
) -> Response:
    #
    # Check availability before waiting, but do not retain the old pair
    # throughout the wait. The service owns only the latest pair.
    #
    require_current_pair(camera_service.get_latest_frame_set())

    frame_set = await camera_service.wait_for_frame_set(
        after_sequence,
        timeout_s,
    )

    if frame_set is None:
        #
        # A disconnect wakes the wait immediately. A stalled producer
        # may have made its most recent frame stale during the wait.
        #
        require_current_pair(camera_service.get_latest_frame_set())

        return Response(
            status_code=status.HTTP_204_NO_CONTENT,
            headers={"Cache-Control": "no-store"},
        )

    require_current_pair(frame_set)

    rgb = frame_set.rgb
    depth = frame_set.depth

    assert rgb is not None and depth is not None

    async def chunks():
        #
        # Keep the producer's two immutable byte buffers. Joining them
        # would allocate another full RGB-D pair for every request.
        #
        yield rgb.data
        yield depth.data

    return StreamingResponse(
        chunks(),
        media_type="application/x-rgbd-frame",
        headers={
            "Cache-Control": "no-store",
            "Content-Length": str(len(rgb.data) + len(depth.data)),
            "X-Frame-Sequence": str(frame_set.sequence),
            "X-Frame-Timestamp": frame_set.timestamp.isoformat(),
            "X-RGB-Length": str(len(rgb.data)),
            "X-RGB-Width": str(rgb.width),
            "X-RGB-Height": str(rgb.height),
            "X-RGB-Pixel-Format": rgb.pixel_format,
            "X-Depth-Length": str(len(depth.data)),
            "X-Depth-Width": str(depth.width),
            "X-Depth-Height": str(depth.height),
            "X-Depth-Pixel-Format": depth.pixel_format,
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
