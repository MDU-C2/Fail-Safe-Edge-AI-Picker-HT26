import asyncio

from gateway.camera.oak_producer import OakCameraProducer
from gateway.camera.service import camera_service


async def main():
    producer = OakCameraProducer(
        camera_service=camera_service,
        ip="169.254.1.223",
        fps=4.0,
        width=1920,
        height=1080,
        stereo_width=640,
        stereo_height=400,
    )

    print("Starting OAK camera...")

    await producer.start()

    print("Camera started")

    try:
        for _ in range(10):
            await asyncio.sleep(1)

            status = camera_service.get_status()

            print()
            print("Camera status:")
            print(status)

            rgb = camera_service.get_rgb_frame()
            depth = camera_service.get_depth_frame()

            if rgb is not None:
                print(
                    "RGB:",
                    rgb.width,
                    "x",
                    rgb.height,
                    rgb.pixel_format,
                    len(rgb.data),
                    "bytes",
                )

            if depth is not None:
                print(
                    "Depth:",
                    depth.width,
                    "x",
                    depth.height,
                    depth.pixel_format,
                    len(depth.data),
                    "bytes",
                )

    finally:
        print("Stopping camera...")
        await producer.stop()
        print("Camera stopped")


asyncio.run(main())