"""Reconnect logic of the OAK producer, without a camera.

The DepthAI calls (open, close, read frames) are replaced by a fake,
so this runs without hardware; the real camera is in test_oak.py.
"""

import asyncio
import time

import pytest

pytest.importorskip("depthai")

from gateway.camera.oak_producer import OakCameraProducer
from gateway.camera.service import CameraService
from gateway.models.camera import CameraIntrinsics


FRAME = (b"rgb", b"depth")


class FakeOak(OakCameraProducer):
    """OakCameraProducer with the device replaced by a script.

    frames: what each _get_frames call does in turn; a frame tuple,
    None (no frame yet) or an exception to raise. When it runs out,
    frames keep coming. failed_opens: how many reconnects fail first.
    """

    def __init__(
        self,
        service: CameraService,
        frames: list,
        failed_opens: int = 0,
    ) -> None:
        super().__init__(
            camera_service=service,
            fps=50.0,
            stall_timeout_seconds=0.3,
            quick_retries=2,
            quick_retry_seconds=0.05,
            slow_retry_seconds=0.3,
        )

        self.frames = list(frames)
        self.failed_opens = failed_opens

        self.opens: list[float] = []
        self.closes = 0

    def _open_camera(
        self,
        connect_timeout_seconds: float,
    ) -> None:
        self.opens.append(time.monotonic())

        #
        # The first open is start(); only reconnects fail.
        #
        if len(self.opens) > 1 and self.failed_opens > 0:
            self.failed_opens -= 1

            raise RuntimeError("camera not found")

        self._intrinsics = CameraIntrinsics(
            width=1,
            height=1,
            fx=1.0,
            fy=1.0,
            cx=0.0,
            cy=0.0,
            matrix=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        )

    def _close_camera(self) -> None:
        self.closes += 1

    def _get_frames(self):
        if not self.frames:
            return FRAME

        step = self.frames.pop(0)

        if isinstance(step, Exception):
            raise step

        return step


async def wait_until(
    condition,
    timeout: float = 5.0,
) -> None:
    deadline = time.monotonic() + timeout

    while not condition():
        assert time.monotonic() < deadline, "timed out"

        await asyncio.sleep(0.01)


def test_reconnects_after_capture_error_with_backoff() -> None:
    async def scenario() -> None:
        service = CameraService()

        oak = FakeOak(
            service,
            frames=[FRAME, FRAME, RuntimeError("X_LINK_ERROR")],
            failed_opens=3,
        )

        await oak.start()

        try:
            #
            # Down, with the reason, while it reconnects.
            #
            await wait_until(lambda: not service.is_connected())

            assert "X_LINK_ERROR" in service.get_status().error

            #
            # start, 3 failed reconnects, 1 good one.
            #
            await wait_until(lambda: len(oak.opens) == 5)
            await wait_until(service.is_connected)

            assert service.get_status().error is None

            #
            # Two quick retries, then the slow interval.
            #
            gaps = [later - earlier for earlier, later in zip(oak.opens[1:], oak.opens[2:])]

            assert gaps[0] < 0.2
            assert gaps[1] >= 0.25 and gaps[2] >= 0.25

            #
            # Frames flow again.
            #
            sequence = service.get_status().sequence

            await wait_until(lambda: service.get_status().sequence > sequence)

        finally:
            await oak.stop()

    asyncio.run(scenario())


def test_reconnects_when_frames_stop() -> None:
    async def scenario() -> None:
        service = CameraService()

        #
        # One frame, then nothing for well over the stall timeout.
        #
        oak = FakeOak(service, frames=[FRAME] + [None] * 1000)

        await oak.start()

        try:
            await wait_until(lambda: len(oak.opens) == 2)

            assert oak.closes >= 1

        finally:
            await oak.stop()

    asyncio.run(scenario())


def test_stop_interrupts_reconnect_wait() -> None:
    async def scenario() -> None:
        service = CameraService()

        #
        # Every reconnect fails, so it ends up in the slow wait.
        #
        oak = FakeOak(service, frames=[RuntimeError("gone")], failed_opens=1000)

        oak._slow_retry_seconds = 60.0

        await oak.start()

        #
        # start and the two quick retries; then the 60 s wait.
        #
        await wait_until(lambda: len(oak.opens) == 3)

        await asyncio.sleep(0.1)

        started = time.monotonic()

        await oak.stop()

        assert time.monotonic() - started < 1.0
        assert not service.is_connected()

    asyncio.run(scenario())
