"""Camera endpoints against the real OAK camera (run with --oak).

Uses GATEWAY_CAMERA_IP and the RGB size from gateway/.env. Only one
process can hold the camera, so stop any other gateway first.
"""

import time

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
    """StereoDepth gives empty depth for a second or two after the
    camera opens (then over 90 % valid on the lab table), so wait."""

    deadline = time.monotonic() + 15

    while True:
        depth = oak_gateway.client().get("/camera/depth").content

        values = memoryview(depth).cast("H")

        valid = sum(1 for value in values[::97] if value > 0)

        if valid > 0 or time.monotonic() > deadline:
            break

        time.sleep(0.5)

    assert valid > 0, "Every sampled depth pixel is 0 (invalid) 15 s after start"


@pytest.mark.cpp_only
def test_frames_are_undistorted_by_default(
    oak_gateway: Gateway,
) -> None:
    """GATEWAY_CAMERA_UNDISTORT is on by default: the gateway undistorts
    CAM_A on the camera, so the frames carry no distortion and the
    intrinsics are the pinhole K of the served size."""

    settings = get_settings()

    client = oak_gateway.client()

    distortion = client.get("/camera/distortion").json()

    assert distortion["model"] == "perspective"
    assert distortion["coefficients"] == [0.0] * 14

    intrinsics = client.get("/camera/intrinsics").json()

    assert (intrinsics["width"], intrinsics["height"]) == (
        settings.camera_rgb_width,
        settings.camera_rgb_height,
    )
    assert 0 < intrinsics["cx"] < intrinsics["width"]
    assert 0 < intrinsics["cy"] < intrinsics["height"]
