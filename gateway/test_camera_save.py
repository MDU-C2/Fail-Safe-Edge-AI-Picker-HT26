from pathlib import Path

import requests
from PIL import Image


GATEWAY_URL = "http://127.0.0.1:8000/api/v1"
CLIENT_ID = "operator"

OUTPUT_FILE = Path("rgb.png")


def main() -> None:
    response = requests.get(
        f"{GATEWAY_URL}/camera/rgb",
        headers={
            "X-Dev-Client": CLIENT_ID,
        },
        timeout=30,
    )

    response.raise_for_status()

    width = int(
        response.headers["X-Frame-Width"]
    )

    height = int(
        response.headers["X-Frame-Height"]
    )

    pixel_format = (
        response.headers["X-Pixel-Format"]
    )

    if pixel_format != "BGR888p":
        raise RuntimeError(
            f"Expected BGR888p, "
            f"received {pixel_format!r}"
        )

    data = response.content

    plane_size = width * height

    expected_size = plane_size * 3

    if len(data) != expected_size:
        raise RuntimeError(
            f"Invalid RGB payload size: "
            f"expected {expected_size} bytes, "
            f"received {len(data)} bytes"
        )

    #
    # BGR888p is planar:
    #
    # [all blue pixels]
    # [all green pixels]
    # [all red pixels]
    #

    blue = Image.frombytes(
        "L",
        (width, height),
        data[0:plane_size],
    )

    green = Image.frombytes(
        "L",
        (width, height),
        data[
            plane_size:
            2 * plane_size
        ],
    )

    red = Image.frombytes(
        "L",
        (width, height),
        data[
            2 * plane_size:
            3 * plane_size
        ],
    )

    #
    # Convert BGR planes into a normal RGB image.
    #

    image = Image.merge(
        "RGB",
        (
            red,
            green,
            blue,
        ),
    )

    image.save(
        OUTPUT_FILE,
        "PNG",
    )

    print(
        f"Saved: {OUTPUT_FILE.resolve()}"
    )

    print(
        f"Image: {width}x{height}"
    )


if __name__ == "__main__":
    main()