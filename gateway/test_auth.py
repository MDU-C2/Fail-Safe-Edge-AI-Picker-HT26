import os

import requests


URL = "http://127.0.0.1:8000/api/v1"

TOKEN = "BAD_TOKEN_OPERATOR"

headers = {
    "Authorization": f"Bearer {TOKEN}"
}


print("Getting camera status...")

response = requests.get(
    f"{URL}/camera/status",
    headers=headers,
)

response.raise_for_status()

print(response.json())


print("Getting RGB image...")

response = requests.get(
    f"{URL}/camera/rgb",
    headers=headers,
)

response.raise_for_status()

print(
    f"RGB: "
    f"{response.headers['X-Frame-Width']}x"
    f"{response.headers['X-Frame-Height']} "
    f"{response.headers['X-Pixel-Format']} "
    f"{len(response.content):,} bytes"
)


print("Getting depth image...")

response = requests.get(
    f"{URL}/camera/depth",
    headers=headers,
)

response.raise_for_status()

print(
    f"Depth: "
    f"{response.headers['X-Frame-Width']}x"
    f"{response.headers['X-Frame-Height']} "
    f"{response.headers['X-Pixel-Format']} "
    f"{len(response.content):,} bytes"
)