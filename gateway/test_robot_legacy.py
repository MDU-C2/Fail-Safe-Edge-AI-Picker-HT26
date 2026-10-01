import os

import requests


URL = "http://127.0.0.1:8000/api/v1"
TOKEN = "BAD_TOKEN_OPERATOR"

headers = {
    "Authorization": f"Bearer {TOKEN}",
}


# Acquire control
response = requests.post(
    f"{URL}/control/acquire",
    headers=headers,
)

response.raise_for_status()

print("Control:")
print(response.json())


# Send command to robot
command = "WAYEND,515.0,200.0,250.0"

response = requests.post(
    f"{URL}/robot/command",
    headers=headers,
    json={
        "command": command,
        "parameters": {},
    },
)

response.raise_for_status()

print("Robot:")
print(response.json())