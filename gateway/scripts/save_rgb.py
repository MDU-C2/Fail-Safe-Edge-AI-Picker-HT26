"""
Fetch the latest RGB frame from the gateway and save it as a PNG.

Usage (from the repository root):
    python gateway/scripts/save_rgb.py
    python gateway/scripts/save_rgb.py --output frame.png

Needs: requests, pillow
"""

import argparse
from pathlib import Path

import requests
from PIL import Image


DEFAULT_URL = "http://127.0.0.1:8000/api/v1"
DEFAULT_TOKEN = "BAD_TOKEN_OPERATOR"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Save the latest RGB frame as a PNG",
    )

    parser.add_argument("--url", default=DEFAULT_URL, help="Gateway API base URL")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="Bearer token (strict mode)")
    parser.add_argument("--client", default="operator", help="Client id (development mode)")
    parser.add_argument("--output", type=Path, default=Path("rgb.png"), help="PNG file to write")

    args = parser.parse_args()

    response = requests.get(
        f"{args.url}/camera/rgb",
        headers={
            "Authorization": f"Bearer {args.token}",
            "X-Dev-Client": args.client,
        },
        timeout=30,
    )

    response.raise_for_status()

    width = int(response.headers["X-Frame-Width"])
    height = int(response.headers["X-Frame-Height"])
    pixel_format = response.headers["X-Pixel-Format"]

    if pixel_format != "BGR888p":
        raise RuntimeError(f"Expected BGR888p, received {pixel_format!r}")

    data = response.content

    plane_size = width * height

    if len(data) != 3 * plane_size:
        raise RuntimeError(
            f"Invalid RGB payload size: expected {3 * plane_size} bytes, "
            f"received {len(data)} bytes"
        )

    #
    # BGR888p is planar: all blue pixels, then all green, then all red.
    #
    blue, green, red = (
        Image.frombytes(
            "L",
            (width, height),
            data[index * plane_size:(index + 1) * plane_size],
        )
        for index in range(3)
    )

    image = Image.merge("RGB", (red, green, blue))

    image.save(args.output, "PNG")

    print(f"Saved {width}x{height} image to {args.output.resolve()}")


if __name__ == "__main__":
    main()
