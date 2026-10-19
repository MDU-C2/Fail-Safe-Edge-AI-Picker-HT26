import asyncio

from gateway.camera.producer import CameraProducer
from gateway.camera.service import CameraService
from gateway.models.camera import CameraIntrinsics

WIDTH = 640
HEIGHT = 480


def _mock_rgb() -> bytes:
    """A BGR888p test pattern in the same layout as the OAK.

    Planar: all blue values, then all green, then all red. Blue
    grows downwards, green to the right, red is constant.
    """

    blue = b"".join(
        bytes([row * 255 // (HEIGHT - 1)]) * WIDTH
        for row in range(HEIGHT)
    )

    green = bytes(
        column * 255 // (WIDTH - 1)
        for column in range(WIDTH)
    ) * HEIGHT

    red = bytes([128]) * (WIDTH * HEIGHT)

    return blue + green + red


def _mock_depth() -> bytes:
    """RAW16 depth in mm, little-endian like the OAK.

    The image is a plane tilted from 500 mm at the top to 979 mm
    at the bottom, with an invalid (0) border column on the left.
    """

    rows = []

    for row in range(HEIGHT):
        value = (500 + row).to_bytes(2, "little")

        rows.append(
            bytes(2) + value * (WIDTH - 1)
        )

    return b"".join(rows)


class MockCameraProducer(CameraProducer):
    def __init__(
        self,
        camera_service: CameraService,
    ) -> None:
        self._camera_service = camera_service

        self._running = False
        self._task: asyncio.Task | None = None

        self._rgb = _mock_rgb()
        self._depth = _mock_depth()

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
                width=WIDTH,
                height=HEIGHT,
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
        await self._camera_service.publish_frame_set(
            rgb_pixel_format="BGR888p",
            rgb_width=WIDTH,
            rgb_height=HEIGHT,
            rgb_data=self._rgb,
            depth_width=WIDTH,
            depth_height=HEIGHT,
            depth_pixel_format="RAW16",
            depth_data=self._depth,
        )