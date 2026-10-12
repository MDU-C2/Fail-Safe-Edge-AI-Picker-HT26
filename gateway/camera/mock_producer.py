import asyncio

from gateway.camera.producer import CameraProducer
from gateway.camera.service import CameraService
from gateway.models.camera import CameraIntrinsics


class MockCameraProducer(CameraProducer):
    def __init__(
        self,
        camera_service: CameraService,
    ) -> None:
        self._camera_service = camera_service

        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._running:
            return

        self._running = True

        #
        # Placeholder values matching the 640x480 mock frames.
        # They do not describe a real camera.
        #
        self._camera_service.set_intrinsics(
            CameraIntrinsics(
                width=640,
                height=480,
                fx=500.0,
                fy=500.0,
                cx=320.0,
                cy=240.0,
                matrix=[
                    [500.0, 0.0, 320.0],
                    [0.0, 500.0, 240.0],
                    [0.0, 0.0, 1.0],
                ],
            )
        )

        await self._camera_service.set_connected(True)

        self._task = asyncio.create_task(
            self._capture_loop()
        )

    async def stop(self) -> None:
        if not self._running:
            return

        self._running = False

        if self._task is not None:
            await self._task
            self._task = None

        await self._camera_service.set_connected(False)

    async def _capture_loop(self) -> None:
        while self._running:
            await self._capture_frame()

            await asyncio.sleep(1.0)

    async def _capture_frame(self) -> None:
        rgb_data = (
            b"mock-rgb-frame"
        )

        depth_data = (
            b"mock-depth-frame"
        )

        await self._camera_service.publish_frame_set(
            rgb_pixel_format="RGB8",
            rgb_width=640,
            rgb_height=480,
            rgb_data=rgb_data,
            depth_width=640,
            depth_height=480,
            depth_pixel_format="DEPTH16",
            depth_data=depth_data,
        )