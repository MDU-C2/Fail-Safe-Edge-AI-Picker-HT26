"""Camera endpoints against the real OAK camera (run with --oak).

Uses GATEWAY_CAMERA_IP and the RGB size from gateway/.env. Only one
process can hold the camera, so stop any other gateway first.
"""

import pytest

from gateway.config import get_settings
from gateway.tests.support import Gateway
from gateway.tests.test_camera import check_camera_contract


pytestmark = pytest.mark.oak


def test_camera_contract(
    oak_gateway: Gateway,
) -> None:
    settings = get_settings()

    assert check_camera_contract(oak_gateway) == (
        settings.camera_rgb_width,
        settings.camera_rgb_height,
    )


def test_depth_has_valid_pixels(
    oak_gateway: Gateway,
) -> None:
    depth = oak_gateway.client().get("/camera/depth").content

    values = memoryview(depth).cast("H")

    valid = sum(1 for value in values[::97] if value > 0)

    assert valid > 0, "Every sampled depth pixel is 0 (invalid)"
