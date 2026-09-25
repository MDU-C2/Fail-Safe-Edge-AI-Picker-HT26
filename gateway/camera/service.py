from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock

from gateway.events.manager import event_manager
from gateway.models.camera import CameraStatus


@dataclass(frozen=True)
class CameraFrame:
    data: bytes
    content_type: str

    sequence: int
    timestamp: datetime

    width: int
    height: int

    pixel_format: str


@dataclass(frozen=True)
class CameraFrameSet:
    sequence: int
    timestamp: datetime

    rgb: CameraFrame | None
    depth: CameraFrame | None


class CameraService:
    """
    Shared camera state and latest-frame buffer.

    Camera producers publish frames into this service.
    Network clients read frames from this service.

    Reading a frame never consumes it, allowing multiple
    clients to access the same camera data simultaneously.
    """

    def __init__(self) -> None:
        self._connected = False

        self._sequence = 0
        self._latest: CameraFrameSet | None = None

        self._lock = Lock()

    def get_status(self) -> CameraStatus:
        with self._lock:
            latest = self._latest

            return CameraStatus(
                connected=self._connected,

                rgb_available=(
                    latest is not None
                    and latest.rgb is not None
                ),

                depth_available=(
                    latest is not None
                    and latest.depth is not None
                ),

                sequence=(
                    latest.sequence
                    if latest is not None
                    else None
                ),

                timestamp=(
                    latest.timestamp
                    if latest is not None
                    else None
                ),

                rgb_width=(
                    latest.rgb.width
                    if latest is not None
                    and latest.rgb is not None
                    else None
                ),

                rgb_height=(
                    latest.rgb.height
                    if latest is not None
                    and latest.rgb is not None
                    else None
                ),

                rgb_format=(
                    latest.rgb.pixel_format
                    if latest is not None
                    and latest.rgb is not None
                    else None
                ),

                depth_width=(
                    latest.depth.width
                    if latest is not None
                    and latest.depth is not None
                    else None
                ),

                depth_height=(
                    latest.depth.height
                    if latest is not None
                    and latest.depth is not None
                    else None
                ),

                depth_format=(
                    latest.depth.pixel_format
                    if latest is not None
                    and latest.depth is not None
                    else None
                ),
            )

    def get_latest_frame_set(
        self,
    ) -> CameraFrameSet | None:
        with self._lock:
            return self._latest

    def get_rgb_frame(
        self,
    ) -> CameraFrame | None:
        frame_set = self.get_latest_frame_set()

        if frame_set is None:
            return None

        return frame_set.rgb

    def get_depth_frame(
        self,
    ) -> CameraFrame | None:
        frame_set = self.get_latest_frame_set()

        if frame_set is None:
            return None

        return frame_set.depth

    async def set_connected(
        self,
        connected: bool,
    ) -> None:
        changed = False

        with self._lock:
            if self._connected != connected:
                self._connected = connected
                changed = True

        if changed:
            await self.publish_status()

    async def publish_frame_set(
        self,
        *,
        rgb_data: bytes | None,
        depth_data: bytes | None,

        rgb_width: int,
        rgb_height: int,
        rgb_pixel_format: str,

        depth_width: int,
        depth_height: int,
        depth_pixel_format: str,

        rgb_content_type: str = "application/octet-stream",
        depth_content_type: str = "application/octet-stream",

        timestamp: datetime | None = None,
    ) -> CameraFrameSet:
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        with self._lock:
            self._sequence += 1
            sequence = self._sequence

            rgb_frame = None

            if rgb_data is not None:
                rgb_frame = CameraFrame(
                    data=rgb_data,
                    content_type=rgb_content_type,
                    sequence=sequence,
                    timestamp=timestamp,
                    width=rgb_width,
                    height=rgb_height,
                    pixel_format=rgb_pixel_format,
                )

            depth_frame = None

            if depth_data is not None:
                depth_frame = CameraFrame(
                    data=depth_data,
                    content_type=depth_content_type,
                    sequence=sequence,
                    timestamp=timestamp,
                    width=depth_width,
                    height=depth_height,
                    pixel_format=depth_pixel_format,
                )

            frame_set = CameraFrameSet(
                sequence=sequence,
                timestamp=timestamp,
                rgb=rgb_frame,
                depth=depth_frame,
            )

            self._latest = frame_set
            self._connected = True

        return frame_set

    async def publish_status(self) -> None:
        status = self.get_status()

        await event_manager.publish(
            "camera.status",
            status.model_dump(mode="json"),
        )


camera_service = CameraService()