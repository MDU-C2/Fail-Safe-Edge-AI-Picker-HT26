"""
Start the Python gateway with the mock camera made to look like the
OAK: frames of GATEWAY_CAMERA_RGB_WIDTH x HEIGHT, a fresh bytes
object per frame (as OakCameraProducer does with bytes(getData())),
at GATEWAY_CAMERA_FPS. With MEASURE_IMPORT_DEPTHAI=1 depthai is also
imported, as with the oak provider.

    python gateway/cpp/tools/launch_python_1080p.py <port>    (from the repository root)

For measure_gateway.py; it changes nothing in the gateway itself.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.getcwd())

import uvicorn

import gateway.camera.mock_producer as mock

mock.WIDTH = int(os.environ.get("GATEWAY_CAMERA_RGB_WIDTH", "1920"))
mock.HEIGHT = int(os.environ.get("GATEWAY_CAMERA_RGB_HEIGHT", "1080"))

FPS = float(os.environ.get("GATEWAY_CAMERA_FPS", "4"))

if os.environ.get("MEASURE_IMPORT_DEPTHAI") == "1":
    import depthai  # noqa: F401


original_capture = mock.MockCameraProducer._capture_frame


async def capture_fresh(self) -> None:
    self._rgb = bytes(bytearray(self._rgb))
    self._depth = bytes(bytearray(self._depth))

    await original_capture(self)


async def capture_loop(self) -> None:
    while self._running:
        await self._capture_frame()
        await asyncio.sleep(1.0 / FPS)


mock.MockCameraProducer._capture_frame = capture_fresh
mock.MockCameraProducer._capture_loop = capture_loop

uvicorn.run("gateway.main:app", host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
