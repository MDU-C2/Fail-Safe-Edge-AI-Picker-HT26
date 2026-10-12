import requests

URL = "http://127.0.0.1:8000/api/v1"

TOKEN = "BAD_TOKEN_OPERATOR"

#
# The Bearer token is used in strict mode and the
# X-Dev-Client header in development mode.
#
headers = {
    "Authorization": f"Bearer {TOKEN}",
    "X-Dev-Client": "operator",
}


print("Getting camera intrinsics...")

response = requests.get(
    f"{URL}/camera/intrinsics",
    headers=headers,
)

response.raise_for_status()

intrinsics = response.json()

print(intrinsics)


matrix = intrinsics["matrix"]

assert len(matrix) == 3
assert all(len(row) == 3 for row in matrix)

assert intrinsics["fx"] == matrix[0][0]
assert intrinsics["fy"] == matrix[1][1]
assert intrinsics["cx"] == matrix[0][2]
assert intrinsics["cy"] == matrix[1][2]

assert intrinsics["fx"] > 0
assert intrinsics["fy"] > 0

assert 0 < intrinsics["cx"] < intrinsics["width"]
assert 0 < intrinsics["cy"] < intrinsics["height"]


print("Comparing with the RGB frame size...")

response = requests.get(
    f"{URL}/camera/rgb",
    headers=headers,
)

response.raise_for_status()

rgb_width = int(response.headers["X-Frame-Width"])
rgb_height = int(response.headers["X-Frame-Height"])

assert intrinsics["width"] == rgb_width
assert intrinsics["height"] == rgb_height

print(
    f"Intrinsics match the RGB frame size: "
    f"{rgb_width}x{rgb_height}"
)


print("Checking that a token is required...")

response = requests.get(
    f"{URL}/health",
)

response.raise_for_status()

if response.json()["security_mode"] == "strict":
    response = requests.get(
        f"{URL}/camera/intrinsics",
    )

    assert response.status_code == 401

    print("Request without a token was rejected")

else:
    print("Skipped: gateway is in development mode")


print("OK")
