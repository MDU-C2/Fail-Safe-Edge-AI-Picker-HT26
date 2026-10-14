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

CALIBRATION = {
    "camera_to_robot": [
        [1.0, 0.0, 0.0, 400.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 600.0],
        [0.0, 0.0, 0.0, 1.0],
    ],
    "units": "mm",
}


response = requests.get(
    f"{URL}/health",
)

response.raise_for_status()

security_mode = response.json()["security_mode"]

print(f"Gateway is in {security_mode} mode")


print("Uploading without control must be rejected...")

response = requests.get(
    f"{URL}/control",
    headers=headers,
)

response.raise_for_status()

if response.json()["client_id"] == "operator":
    print("Skipped: operator already holds control from an earlier run")

else:
    response = requests.post(
        f"{URL}/calibration",
        headers=headers,
        json=CALIBRATION,
    )

    assert response.status_code == 409, response.status_code

    print("Rejected with 409")


print("Acquiring control and uploading...")

response = requests.post(
    f"{URL}/control/acquire",
    headers=headers,
)

if response.status_code != 409:
    response.raise_for_status()

response = requests.post(
    f"{URL}/calibration",
    headers=headers,
    json=CALIBRATION,
)

response.raise_for_status()

stored = response.json()

print(stored)

assert stored["calibration"] == CALIBRATION
assert stored["updated_by"] == "operator"


print("Reading it back...")

response = requests.get(
    f"{URL}/calibration",
    headers=headers,
)

response.raise_for_status()

assert response.json() == stored

print("Read back the same calibration")


print("Uploading something that is not a JSON object must fail...")

response = requests.post(
    f"{URL}/calibration",
    headers=headers,
    json=[1, 2, 3],
)

assert response.status_code == 422, response.status_code

print("Rejected with 422")


if security_mode == "development":
    print("Read-only client can read but not upload...")

    diagnostics = {
        "X-Dev-Client": "diagnostics",
    }

    response = requests.get(
        f"{URL}/calibration",
        headers=diagnostics,
    )

    assert response.status_code == 200, response.status_code

    response = requests.post(
        f"{URL}/calibration",
        headers=diagnostics,
        json=CALIBRATION,
    )

    assert response.status_code == 403, response.status_code

    print("Read allowed, upload rejected with 403")

    print("A client with control permission but without the lease cannot upload...")

    response = requests.post(
        f"{URL}/calibration",
        headers={
            "X-Dev-Client": "autonomy",
        },
        json=CALIBRATION,
    )

    assert response.status_code == 409, response.status_code

    print("Rejected with 409")

else:
    print("Reading without a token must be rejected...")

    response = requests.get(
        f"{URL}/calibration",
    )

    assert response.status_code == 401, response.status_code

    print("Rejected with 401")


print("OK")
