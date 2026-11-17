"""Camera endpoints against the mock camera.

The mock produces 640x480 frames in the same wire format as the
OAK, so these tests check the format the clients rely on. The same
checks against the real camera are in test_oak.py.
"""

import time
from pathlib import Path

from gateway.tests.conftest import mock_settings
from gateway.tests.support import Gateway, expect


def check_camera_contract(
    gateway: Gateway,
) -> tuple[int, int]:
    """Shared by the mock and OAK tests. Returns the frame size."""

    client = gateway.client("diagnostics")

    status = expect(client.get("/camera/status"), 200)

    assert status["connected"]
    assert status["rgb_available"] and status["depth_available"]

    rgb = client.get("/camera/rgb")
    expect(rgb, 200)

    width = int(rgb.headers["X-Frame-Width"])
    height = int(rgb.headers["X-Frame-Height"])

    assert rgb.headers["X-Pixel-Format"] == "BGR888p"
    assert len(rgb.content) == 3 * width * height

    depth = client.get("/camera/depth")
    expect(depth, 200)

    #
    # Depth is aligned to the RGB camera, so it has the same size.
    #
    assert depth.headers["X-Pixel-Format"] == "RAW16"
    assert int(depth.headers["X-Frame-Width"]) == width
    assert int(depth.headers["X-Frame-Height"]) == height
    assert len(depth.content) == 2 * width * height

    for response in (rgb, depth):
        assert int(response.headers["X-Frame-Sequence"]) >= 1
        assert response.headers["X-Frame-Timestamp"]

    intrinsics = expect(client.get("/camera/intrinsics"), 200)

    matrix = intrinsics["matrix"]

    assert (intrinsics["width"], intrinsics["height"]) == (width, height)
    assert matrix == [
        [intrinsics["fx"], 0.0, intrinsics["cx"]],
        [0.0, intrinsics["fy"], intrinsics["cy"]],
        [0.0, 0.0, 1.0],
    ]
    assert intrinsics["fx"] > 0 and intrinsics["fy"] > 0
    assert 0 < intrinsics["cx"] < width
    assert 0 < intrinsics["cy"] < height

    return width, height


def test_camera_contract(
    gateway: Gateway,
) -> None:
    assert check_camera_contract(gateway) == (640, 480)


def test_mock_frames_have_known_pattern(
    strict_gateway: Gateway,
) -> None:
    client = strict_gateway.client()

    width, height = 640, 480
    plane = width * height

    rgb = client.get("/camera/rgb").content

    blue, green, red = rgb[:plane], rgb[plane:2 * plane], rgb[2 * plane:]

    #
    # Blue grows downwards, green to the right, red is constant.
    #
    assert blue[0] == 0 and blue[-1] == 255
    assert green[0] == 0 and green[width - 1] == 255
    assert set(red) == {128}

    depth = client.get("/camera/depth").content

    def depth_at(
        u: int,
        v: int,
    ) -> int:
        offset = 2 * (v * width + u)

        return int.from_bytes(depth[offset:offset + 2], "little")

    assert depth_at(0, 0) == 0
    assert depth_at(1, 0) == 500
    assert depth_at(width - 1, height - 1) == 500 + height - 1


def test_frames_advance(
    strict_gateway: Gateway,
) -> None:
    client = strict_gateway.client()

    first = int(client.get("/camera/rgb").headers["X-Frame-Sequence"])

    #
    # The mock publishes one frame per second.
    #
    time.sleep(1.5)

    second = int(client.get("/camera/rgb").headers["X-Frame-Sequence"])

    assert second > first


def test_camera_needs_token(
    strict_gateway: Gateway,
) -> None:
    anonymous = strict_gateway.client(None)

    for path in ("/camera/status", "/camera/rgb", "/camera/depth", "/camera/intrinsics"):
        expect(anonymous.get(path), 401)


def test_stale_frames_are_refused(
    tmp_path: Path,
) -> None:
    #
    # The mock publishes once a second, so with a 10 ms limit the
    # latest frame is almost always too old.
    #
    gateway = Gateway(
        tmp_path,
        "development",
        {
            **mock_settings(),
            "GATEWAY_CAMERA_MAX_FRAME_AGE_SECONDS": "0.01",
        },
    )

    gateway.start()

    try:
        client = gateway.client("diagnostics")

        expect(client.get("/camera/status"), 200)

        time.sleep(0.2)

        for path in ("/camera/rgb", "/camera/depth"):
            detail = expect(client.get(path), 503)["detail"]

            assert "the camera has stalled" in detail

    finally:
        gateway.stop()
