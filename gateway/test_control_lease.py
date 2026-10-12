import threading
import time

import requests


URL = "http://127.0.0.1:8000/api/v1"
TOKEN = "BAD_TOKEN_OPERATOR"

#
# With GATEWAY_ROBOT_PROVIDER=tcp this text is sent to the real robot.
# The gateway rejects an empty command, so a placeholder is used.
#
COMMAND = "NOOP"

RENEW_INTERVAL_SECONDS = 2.0
HOLD_SECONDS = 8.0

headers = {
    "Authorization": f"Bearer {TOKEN}",
}

stop = threading.Event()


def renew_lease() -> None:
    #
    # The lease lasts 5 seconds by default, so it is
    # renewed well before it runs out.
    #
    while not stop.wait(RENEW_INTERVAL_SECONDS):
        response = requests.post(
            f"{URL}/control/renew",
            headers=headers,
        )

        response.raise_for_status()

        print("Lease renewed")


# Acquire control
response = requests.post(
    f"{URL}/control/acquire",
    headers=headers,
)

response.raise_for_status()

print("Control:")
print(response.json())


# Renew the lease in a separate thread
thread = threading.Thread(
    target=renew_lease,
    daemon=True,
)

thread.start()

try:
    #
    # Wait longer than one lease period. Control is only
    # still held afterwards if the renew thread works.
    #
    print(f"Holding control for {HOLD_SECONDS:g} seconds...")

    time.sleep(HOLD_SECONDS)

    response = requests.get(
        f"{URL}/control",
        headers=headers,
    )

    response.raise_for_status()

    lease = response.json()

    assert lease["active"]
    assert lease["client_id"] == "operator"

    print("Control is still held")


    # Send command to robot
    response = requests.post(
        f"{URL}/robot/command",
        headers=headers,
        json={
            "command": COMMAND,
            "parameters": {},
        },
    )

    response.raise_for_status()

    print("Robot:")
    print(response.json())

finally:
    stop.set()
    thread.join()

print("OK")
