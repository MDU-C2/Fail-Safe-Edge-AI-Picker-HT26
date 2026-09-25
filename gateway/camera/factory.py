from gateway.camera.mock_producer import MockCameraProducer
from gateway.camera.producer import CameraProducer
from gateway.camera.service import camera_service
from gateway.config import Settings


def create_camera_producer(
    settings: Settings,
) -> CameraProducer:
    if settings.camera_provider == "mock":
        return MockCameraProducer(
            camera_service=camera_service,
        )

    if settings.camera_provider == "oak":
        #
        # Import only if OAK is actually selected.
        #
        # This allows developers without DepthAI installed
        # to continue using the mock camera.
        #
        from gateway.camera.oak_producer import (
            OakCameraProducer,
        )

        return OakCameraProducer(
            camera_service=camera_service,
            fps=settings.camera_fps,
        )

    raise RuntimeError(
        "Unknown camera provider: "
        f"{settings.camera_provider}"
    )