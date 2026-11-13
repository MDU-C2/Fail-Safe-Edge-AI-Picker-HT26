import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock

from gateway.events.manager import event_manager
from gateway.models.camera import (
    CameraIntrinsics,
    CameraStatus,
)


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
        self._error: str | None = None

        self._sequence = 0
        self._latest: CameraFrameSet | None = None

        self._intrinsics: CameraIntrinsics | None = None

        self._lock = Lock()

        #
        # Long-poll readers sleep until a producer publishes or the
        # connection changes. No polling thread or frame queue per client.
        # Producers and async readers use the application's event loop.
        #
        self._frame_changed = asyncio.Condition()

    def get_status(self) -> CameraStatus:
        with self._lock:
            latest = self._latest

            return CameraStatus(
                connected=self._connected,
                error=self._error,

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

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

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

    async def wait_for_frame_set(
        self,
        after_sequence: int | None,
        timeout_seconds: float,
    ) -> CameraFrameSet | None:
        """Return a newer pair, or None on timeout/disconnection.

        A returned immutable frame set remains one exact pair even if
        the producer publishes again while the response is streaming.
        Waiting does not retain an old frame or consume a worker thread.
        Cancellation propagates and asyncio removes the condition waiter.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds

        async with self._frame_changed:
            while True:
                with self._lock:
                    if not self._connected:
                        return None

                    if self._latest is not None and (
                        after_sequence is None
                        or self._latest.sequence > after_sequence
                    ):
                        return self._latest

                remaining = deadline - loop.time()

                if remaining <= 0:
                    return None

                try:
                    await asyncio.wait_for(
                        self._frame_changed.wait(),
                        remaining,
                    )

                except asyncio.TimeoutError:
                    return None

    def get_depth_frame(
        self,
    ) -> CameraFrame | None:
        frame_set = self.get_latest_frame_set()

        if frame_set is None:
            return None

        return frame_set.depth

    def get_intrinsics(
        self,
    ) -> CameraIntrinsics | None:
        with self._lock:
            return self._intrinsics

    def set_intrinsics(
        self,
        intrinsics: CameraIntrinsics | None,
    ) -> None:
        with self._lock:
            self._intrinsics = intrinsics

    async def set_connected(
        self,
        connected: bool,
        error: str | None = None,
    ) -> None:
        """Set the connection state; error says why it is down."""

        if connected:
            error = None

        changed = False

        with self._lock:
            if (self._connected, self._error) != (connected, error):
                self._connected = connected
                self._error = error
                changed = True

        if changed:
            async with self._frame_changed:
                self._frame_changed.notify_all()

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
            self._error = None

        async with self._frame_changed:
            self._frame_changed.notify_all()

        return frame_set

    async def publish_status(self) -> None:
        status = self.get_status()

        await event_manager.publish(
            "camera.status",
            status.model_dump(mode="json"),
        )


camera_service = CameraService()
